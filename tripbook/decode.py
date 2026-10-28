"""Decoding of SkyTraq data-logger memory into GPS points, and splitting into trips.

Log format (as written by the SkyTraq Venus logger firmware):
  * long entry, 18 bytes, tag 0x40 (normal) or 0x60 (point of interest / button press):
        speed (10 bit), GPS week, time of week, absolute ECEF x/y/z (int32, odd byte order)
  * short entry, 8 bytes, tag 0x80:
        speed, seconds since previous entry, 10 bit signed ECEF deltas
  * erased flash is 0xFF and is skipped.
"""
from __future__ import annotations

import json
import math
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)


@dataclass
class Point:
    time: datetime  # UTC, timezone aware
    lat: float
    lon: float
    alt: float = 0.0
    speed: int = 0  # km/h
    poi: bool = False


# --------------------------------------------------------------------------- geometry

def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS84_E2))
    h = 0.0
    for _ in range(6):
        sin_lat = math.sin(lat)
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def geodetic_to_ecef(lat: float, lon: float, alt: float) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(la) ** 2)
    x = (n + alt) * math.cos(la) * math.cos(lo)
    y = (n + alt) * math.cos(la) * math.sin(lo)
    z = (n * (1 - WGS84_E2) + alt) * math.sin(la)
    return x, y, z


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def path_length_km(points: list[Point]) -> float:
    return sum(haversine_m(a.lat, a.lon, b.lat, b.lon) for a, b in zip(points, points[1:])) / 1000.0


# --------------------------------------------------------------------------- raw log decoding

