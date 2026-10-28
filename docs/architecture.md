# Architecture

Tripbook is a single Python package (`tripbook/`) with a PySide6 (Qt) user interface and an SQLite
database. It has one third-party dependency, PySide6; serial communication, decoding, the database and
all imports/exports use the Python standard library.

## Layers

From the bottom up; only the last two import Qt, so everything below them can be tested without a display.

| Module | Responsibility |
|---|---|
| `decode.py` | Pure functions. Decodes the tracker's flash memory into GPS `Point`s ([format](tracker-protocol.md#log-format)) and back (`encode_long`/`encode_short`, used by the tests). Geometry (ECEF ⇄ WGS84, haversine), jitter filtering (`clean_points`) and splitting into trips at stops (`split_trips`). `pack_points`/`unpack_points` are the storage format of a trip's track (zlib-compressed JSON). |
| `device.py` | The tracker's binary protocol ([details](tracker-protocol.md#serial-protocol)) over a small POSIX `SerialPort` (termios; no pyserial). `Logger` wraps the commands (login, memory status, sector download, erase, user data such as the registration); `open_logger` detects the baud rate. Long operations take a `cancelled` callable and raise `Cancelled`; protocol failures raise `DeviceError`. On systems without termios (Windows) `SERIAL_SUPPORTED` is false and opening a port raises a clear `DeviceError`. |
| `db.py` | `Database` wraps one sqlite3 connection. The whole schema is `SCHEMA` (`CREATE TABLE IF NOT EXISTS`, applied on every open); columns added later are listed in `MIGRATIONS`, one-off data upgrades are guarded by a setting. Trips, places and auto-marking (`resolve_places`, `apply_rules`), odometers, splitting/combining trips, mileage rates, reminders, backups. |
| `exchange.py` | Everything file- or network-facing: raw dump and GPX import (`import_points`: split → add → name places), GPX/KML/CSV export, the HTML report (`report_html`), Little LogBook database import (including its encryption), and OpenStreetMap lookups (`reverse_geocode` via Nominatim, `place_suggestions` via Photon), cached in `geocode_cache`. |
| `dialogs.py` | Edit dialogs, settings, the split and place-suggestion dialogs, `Job` (a `QThread` running `fn(job)` with progress and cancel) and the tracker `DeviceDialog`. |
| `mainwindow.py`, `mapview.py` | The main window with its tabs, filters, menus and reports. `MapView` is a Leaflet page in a `QWebEngineView`: Python calls JavaScript functions (`showTrips`, `showPlaces`, `showCursor`, `focusOn`); the page reports map clicks and layer changes back through `console.log` messages. |
| `app.py` | Entry point: data folder, logging, `QApplication`. |

## Data model and conventions

- **Times** are stored as UTC ISO strings (`2026-10-28T05:16:38Z`, see `to_iso`/`from_iso`) and shown in
  local time.
- **Distances** are always kilometres in the database. Convert only at the user interface, both ways:
  `db.to_unit()` for display and `db.from_unit()` for input (`dialogs.dist_spin` is the entry field).
- **Odometers are not stored per trip.** `_fill_odometers` computes them: the vehicle's opening odometer
  applies from the first trip, odometer readings (a date or a UTC timestamp) take over from their moment
  on, and each trip adds its GPS distance.
- **Imports are deduplicated by time**: a trip overlapping an existing trip of the same vehicle is skipped,
  so importing the same dump twice is safe.
- **Places** label a trip end within their radius (`PLACE_RADIUS_M`, 400 m). Rules mark trips between two
  places; they only touch trips nobody marked by hand (`auto_marked`).
- **`split_trip` and `merge_trips` are inverses**: a split shares its split point between both halves.
- **Backups never let SQLite write to the destination** – its file locks hang on SMB/CIFS network
  shares. `Database.snapshot()` makes a local copy, `install_file` copies it as a plain file; backups are
  read back without locks (`read_only_uri`).

## Data folder

`app.data_dir()`: `$TRIPBOOK_DATA`, else `~/.local/share/tripbook` (Linux), `~/Library/Application
Support/Tripbook` (macOS) or `%APPDATA%\Tripbook` (Windows). It holds `tripbook.db`, `downloads/*.bin`,
`backups/` and `tripbook.log`. **When experimenting, set `TRIPBOOK_DATA` to a scratch folder** so your real
logbook is never touched.

## Tests

`tests/test_core.py` covers decoding, the tracker protocol, the database and import/export logic.
`tests/fake_device.py` synthesises drives (`sample_log`) and runs `FakeLogger`, a simulated tracker on a
pseudo terminal that speaks the protocol (including password protection), so the whole download path runs
without hardware. When you change the protocol in `device.py`, update `FakeLogger` to match. The tracker
tests are skipped where pseudo terminals don't exist (Windows).
