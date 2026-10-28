"""Serial communication with SkyTraq-based GPS trip loggers (the USB "Little LogBook" style tracker).

Packets: A0 A1 <len hi> <len lo> <payload> <xor checksum> 0D 0A.  Every command is
answered with ACK (0x83 <cmd>) or NACK (0x84 <cmd>), optionally followed by a reply packet.
"""
from __future__ import annotations

import glob
import logging
import os
import select
import termios
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("tripbook.device")

SUPPORTED_USB_IDS = {("0483", "5740"): "STM32 virtual COM (new generation)",
                     ("067b", "2303"): "Prolific PL2303 (old generation)"}
SECTOR_SIZE = 4096
MAX_SECTORS = 508

CMD_SYSTEM_RESTART = 0x01
CMD_QUERY_VERSION = 0x02
CMD_FACTORY_DEFAULTS = 0x04
CMD_CONFIGURE_SERIAL = 0x05
CMD_MESSAGE_TYPE = 0x09
CMD_GET_LOG_CONFIG = 0x17
CMD_WRITE_LOG_CONFIG = 0x18
CMD_ERASE_LOG = 0x19
CMD_READ_SECTORS = 0x1D
CMD_AGPS_STATUS = 0x34
CMD_LOGIN = 0x46
CMD_LOGOUT = 0x47
CMD_CHANGE_PASSWORD = 0x48
CMD_CLEAR_PASSWORD = 0x73
CMD_GET_USERDATA = 0x74
CMD_SET_USERDATA = 0x75
RSP_ACK = 0x83
RSP_NACK = 0x84
RSP_LOGIN_STATUS = 0xD1
USERDATA_DELIMITER = 0xFA

BAUDS = {4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200,
         38400: termios.B38400, 57600: termios.B57600, 115200: termios.B115200}


class DeviceError(Exception):
    pass


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- port discovery

@dataclass
class PortInfo:
    path: str
    vid: str = ""
    pid: str = ""
    product: str = ""

    @property
    def supported(self) -> bool:
        return (self.vid, self.pid) in SUPPORTED_USB_IDS

    def label(self) -> str:
        desc = SUPPORTED_USB_IDS.get((self.vid, self.pid)) or self.product or "serial port"
        ids = f" [{self.vid}:{self.pid}]" if self.vid else ""
        return f"{self.path} – {desc}{ids}"


def _usb_attr(dev_dir: Path, name: str) -> str:
    for d in [dev_dir, *dev_dir.parents][:6]:
        f = d / name
        if f.is_file():
            try:
                return f.read_text().strip()
            except OSError:
                return ""
    return ""


def list_ports() -> list[PortInfo]:
    ports = []
    for path in sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")):
        dev = Path("/sys/class/tty") / os.path.basename(path) / "device"
        info = PortInfo(path)
        if dev.exists():
            real = dev.resolve()
            info.vid = _usb_attr(real, "idVendor").lower()
            info.pid = _usb_attr(real, "idProduct").lower()
            info.product = _usb_attr(real, "product")
        ports.append(info)
    ports.sort(key=lambda p: not p.supported)
    return ports


# --------------------------------------------------------------------------- raw serial port

class SerialPort:
    """Minimal raw 8N1 serial port on top of termios (no pyserial needed)."""

    def __init__(self, path: str, baud: int = 115200):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError as e:
            raise DeviceError(f"No permission to open {path}. Add yourself to the 'dialout' group "
                              f"(sudo usermod -aG dialout $USER) and log in again.") from e
        except OSError as e:
            raise DeviceError(f"Cannot open {path}: {e.strerror}") from e
        self.set_baud(baud)

    def set_baud(self, baud: int) -> None:
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = termios.IGNPAR  # iflag
        attrs[1] = 0  # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        attrs[3] = 0  # lflag
        attrs[4] = attrs[5] = BAUDS[baud]
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.baud = baud

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            _, w, _ = select.select([], [self.fd], [], 2.0)
            if not w:
                raise DeviceError("Timeout writing to device")
            n = os.write(self.fd, view)
            view = view[n:]

    def read(self, size: int, timeout: float) -> bytes:
        """Read up to `size` bytes, returning early only when the timeout expires."""
        out = bytearray()
        deadline = time.monotonic() + timeout
        while len(out) < size:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                break
            try:
                chunk = os.read(self.fd, size - len(out))
            except BlockingIOError:
                continue
            if not chunk:
                time.sleep(0.005)
                continue
            out += chunk
        return bytes(out)

    def read_available(self) -> bytes:
        try:
            return os.read(self.fd, 65536)
        except BlockingIOError:
            return b""

    def flush_input(self) -> None:
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


