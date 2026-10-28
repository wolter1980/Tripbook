# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Tripbook is an open-source (GPL-3.0-or-later) PySide6 desktop app that downloads GPS logs from the SkyTraq-based Little LogBook USB trip tracker, splits them into trips and produces business/private vehicle logbooks. Developed on Linux; the app is cross-platform, but tracker download needs POSIX serial (Linux/macOS – `device.SERIAL_SUPPORTED` is false on Windows). Python 3.11+, PySide6 with QtWebEngine; no other third-party dependencies (no pyserial). Packaged with `pyproject.toml` (setuptools; version from `tripbook.__version__`). It is an independent project, not affiliated with Little LogBook: never copy code or assets from their software, and keep the "not affiliated" wording in user-facing docs.

## Commands

```sh
bin/tripbook                                              # run from the checkout (sets PYTHONPATH, runs python3 -m tripbook)
python3 -m unittest discover -s tests                     # all tests (~7 s, no hardware or Qt needed), from the repo root
python3 -m unittest discover -s tests -k test_download    # tests matching a name
pip install -e .                                          # installs PySide6 and the `tripbook` command (Windows/macOS)
./install.sh [--udev]                                     # launcher symlink, menu/desktop entry; --udev installs the tracker rule via sudo
```

Environment: `TRIPBOOK_DATA` overrides the data dir (default `~/.local/share/tripbook/` on Linux, `~/Library/Application Support/Tripbook` on macOS, `%APPDATA%\Tripbook` on Windows; holding `tripbook.db`, `downloads/*.bin`, `backups/`, `tripbook.log`). `TRIPBOOK_DEBUG=1` logs every byte exchanged with the tracker. Use `TRIPBOOK_DATA` pointing at a scratch dir when running the app experimentally, so the user's real logbook is not touched.

## Architecture

Human-oriented versions of this live in `docs/architecture.md` and `docs/tracker-protocol.md`; keep them in sync with code changes, along with `docs/user-guide.md` and `CHANGELOG.md`.

Layering, bottom to top (only the last two import Qt):

- `decode.py` — pure functions. Decodes SkyTraq logger flash (18-byte long entries with absolute ECEF, 8-byte short entries with deltas, `0xFF` = erased) into `Point`s, plus the inverse `encode_long/encode_short` (used by the test fake). `clean_points` / `split_trips` turn points into `TripCandidate`s by stop interval. `pack_points`/`unpack_points` are the storage format for a trip's track (zlib-compressed JSON) in the `trip.points` BLOB.
- `device.py` — SkyTraq binary protocol over a hand-rolled termios `SerialPort` (termios import is optional so the app starts on Windows). Frames are `A0 A1 <len> <payload> <xor> 0D 0A`; every command gets ACK/NACK then optionally a reply. `Logger` wraps commands (login/password, log config, sector read, erase, user data = vehicle registration); `open_logger` does baud detection. Long operations take a `cancelled` callable and raise `Cancelled`; protocol failures raise `DeviceError`.
- `db.py` — `Database` wraps one sqlite3 connection; the whole schema is `SCHEMA` (`CREATE TABLE IF NOT EXISTS`, applied on every open; columns added to an existing table must also be listed in `MIGRATIONS`, which adds them to older databases). Times are stored as UTC ISO strings (`to_iso`/`from_iso`, `...Z`); distances are always km internally — convert at the UI edge with `db.to_unit()` / `db.from_unit()` (display *and* input; `dialogs.dist_spin` for entry fields). Odometer anchors may be a plain date or a UTC timestamp. Odometer values are **not stored per trip**: `_fill_odometers` recomputes them from the vehicle's opening odometer plus odometer readings plus cumulative GPS distance. Imports are deduplicated by time overlap (`overlaps`), so re-importing the same dump is safe. Places label trip endpoints by radius (`resolve_places`); rules auto-mark trip types between places (`apply_rules`). `split_trip`/`merge_trips` are inverses (a split shares its split point between both halves).
- `exchange.py` — everything file/network facing: raw dump / GPX import (`import_points` is the common path: split → add → resolve places), GPX/CSV export, HTML logbook report (`report_html`, rendered to PDF/print via `QPrinter` in the main window), Nominatim reverse geocoding with `geocode_cache`, and import of Little LogBook `.db` files (which may be RC4-encrypted; see `decrypt_llb_database`).
- `dialogs.py` — form dialogs, `Job` (a `QThread` running `fn(job)` with progress/cancel; converts `Cancelled`/`DeviceError` to user messages), and `DeviceDialog` (tracker info, download, erase, assign vehicle). Every download is saved as a raw `.bin` in `downloads/` before import.
- `mainwindow.py` — `MainWindow` with tabs (trips table via `TripModel`, places, rules, expenses, reminders, reports), vehicle/period filters, toolbar and menus. `mapview.py` is a Leaflet page inside `QWebEngineView`; Python calls JS functions (`showTrips`, `showPlaces`, `focusOn`) and map clicks come back as `console.log('tripbook-click:lat,lon')` messages.

## Tests

`tests/test_core.py` covers decode, device download, database and exchange logic. `tests/fake_device.py` synthesises drives (`sample_log`) and runs a `FakeLogger` on a pseudo terminal that speaks the tracker protocol (including password protection), so device code changes should be checked against it. When changing the protocol in `device.py`, update `FakeLogger` to match.

## Relation to Little LogBook

Tripbook mirrors the Windows Little LogBook Global app (see `CHANGELOG.md` for the version it was last compared with). Its release notes are in `https://produpdate.littlelogbook.co.za/Updates.xml`; the installer is `https://update.littlelogbook.co.za/LittleLogBookGlobal.zip` (an MSI whose cabinet also contains an empty template of its SQLite schema, useful for the importer); the user guide is `https://www.littlelogbook.net/documents/Little-LogBook-Global-User-Guide.pdf`.

Backups never let SQLite write to the destination (its locks hang on SMB/CIFS shares): `Database.snapshot()` + `install_file`, and backups are read with `read_only_uri` (immutable, no locks).
