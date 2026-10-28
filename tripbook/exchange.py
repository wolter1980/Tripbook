"""Imports (raw dumps, GPX, Little LogBook databases), exports (GPX, CSV, HTML report) and geocoding."""
from __future__ import annotations

import csv
import html
import json
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import Database, Trip, to_iso
from .decode import Point, TripCandidate, decode_log, pack_points, split_trips

USER_AGENT = "Tripbook/1.0 (Linux desktop trip logbook)"


def import_points(db: Database, vehicle_id: int, points: list[Point], source: str) -> int:
    added = []
    for cand in split_trips(points, db.stop_interval_min):
        tid = db.add_trip_from_points(vehicle_id, cand, source)
        if tid:
            added.append(tid)
    db.conn.commit()
    if added:
        db.resolve_places(added)
    return len(added)


def import_raw_dump(db: Database, vehicle_id: int, path: Path) -> int:
    return import_points(db, vehicle_id, decode_log(Path(path).read_bytes()), "device")


# --------------------------------------------------------------------------- GPX

def _parse_time(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_gpx(path: Path) -> list[Point]:
    root = ET.parse(path).getroot()
    pts = []
    for el in root.iter():
        if not el.tag.endswith("trkpt") and not el.tag.endswith("rtept"):
            continue
        t = ele = None
        speed = 0.0
        for c in el:
            tag = c.tag.rsplit("}", 1)[-1]
            if tag == "time" and c.text:
                t = _parse_time(c.text)
            elif tag == "ele" and c.text:
                ele = float(c.text)
            elif tag == "speed" and c.text:
                speed = float(c.text) * 3.6
        if t:
            pts.append(Point(t, float(el.get("lat")), float(el.get("lon")), ele or 0.0, int(speed)))
    return pts


def write_gpx(path: Path, tracks: list[tuple[str, list[Point]]]) -> None:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="Tripbook" xmlns="http://www.topografix.com/GPX/1/1">']
    for name, pts in tracks:
        out.append(f"<trk><name>{html.escape(name)}</name><trkseg>")
        for p in pts:
            out.append(f'<trkpt lat="{p.lat:.6f}" lon="{p.lon:.6f}"><ele>{p.alt:.1f}</ele>'
                       f"<time>{to_iso(p.time)}</time><speed>{p.speed / 3.6:.2f}</speed></trkpt>")
        out.append("</trkseg></trk>")
    out.append("</gpx>")
    Path(path).write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------------------- Little LogBook database

def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")}
    except sqlite3.DatabaseError:
        return set()


LLB_DB_PASSWORD = b"l1ttl3l0gb00k"  # fixed password the Windows app encrypts llb.s3db with


def _rc4_keystream(key: bytes, n: int) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for _ in range(n):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def decrypt_llb_database(data: bytes, password: bytes = LLB_DB_PASSWORD) -> bytes | None:
    """Undo System.Data.SQLite's legacy encryption: every page is RC4'd with key SHA1(password)[:16]."""
    import hashlib
    key = hashlib.sha1(password).digest()[:16]
    head = bytes(a ^ b for a, b in zip(data[:18], _rc4_keystream(key, 18)))
    if not head.startswith(b"SQLite format 3\0"):
        return None
    page = int.from_bytes(head[16:18], "big")
    page = 65536 if page == 1 else page
    ks = int.from_bytes(_rc4_keystream(key, page), "little")
    out = bytearray()
    for off in range(0, len(data), page):
        chunk = data[off:off + page]
        out += (int.from_bytes(chunk, "little") ^ ks).to_bytes(page, "little")[:len(chunk)]
    return bytes(out)


def _llb_time(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        # .NET ticks or unix seconds
        if v > 1e15:
            return datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=v / 10)
        return datetime.fromtimestamp(v, timezone.utc)
    s = str(v).strip().replace("T", " ")
    utc = s.endswith("Z")
    s = s.rstrip("Z")
    if "." in s:
        base, frac = s.split(".", 1)
        s = f"{base}.{frac[:6]}"
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=timezone.utc) if utc else dt.astimezone()  # no "Z": local time
    return None


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "t", "yes")


