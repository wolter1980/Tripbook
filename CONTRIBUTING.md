# Contributing to Tripbook

Thanks for helping! Tripbook is a small community project: bug reports, testing on other systems,
documentation and code are all welcome. Please follow the [code of conduct](CODE_OF_CONDUCT.md).

## Reporting bugs and ideas

Open an [issue](https://github.com/wolter1980/Tripbook/issues/new/choose) with what you did, what you
expected and what happened, plus your operating system, Python and PySide6 versions, the Tripbook
version (Help → About) and the tracker model if it is involved. The log file helps a lot
(`tripbook.log` in the data folder; start Tripbook with `TRIPBOOK_DEBUG=1` to log the tracker traffic).

> **Protect your privacy.** A logbook (`tripbook.db`), a tracker dump (`.bin`), a Little LogBook database
> or a GPX file contains where you live, work and drive. Don't attach them to public issues. If a bug
> only shows with your data, say so in the issue and we'll find a private way to share a trimmed copy.

Security problems: see [SECURITY.md](SECURITY.md) – please don't report them in public issues.

## Good places to start

- **Windows tracker download** – a `SerialPort` implementation for COM ports (pyserial, or the Windows
  API), plus port discovery. See `tripbook/device.py`; the rest of the protocol code is portable.
- **Testing on macOS** with a real tracker, and fixing what turns up.
- **Packaging** – stand-alone apps or installers for Windows, macOS and Linux (PyInstaller, Briefcase,
  Flatpak, …) and a GitHub Actions workflow that builds them.
- **Translations** – the interface is English only today.
- **Reports** – other layouts, e.g. the format a particular tax authority asks for.
- Anything labelled [`good first issue`](https://github.com/wolter1980/Tripbook/labels/good%20first%20issue).

For bigger changes, please open an issue first to agree on the approach.

## Development setup

```sh
git clone https://github.com/wolter1980/Tripbook.git
cd Tripbook
python3 -m venv .venv && source .venv/bin/activate     # Windows: python -m venv .venv && .venv\Scripts\activate
pip install -e .                                       # installs PySide6 and the tripbook command
```

Run Tripbook from the checkout with a scratch data folder, so your real logbook is never touched:

```sh
TRIPBOOK_DATA=/tmp/tripbook-dev python3 -m tripbook    # Windows (PowerShell): $env:TRIPBOOK_DATA="$env:TEMP\tripbook-dev"; python -m tripbook
```

On Linux `bin/tripbook` runs the checkout without installing anything.

## Tests

```sh
python3 -m unittest discover -s tests                      # all tests, a few seconds
python3 -m unittest discover -s tests -k test_download     # tests matching a name
```

Run them from the repository root (on Windows the command is `python`). No tracker is needed: `tests/fake_device.py` simulates
one on a pseudo terminal, including password protection, so the whole download path is tested. If you
change the protocol in `device.py`, update the fake tracker too. Add a test for every bug fix and
feature in the database, decoding or import/export code. GitHub Actions runs the tests on Linux, macOS
and Windows for every pull request.

Before changing code, read [docs/architecture.md](docs/architecture.md); the important conventions are:

- distances are stored in km and converted only at the user interface (`db.to_unit`/`db.from_unit`);
- times are stored in UTC;
- a new column needs an entry in `MIGRATIONS` so existing logbooks are upgraded;
- network and file work that can be slow runs off the GUI thread (`Job`, `run_file_task`).

## Code style

- Match the surrounding code: naming, comment density, idioms. Lines up to 120 characters.
- Only the standard library besides PySide6, unless there is a strong reason (discuss it in an issue).
- Keep the user interface wording plain and consistent with what is already there.
- Update the [user guide](docs/user-guide.md) and [CHANGELOG.md](CHANGELOG.md) when behaviour changes.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make focused commits with clear messages.
3. Run the tests.
4. Open a pull request describing what changed and how you tested it (with a real tracker or not).

By contributing you agree that your contribution is licensed under the
[GNU General Public License v3.0 or later](LICENSE), like the rest of Tripbook.

## Little LogBook

Tripbook is not affiliated with Little LogBook. Contributions must not include code, files or assets from
the Little LogBook software; describe behaviour and formats in your own words and code (see
[docs/tracker-protocol.md](docs/tracker-protocol.md)).