def _s32(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


def _ecef_plausible(x: int, y: int, z: int) -> bool:
    r = math.sqrt(x * x + y * y + z * z)
    return 6_300_000 < r < 6_450_000  # earth surface up to ~70 km altitude


def decode_log(buf: bytes, max_time: datetime | None = None) -> list[Point]:
    """Decode a raw logger memory dump into a time-sorted, de-duplicated list of points."""
    max_time = max_time or datetime.now(timezone.utc) + timedelta(days=366)
    min_time = datetime(2005, 1, 1, tzinfo=timezone.utc)
    points: dict[datetime, Point] = {}
    t: datetime | None = None
    x = y = z = 0
    i, n = 0, len(buf)
    while i < n:
        tag = buf[i] & 0xE0
        if tag in (0x40, 0x60) and i + 18 <= n:
            d = buf[i:i + 18]
            i += 18
            speed = ((d[0] & 0x03) << 8) | d[1]
            wno = (d[3] | ((d[2] & 0x0F) << 8)) + 1024
            if wno < 1450:
                wno += 1024
            tow = (d[4] << 12) | (d[5] << 4) | (d[2] >> 4)
            x = _s32(d[7] | (d[6] << 8) | (d[9] << 16) | (d[8] << 24))
            y = _s32(d[11] | (d[10] << 8) | (d[13] << 16) | (d[12] << 24))
            z = _s32(d[15] | (d[14] << 8) | (d[17] << 16) | (d[16] << 24))
            t = GPS_EPOCH + timedelta(weeks=wno, seconds=tow)
            poi = tag == 0x60
            if tow >= 604800 or not (min_time <= t <= max_time) or not _ecef_plausible(x, y, z):
                t = None  # corrupt anchor: ignore following short entries too
                continue
        elif tag == 0x80 and t is not None and i + 8 <= n:
            d = buf[i:i + 8]
            i += 8
            speed = ((d[0] & 0x03) << 8) | d[1]
            t = t + timedelta(seconds=(d[2] << 8) | d[3])
            dx = (d[4] << 2) | (d[5] >> 6)
            dy = (d[5] & 0x3F) | (((d[6] >> 4) & 0x0F) << 6)
            dz = ((d[6] & 0x03) << 8) | d[7]
            x += 511 - dx if dx >= 512 else dx
            y += 511 - dy if dy >= 512 else dy
            z += 511 - dz if dz >= 512 else dz
            poi = False
            if t > max_time or not _ecef_plausible(x, y, z):
                t = None
                continue
        else:
            i += 1
            continue
        if t in points:
            continue
        lat, lon, alt = ecef_to_geodetic(x, y, z)
        points[t] = Point(t, lat, lon, alt, speed, poi)
    return [points[k] for k in sorted(points)]


def encode_long(p: Point) -> bytes:
    """Inverse of the long entry decoder (used for tests and GPX→raw round trips)."""
    delta = p.time - GPS_EPOCH
    wno = delta.days // 7
    tow = int(delta.total_seconds()) - wno * 7 * 86400
    x, y, z = (int(round(v)) & 0xFFFFFFFF for v in geodetic_to_ecef(p.lat, p.lon, p.alt))
    sp = min(int(p.speed), 1023)
    d = bytearray(18)
    d[0] = (0x60 if p.poi else 0x40) | (sp >> 8)
    d[1] = sp & 0xFF
    d[2] = (((wno - 1024) >> 8) & 0x0F) | ((tow & 0x0F) << 4)
    d[3] = wno & 0xFF
    d[4] = (tow >> 12) & 0xFF
    d[5] = (tow >> 4) & 0xFF
    for off, v in ((6, x), (10, y), (14, z)):
        d[off], d[off + 1], d[off + 2], d[off + 3] = (v >> 8) & 0xFF, v & 0xFF, (v >> 24) & 0xFF, (v >> 16) & 0xFF
    return bytes(d)


def encode_short(dt_s: int, dx: int, dy: int, dz: int, speed: int) -> bytes:
    def enc(v: int) -> int:
        assert -511 <= v <= 511
        return v if v >= 0 else 511 - v
    ex, ey, ez = enc(dx), enc(dy), enc(dz)
    sp = min(int(speed), 1023)
    return bytes([
        0x80 | (sp >> 8), sp & 0xFF, (dt_s >> 8) & 0xFF, dt_s & 0xFF,
        (ex >> 2) & 0xFF, ((ex & 0x03) << 6) | (ey & 0x3F),
        ((ey >> 6) << 4) | ((ez >> 8) & 0x03), ez & 0xFF,
    ])


# --------------------------------------------------------------------------- trip splitting

@dataclass
class TripCandidate:
    points: list[Point]

    @property
    def start(self) -> Point:
        return self.points[0]

    @property
    def end(self) -> Point:
        return self.points[-1]

    @property
    def distance_km(self) -> float:
        return path_length_km(self.points)

    @property
    def duration_s(self) -> int:
        return int((self.end.time - self.start.time).total_seconds())


def clean_points(points: list[Point], stop_interval_s: int, jitter_m: float = 5.0,
                 max_kmh: float = 300.0) -> list[Point]:
    """Drop stationary GPS jitter and impossible jumps, like the original's spurious-point filter."""
    kept: list[Point] = []
    for p in points:
        if not kept:
            kept.append(p)
            continue
        last = kept[-1]
        dt = (p.time - last.time).total_seconds()
        if dt <= 0:
            continue
        d = haversine_m(last.lat, last.lon, p.lat, p.lon)
        if dt < stop_interval_s:
            if d < jitter_m and not p.poi:
                continue
            if d / dt * 3.6 > max_kmh:
                continue
        kept.append(p)
    return kept


def split_trips(points: list[Point], stop_interval_min: int = 5, min_points: int = 10,
                min_distance_km: float = 0.2) -> list[TripCandidate]:
    """Split points into trips wherever the logger was silent for at least the stop interval."""
    gap = max(1, stop_interval_min) * 60
    pts = clean_points(sorted(points, key=lambda p: p.time), gap)
    trips: list[TripCandidate] = []
    cur: list[Point] = []
    for p in pts:
        if cur and (p.time - cur[-1].time).total_seconds() >= gap:
            trips.append(TripCandidate(cur))
            cur = []
        cur.append(p)
    if cur:
        trips.append(TripCandidate(cur))
    return [t for t in trips if len(t.points) >= min_points and t.distance_km >= min_distance_km]


# --------------------------------------------------------------------------- storage helpers

def pack_points(points: list[Point]) -> bytes:
    rows = [[int(p.time.timestamp()), round(p.lat, 6), round(p.lon, 6), round(p.alt, 1), p.speed, int(p.poi)]
            for p in points]
    return zlib.compress(json.dumps(rows, separators=(",", ":")).encode())


def unpack_points(blob: bytes | None) -> list[Point]:
    if not blob:
        return []
    rows = json.loads(zlib.decompress(blob))
    return [Point(datetime.fromtimestamp(r[0], timezone.utc), r[1], r[2], r[3], r[4], bool(r[5])) for r in rows]