def open_llb_database(path: Path) -> tuple[sqlite3.Connection, Path | None]:
    """Open a Little LogBook database read-only, decrypting it to a temporary copy when needed."""
    data = Path(path).read_bytes()
    if data.startswith(b"SQLite format 3\0"):
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True), None
    plain = decrypt_llb_database(data)
    if plain is None:
        raise ValueError("This file is not a Little LogBook database (or uses an unknown password).")
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="llb-")
    with open(fd, "wb") as fh:
        fh.write(plain)
    return sqlite3.connect(tmp), Path(tmp)


def import_littlelogbook_db(db: Database, path: Path) -> dict[str, int]:
    """Import vehicles, named places, rules, trips, expenses and odometer readings from Little LogBook."""
    src, tmp = open_llb_database(path)
    try:
        return _import_llb(db, src)
    finally:
        src.close()
        if tmp:
            tmp.unlink(missing_ok=True)


def _import_llb(db: Database, src: sqlite3.Connection) -> dict[str, int]:
    src.row_factory = sqlite3.Row
    stats = {"vehicles": 0, "places": 0, "rules": 0, "trips": 0, "expenses": 0, "odometer readings": 0,
             "skipped (already present)": 0}

    type_map: dict[int, int] = {}
    existing_types = {r["name"].lower(): r["id"] for r in db.trip_types()}
    if _cols(src, "TripType"):
        for r in src.execute("SELECT * FROM TripType"):
            name = (r["Description"] or "").strip()
            if not name or _truthy(r["IsDeleted"]):
                continue
            tid = existing_types.get(name.lower())
            if tid is None:
                tid = db.x("INSERT INTO trip_type(name) VALUES (?)", (name,))
                existing_types[name.lower()] = tid
            type_map[r["TripTypeId"]] = tid

    vehicle_map: dict[int, int] = {}
    dcols = _cols(src, "Device")
    if dcols:
        for r in src.execute("SELECT * FROM Device"):
            reg = (r["RegistrationNumber"] or "").strip()
            name = " ".join(x for x in [(r["Make"] or "").strip(), reg] if x) or f"Vehicle {r['DeviceId']}"
            ex = db.q("SELECT id FROM vehicle WHERE registration=? AND registration<>''", (reg,))
            if ex:
                vehicle_map[r["DeviceId"]] = ex[0]["id"]
                continue
            vehicle_map[r["DeviceId"]] = db.x(
                "INSERT INTO vehicle(name, registration, make, year, odometer_start, device_serial, "
                "device_password, closed) VALUES (?,?,?,?,?,?,?,?)",
                (name, reg, r["Make"] or "", r["Year"] or "", float(r["OdometerStart"] or 0),
                 r["DeviceSerial"] or "", r["Password"] if "Password" in dcols and r["Password"] else "",
                 int(_truthy(r["Closed"])) if "Closed" in dcols else 0))
            stats["vehicles"] += 1

    # Named places are GeoNames with negative ids; the many positive ones are an automatic suburb list.
    place_map: dict[int, int] = {}
    if _cols(src, "GeoName"):
        for r in src.execute("SELECT * FROM GeoName WHERE GeoNameId < 0"):
            if _truthy(r["IsDeleted"]) or r["Latitude"] is None or not r["Description"]:
                continue
            ex = db.q("SELECT id FROM place WHERE name=? AND abs(lat-?)<0.0005 AND abs(lon-?)<0.0005",
                      (r["Description"], r["Latitude"], r["Longitude"]))
            if ex:
                place_map[r["GeoNameId"]] = ex[0]["id"]
                continue
            place_map[r["GeoNameId"]] = db.x("INSERT INTO place(name, lat, lon, radius_m) VALUES (?,?,?,50)",
                                             (r["Description"], r["Latitude"], r["Longitude"]))
            stats["places"] += 1

    if _cols(src, "Rules"):
        reasons = {}
        if _cols(src, "RuleTags"):
            for r in src.execute("SELECT * FROM RuleTags WHERE IsDeleted IN (0, '0', 'FALSE', 'False')"):
                if _truthy(r["IsDefault"]) or r["RuleId"] not in reasons:
                    reasons[r["RuleId"]] = r["Description"] or ""
        for r in src.execute("SELECT * FROM Rules"):
            a, b = place_map.get(r["StartTagId"]), place_map.get(r["EndTagId"])
            tt = type_map.get(r["TripTypeId"])
            if _truthy(r["IsDeleted"]) or not (a and b and tt):
                continue
            vid = vehicle_map.get(r["DeviceId"])
            if db.q("SELECT 1 FROM rule WHERE start_place_id=? AND end_place_id=? AND vehicle_id IS ?", (a, b, vid)):
                continue
            db.x("INSERT INTO rule(vehicle_id, start_place_id, end_place_id, trip_type_id, reverse, reason) "
                 "VALUES (?,?,?,?,?,?)", (vid, a, b, tt, int(_truthy(r["Reverse"])), reasons.get(r["RuleId"], "")))
            stats["rules"] += 1

    place_names = {p["id"]: p["name"] for p in db.places()}
    tcols = _cols(src, "Trip")
    if tcols:
        for r in src.execute("SELECT * FROM Trip ORDER BY StartDate"):
            if _truthy(r["IsDeleted"]) or r["DeviceId"] not in vehicle_map:
                continue
            vid = vehicle_map[r["DeviceId"]]
            pts = decode_log(bytes(r["TripData"])) if r["TripData"] else []
            start = pts[0].time if pts else _llb_time(r["StartDate"])
            end = pts[-1].time if pts else _llb_time(r["EndDate"]) or start
            if not start or db.overlaps(vid, start, end):
                stats["skipped (already present)"] += 1
                continue
            sl = (pts[0].lat, pts[0].lon) if pts else (r["StartLatitude"], r["StartLongitude"])
            el = (pts[-1].lat, pts[-1].lon) if pts else (
                (r["EndLatitude"], r["EndLongitude"]) if "EndLatitude" in tcols else (None, None))
            sp, ep = place_map.get(r["StartTagId"]), place_map.get(r["EndTagId"])
            sdesc = place_names[sp] if sp else r["StartDescription"] or ""
            edesc = place_names[ep] if ep else r["EndDescription"] or ""
            db.conn.execute(
                "INSERT INTO trip(vehicle_id, start_time, end_time, start_lat, start_lon, end_lat, end_lon, "
                "start_place_id, end_place_id, start_desc, end_desc, distance_km, duration_s, trip_type_id, "
                "reason, poi_count, points, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, to_iso(start), to_iso(end), *sl, *el, sp, ep, sdesc, edesc, float(r["Distance"] or 0), int((end - start).total_seconds()),
                 type_map.get(r["TripTypeId"]), r["TripReason"] or "",
                 int(r["PoiCount"] or 0) if "PoiCount" in tcols else 0,
                 pack_points(pts) if pts else None, "littlelogbook" if pts else "manual"))
            stats["trips"] += 1
        db.conn.commit()

    if _cols(src, "Expenses"):
        for r in src.execute("SELECT * FROM Expenses"):
            if _truthy(r["IsDeleted"]) or r["DeviceId"] not in vehicle_map:
                continue
            d = (_llb_time(r["DateIncurred"]) or datetime.now()).astimezone().date().isoformat()
            vid, amount, desc = vehicle_map[r["DeviceId"]], round(float(r["Value"] or 0), 2), r["Description"] or ""
            if db.q("SELECT 1 FROM expense WHERE vehicle_id=? AND date=? AND amount=? AND description=?",
                    (vid, d, amount, desc)):
                continue
            volume = round(float(r["Volume"] or 0), 2)
            db.conn.execute("INSERT INTO expense(vehicle_id, date, category, description, amount, volume) "
                            "VALUES (?,?,?,?,?,?)", (vid, d, "Fuel" if volume > 0 else "Other", desc, amount, volume))
            stats["expenses"] += 1
        db.conn.commit()

    if _cols(src, "Odometer"):
        for r in src.execute("SELECT * FROM Odometer"):
            vid = vehicle_map.get(r["DeviceId"])
            reading = float(r["Reading"] or 0)
            d = _llb_time(r["Date"])
            if not vid or reading <= 0 or not d:
                continue
            day = d.astimezone().date().isoformat()
            if db.q("SELECT 1 FROM odometer WHERE vehicle_id=? AND date=? AND reading=?", (vid, day, reading)):
                continue
            db.x("INSERT INTO odometer(vehicle_id, date, reading) VALUES (?,?,?)", (vid, day, reading))
            stats["odometer readings"] += 1

    db.apply_rules()
    return stats