# --------------------------------------------------------------------------- protocol

@dataclass
class LogConfig:
    write_pointer: int = 0
    sectors_left: int = 0
    total_sectors: int = 0
    max_time: int = 0
    min_time: int = 0
    max_distance: int = 0
    min_distance: int = 0
    max_speed: int = 0
    min_speed: int = 0
    enabled: int = 0
    fifo_mode: int = 0

    @property
    def used_sectors(self) -> int:
        return max(0, self.total_sectors - self.sectors_left + 1)


@dataclass
class UserData:
    registration_date: str = ""
    serial: str = ""
    registration_number: str = ""


def checksum(payload: bytes) -> int:
    c = 0
    for b in payload:
        c ^= b
    return c


def frame(payload: bytes) -> bytes:
    return b"\xA0\xA1" + len(payload).to_bytes(2, "big") + payload + bytes([checksum(payload)]) + b"\r\n"


class Logger:
    """Talks to the logger. All methods block; run them in a worker thread."""

    def __init__(self, port: SerialPort, cancelled: Callable[[], bool] = lambda: False):
        self.port = port
        self.cancelled = cancelled
        self._buf = bytearray()

    # -- low level ------------------------------------------------------------------
    def _check_cancel(self) -> None:
        if self.cancelled():
            raise Cancelled()

    def _take(self, n: int, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while len(self._buf) < n:
            self._check_cancel()
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            chunk = self.port.read(1, min(left, 0.1))
            if chunk:
                self._buf += chunk + self.port.read_available()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _read_packet(self, timeout: float = 1.5) -> bytes | None:
        deadline = time.monotonic() + timeout
        prev = -1
        while time.monotonic() < deadline:
            b = self._take(1, deadline - time.monotonic())
            if b is None:
                return None
            if prev == 0xA0 and b[0] == 0xA1:
                hdr = self._take(2, 0.5)
                if hdr is None:
                    return None
                length = int.from_bytes(hdr, "big")
                rest = self._take(length + 3, 1.0)
                if rest is None:
                    return None
                payload, cs, tail = rest[:length], rest[length], rest[length + 1:]
                if tail == b"\r\n" and checksum(payload) == cs:
                    log.debug("<< %s", payload.hex(" "))
                    return payload
                prev = -1
                continue
            prev = b[0]
        return None

    def _send(self, payload: bytes) -> None:
        log.debug(">> %s", payload.hex(" "))
        self.port.write(frame(payload))

    def command(self, payload: bytes, timeout: float = 1.5) -> bool:
        """Send a command and wait for its ACK. Non-matching packets (NMEA, other replies) are skipped."""
        self._send(payload)
        for _ in range(3):
            pkt = self._read_packet(timeout)
            if pkt is None:
                continue
            if len(pkt) >= 2 and pkt[1] == payload[0]:
                if pkt[0] == RSP_ACK:
                    return True
                if pkt[0] == RSP_NACK:
                    return False
        return False

    def retry(self, fn: Callable[[], object], attempts: int = 5):
        for _ in range(attempts):
            self._check_cancel()
            result = fn()
            if result:
                return result
            time.sleep(0.1)
        return None

    # -- connection -----------------------------------------------------------------
    def detect_speed(self) -> int:
        """Find the baud rate the logger is talking at (115200 for current devices)."""
        query = frame(bytes([CMD_QUERY_VERSION, 0x01]))
        for baud in (115200, 9600):
            self._check_cancel()
            self.port.set_baud(baud)
            self._buf.clear()
            data = self.port.read(4000, 0.5)
            if b"$GP" in data or b"$GN" in data:
                log.info("NMEA traffic detected at %d baud", baud)
                return baud
            self.port.write(query)
            data = self.port.read(4000, 2.0)
            if b"\xA0\xA1\x00\x02\x83\x02" in data:
                log.info("Binary ACK detected at %d baud", baud)
                return baud
        self.port.set_baud(115200)
        return 0

    # -- queries --------------------------------------------------------------------
    def firmware_version(self) -> str | None:
        def once():
            if not self.command(bytes([CMD_QUERY_VERSION, 0x01])):
                return None
            p = self._read_packet()
            if not p or len(p) < 14:
                return None
            return f"{p[3]}.{p[4]}.{p[5]}-{p[7]}.{p[8]}.{p[9]}-{p[11]}.{p[12]}.{p[13]}"
        return self.retry(once)

    def user_data(self) -> UserData | None:
        def once():
            if not self.command(bytes([CMD_GET_USERDATA, 0x01])):
                return None
            p = self._read_packet()
            if not p or len(p) <= 2:
                return None
            parts = bytes(p[2:]).split(bytes([USERDATA_DELIMITER]))
            parts += [b""] * (3 - len(parts))
            dec = lambda b: b.replace(b"\0", b"").decode("utf-8", "replace").strip()
            return UserData(dec(parts[0]), dec(parts[1]), dec(parts[2]))
        return self.retry(once)

    def set_user_data(self, ud: UserData) -> bool:
        body = bytearray()
        for field in (ud.registration_date, ud.serial, ud.registration_number):
            body += field.encode() + bytes([USERDATA_DELIMITER])
        body = body.ljust(81, b"\0")
        return bool(self.retry(lambda: self.command(bytes([CMD_SET_USERDATA, 0x26]) + body)))

    def log_config(self) -> LogConfig | None:
        def once():
            if not self.command(bytes([CMD_GET_LOG_CONFIG])):
                return None
            p = self._read_packet()
            if not p or len(p) <= 34:
                return None
            u32 = lambda o: int.from_bytes(p[o:o + 4], "little")
            u16 = lambda o: int.from_bytes(p[o:o + 2], "little")
            return LogConfig(u32(1), u16(5), u16(7), u32(9), u32(13), u32(17), u32(21), u32(25), u32(29),
                             p[33], p[34])
        return self.retry(once, 10)

    def write_log_config(self, max_time=3, min_time=3, max_dist=3, min_dist=3, max_speed=3, min_speed=3) -> bool:
        payload = bytes([CMD_WRITE_LOG_CONFIG])
        for v in (max_time, min_time, max_dist, min_dist, max_speed, min_speed):
            payload += int(v).to_bytes(4, "big")
        payload += bytes([1, 0])  # logging enabled, no FIFO overwrite
        return bool(self.retry(lambda: self.command(payload)))

    def agps_hours_left(self) -> int | None:
        def once():
            now = datetime.now(timezone.utc)
            payload = bytes([CMD_AGPS_STATUS, now.year >> 8, now.year & 0xFF, now.month, now.day,
                             now.hour, now.minute, now.second])
            if not self.command(payload):
                return None
            p = self._read_packet()
            return (int.from_bytes(p[1:3], "little"),) if p and len(p) >= 4 else None
        r = self.retry(once, 3)
        return r[0] if r else None

    # -- security -------------------------------------------------------------------
    @staticmethod
    def _pw(password: str) -> bytes:
        return password.encode("latin-1", "replace")[:16].ljust(16, b"\0")

    def login(self, password: str) -> bool:
        def once():
            if not self.command(bytes([CMD_LOGIN]) + self._pw(password)):
                return False
            p = self._read_packet(1.0)
            return not (p and len(p) >= 2 and p[0] == RSP_LOGIN_STATUS and p[1] == 0)
        return bool(self.retry(once))

    def logout(self) -> bool:
        return bool(self.retry(lambda: self.command(bytes([CMD_LOGOUT]))))

    def change_password(self, old: str, new: str) -> bool:
        return bool(self.retry(lambda: self.command(bytes([CMD_CHANGE_PASSWORD]) + self._pw(old) + self._pw(new))))

    # -- log data -------------------------------------------------------------------
    def set_nmea_output(self, enabled: bool) -> bool:
        return bool(self.retry(lambda: self.command(bytes([CMD_MESSAGE_TYPE, 1 if enabled else 0, 0]))))

    def _read_sector(self, sector: int) -> bytes | None:
        # Same request as the vendor software: start sector + count of 2.
        if not self.command(bytes([CMD_READ_SECTORS, sector >> 8, sector & 0xFF, 0, 2])):
            return None
        data = bytearray()
        marker = b"END\x00CHECKSUM="
        while True:
            chunk = self._take(1, 1.5)
            if chunk is None:
                # Fallback to the vendor's looser framing: "END" + 10 trailer bytes + checksum.
                idx = data.rfind(b"END")
                if idx >= 0 and len(data) >= idx + 14:
                    tail = bytes(data[idx + 13:])
                    data = data[:idx]
                    break
                log.warning("sector %d: timeout after %d bytes", sector, len(data))
                return None
            start = max(0, len(data) - len(marker))
            data += chunk + self._buf
            self._buf.clear()
            idx = data.find(marker, start)
            if idx >= 0:
                tail = bytes(data[idx + len(marker):])
                data = data[:idx]
                break
        cs = tail[:1] or self._take(1, 1.5)
        time.sleep(0.05)
        self._buf.clear()
        self.port.read(64, 0.05)
        if cs is None or checksum(data) != cs[0]:
            log.warning("sector %d: checksum mismatch", sector)
            return None
        return bytes(data)

    def download(self, config: LogConfig, progress: Callable[[int, int], None] = lambda a, b: None) -> bytes:
        count = min(config.used_sectors, config.total_sectors or MAX_SECTORS, 4096)
        self.set_nmea_output(False)
        self._buf.clear()
        self.port.flush_input()
        out = bytearray()
        try:
            for s in range(count):
                data = None
                for _ in range(6):
                    data = self._read_sector(s)
                    if data is not None:
                        break
                    time.sleep(0.2)
                if data is None:
                    raise DeviceError(f"Failed to read sector {s + 1} of {count}")
                # Each request returns two sectors; keep the first so sectors are not duplicated.
                out += data if s == count - 1 else data[:SECTOR_SIZE]
                progress(s + 1, count)
        finally:
            try:
                self.set_nmea_output(True)
            except Cancelled:
                pass
        return bytes(out)

    def clear_log(self) -> bool:
        ud = self.user_data()
        if not self.retry(lambda: self.command(bytes([CMD_ERASE_LOG]), timeout=10.0)):
            return False
        self.write_log_config()
        if ud and ud.serial:
            self.set_user_data(ud)
        return True


@dataclass
class DeviceInfo:
    port: str
    baud: int = 0
    firmware: str = ""
    user: UserData | None = None
    config: LogConfig | None = None
    agps_hours: int | None = None


def open_logger(path: str, cancelled: Callable[[], bool] = lambda: False) -> tuple[SerialPort, Logger, int]:
    port = SerialPort(path, 115200)
    lg = Logger(port, cancelled)
    try:
        baud = lg.detect_speed()
    except BaseException:
        port.close()
        raise
    if not baud:
        port.close()
        raise DeviceError(f"No GPS logger answered on {path}. Unplug and reconnect the device and try again.")
    return port, lg, baud
