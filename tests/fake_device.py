"""A simulated SkyTraq trip logger on a pseudo terminal, for testing without hardware."""
from __future__ import annotations

import math
import os
import threading
import tty
from datetime import datetime, timedelta, timezone

from tripbook import device as D
from tripbook.decode import Point, encode_long, encode_short, geodetic_to_ecef


def synth_drive(start: datetime, lat: float, lon: float, minutes: int, heading_deg: float,
                step_s: int = 5, speed_kmh: float = 54) -> tuple[bytes, list[Point]]:
    """Encode a straight drive as the logger would: a long entry every 20 points, short deltas between."""
    out = bytearray()
    pts = []
    v = speed_kmh / 3.6
    t = start
    prev_ecef = None
    for i in range(minutes * 60 // step_s):
        d = v * step_s * i
        la = lat + d * math.cos(math.radians(heading_deg)) / 111_320
        lo = lon + d * math.sin(math.radians(heading_deg)) / (111_320 * math.cos(math.radians(lat)))
        p = Point(t, la, lo, 1600.0, int(speed_kmh))
        ecef = tuple(int(round(c)) for c in geodetic_to_ecef(la, lo, 1600.0))
        if i % 20 == 0:
            out += encode_long(p)
        else:
            dx, dy, dz = (a - b for a, b in zip(ecef, prev_ecef))
            out += encode_short(step_s, dx, dy, dz, int(speed_kmh))
        prev_ecef = ecef
        pts.append(p)
        t += timedelta(seconds=step_s)
    return bytes(out), pts


class FakeLogger(threading.Thread):
    def __init__(self, log_bytes: bytes, password: str = "", total_sectors: int = 100,
                 userdata=("01Jan2024", "45292.5", "CA 123-456"), nmea_noise: bool = True):
        super().__init__(daemon=True)
        self.master, self.slave = os.openpty()
        tty.setraw(self.master)
        self.path = os.ttyname(self.slave)
        pad = (-len(log_bytes)) % D.SECTOR_SIZE
        self.flash = bytearray(log_bytes + b"\xff" * pad)
        self.total = total_sectors
        self.password = password
        self.logged_in = not password
        self.userdata = list(userdata)
        self.nmea = nmea_noise
        self.erased = False
        self.running = True
        self.buf = bytearray()

    @property
    def used(self) -> int:
        return len(self.flash) // D.SECTOR_SIZE

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def packet(self, payload: bytes) -> None:
        self.send(D.frame(payload))

    def ack(self, cmd: int, ok: bool = True) -> None:
        if self.nmea:
            self.send(b"$GPGGA,000000.00,0000.0000,N,00000.0000,E,0,00,99.9,0.0,M,0.0,M,,*6B\r\n")
        self.packet(bytes([D.RSP_ACK if ok else D.RSP_NACK, cmd]))

    def run(self) -> None:
        while self.running:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            self.buf += chunk
            while True:
                i = self.buf.find(b"\xA0\xA1")
                if i < 0 or len(self.buf) < i + 4:
                    break
                n = int.from_bytes(self.buf[i + 2:i + 4], "big")
                if len(self.buf) < i + 4 + n + 3:
                    break
                payload = bytes(self.buf[i + 4:i + 4 + n])
                del self.buf[:i + 4 + n + 3]
                self.handle(payload)

    def handle(self, p: bytes) -> None:
        cmd = p[0]
        locked = cmd not in (D.CMD_QUERY_VERSION, D.CMD_LOGIN, D.CMD_MESSAGE_TYPE) and not self.logged_in
        if locked:
            self.ack(cmd, False)
            return
        if cmd == D.CMD_QUERY_VERSION:
            self.ack(cmd)
            self.packet(bytes([0x80, 1, 0, 1, 101, 101, 0, 2, 228, 2, 0, 13, 9, 23]))
        elif cmd == D.CMD_LOGIN:
            pw = p[1:17].rstrip(b"\0").decode()
            self.ack(cmd)
            self.logged_in = pw == self.password
            self.packet(bytes([D.RSP_LOGIN_STATUS, 1 if self.logged_in else 0]))
        elif cmd == D.CMD_GET_USERDATA:
            self.ack(cmd)
            body = b"\xfa".join(x.encode() for x in self.userdata) + b"\xfa"
            self.packet(bytes([0xF4, 0x01]) + body.ljust(81, b"\0"))
        elif cmd == D.CMD_SET_USERDATA:
            parts = p[2:].split(b"\xfa")
            self.userdata = [x.rstrip(b"\0").decode() for x in parts[:3]]
            self.ack(cmd)
        elif cmd == D.CMD_GET_LOG_CONFIG:
            self.ack(cmd)
            left = self.total - self.used + 1
            body = bytearray([0x97])
            body += (len(self.flash)).to_bytes(4, "little") + left.to_bytes(2, "little")
            body += self.total.to_bytes(2, "little")
            for v in (3600, 5, 1000, 10, 200, 5):
                body += v.to_bytes(4, "little")
            body += bytes([1, 0])
            self.packet(bytes(body))
        elif cmd == D.CMD_READ_SECTORS:
            s = (p[1] << 8) | p[2]
            count = (p[3] << 8) | p[4]
            self.ack(cmd)
            data = bytes(self.flash[s * D.SECTOR_SIZE:(s + count) * D.SECTOR_SIZE])
            data = data.ljust(count * D.SECTOR_SIZE, b"\xff")
            self.send(data + b"END\x00CHECKSUM=" + bytes([D.checksum(data)]) + b"\x00\r\n\x00\x00")
        elif cmd == D.CMD_ERASE_LOG:
            self.flash = bytearray()
            self.erased = True
            self.ack(cmd)
        elif cmd in (D.CMD_MESSAGE_TYPE, D.CMD_WRITE_LOG_CONFIG, D.CMD_LOGOUT):
            self.ack(cmd)
        elif cmd == D.CMD_AGPS_STATUS:
            self.ack(cmd)
            self.packet(bytes([0xB2, 72, 0, 1]))
        else:
            self.ack(cmd, False)

    def stop(self) -> None:
        self.running = False
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


def sample_log() -> tuple[bytes, list[Point]]:
    t0 = datetime(2026, 10, 20, 6, 30, tzinfo=timezone.utc)
    a, pa = synth_drive(t0, -26.1076, 28.0567, 20, 90)  # home → office
    b, pb = synth_drive(t0 + timedelta(hours=9), pa[-1].lat, pa[-1].lon, 20, 270)  # office → home
    return a + b, pa + pb
