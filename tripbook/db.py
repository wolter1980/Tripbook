"""SQLite storage for vehicles, trips, places, rules, expenses and reminders."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
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
    radius_m REAL NOT NULL DEFAULT 400,
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
    reason TEXT NOT NULL DEFAULT '',
    reverse_reason TEXT NOT NULL DEFAULT ''
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
    done INTEGER NOT NULL DEFAULT 0,
    notice_days INTEGER NOT NULL DEFAULT 14,
    notice_km REAL NOT NULL DEFAULT 500
);
CREATE TABLE IF NOT EXISTS odometer (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicle(id) ON DELETE CASCADE,
    date TEXT NOT NULL,  -- UTC ISO time of the reading (older rows: a plain YYYY-MM-DD date)
    reading REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mileage_rate (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER REFERENCES vehicle(id) ON DELETE CASCADE,
    from_date TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    rate REAL NOT NULL DEFAULT 0  -- money per km, from from_date until the next rate
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

# Columns added after the first release: (table, column, declaration) for databases created before them.
MIGRATIONS = [
    ("rule", "reverse_reason", "TEXT NOT NULL DEFAULT ''"),
    ("reminder", "notice_days", "INTEGER NOT NULL DEFAULT 14"),
    ("reminder", "notice_km", "REAL NOT NULL DEFAULT 500"),
]

KM_PER_MILE = 1.609344
# A trip end belongs to a place within this distance. Little LogBook matched up to about 400 m: a parked
# car's first/last GPS fix often lies a few hundred metres from the spot that was named.
PLACE_RADIUS_M = 400.0
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


@dataclass
class UnnamedSpot:
    """A location where trips start or end that has no place yet."""
    lat: float
    lon: float
    ends: list[tuple[int, str]] = field(default_factory=list)  # (trip id, "start"/"end")
    labels: dict[str, int] = field(default_factory=dict)  # text the trips show now (e.g. a suburb) -> count

    @property
    def label(self) -> str:
        return max(self.labels, key=self.labels.get) if self.labels else _coord_label(self.lat, self.lon)


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
        for table, col, decl in MIGRATIONS:
            if col not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.commit()
        if self.setting("place_radius_upgraded") != "1":
            # Places made with the old radii (50 m on Little LogBook import, 150 m default) missed most visits.
            self.conn.execute("UPDATE place SET radius_m=? WHERE radius_m IN (50, 150)", (PLACE_RADIUS_M,))
            self.set_setting("place_radius_upgraded", "1")
            self.resolve_places()
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

    @property
    def unit(self) -> str:
        return "mi" if self.use_miles else "km"

    def to_unit(self, km: float) -> float:
        """Kilometres (how everything is stored) to the distance unit the user chose."""
        return km / KM_PER_MILE if self.use_miles else km

    def from_unit(self, value: float) -> float:
        """A distance typed in the user's unit back to kilometres."""
        return value * KM_PER_MILE if self.use_miles else value

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
        """Odometer = last known reading (or opening odometer) + GPS distance of all trips since then.

        The opening odometer is where the vehicle's logbook starts, so it applies from the first trip
        whatever date it carries; only odometer readings are placed in time."""
        by_vehicle: dict[int, list[Trip]] = {}
        for t in trips:
            by_vehicle.setdefault(t.vehicle_id, []).append(t)
        for vid, vtrips in by_vehicle.items():
            v = self.conn.execute("SELECT odometer_start FROM vehicle WHERE id=?", (vid,)).fetchone()
            if not v:
                continue
            # "" sorts before every date: a dated opening odometer must never override later readings.
            anchors = [("", v["odometer_start"])] + sorted(
                (r["date"], r["reading"]) for r in self.q("SELECT date, reading FROM odometer WHERE vehicle_id=?",
                                                          (vid,)))
            all_trips = self.q("SELECT id, start_time, distance_km FROM trip WHERE vehicle_id=? ORDER BY start_time",
                               (vid,))
            odo_by_id: dict[int, float] = {}
            ai, base_date, odo = 0, anchors[0][0], anchors[0][1]
            for r in all_trips:
                # A date-only anchor sorts before every trip of that day, a timed one only before later trips.
                while ai + 1 < len(anchors) and anchors[ai + 1][0] <= r["start_time"]:
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
        if len({t.has_points for t in trips}) > 1:
            raise ValueError("A manual trip has no GPS track, so it cannot be combined with recorded trips.")
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

    def split_trip(self, trip_id: int, index: int) -> int:
        """Split a recorded trip at track point `index` (shared by both halves); returns the new second trip."""
        t = self.trip(trip_id)
        pts = self.trip_points(trip_id)
        if not 0 < index < len(pts) - 1:
            raise ValueError("Choose a split point between the start and the end of the trip.")
        a, b = TripCandidate(pts[:index + 1]), TripCandidate(pts[index:])
        self.conn.execute(
            "UPDATE trip SET end_time=?, end_lat=?, end_lon=?, end_place_id=NULL, end_desc='', distance_km=?, "
            "duration_s=?, poi_count=?, points=? WHERE id=?",
            (to_iso(a.end.time), a.end.lat, a.end.lon, round(a.distance_km, 3), a.duration_s,
             sum(p.poi for p in a.points), pack_points(a.points), trip_id))
        cur = self.conn.execute(
            "INSERT INTO trip(vehicle_id, start_time, end_time, start_lat, start_lon, end_lat, end_lon, "
            "end_place_id, end_desc, distance_km, duration_s, trip_type_id, reason, auto_marked, poi_count, points, "
            "source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.vehicle_id, to_iso(b.start.time), to_iso(b.end.time), b.start.lat, b.start.lon, b.end.lat, b.end.lon,
             t.end_place_id, t.end_desc, round(b.distance_km, 3), b.duration_s, t.trip_type_id, t.reason,
             int(t.auto_marked), sum(p.poi for p in b.points[1:]), pack_points(b.points), t.source))
        self.conn.commit()
        self.resolve_places([trip_id, cur.lastrowid])
        return cur.lastrowid

    def reasons(self, limit: int = 100) -> list[str]:
        """Previously used trip and rule reasons, most used first."""
        return [r[0] for r in self.q(
            "SELECT reason, COUNT(*) n FROM (SELECT reason FROM trip UNION ALL SELECT reason FROM rule "
            "UNION ALL SELECT reverse_reason FROM rule) WHERE reason<>'' GROUP BY reason ORDER BY n DESC, reason "
            "LIMIT ?", (limit,))]

    # -- mileage rates --------------------------------------------------------------
    def mileage_rate(self, vehicle_id: int, day: date) -> float:
        """Rate per km on `day`: the latest rate starting on or before it, a vehicle's own rates first."""
        r = self.conn.execute(
            "SELECT rate FROM mileage_rate WHERE (vehicle_id=? OR vehicle_id IS NULL) AND from_date<=? "
            "ORDER BY vehicle_id IS NULL, from_date DESC LIMIT 1", (vehicle_id, day.isoformat())).fetchone()
        return r[0] if r else 0.0

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

    def name_place(self, lat: float, lon: float, name: str, place_id: int | None = None) -> int:
        """Name the location at lat/lon for every trip: rename its place (the one a trip end is attached to,
        else the nearest one in range) or create a new place, then relabel all trips."""
        place = (self.q("SELECT * FROM place WHERE id=?", (place_id,)) or [None])[0] if place_id else None
        place = place or self.nearest_place(lat, lon)
        if place:
            self.x("UPDATE place SET name=? WHERE id=?", (name, place["id"]))
            pid = place["id"]
        else:
            pid = self.x("INSERT INTO place(name, lat, lon, radius_m, address) VALUES (?,?,?,?,?)",
                         (name, lat, lon, PLACE_RADIUS_M, self.cached_address(lat, lon) or ""))
        self.resolve_places()
        return pid

    def create_places(self, places: list[tuple[str, float, float]]) -> None:
        """Add several (name, lat, lon) places at once and relabel all trips."""
        self.conn.executemany("INSERT INTO place(name, lat, lon, radius_m) VALUES (?,?,?,?)",
                              [(n, la, lo, PLACE_RADIUS_M) for n, la, lo in places])
        self.conn.commit()
        self.resolve_places()

    def unnamed_spots(self, include_described: bool = False) -> list[UnnamedSpot]:
        """Trip ends that belong to no place, grouped into spots of PLACE_RADIUS_M, most visited first.
        Without `include_described` only ends that show bare coordinates are considered."""
        ends = []
        for r in self.q("SELECT id, start_lat, start_lon, end_lat, end_lon, start_place_id, end_place_id, "
                        "start_desc, end_desc FROM trip WHERE start_lat IS NOT NULL"):
            for which in ("start", "end"):
                lat, desc = r[f"{which}_lat"], r[f"{which}_desc"]
                if lat is not None and r[f"{which}_place_id"] is None and (include_described or not desc):
                    ends.append((lat, r[f"{which}_lon"], r["id"], which, desc))
        # Seed spots from the densest points so a spot is centred where the car usually stops.
        density = {i: sum(haversine_m(a[0], a[1], b[0], b[1]) <= PLACE_RADIUS_M / 2 for b in ends)
                   for i, a in enumerate(ends)} if len(ends) <= 3000 else {}
        spots: list[UnnamedSpot] = []
        for i in sorted(range(len(ends)), key=lambda i: -density.get(i, 0)):
            lat, lon, tid, which, desc = ends[i]
            spot = next((s for s in spots if haversine_m(lat, lon, s.lat, s.lon) <= PLACE_RADIUS_M), None)
            if spot is None:
                spot = UnnamedSpot(lat, lon)
                spots.append(spot)
            spot.ends.append((tid, which))
            if desc:
                spot.labels[desc] = spot.labels.get(desc, 0) + 1
        return sorted(spots, key=lambda s: -len(s.ends))

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
                    reason = r["reason"] if fwd else (r["reverse_reason"] or r["reason"])
                    self.conn.execute(
                        "UPDATE trip SET trip_type_id=?, auto_marked=1, "
                        "reason=CASE WHEN reason='' THEN ? ELSE reason END WHERE id=?",
                        (r["trip_type_id"], reason, t["id"]))
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
    def due_reminders(self) -> list[tuple[sqlite3.Row, str]]:
        """(reminder, status) pairs; status is "overdue", "due soon" (within the reminder's notice) or ""."""
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
                elif d <= today + timedelta(days=r["notice_days"]):
                    status = "due soon"
            if r["due_odometer"]:
                odo = odo_cache.setdefault(r["vehicle_id"], self.current_odometer(r["vehicle_id"]))
                if odo >= r["due_odometer"]:
                    status = "overdue"
                elif odo >= r["due_odometer"] - r["notice_km"] and not status:
                    status = "due soon"
            out.append((r, status))
        return out

    # -- backups --------------------------------------------------------------------
    def backup_to(self, path: Path) -> None:
        self.conn.commit()
        dst = sqlite3.connect(path)
        with dst:
            self.conn.backup(dst)
        dst.close()

    def auto_backup(self, folder: Path, keep: int) -> Path:
        """Write a timestamped backup into `folder` and delete the oldest beyond `keep`."""
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"tripbook-{datetime.now():%Y%m%d-%H%M%S}.db"
        self.backup_to(path)
        for old in sorted(folder.glob("tripbook-*.db"))[:-max(1, keep)]:
            old.unlink(missing_ok=True)
        return path

    def restore_from(self, path: Path) -> None:
        """Replace the whole database with a backup (checked to be a Tripbook database first)."""
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trip'").fetchone():
                raise ValueError("This file is not a Tripbook database.")
            src.backup(self.conn)
        except sqlite3.DatabaseError as e:
            raise ValueError(f"This file is not a Tripbook database ({e}).") from None
        finally:
            src.close()
        self.conn.close()
        self.__init__(self.path)  # re-apply schema migrations to an older backup

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