# --------------------------------------------------------------------------- reports

def distance_factor(db: Database) -> tuple[float, str]:
    return (1 / 1.609344, "mi") if db.use_miles else (1.0, "km")


def _local(dt: datetime) -> datetime:
    return dt.astimezone()


def export_trips_csv(db: Database, trips: list[Trip], path: Path) -> None:
    f, unit = distance_factor(db)
    types = {r["id"]: r["name"] for r in db.trip_types()}
    vehicles = {r["id"]: r["name"] for r in db.vehicles()}
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Vehicle", "Date", "Start time", "End time", "From", "To", f"Odometer start ({unit})",
                    f"Odometer end ({unit})", f"Distance ({unit})", "Duration (min)", "Type", "Reason"])
        for t in trips:
            s, e = _local(t.start_time), _local(t.end_time)
            w.writerow([vehicles.get(t.vehicle_id, ""), s.date().isoformat(), s.strftime("%H:%M"),
                        e.strftime("%H:%M"), t.start_label(), t.end_label(), f"{t.odo_start * f:.0f}",
                        f"{t.odo_end * f:.0f}", f"{t.distance_km * f:.1f}", round(t.duration_s / 60),
                        types.get(t.trip_type_id, ""), t.reason])


def summary(db: Database, trips: list[Trip], expenses: list[sqlite3.Row]) -> dict:
    types = {r["id"]: r for r in db.trip_types()}
    total = sum(t.distance_km for t in trips)
    per_type: dict[str, float] = {}
    business = 0.0
    for t in trips:
        tt = types.get(t.trip_type_id)
        name = tt["name"] if tt else "Unmarked"
        per_type[name] = per_type.get(name, 0) + t.distance_km
        if tt and tt["is_business"]:
            business += t.distance_km
    per_cat: dict[str, float] = {}
    for e in expenses:
        per_cat[e["category"]] = per_cat.get(e["category"], 0) + e["amount"]
    return {"trips": len(trips), "total_km": total, "business_km": business,
            "business_pct": (business / total * 100) if total else 0.0, "per_type": per_type,
            "expenses": per_cat, "expense_total": sum(per_cat.values()),
            "fuel_l": sum(e["volume"] for e in expenses if e["category"] == "Fuel")}


