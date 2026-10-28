# Tripbook

A vehicle trip logbook for Linux, modelled on the Windows "Little LogBook" software.
It downloads trips from the SkyTraq-based USB GPS tracker, splits them into trips,
lets you mark them business or private, and produces logbook reports (PDF/CSV).

## Features

- **Tracker download** over USB serial (`0483:5740` new generation, `067b:2303` old generation):
  password login, memory status, sector download with progress, optional erase afterwards,
  writing the vehicle registration to the tracker. Every download is also kept as a raw `.bin`
  dump in `~/.local/share/tripbook/downloads/`, so it can be imported again.
- **Trips**: automatic splitting after a configurable stop (default 5 min), GPS jitter filtering,
  map view (OpenStreetMap), inline editing of From/To/Type/Reason, combining consecutive trips,
  manual trips, address lookup (OpenStreetMap Nominatim).
- **Places**: name a location once and every trip that starts or ends there is labelled.
- **Auto-mark rules**: e.g. Home ⇄ Office is always Business.
- **Odometer**: opening odometer plus GPS distance, corrected by odometer readings you enter.
- **Expenses** (fuel with litres, maintenance, …) and **reminders** (by date and/or odometer, repeating).
- **Reports**: logbook with summary and business-use percentage, by vehicle/period/type → PDF, print, CSV.
- **Import**: raw tracker dumps, GPX files, and an existing Little LogBook database (`.db`), including
  its stored GPS tracks. **Export**: GPX, CSV, database backup.

## Install / run

Needs Python 3.11+ and PySide6 with QtWebEngine (Fedora: `sudo dnf install python3-pyside6`).

```sh
./install.sh            # launcher in ~/.local/bin + application menu entry
./install.sh --udev     # additionally: tracker access without the dialout group (asks for sudo)
tripbook
```

Without the udev rule, either add yourself to `dialout` (`sudo usermod -aG dialout $USER`, then log in
again) or the tracker port cannot be opened. The rule also stops ModemManager from probing the tracker.

Data lives in `~/.local/share/tripbook/` (`tripbook.db`, `downloads/`, `tripbook.log`).
Set `TRIPBOOK_DEBUG=1` to log every byte exchanged with the tracker.

## Tests

```sh
PYTHONPATH=.:tests python3 -m unittest discover -s tests
```

`tests/fake_device.py` simulates a tracker on a pseudo terminal (including password protection),
so the full download path is tested without hardware.
