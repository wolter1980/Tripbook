"""SQLite storage for vehicles, trips, places, rules, expenses and reminders."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .decode import Point, TripCandidate, haversine_m, pack_points, unpack_points

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    registration TEXT NOT NULL DEFAULT '',
    make TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    odometer_start REAL NOT NULL DEFAULT 0,
    odometer_start_date TEXT,
    device_serial TEXT NOT NULL DEFAULT '',
    device_password TEXT NOT NULL DEFAULT '',
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trip_type (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#888888',
    is_business INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS place (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m REAL NOT NULL DEFAULT 150,
    address TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS trip (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicle(id) ON DELETE CASCADE,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL,
    start_place_id INTEGER REFERENCES place(id) ON DELETE SET NULL,
    end_place_id INTEGER REFERENCES place(id) ON DELETE SET NULL,
    start_desc TEXT NOT NULL DEFAULT '',
    end_desc TEXT NOT NULL DEFAULT '',
    distance_km REAL NOT NULL DEFAULT 0,
    duration_s INTEGER NOT NULL DEFAULT 0,
    trip_type_id INTEGER REFERENCES trip_type(id) ON DELETE SET NULL,
    reason TEXT NOT NULL DEFAULT '',
    auto_marked INTEGER NOT NULL DEFAULT 0,
    poi_count INTEGER NOT NULL DEFAULT 0,
    points BLOB,
    source TEXT NOT NULL DEFAULT 'device'
);
CREATE INDEX IF NOT EXISTS trip_vehicle_time ON trip(vehicle_id, start_time);
CREATE TABLE IF NOT EXISTS rule (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER REFERENCES vehicle(id) ON DELETE CASCADE,
    start_place_id INTEGER NOT NULL REFERENCES place(id) ON DELETE CASCADE,
    end_place_id INTEGER NOT NULL REFERENCES place(id) ON DELETE CASCADE,
    trip_type_id INTEGER NOT NULL REFERENCES trip_type(id) ON DELETE CASCADE,
    reverse INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS expense (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicle(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Fuel',
    description TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    volume REAL NOT NULL DEFAULT 0,
    odometer REAL
);
CREATE TABLE IF NOT EXISTS reminder (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicle(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    due_date TEXT,
    due_odometer REAL,
    repeat_months INTEGER NOT NULL DEFAULT 0,
    repeat_km REAL NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS odometer (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicle(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    reading REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS download (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER,
    time TEXT NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    trips_added INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS geocode_cache (
    key TEXT PRIMARY KEY,
    address TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_TYPES = [("Business", "#2a7de1", 1), ("Private", "#e07b39", 0)]
EXPENSE_CATEGORIES = ["Fuel", "Maintenance", "Tyres", "Insurance", "Licence", "Tolls", "Parking", "Finance", "Other"]


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass
class Trip:
    id: int
    vehicle_id: int
    start_time: datetime
    end_time: datetime
    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None
    start_place_id: int | None
    end_place_id: int | None
    start_desc: str
    end_desc: str
    distance_km: float
    duration_s: int
    trip_type_id: int | None
    reason: str
    auto_marked: bool
    poi_count: int
    source: str
    has_points: bool
    odo_start: float = 0.0
    odo_end: float = 0.0

    @staticmethod
    def from_row(r: sqlite3.Row) -> "Trip":
        return Trip(r["id"], r["vehicle_id"], from_iso(r["start_time"]), from_iso(r["end_time"]),
                    r["start_lat"], r["start_lon"], r["end_lat"], r["end_lon"],
                    r["start_place_id"], r["end_place_id"], r["start_desc"], r["end_desc"],
                    r["distance_km"], r["duration_s"], r["trip_type_id"], r["reason"],
                    bool(r["auto_marked"]), r["poi_count"], r["source"], bool(r["has_points"]))

    def start_label(self) -> str:
        return self.start_desc or _coord_label(self.start_lat, self.start_lon)

    def end_label(self) -> str:
        return self.end_desc or _coord_label(self.end_lat, self.end_lon)


def _coord_label(lat, lon) -> str:
    return f"{lat:.5f}, {lon:.5f}" if lat is not None and lon is not None else ""


TRIP_COLS = ("id, vehicle_id, start_time, end_time, start_lat, start_lon, end_lat, end_lon, start_place_id, "
             "end_place_id, start_desc, end_desc, distance_km, duration_s, trip_type_id, reason, auto_marked, "
             "poi_count, source, points IS NOT NULL AS has_points")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        if not self.conn.execute("SELECT 1 FROM trip_type").fetchone():
            self.conn.executemany("INSERT INTO trip_type(name, color, is_business) VALUES (?,?,?)", DEFAULT_TYPES)
        self.conn.commit()

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def x(self, sql: str, args=()) -> int:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur.lastrowid

    # -- settings -------------------------------------------------------------------
    def setting(self, key: str, default: str = "") -> str:
        r = self.conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
        return r[0] if r and r[0] is not None else default

    def set_setting(self, key: str, value) -> None:
        self.x("INSERT INTO setting(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, str(value)))

    @property
    def stop_interval_min(self) -> int:
        return int(self.setting("stop_interval_min", "5"))

    @property
    def use_miles(self) -> bool:
        return self.setting("units", "km") == "mi"

    # -- lookups --------------------------------------------------------------------
    def vehicles(self, include_closed: bool = True) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM vehicle" + ("" if include_closed else " WHERE closed=0") + " ORDER BY name")

    def trip_types(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM trip_type ORDER BY id")

    def places(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM place ORDER BY name")

    # -- trips ----------------------------------------------------------------------
    def trips(self, vehicle_id: int | None = None, start: datetime | None = None, end: datetime | None = None,
              type_filter: int | None = None, text: str = "") -> list[Trip]:
        sql = f"SELECT {TRIP_COLS} FROM trip WHERE 1=1"
        args: list = []
        if vehicle_id:
            sql += " AND vehicle_id=?"
            args.append(vehicle_id)
        if start:
            sql += " AND start_time>=?"
            args.append(to_iso(start))
        if end:
            sql += " AND start_time<?"
            args.append(to_iso(end))
        if type_filter == -1:
            sql += " AND trip_type_id IS NULL"
        elif type_filter:
            sql += " AND trip_type_id=?"
            args.append(type_filter)
        if text:
            sql += " AND (start_desc LIKE ? OR end_desc LIKE ? OR reason LIKE ?)"
            args += [f"%{text}%"] * 3
        trips = [Trip.from_row(r) for r in self.q(sql + " ORDER BY start_time", args)]
        self._fill_odometers(trips)
        return trips

    def trip(self, trip_id: int) -> Trip:
        return Trip.from_row(self.q(f"SELECT {TRIP_COLS} FROM trip WHERE id=?", (trip_id,))[0])

    def trip_points(self, trip_id: int) -> list[Point]:
        r = self.conn.execute("SELECT points FROM trip WHERE id=?", (trip_id,)).fetchone()
        return unpack_points(r[0]) if r else []

    def _fill_odometers(self, trips: list[Trip]) -> None:
        """Odometer = last known reading (or opening odometer) + GPS distance of all trips since then."""
        by_vehicle: dict[int, list[Trip]] = {}
        for t in trips:
            by_vehicle.setdefault(t.vehicle_id, []).append(t)
        for vid, vtrips in by_vehicle.items():
            v = self.conn.execute("SELECT odometer_start, odometer_start_date FROM vehicle WHERE id=?",
                                  (vid,)).fetchone()
            if not v:
                continue
            anchors = [(v["odometer_start_date"] or "0000", v["odometer_start"])]
            anchors += [(r["date"], r["reading"]) for r in
                        self.q("SELECT date, reading FROM odometer WHERE vehicle_id=? ORDER BY date", (vid,))]
            anchors.sort()
            all_trips = self.q("SELECT id, start_time, distance_km FROM trip WHERE vehicle_id=? ORDER BY start_time",
                               (vid,))
            odo_by_id: dict[int, float] = {}
            ai, base_date, odo = 0, anchors[0][0], anchors[0][1]
            for r in all_trips:
                day = r["start_time"][:10]
                while ai + 1 < len(anchors) and anchors[ai + 1][0] <= day:
                    ai += 1
                    base_date, odo = anchors[ai]
                odo_by_id[r["id"]] = odo
                odo += r["distance_km"]
            for t in vtrips:
                t.odo_start = odo_by_id.get(t.id, 0.0)
                t.odo_end = t.odo_start + t.distance_km

    def current_odometer(self, vehicle_id: int) -> float:
        trips = self.trips(vehicle_id)
        if trips:
            return trips[-1].odo_end
        v = self.conn.execute("SELECT odometer_start FROM vehicle WHERE id=?", (vehicle_id,)).fetchone()
        r = self.conn.execute("SELECT MAX(reading) FROM odometer WHERE vehicle_id=?", (vehicle_id,)).fetchone()
        return max(v[0] if v else 0, r[0] or 0)

    def overlaps(self, vehicle_id: int, start: datetime, end: datetime) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM trip WHERE vehicle_id=? AND start_time<=? AND end_time>=? LIMIT 1",
            (vehicle_id, to_iso(end), to_iso(start))).fetchone())

    def add_trip_from_points(self, vehicle_id: int, cand: TripCandidate, source: str = "device") -> int | None:
        if self.overlaps(vehicle_id, cand.start.time, cand.end.time):
            return None
        s, e = cand.start, cand.end
        cur = self.conn.execute(
            "INSERT INTO trip(vehicle_id, start_time, end_time, start_lat, start_lon, end_lat, end_lon, "
            "distance_km, duration_s, poi_count, points, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (vehicle_id, to_iso(s.time), to_iso(e.time), s.lat, s.lon, e.lat, e.lon, round(cand.distance_km, 3),
             cand.duration_s, sum(p.poi for p in cand.points), pack_points(cand.points), source))
        return cur.lastrowid

    def add_manual_trip(self, vehicle_id: int, start: datetime, end: datetime, start_desc: str, end_desc: str,
                        distance_km: float, trip_type_id: int | None, reason: str) -> int:
        return self.x("INSERT INTO trip(vehicle_id, start_time, end_time, start_desc, end_desc, distance_km, "
                      "duration_s, trip_type_id, reason, source) VALUES (?,?,?,?,?,?,?,?,?, 'manual')",
                      (vehicle_id, to_iso(start), to_iso(end), start_desc, end_desc, distance_km,
                       int((end - start).total_seconds()), trip_type_id, reason))

    def update_trip(self, trip_id: int, **fields) -> None:
        if "start_time" in fields:
            fields["start_time"] = to_iso(fields["start_time"])
        if "end_time" in fields:
            fields["end_time"] = to_iso(fields["end_time"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE trip SET {cols} WHERE id=?", (*fields.values(), trip_id))

    def delete_trips(self, ids: list[int]) -> None:
        self.conn.executemany("DELETE FROM trip WHERE id=?", [(i,) for i in ids])
        self.conn.commit()

    def merge_trips(self, ids: list[int]) -> int:
        trips = sorted((self.trip(i) for i in ids), key=lambda t: t.start_time)
        if len(trips) < 2 or len({t.vehicle_id for t in trips}) != 1:
            raise ValueError("Select at least two trips of the same vehicle.")
        all_ids = [t.id for t in self.trips(trips[0].vehicle_id)]
        idx = [all_ids.index(t.id) for t in trips]
        if idx != list(range(idx[0], idx[0] + len(idx))):
            raise ValueError("Only consecutive trips can be combined.")
        pts: list[Point] = []
        for t in trips:
            pts += self.trip_points(t.id)
        first, last = trips[0], trips[-1]
        reason = "; ".join(dict.fromkeys(t.reason for t in trips if t.reason))
        self.conn.execute(
            "UPDATE trip SET end_time=?, end_lat=?, end_lon=?, end_place_id=?, end_desc=?, distance_km=?, "
            "duration_s=?, reason=?, poi_count=?, points=? WHERE id=?",
            (to_iso(last.end_time), last.end_lat, last.end_lon, last.end_place_id, last.end_desc,
             round(sum(t.distance_km for t in trips), 3), int((last.end_time - first.start_time).total_seconds()),
             reason, sum(t.poi_count for t in trips), pack_points(pts) if pts else None, first.id))
        self.conn.executemany("DELETE FROM trip WHERE id=?", [(t.id,) for t in trips[1:]])
        self.conn.commit()
        return first.id

    # -- places & auto-marking ------------------------------------------------------
    def nearest_place(self, lat: float | None, lon: float | None, places=None) -> sqlite3.Row | None:
        if lat is None or lon is None:
            return None
        best, best_d = None, 1e18
        for p in places if places is not None else self.places():
            d = haversine_m(lat, lon, p["lat"], p["lon"])
            if d <= p["radius_m"] and d < best_d:
                best, best_d = p, d
        return best

    def resolve_places(self, trip_ids: list[int] | None = None) -> None:
        """Attach trip ends to named places, then apply auto-mark rules."""
        places = self.places()
        by_id = {p["id"]: p for p in places}
        sql = "SELECT id, start_lat, start_lon, end_lat, end_lon, start_place_id, end_place_id, start_desc, " \
              "end_desc FROM trip WHERE start_lat IS NOT NULL"
        rows = self.q(sql) if trip_ids is None else [r for i in trip_ids for r in self.q(sql + " AND id=?", (i,))]
        updates = []
        for r in rows:
            # A trip keeps the place it was assigned to unless it lies within another place's radius.
            sp = self.nearest_place(r["start_lat"], r["start_lon"], places) or by_id.get(r["start_place_id"])
            ep = self.nearest_place(r["end_lat"], r["end_lon"], places) or by_id.get(r["end_place_id"])
            sd = sp["name"] if sp else r["start_desc"]
            ed = ep["name"] if ep else r["end_desc"]
            new = (sp["id"] if sp else None, ep["id"] if ep else None, sd, ed)
            if new != (r["start_place_id"], r["end_place_id"], r["start_desc"], r["end_desc"]):
                updates.append((*new, r["id"]))
        self.conn.executemany("UPDATE trip SET start_place_id=?, end_place_id=?, start_desc=?, end_desc=? "
                              "WHERE id=?", updates)
        self.conn.commit()
        self.apply_rules()

    def delete_places(self, ids: list[int]) -> None:
        for pid in ids:
            self.conn.execute("UPDATE trip SET start_desc='' WHERE start_place_id=?", (pid,))
            self.conn.execute("UPDATE trip SET end_desc='' WHERE end_place_id=?", (pid,))
            self.conn.execute("DELETE FROM place WHERE id=?", (pid,))
        self.conn.commit()
        self.resolve_places()

    def apply_rules(self) -> int:
        rules = self.q("SELECT * FROM rule")
        changed = 0
        # Clear previous automatic marks so edited/deleted rules take effect.
        self.conn.execute("UPDATE trip SET trip_type_id=NULL, auto_marked=0 WHERE auto_marked=1")
        for t in self.q("SELECT id, vehicle_id, start_place_id, end_place_id FROM trip "
                        "WHERE trip_type_id IS NULL AND start_place_id IS NOT NULL AND end_place_id IS NOT NULL"):
            for r in rules:
                if r["vehicle_id"] and r["vehicle_id"] != t["vehicle_id"]:
                    continue
                fwd = (r["start_place_id"], r["end_place_id"]) == (t["start_place_id"], t["end_place_id"])
                rev = r["reverse"] and (r["end_place_id"], r["start_place_id"]) == (t["start_place_id"],
                                                                                    t["end_place_id"])
                if fwd or rev:
                    self.conn.execute(
                        "UPDATE trip SET trip_type_id=?, auto_marked=1, "
                        "reason=CASE WHEN reason='' THEN ? ELSE reason END WHERE id=?",
                        (r["trip_type_id"], r["reason"], t["id"]))
                    changed += 1
                    break
        self.conn.commit()
        return changed

    # -- geocode cache --------------------------------------------------------------
    def cached_address(self, lat: float, lon: float) -> str | None:
        r = self.conn.execute("SELECT address FROM geocode_cache WHERE key=?", (f"{lat:.4f},{lon:.4f}",)).fetchone()
        return r[0] if r else None

    def cache_address(self, lat: float, lon: float, address: str) -> None:
        self.x("INSERT OR REPLACE INTO geocode_cache(key, address) VALUES (?,?)", (f"{lat:.4f},{lon:.4f}", address))

    # -- reminders ------------------------------------------------------------------
    def due_reminders(self, soon_days: int = 14, soon_km: float = 500) -> list[tuple[sqlite3.Row, str]]:
        out = []
        today = date.today()
        odo_cache: dict[int, float] = {}
        for r in self.q("SELECT r.*, v.name AS vehicle FROM reminder r JOIN vehicle v ON v.id=r.vehicle_id "
                        "WHERE done=0"):
            status = ""
            if r["due_date"]:
                d = date.fromisoformat(r["due_date"])
                if d < today:
                    status = "overdue"
                elif d <= today + timedelta(days=soon_days):
                    status = "due soon"
            if r["due_odometer"]:
                odo = odo_cache.setdefault(r["vehicle_id"], self.current_odometer(r["vehicle_id"]))
                if odo >= r["due_odometer"]:
                    status = "overdue"
                elif odo >= r["due_odometer"] - soon_km and not status:
                    status = "due soon"
            out.append((r, status))
        return out

    def complete_reminder(self, rid: int) -> None:
        r = self.q("SELECT * FROM reminder WHERE id=?", (rid,))[0]
        if r["repeat_months"] or r["repeat_km"]:
            due_date = r["due_date"]
            if r["repeat_months"] and due_date:
                d = date.fromisoformat(due_date)
                m = d.month - 1 + r["repeat_months"]
                y, m = d.year + m // 12, m % 12 + 1
                due_date = date(y, m, min(d.day, 28)).isoformat()
            due_odo = r["due_odometer"]
            if r["repeat_km"]:
                due_odo = self.current_odometer(r["vehicle_id"]) + r["repeat_km"]
            self.x("UPDATE reminder SET due_date=?, due_odometer=? WHERE id=?", (due_date, due_odo, rid))
        else:
            self.x("UPDATE reminder SET done=1 WHERE id=?", (rid,))