def report_html(db: Database, vehicle: sqlite3.Row | None, trips: list[Trip], expenses: list[sqlite3.Row],
                period: str) -> str:
    f, unit = distance_factor(db)
    cur = db.setting("currency", "")
    s = summary(db, trips, expenses)
    types = {r["id"]: r["name"] for r in db.trip_types()}
    esc = html.escape
    title = f"Travel logbook – {esc(vehicle['name']) if vehicle else 'all vehicles'}"
    rows = []
    for t in trips:
        st, et = _local(t.start_time), _local(t.end_time)
        rows.append(f"<tr><td>{st:%Y-%m-%d}</td><td>{st:%H:%M}–{et:%H:%M}</td><td>{esc(t.start_label())}</td>"
                    f"<td>{esc(t.end_label())}</td><td class=n>{t.odo_start * f:,.0f}</td>"
                    f"<td class=n>{t.odo_end * f:,.0f}</td><td class=n>{t.distance_km * f:,.1f}</td>"
                    f"<td>{esc(types.get(t.trip_type_id, ''))}</td><td>{esc(t.reason)}</td></tr>")
    type_rows = "".join(f"<tr><td>{esc(k)}</td><td class=n>{v * f:,.1f} {unit}</td>"
                        f"<td class=n>{(v / s['total_km'] * 100 if s['total_km'] else 0):.1f}%</td></tr>"
                        for k, v in sorted(s["per_type"].items()))
    exp_rows = "".join(f"<tr><td>{esc(k)}</td><td class=n>{cur}{v:,.2f}</td></tr>"
                       for k, v in sorted(s["expenses"].items()))
    veh = ""
    if vehicle:
        veh = (f"<p>Registration: <b>{esc(vehicle['registration'])}</b> &nbsp; Make: {esc(vehicle['make'])} "
               f"&nbsp; Year: {esc(vehicle['year'])}</p>")
    odo = ""
    if trips:
        odo = (f"<p>Opening odometer: <b>{trips[0].odo_start * f:,.0f} {unit}</b> &nbsp; "
               f"Closing odometer: <b>{trips[-1].odo_end * f:,.0f} {unit}</b></p>")
    return f"""<html><head><meta charset="utf-8"><style>
body {{ font-family: sans-serif; font-size: 9pt; }}
h1 {{ font-size: 15pt; margin-bottom: 2px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 12px; }}
th, td {{ border: 1px solid #bbb; padding: 3px 5px; text-align: left; }}
th {{ background: #e8eef6; }}
td.n {{ text-align: right; }}
</style></head><body>
<h1>{title}</h1><p>Period: {esc(period)}</p>{veh}{odo}
<h2>Summary</h2>
<table><tr><th>Trips</th><th>Total distance</th><th>Business distance</th><th>Business use</th></tr>
<tr><td class=n>{s['trips']}</td><td class=n>{s['total_km'] * f:,.1f} {unit}</td>
<td class=n>{s['business_km'] * f:,.1f} {unit}</td><td class=n>{s['business_pct']:.1f}%</td></tr></table>
<table><tr><th>Trip type</th><th>Distance</th><th>Share</th></tr>{type_rows}</table>
{f'<table><tr><th>Expense category</th><th>Amount</th></tr>{exp_rows}<tr><th>Total</th><th class=n>{cur}{s["expense_total"]:,.2f}</th></tr></table>' if exp_rows else ''}
<h2>Trips</h2>
<table><tr><th>Date</th><th>Time</th><th>From</th><th>To</th><th>Odo start</th><th>Odo end</th>
<th>{unit}</th><th>Type</th><th>Reason</th></tr>{''.join(rows)}</table>
<p style="color:#777">Generated by Tripbook on {datetime.now():%Y-%m-%d %H:%M}</p>
</body></html>"""


# --------------------------------------------------------------------------- reverse geocoding

def reverse_geocode(db: Database, lat: float, lon: float) -> str:
    """Street address for a coordinate via OpenStreetMap Nominatim (cached; callers must rate-limit to 1/s)."""
    cached = db.cached_address(lat, lon)
    if cached is not None:
        return cached
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "zoom": 18, "addressdetails": 1})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    a = data.get("address", {})
    street = " ".join(x for x in [a.get("house_number"), a.get("road")] if x)
    town = a.get("suburb") or a.get("town") or a.get("city") or a.get("village") or ""
    label = ", ".join(x for x in [street, town] if x) or data.get("display_name", "")
    db.cache_address(lat, lon, label)
    return label
