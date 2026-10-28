# Tracker protocol and log format

What Tripbook knows about the Little LogBook tracker, as implemented in `tripbook/device.py` and
`tripbook/decode.py`. The trackers are built on a SkyTraq Venus GPS chip with its data-logger firmware;
the information comes from SkyTraq's published binary protocol and from observing the hardware. Additions
and corrections are welcome – please describe how you verified them.

## Hardware

| Generation | USB id | Serial |
|---|---|---|
| New (since 2016) | `0483:5740` (STM32 virtual COM port) | `/dev/ttyACM*` on Linux, `/dev/cu.usbmodem*` on macOS |
| Old | `067b:2303` (Prolific PL2303) | `/dev/ttyUSB*` on Linux, `/dev/cu.usbserial*` on macOS |

8 data bits, no parity, 1 stop bit. Current devices talk at 115200 baud, some at 9600: Tripbook sends a
version query and listens for a binary ACK or NMEA sentences at each speed. Between commands the tracker
streams NMEA sentences; Tripbook switches that off during a download and back on afterwards.

## Serial protocol

### Framing

```
A0 A1 | length (2 bytes, big-endian) | payload | checksum (1 byte) | 0D 0A
```

The checksum is the XOR of all payload bytes. The first payload byte is the message id. Every command is
answered with **ACK** `83 <id>` or **NACK** `84 <id>`, optionally followed by a reply message.

### Commands used by Tripbook

| Id | Command | Payload after the id | Reply |
|---|---|---|---|
| `02` | Query software version | `01` | 3 version triplets (kernel, ODM, revision) |
| `09` | Message type | `01 00` NMEA output on, `00 00` off | – |
| `17` | Get log configuration | – | memory status, see below |
| `18` | Write log configuration | max/min time, max/min distance, max/min speed (4 bytes each, big-endian), logging on `01`, FIFO mode `00` | – |
| `19` | Erase log | – | – (takes several seconds) |
| `1D` | Read sectors | start sector (2 bytes, big-endian), `00`, count `02` | raw sector data, then `END\0CHECKSUM=` and an XOR checksum byte |
| `34` | AGPS status | current date and time | hours of satellite aiding data left |
| `46` | Login | password, 16 bytes, zero padded | `D1 00` = refused |
| `47` | Logout | – | – |
| `48` | Change password | old password, new password (16 bytes each) | – |
| `74` | Get user data | `01` | fields separated by `FA`: registration date, serial, registration number |
| `75` | Set user data | `26`, then the three fields, each followed by `FA` | – |

A password-protected tracker NACKs most commands until it receives the right login. After erasing the
log, Tripbook writes the log configuration (thresholds of 3, logging on) and the user data back, so the
tracker keeps logging and stays registered to the vehicle.

### Memory

The flash is read in 4096-byte sectors (508 at most). The log configuration reports the total number of
sectors and the sectors left; used = total − left + 1. Each read request returns two sectors; Tripbook
keeps the first of each pair, except for the last request.

## Log format

The flash contains a stream of entries; erased flash is `FF` and is skipped. Positions are Earth-centred
(ECEF) coordinates in metres; times are GPS week number and time of week.

**Long entry** – 18 bytes, an absolute fix. The top 3 bits of the first byte are the tag: `0x40` for a
normal fix, `0x60` for a point of interest (the button on the Little LogBook GO).

| Bytes | Content |
|---|---|
| 0–1 | tag (top bits), speed in km/h (10 bits) |
| 2–3 | GPS week number (12 bits, +1024 rollover) and the low 4 bits of the time of week |
| 4–5 | time of week, remaining bits (seconds) |
| 6–9, 10–13, 14–17 | ECEF x, y, z as signed 32-bit values with the 16-bit halves swapped (`b1 b0 b3 b2`) |

**Short entry** – 8 bytes, tag `0x80`, relative to the previous entry:

| Bytes | Content |
|---|---|
| 0–1 | tag, speed in km/h (10 bits) |
| 2–3 | seconds since the previous entry |
| 4–7 | 10-bit deltas of x, y and z (values ≥ 512 are negative: 511 − value) |

`decode_log` drops entries with impossible times or positions (corrupt anchors invalidate the short
entries after them), de-duplicates by time and returns the points sorted. `encode_long`/`encode_short`
are the exact inverses and are used by the test suite to build realistic logs.

## Little LogBook database

The Windows software keeps its data in an SQLite database (`llb.s3db`) encrypted with System.Data.SQLite's
legacy RC4 scheme: every page is XORed with an RC4 key stream whose key is the first 16 bytes of the SHA-1
of a fixed password. `exchange.decrypt_llb_database` undoes this so users can import **their own** data;
nothing is ever written back. Trips keep their raw tracker data in `Trip.TripData`, in the log format above.
