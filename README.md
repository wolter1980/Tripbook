# Tripbook

[![Tests](https://github.com/wolter1980/Tripbook/actions/workflows/tests.yml/badge.svg)](https://github.com/wolter1980/Tripbook/actions/workflows/tests.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

**Tripbook is a free, open-source vehicle trip logbook for the Little LogBook GPS trip tracker.**
It downloads trips from the USB tracker, splits them into trips, lets you mark them business or
private, and produces logbook reports for tax and mileage claims (PDF, CSV, KML).

It was built on Linux, where the Windows-only Little LogBook software does not run, and it is written
in Python with Qt, so it also runs on Windows and macOS (see [Platform support](#platform-support)).
It can import your existing Little LogBook database, so you can move over without losing history.

> **Not affiliated with Little LogBook.** Tripbook is an independent community project. It is not made,
> endorsed or supported by Mbeleni Data Solutions or the Little LogBook team. "Little LogBook" is used
> only to say which tracker and software Tripbook works with. For the official software and support, see
> [littlelogbook.net](https://www.littlelogbook.net/).

## Features

- **Tracker download** over USB serial (SkyTraq-based trackers: `0483:5740` new generation,
  `067b:2303` old generation): password login, memory status (with a warning from 80 % full), sector
  download with progress, optional erase afterwards, writing the vehicle registration to the tracker,
  and pairing a replacement tracker with an existing vehicle. Every download is also kept as a raw
  `.bin` dump, so it can be imported again.
- **Trips**: automatic splitting after a configurable stop (default 5 min), GPS jitter filtering,
  map view (map, satellite, hybrid and terrain views, or your own tile server), inline editing of
  From/To/Type/Reason, splitting a trip at any point of its track, combining consecutive trips,
  manual trips, address lookup (OpenStreetMap Nominatim).
- **Places**: name a location once and every trip that starts or ends there is labelled.
  Unnamed spots get name suggestions from OpenStreetMap (nearby businesses, buildings and addresses).
- **Auto-mark rules**: e.g. Home ⇄ Office is always Business, with separate reasons for the outbound
  and the return trip.
- **Odometer**: opening odometer plus GPS distance, corrected by odometer readings (date and time) you enter.
- **Expenses** (fuel with litres, maintenance, …) and **reminders** (service interval, licence renewal or
  custom; by date and/or odometer, repeating, with their own advance notice).
- **Mileage rates**: a rate per km/mile valid from a date; reports show the business mileage claim.
- **Reports**: logbook with summary and business-use percentage, by vehicle/period/type, with every
  trip and/or monthly totals with odometers → PDF, print, CSV, KML. Report options are remembered.
- **Units**: everything is shown and entered in kilometres or miles, as chosen in the settings.
- **Import**: raw tracker dumps, GPX files, and an existing Little LogBook database (including encrypted
  ones and their stored GPS tracks). **Export**: GPX, KML (Google Earth), CSV, database backup.
- **Backups**: automatic backup when Tripbook closes into a folder you choose, e.g. a USB drive or a
  network share (the newest 5 are kept, configurable), and restore.

See the [user guide](docs/user-guide.md) for how to use it.

## Platform support

Tripbook is developed and used on Linux (Fedora). It is plain Python and Qt, so the application runs
anywhere PySide6 does; only talking to the tracker is operating-system specific.

| | Linux | macOS | Windows |
|---|---|---|---|
| Logbook, places, rules, reports, maps, imports, backups | ✅ tested | ✅ expected to work | ✅ expected to work |
| Download from the tracker over USB | ✅ tested | ⚠️ should work (POSIX serial), untested | ❌ not yet – import `.bin`/GPX files instead |
| Automated tests (CI) | ✅ | ✅ | ✅ (tracker tests skipped) |

Help is very welcome to test on macOS and to add tracker download on Windows (a `SerialPort`
implementation for COM ports, e.g. with pyserial) – see [CONTRIBUTING.md](CONTRIBUTING.md).

## Install

Tripbook needs **Python 3.11+** and **PySide6 6.5+** (Qt for Python, including Qt WebEngine for the maps).

### Linux

On Fedora the Qt bindings come from the distribution; elsewhere `pip` installs them.

```sh
git clone https://github.com/wolter1980/Tripbook.git
cd Tripbook
sudo dnf install python3-pyside6        # Fedora; or: python3 -m pip install --user PySide6
./install.sh                            # launcher in ~/.local/bin + application menu entry
./install.sh --udev                     # optional: tracker access without the dialout group (asks for sudo)
tripbook
```

To open the tracker's serial port your user needs permission: install the udev rule above (it also stops
ModemManager from probing the tracker), or add yourself to the `dialout` group
(`sudo usermod -aG dialout $USER`). The `tripbook` launcher picks up a newly added group even before you
log in again.

### Windows and macOS

Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/), then in a terminal
(Command Prompt or PowerShell on Windows):

```sh
git clone https://github.com/wolter1980/Tripbook.git
cd Tripbook
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS
pip install .
tripbook
```

No git? Download the source as a ZIP from the green **Code** button on GitHub, unpack it and run the
same commands from that folder.

### Building a standalone app

There are no ready-made installers yet. `pip install .` (above) is the supported way to install.
Packaging Tripbook as a stand-alone application per platform (for example with PyInstaller or
Briefcase) is a welcome contribution.

## Where your data lives

| | |
|---|---|
| Linux | `~/.local/share/tripbook/` (or `$XDG_DATA_HOME/tripbook`) |
| macOS | `~/Library/Application Support/Tripbook/` |
| Windows | `%APPDATA%\Tripbook\` |

It holds `tripbook.db` (the logbook), `downloads/` (raw tracker dumps), `backups/` and `tripbook.log`.
Set `TRIPBOOK_DATA` to use another folder, and `TRIPBOOK_DEBUG=1` to log every byte exchanged with the
tracker.

Tripbook talks to the internet only for map tiles (OpenStreetMap, Esri, OpenTopoMap) and, when you ask
for it, address lookups and place name suggestions (OpenStreetMap Nominatim and Photon). Your logbook
never leaves your computer.

## Documentation

- [User guide](docs/user-guide.md) – using Tripbook day to day
- [Architecture](docs/architecture.md) – how the code is organised
- [Tracker protocol and log format](docs/tracker-protocol.md) – what is known about the hardware
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Security](SECURITY.md)

## Contributing

Bug reports, ideas and pull requests are welcome – from people with a tracker and without one (the
test suite simulates a tracker). Start with [CONTRIBUTING.md](CONTRIBUTING.md); good first areas are
listed there.

## License

Tripbook is free software: you can redistribute it and/or modify it under the terms of the
[GNU General Public License](LICENSE) as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version. It is distributed in the hope that it will be
useful, but WITHOUT ANY WARRANTY; see the license for details.

Map data © OpenStreetMap contributors.
