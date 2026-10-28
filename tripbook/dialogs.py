"""Edit dialogs, settings, the trip split dialog and the device download dialog."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QDate, QDateTime, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDateTimeEdit, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider,
                               QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import device as D
from .db import EXPENSE_CATEGORIES, Database, UnnamedSpot
from .decode import Point, path_length_km
from .exchange import place_suggestions
from .mapview import DEFAULT_TILES, MAP_VIEWS, MapView


def qdate(s: str | None) -> QDate:
    return QDate.fromString(s, "yyyy-MM-dd") if s else QDate.currentDate()


def type_combo(db: Database, current: int | None = None, allow_none: bool = True) -> QComboBox:
    cb = QComboBox()
    if allow_none:
        cb.addItem("— unmarked —", None)
    for t in db.trip_types():
        cb.addItem(t["name"], t["id"])
    cb.setCurrentIndex(max(0, cb.findData(current)))
    return cb


def vehicle_combo(db: Database, current: int | None = None, include_all: bool = False) -> QComboBox:
    cb = QComboBox()
    if include_all:
        cb.addItem("All vehicles", None)
    for v in db.vehicles():
        cb.addItem(v["name"] + (" (closed)" if v["closed"] else ""), v["id"])
    if current is not None:
        cb.setCurrentIndex(max(0, cb.findData(current)))
    return cb


def place_combo(db: Database, current: int | None = None) -> QComboBox:
    cb = QComboBox()
    for p in db.places():
        cb.addItem(p["name"], p["id"])
    if current is not None:
        cb.setCurrentIndex(max(0, cb.findData(current)))
    return cb


class FormDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.form = QFormLayout()
        lay = QVBoxLayout(self)
        lay.addLayout(self.form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.setMinimumWidth(420)


def spin(lo=0.0, hi=9_999_999.0, decimals=1, value=0.0, suffix="") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setValue(value or 0)
    s.setSuffix(suffix)
    s.setGroupSeparatorShown(True)
    return s


def dist_spin(db: Database, km: float | None = 0.0, decimals: int = 1) -> QDoubleSpinBox:
    """A distance entry in the user's unit; read it back with db.from_unit(widget.value())."""
    return spin(decimals=decimals, value=db.to_unit(km or 0), suffix=f" {db.unit}")


def reason_combo(db: Database, text: str = "") -> QComboBox:
    """Editable reason field offering previously used reasons."""
    c = QComboBox()
    c.setEditable(True)
    c.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    c.addItems(db.reasons())
    c.setEditText(text)
    return c


class VehicleDialog(FormDialog):
    def __init__(self, db: Database, row=None, parent=None):
        super().__init__("Vehicle", parent)
        g = (lambda k, d="": row[k] if row and row[k] is not None else d)
        self.name = QLineEdit(g("name"))
        self.reg = QLineEdit(g("registration"))
        self.make = QLineEdit(g("make"))
        self.year = QLineEdit(g("year"))
        self.db = db
        self.odo = dist_spin(db, g("odometer_start", 0))
        self.serial = QLineEdit(g("device_serial"))
        self.password = QLineEdit(g("device_password"))
        self.password.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.password.setMaxLength(16)
        self.closed = QCheckBox("Vehicle sold / no longer in use")
        self.closed.setChecked(bool(g("closed", 0)))
        odo_note = QLabel("The odometer at the first trip in the logbook. Correct it later with odometer readings "
                          "(Vehicles tab), not by changing this.")
        odo_note.setWordWrap(True)
        odo_note.setStyleSheet("color:#777")
        for label, w in [("Name", self.name), ("Registration", self.reg), ("Make / model", self.make),
                         ("Year", self.year), ("Opening odometer", self.odo), ("", odo_note),
                         ("Tracker serial", self.serial), ("Tracker password", self.password), ("", self.closed)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        return dict(name=self.name.text().strip() or self.reg.text().strip() or "Vehicle",
                    registration=self.reg.text().strip(), make=self.make.text().strip(),
                    year=self.year.text().strip(), odometer_start=self.db.from_unit(self.odo.value()),
                    device_serial=self.serial.text().strip(), device_password=self.password.text(),
                    closed=int(self.closed.isChecked()))


class ManualTripDialog(FormDialog):
    def __init__(self, db: Database, vehicle_id: int | None, parent=None):
        super().__init__("Add trip", parent)
        now = QDateTime.currentDateTime()
        self.vehicle = vehicle_combo(db, vehicle_id)
        self.start = QDateTimeEdit(now.addSecs(-1800))
        self.end = QDateTimeEdit(now)
        for w in (self.start, self.end):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.frm = QLineEdit()
        self.to = QLineEdit()
        self.db = db
        self.dist = dist_spin(db)
        self.type = type_combo(db)
        self.reason = reason_combo(db)
        for label, w in [("Vehicle", self.vehicle), ("Start", self.start), ("End", self.end), ("From", self.frm),
                         ("To", self.to), ("Distance", self.dist), ("Type", self.type), ("Reason", self.reason)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        return dict(vehicle_id=self.vehicle.currentData(),
                    start=self.start.dateTime().toPython().astimezone(),
                    end=self.end.dateTime().toPython().astimezone(),
                    start_desc=self.frm.text().strip(), end_desc=self.to.text().strip(),
                    distance_km=self.db.from_unit(self.dist.value()), trip_type_id=self.type.currentData(),
                    reason=self.reason.currentText().strip())


class ExpenseDialog(FormDialog):
    def __init__(self, db: Database, row=None, vehicle_id=None, parent=None):
        super().__init__("Expense", parent)
        g = (lambda k, d=None: row[k] if row and row[k] is not None else d)
        self.vehicle = vehicle_combo(db, g("vehicle_id", vehicle_id))
        self.date = QDateEdit(qdate(g("date")))
        self.date.setCalendarPopup(True)
        self.cat = QComboBox()
        self.cat.setEditable(True)
        self.cat.addItems(EXPENSE_CATEGORIES)
        self.cat.setCurrentText(g("category", "Fuel"))
        self.desc = QLineEdit(g("description", ""))
        self.amount = spin(decimals=2, value=g("amount", 0), suffix=" " + db.setting("currency", ""))
        self.volume = spin(decimals=2, value=g("volume", 0), suffix=" l")
        self.db = db
        self.odo = dist_spin(db, g("odometer", 0), decimals=0)
        for label, w in [("Vehicle", self.vehicle), ("Date", self.date), ("Category", self.cat),
                         ("Description", self.desc), ("Amount", self.amount), ("Fuel volume", self.volume),
                         ("Odometer (optional)", self.odo)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        return dict(vehicle_id=self.vehicle.currentData(), date=self.date.date().toString("yyyy-MM-dd"),
                    category=self.cat.currentText().strip() or "Other", description=self.desc.text().strip(),
                    amount=self.amount.value(), volume=self.volume.value(),
                    odometer=self.db.from_unit(self.odo.value()) or None)


# Reminder presets like the original's: (title, repeat months, repeat distance, notice days, notice distance,
# due by date); distances are round numbers in the user's unit, as (km, miles).
REMINDER_KINDS = {
    "Service interval": ("Service", 12, (15000, 10000), 30, (500, 300), True),
    "Licence renewal": ("Licence renewal", 12, (0, 0), 30, (0, 0), True),
    "Custom": ("", 0, (0, 0), 14, (500, 300), True),
}


class ReminderDialog(FormDialog):
    def __init__(self, db: Database, row=None, vehicle_id=None, parent=None):
        super().__init__("Reminder", parent)
        g = (lambda k, d=None: row[k] if row and row[k] is not None else d)
        self.db = db
        self.kind = QComboBox()
        self.kind.addItems(list(REMINDER_KINDS))
        self.vehicle = vehicle_combo(db, g("vehicle_id", vehicle_id))
        self.title = QLineEdit(g("title", ""))
        self.title.setPlaceholderText("e.g. Service, Licence renewal, Tyres")
        self.use_date = QCheckBox("Due on date")
        self.use_date.setChecked(row is None or bool(g("due_date")))
        self.date = QDateEdit(qdate(g("due_date")))
        self.date.setCalendarPopup(True)
        self.odo = dist_spin(db, g("due_odometer", 0), decimals=0)
        self.rep_m = QSpinBox()
        self.rep_m.setRange(0, 120)
        self.rep_m.setValue(g("repeat_months", 0))
        self.rep_m.setSuffix(" months")
        self.rep_km = dist_spin(db, g("repeat_km", 0), decimals=0)
        self.notice_days = QSpinBox()
        self.notice_days.setRange(0, 365)
        self.notice_days.setSuffix(" days")
        self.notice_days.setValue(g("notice_days", 14))
        self.notice_km = dist_spin(db, g("notice_km", 500), decimals=0)
        drow = QHBoxLayout()
        drow.addWidget(self.use_date)
        drow.addWidget(self.date)
        if row is None:
            self.form.addRow("Kind", self.kind)
            self.kind.currentTextChanged.connect(self._preset)
        for label, w in [("Vehicle", self.vehicle), ("Title", self.title)]:
            self.form.addRow(label, w)
        self.form.addRow("Due date", drow)
        for label, w in [("Due at odometer (0 = none)", self.odo), ("Repeat every", self.rep_m),
                         ("…or every", self.rep_km), ("Remind me", self.notice_days),
                         ("…or within", self.notice_km)]:
            self.form.addRow(label, w)
        if row is None:
            self._preset(self.kind.currentText())

    def _preset(self, kind: str) -> None:
        title, months, dist, days, notice, by_date = REMINDER_KINDS[kind]
        dist, notice = (dist[1], notice[1]) if self.db.use_miles else (dist[0], notice[0])
        self.title.setText(title)
        self.rep_m.setValue(months)
        self.rep_km.setValue(dist)
        self.notice_days.setValue(days)
        self.notice_km.setValue(notice)
        self.use_date.setChecked(by_date)
        vid = self.vehicle.currentData()
        self.odo.setValue(round(self.db.to_unit(self.db.current_odometer(vid))) + dist if dist and vid else 0)

    def values(self) -> dict:
        km = self.db.from_unit
        return dict(vehicle_id=self.vehicle.currentData(), title=self.title.text().strip() or "Reminder",
                    due_date=self.date.date().toString("yyyy-MM-dd") if self.use_date.isChecked() else None,
                    due_odometer=km(self.odo.value()) or None, repeat_months=self.rep_m.value(),
                    repeat_km=km(self.rep_km.value()), notice_days=self.notice_days.value(),
                    notice_km=km(self.notice_km.value()))


class RuleDialog(FormDialog):
    def __init__(self, db: Database, row=None, parent=None):
        super().__init__("Auto-mark rule", parent)
        g = (lambda k, d=None: row[k] if row and row[k] is not None else d)
        self.vehicle = vehicle_combo(db, g("vehicle_id"), include_all=True)
        self.start = place_combo(db, g("start_place_id"))
        self.end = place_combo(db, g("end_place_id"))
        self.reverse = QCheckBox("Also apply to the return trip")
        self.reverse.setChecked(bool(g("reverse", 1)))
        self.type = type_combo(db, g("trip_type_id"), allow_none=False)
        self.reason = reason_combo(db, g("reason", ""))
        self.reason.lineEdit().setPlaceholderText("Reason filled into matching trips (optional)")
        self.reverse_reason = reason_combo(db, g("reverse_reason", ""))
        self.reverse_reason.lineEdit().setPlaceholderText("Empty: same reason as the outbound trip")
        self.reverse.toggled.connect(self.reverse_reason.setEnabled)
        self.reverse_reason.setEnabled(self.reverse.isChecked())
        for label, w in [("Vehicle", self.vehicle), ("From place", self.start), ("To place", self.end),
                         ("", self.reverse), ("Mark as", self.type), ("Outbound reason", self.reason),
                         ("Return trip reason", self.reverse_reason)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        rev = self.reverse.isChecked()
        return dict(vehicle_id=self.vehicle.currentData(), start_place_id=self.start.currentData(),
                    end_place_id=self.end.currentData(), reverse=int(rev),
                    trip_type_id=self.type.currentData(), reason=self.reason.currentText().strip(),
                    reverse_reason=self.reverse_reason.currentText().strip() if rev else "")


class OdometerDialog(FormDialog):
    def __init__(self, db: Database, current_km: float, parent=None):
        super().__init__("Odometer reading", parent)
        self.db = db
        self.when = QDateTimeEdit(QDateTime.currentDateTime())
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.reading = dist_spin(db, current_km, decimals=0)
        note = QLabel("Trips that start after this moment continue from this reading.")
        note.setStyleSheet("color:#777")
        for label, w in [("Read at", self.when), ("Odometer", self.reading), ("", note)]:
            self.form.addRow(label, w)

    def values(self) -> tuple[datetime, float]:
        return self.when.dateTime().toPython().astimezone(), self.db.from_unit(self.reading.value())


class MileageRateDialog(FormDialog):
    def __init__(self, db: Database, row=None, vehicle_id=None, parent=None):
        super().__init__("Mileage rate", parent)
        g = (lambda k, d=None: row[k] if row and row[k] is not None else d)
        self.db = db
        self.vehicle = vehicle_combo(db, g("vehicle_id", vehicle_id), include_all=True)
        self.from_date = QDateEdit(qdate(g("from_date")))
        self.from_date.setCalendarPopup(True)
        self.desc = QLineEdit(g("description", ""))
        self.desc.setPlaceholderText("e.g. Tax-free allowance 2026")
        # stored per km; shown per the user's unit
        self.rate = spin(decimals=4, hi=1000, value=(g("rate", 0) or 0) / db.to_unit(1.0),
                         suffix=f" {db.setting('currency', '')}/{db.unit}")
        for label, w in [("Vehicle", self.vehicle), ("Valid from", self.from_date), ("Description", self.desc),
                         ("Rate", self.rate)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        return dict(vehicle_id=self.vehicle.currentData(), from_date=self.from_date.date().toString("yyyy-MM-dd"),
                    description=self.desc.text().strip(), rate=self.rate.value() * self.db.to_unit(1.0))


class SplitDialog(QDialog):
    """Pick the point where a recorded trip should be split, with the position shown on a map."""

    def __init__(self, db: Database, points: list[Point], label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Split trip")
        self.resize(900, 650)
        self.db = db
        self.points = points
        self.map = MapView(tiles=db.setting("tiles", DEFAULT_TILES), view=db.setting("map_view", "Map"))
        step = max(1, len(points) // 2000)
        coords = [[p.lat, p.lon] for p in points[::step]] + [[points[-1].lat, points[-1].lon]]
        self.map.show_trips([{"coords": coords, "color": "#2a7de1", "label": label,
                              "start": coords[0], "end": coords[-1], "startLabel": "Start", "endLabel": "End"}])
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(1, len(points) - 2)
        self.slider.setValue(self._longest_stop())
        self.slider.valueChanged.connect(self._moved)
        self.info = QLabel()
        hint = QLabel("Drag the slider (or use the arrow keys) to the place where the first trip ended. The point "
                      "proposed first is the longest stop in the trip, if it has one.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Split here")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.map, 1)
        lay.addWidget(self.slider)
        lay.addWidget(self.info)
        lay.addWidget(hint)
        lay.addWidget(bb)
        self._moved(self.slider.value())

    def _longest_stop(self) -> int:
        """Index of the longest pause of a minute or more inside the trip, else the middle point."""
        gaps = [((b.time - a.time).total_seconds(), i) for i, (a, b) in enumerate(zip(self.points, self.points[1:]))]
        pause, best = max(gaps[1:-1] or gaps)
        return min(max(1, best), len(self.points) - 2) if pause >= 60 else len(self.points) // 2

    def _moved(self, i: int) -> None:
        p = self.points[i]
        f, unit = self.db.to_unit(1.0), self.db.unit
        first = path_length_km(self.points[:i + 1]) * f
        rest = path_length_km(self.points[i:]) * f
        pause = (self.points[i + 1].time - p.time).total_seconds()
        self.info.setText(f"<b>{p.time.astimezone():%Y-%m-%d %H:%M:%S}</b> · {p.speed * f:.0f} {unit}/h · "
                          f"first part {first:,.1f} {unit}, second part {rest:,.1f} {unit}"
                          + (f" · stopped {pause / 60:.0f} min here" if pause >= 60 else ""))
        self.map.show_cursor(p.lat, p.lon)

    @property
    def index(self) -> int:
        return self.slider.value()


class SettingsDialog(FormDialog):
    def __init__(self, db: Database, data_dir: Path, parent=None):
        super().__init__("Settings", parent)
        self.stop = QSpinBox()
        self.stop.setRange(1, 240)
        self.stop.setSuffix(" min")
        self.stop.setValue(db.stop_interval_min)
        self.units = QComboBox()
        self.units.addItem("Kilometres", "km")
        self.units.addItem("Miles", "mi")
        self.units.setCurrentIndex(self.units.findData(db.setting("units", "km")))
        self.currency = QLineEdit(db.setting("currency", "€"))
        self.year_start = QComboBox()
        for m in range(1, 13):
            self.year_start.addItem(date(2000, m, 1).strftime("%B"), m)
        self.year_start.setCurrentIndex(int(db.setting("year_start_month", "1")) - 1)
        self.view = QComboBox()
        self.view.addItems(list(MAP_VIEWS) + ["Custom"])
        self.view.setCurrentText(db.setting("map_view", "Map"))
        self.tiles = QLineEdit("" if db.setting("tiles", DEFAULT_TILES) == DEFAULT_TILES else db.setting("tiles"))
        self.tiles.setPlaceholderText("Only for the Custom map, e.g. https://tiles.example.org/{z}/{x}/{y}.png")
        self.backup = QCheckBox("Back up the database automatically when Tripbook closes")
        self.backup.setChecked(db.setting("auto_backup", "1") == "1")
        self.backup_keep = QSpinBox()
        self.backup_keep.setRange(1, 100)
        self.backup_keep.setValue(int(db.setting("backup_keep", "5")))
        self.backup_keep.setSuffix(" backups")
        self.default_backup_dir = data_dir / "backups"
        self.backup_dir = QLineEdit(db.setting("backup_dir", ""))
        self.backup_dir.setPlaceholderText(str(self.default_backup_dir))
        self.backup_dir.setToolTip("Where the backup is written when Tripbook closes, e.g. a USB drive, a network "
                                   "share or a synced cloud folder. Empty: the data folder.")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_backup_dir)
        default = QPushButton("Default")
        default.clicked.connect(self.backup_dir.clear)
        brow = QHBoxLayout()
        brow.addWidget(self.backup_dir, 1)
        brow.addWidget(browse)
        brow.addWidget(default)
        self.geocode = QCheckBox("Look up street addresses for new trips (OpenStreetMap Nominatim)")
        self.geocode.setChecked(db.setting("auto_geocode", "0") == "1")
        info = QLabel(f"Data folder: {data_dir}")
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for label, w in [("Split trips after a stop of", self.stop), ("Distance unit", self.units),
                         ("Currency symbol", self.currency), ("Financial year starts in", self.year_start),
                         ("Default map", self.view), ("Custom map tile URL", self.tiles), ("", self.geocode),
                         ("", self.backup), ("Keep the newest", self.backup_keep)]:
            self.form.addRow(label, w)
        self.form.addRow("Backup folder", brow)
        self.form.addRow("", info)
        self.backup.toggled.connect(self.backup_keep.setEnabled)
        self.backup.toggled.connect(self.backup_dir.setEnabled)
        self.backup_keep.setEnabled(self.backup.isChecked())
        self.backup_dir.setEnabled(self.backup.isChecked())
        self.db = db

    def _pick_backup_dir(self) -> None:
        start = self.backup_dir.text().strip() or str(self.default_backup_dir)
        folder = QFileDialog.getExistingDirectory(self, "Backup folder", start)
        if folder:
            self.backup_dir.setText(folder)

    def accept(self) -> None:
        folder = self.backup_dir.text().strip()
        if folder and self.backup.isChecked():
            path = Path(folder).expanduser()
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".tripbook-write-test"
                probe.write_bytes(b"")
                probe.unlink()
            except OSError as e:
                QMessageBox.warning(self, "Backup folder", f"Tripbook cannot write to {path}:\n{e.strerror or e}")
                return
            self.backup_dir.setText(str(path))
        super().accept()

    def save(self) -> None:
        db = self.db
        db.set_setting("stop_interval_min", self.stop.value())
        db.set_setting("units", self.units.currentData())
        db.set_setting("currency", self.currency.text().strip())
        db.set_setting("year_start_month", self.year_start.currentData())
        db.set_setting("tiles", self.tiles.text().strip() or DEFAULT_TILES)
        view = self.view.currentText()
        db.set_setting("map_view", "Map" if view == "Custom" and not self.tiles.text().strip() else view)
        db.set_setting("auto_backup", "1" if self.backup.isChecked() else "0")
        db.set_setting("backup_keep", self.backup_keep.value())
        db.set_setting("backup_dir", self.backup_dir.text().strip())
        db.set_setting("auto_geocode", "1" if self.geocode.isChecked() else "0")


# --------------------------------------------------------------------------- background work

class Job(QThread):
    """Runs fn(job) in a thread. fn may call job.progress(a, b) / job.say(text) and check job.cancelled()."""
    progressed = Signal(int, int)
    message = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[["Job"], object], parent=None):
        super().__init__(parent)
        self.fn = fn
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def cancelled(self) -> bool:
        return self._cancel

    def progress(self, a: int, b: int) -> None:
        self.progressed.emit(a, b)

    def say(self, text: str) -> None:
        self.message.emit(text)

    def run(self) -> None:
        try:
            self.succeeded.emit(self.fn(self))
        except D.Cancelled:
            self.failed.emit("Cancelled.")
        except D.DeviceError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - shown to the user
            logging.getLogger("tripbook").exception("background job failed")
            self.failed.emit(f"{type(e).__name__}: {e}")


class _LogSignal(QObject):
    line = Signal(str)


class _LogForwarder(logging.Handler):
    """Forwards log records (from any thread) to a Qt signal."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.signal = _LogSignal()
        self.line = self.signal.line

    def emit(self, record):
        self.signal.line.emit(self.format(record))


MEMORY_WARN_PCT = 80  # like the original, warn when the tracker memory is this full


class DeviceDialog(QDialog):
    """Detect the tracker, show its details and download its log into a vehicle."""
    downloaded = Signal(int, bytes, str)  # vehicle id, raw log, saved path

    def __init__(self, db: Database, data_dir: Path, vehicle_id: int | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Download from tracker")
        self.setMinimumSize(620, 460)
        self.db = db
        self.data_dir = data_dir
        self.job: Job | None = None

        self.port = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_ports)
        prow = QHBoxLayout()
        prow.addWidget(self.port, 1)
        prow.addWidget(refresh)
        self.vehicle = vehicle_combo(db, vehicle_id)
        self.erase = QCheckBox("Erase tracker memory after a successful download")
        self.status = QLabel("Connect the tracker with its USB cable.")
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setValue(0)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(2000)

        form = QFormLayout()
        form.addRow("Port", prow)
        form.addRow("Vehicle", self.vehicle)
        form.addRow("", self.erase)

        self.btn_info = QPushButton("Device info")
        self.btn_assign = QPushButton("Write registration to tracker")
        self.btn_replace = QPushButton("Use this tracker for the vehicle…")
        self.btn_replace.setToolTip("Replacement tracker: pair the connected tracker with the selected vehicle")
        self.btn_download = QPushButton("Download trips")
        self.btn_download.setDefault(True)
        self.btn_cancel = QPushButton("Close")
        self.btn_info.clicked.connect(self.do_info)
        self.btn_assign.clicked.connect(self.do_assign)
        self.btn_replace.clicked.connect(self.do_replace)
        self.btn_download.clicked.connect(self.do_download)
        self.btn_cancel.clicked.connect(self.cancel_or_close)
        brow = QHBoxLayout()
        for b in (self.btn_info, self.btn_assign, self.btn_replace):
            brow.addWidget(b)
        brow.addStretch(1)
        brow.addWidget(self.btn_download)
        brow.addWidget(self.btn_cancel)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.status)
        lay.addWidget(self.bar)
        lay.addWidget(self.logbox, 1)
        lay.addLayout(brow)

        self._fwd = _LogForwarder()
        self._fwd.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        self._fwd.line.connect(self.logbox.appendPlainText)
        logging.getLogger("tripbook.device").addHandler(self._fwd)
        self.refresh_ports()

    def closeEvent(self, e):
        if self.job and self.job.isRunning():
            self.job.cancel()
            self.job.wait(5000)
        logging.getLogger("tripbook.device").removeHandler(self._fwd)
        super().closeEvent(e)

    def reject(self):
        if self.job and self.job.isRunning():
            self.job.cancel()
            return
        logging.getLogger("tripbook.device").removeHandler(self._fwd)
        super().reject()

    def refresh_ports(self) -> None:
        self.port.clear()
        for p in D.list_ports():
            self.port.addItem(p.label(), p.path)
        if not self.port.count():
            self.port.addItem("No serial device found – plug in the tracker and press Refresh", None)

    def cancel_or_close(self) -> None:
        self.reject()

    def _vehicle(self):
        vid = self.vehicle.currentData()
        rows = self.db.q("SELECT * FROM vehicle WHERE id=?", (vid,)) if vid else []
        return rows[0] if rows else None

    def _start(self, fn, on_ok) -> None:
        path = self.port.currentData()
        if not path:
            QMessageBox.warning(self, "Tracker", "No serial port selected.")
            return
        for b in (self.btn_info, self.btn_assign, self.btn_replace, self.btn_download):
            b.setEnabled(False)
        self.btn_cancel.setText("Cancel")
        self.bar.setRange(0, 0)
        self.job = Job(lambda job: fn(job, path), self)
        self.job.message.connect(self.status.setText)
        self.job.progressed.connect(self._progress)
        self.job.succeeded.connect(on_ok)
        self.job.failed.connect(self._failed)
        self.job.finished.connect(self._finished)
        self.job.start()

    def _progress(self, a: int, b: int) -> None:
        self.bar.setRange(0, b)
        self.bar.setValue(a)

    def _failed(self, msg: str) -> None:
        self.status.setText(f"<b>Failed:</b> {msg}")
        self.logbox.appendPlainText("ERROR: " + msg)

    def _finished(self) -> None:
        for b in (self.btn_info, self.btn_assign, self.btn_replace, self.btn_download):
            b.setEnabled(True)
        self.btn_cancel.setText("Close")
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 1)
            self.bar.setValue(0)

    # -- device sessions (run in the worker thread) --------------------------------
    @staticmethod
    def _session(job: Job, path: str, password: str):
        job.say(f"Connecting to {path}…")
        port, lg, baud = D.open_logger(path, job.cancelled)
        D.log.info("Connected at %d baud", baud)
        fw = lg.firmware_version() or "unknown"
        D.log.info("Firmware %s", fw)
        if password:
            job.say("Logging in…")
            if not lg.login(password):
                port.close()
                raise D.DeviceError("The tracker rejected the password stored for this vehicle.")
        return port, lg, baud, fw

    def _read_info(self, job: Job, path: str, password: str) -> D.DeviceInfo:
        port, lg, baud, fw = self._session(job, path, password)
        try:
            info = D.DeviceInfo(path, baud, fw)
            info.user = lg.user_data()
            info.config = lg.log_config()
            if info.config is None and info.user is None and not password:
                raise D.DeviceError("The tracker did not answer. It may be password protected: enter the "
                                    "tracker password in the vehicle settings.")
            info.agps_hours = lg.agps_hours_left()
            return info
        finally:
            port.close()

    def do_info(self) -> None:
        v = self._vehicle()
        pw = v["device_password"] if v else ""
        self._start(lambda job, path: self._read_info(job, path, pw), self._show_info)

    def _show_info(self, info: D.DeviceInfo) -> None:
        lines = [f"Port: {info.port} @ {info.baud} baud", f"Firmware: {info.firmware}"]
        if info.user:
            lines += [f"Registration on tracker: {info.user.registration_number or '—'}",
                      f"Serial: {info.user.serial or '—'}", f"Registered: {info.user.registration_date or '—'}"]
            self._select_vehicle_for(info.user)
        if info.config:
            c = info.config
            pct = 100 * c.used_sectors / c.total_sectors if c.total_sectors else 0
            lines += [f"Memory used: {c.used_sectors} of {c.total_sectors} sectors ({pct:.0f}%)",
                      f"Logging: {'on' if c.enabled else 'off'}"]
            if pct >= MEMORY_WARN_PCT:
                lines.append(f"<b>The tracker memory is {pct:.0f}% full</b>: download and erase it soon; a full "
                             "tracker stops recording.")
        if info.agps_hours is not None:
            lines.append(f"AGPS data valid for {info.agps_hours // 24} d {info.agps_hours % 24} h")
        self.status.setText("<br>".join(lines))
        for ln in lines:
            self.logbox.appendPlainText(ln)

    def _select_vehicle_for(self, ud: D.UserData) -> None:
        for v in self.db.vehicles():
            if (ud.registration_number and v["registration"].replace(" ", "").lower() ==
                    ud.registration_number.replace(" ", "").lower()) or (ud.serial and v["device_serial"] == ud.serial):
                self.vehicle.setCurrentIndex(self.vehicle.findData(v["id"]))
                return

    def do_replace(self) -> None:
        """Pair a (replacement) tracker with the selected vehicle, whatever it was registered to before."""
        v = self._vehicle()
        if not v or not v["registration"]:
            QMessageBox.warning(self, "Tracker", "Select the vehicle (with its registration number) that this "
                                "tracker should be used for.")
            return
        vid, reg, name, pw = v["id"], v["registration"], v["name"], v["device_password"]

        def read(job: Job, path: str):
            # A new tracker usually has no password; one moved from another vehicle may have this vehicle's.
            for attempt in dict.fromkeys(["", pw]):
                port, lg, *_ = self._session(job, path, attempt)
                try:
                    ud = lg.user_data()
                finally:
                    port.close()
                if ud is not None:
                    return ud, attempt
            raise D.DeviceError("The tracker did not answer. If it is password protected, enter its password in "
                                "the vehicle settings first.")

        def confirm(result) -> None:
            ud, used_pw = result
            old = ud.registration_number or "nothing"
            if QMessageBox.question(
                    self, "Use tracker for vehicle",
                    f"The connected tracker (serial {ud.serial or 'unknown'}) is registered to {old}.\n\n"
                    f"Use it for {name} from now on? The registration {reg} is written to the tracker and the "
                    "vehicle's tracker serial is updated. Download any trips still on it into the right vehicle "
                    "first.") != QMessageBox.StandardButton.Yes:
                return

            def write(job: Job, path: str):
                port, lg, *_ = self._session(job, path, used_pw)
                try:
                    ud.registration_number = reg
                    if not ud.registration_date:
                        ud.registration_date = datetime.now().strftime("%d%b%Y")
                    if not lg.set_user_data(ud):
                        raise D.DeviceError("The tracker did not accept the registration.")
                    return ud
                finally:
                    port.close()

            def done(ud) -> None:
                self.db.x("UPDATE vehicle SET device_serial=? WHERE id=?", (ud.serial or "", vid))
                self.status.setText(f"Tracker {ud.serial or ''} is now used for <b>{name}</b> ({reg}).")

            QTimer.singleShot(0, lambda: self._start(write, done))

        self._start(read, confirm)

    def do_assign(self) -> None:
        v = self._vehicle()
        if not v or not v["registration"]:
            QMessageBox.warning(self, "Tracker", "Select a vehicle that has a registration number.")
            return
        reg, pw = v["registration"], v["device_password"]

        def run(job: Job, path: str):
            port, lg, *_ = self._session(job, path, pw)
            try:
                ud = lg.user_data() or D.UserData()
                if not ud.registration_date:
                    ud.registration_date = datetime.now().strftime("%d%b%Y")
                ud.registration_number = reg
                if not lg.set_user_data(ud):
                    raise D.DeviceError("The tracker did not accept the registration.")
                return ud
            finally:
                port.close()

        self._start(run, lambda ud: self.status.setText(f"Tracker now registered to <b>{ud.registration_number}</b>."))

    def do_download(self) -> None:
        v = self._vehicle()
        if not v:
            QMessageBox.warning(self, "Tracker", "Create a vehicle first (Vehicles tab).")
            return
        vid, pw, reg = v["id"], v["device_password"], v["registration"]
        out_dir = self.data_dir / "downloads"

        def run(job: Job, path: str):
            port, lg, baud, fw = self._session(job, path, pw)
            try:
                ud = lg.user_data()
                if ud and ud.registration_number:
                    D.log.info("Tracker registered to %s", ud.registration_number)
                cfg = lg.log_config()
                if cfg is None:
                    raise D.DeviceError("Could not read the tracker's memory status. If the tracker is password "
                                        "protected, enter its password in the vehicle settings.")
                D.log.info("%d sectors in use", cfg.used_sectors)
                if cfg.total_sectors and cfg.used_sectors * 100 >= MEMORY_WARN_PCT * cfg.total_sectors:
                    D.log.info("Tracker memory is %.0f%% full: erase it after this download",
                               100 * cfg.used_sectors / cfg.total_sectors)
                job.say(f"Downloading {cfg.used_sectors} memory sectors…")
                t0 = time.monotonic()
                raw = lg.download(cfg, job.progress)
                D.log.info("Downloaded %d bytes in %.0f s", len(raw), time.monotonic() - t0)
                out_dir.mkdir(parents=True, exist_ok=True)
                safe = "".join(c for c in (reg or "vehicle") if c.isalnum()) or "vehicle"
                dump = out_dir / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_{safe}.bin"
                dump.write_bytes(raw)
                D.log.info("Saved raw log to %s", dump)
                return vid, raw, str(dump), (ud.registration_number if ud else "")
            finally:
                port.close()

        self._start(run, self._downloaded)

    def _downloaded(self, result) -> None:
        vid, raw, dump, dev_reg = result
        v = self._vehicle()
        if dev_reg and v and v["registration"] and dev_reg.replace(" ", "").lower() != \
                v["registration"].replace(" ", "").lower():
            self.logbox.appendPlainText(f"Note: tracker is registered to {dev_reg}, imported into {v['name']}.")
        self.downloaded.emit(vid, raw, dump)
        if self.erase.isChecked():
            pw = v["device_password"] if v else ""
            if QMessageBox.question(self, "Erase tracker",
                                    "Trips were imported and the raw log was saved.\n"
                                    "Erase the tracker memory now?") == QMessageBox.StandardButton.Yes:
                def run(job: Job, path: str):
                    port, lg, *_ = self._session(job, path, pw)
                    try:
                        job.say("Erasing tracker memory…")
                        if not lg.clear_log():
                            raise D.DeviceError("The tracker did not confirm the erase.")
                    finally:
                        port.close()
                self._start(run, lambda _: self.status.setText("Tracker memory erased."))

    def report(self, text: str) -> None:
        self.status.setText(text)
        self.bar.setRange(0, 1)
        self.bar.setValue(1)


# --------------------------------------------------------------------------- place name suggestions

class SuggestPlacesDialog(QDialog):
    """Name the spots where trips start or end that have no place yet, with names from OpenStreetMap."""
    BATCH = 20  # spots looked up per click: each takes ~2 s because of the OpenStreetMap usage policies

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Suggest place names")
        self.resize(1250, 720)
        self.db = db
        self.spots: list[UnnamedSpot] = []
        self.found: dict[int, list[str]] = {}  # spot row -> suggestions (filled by the lookup thread)
        self.job: Job | None = None

        self.include = QCheckBox("Also spots that only show a suburb or street name")
        self.include.toggled.connect(self.load)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Create", "Trips", "Shown now", "Name"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._show_selected)
        self.map = MapView(tiles=db.setting("tiles", DEFAULT_TILES), view=db.setting("map_view", "Map"))
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.btn_lookup = QPushButton("Look up names")
        self.btn_lookup.clicked.connect(self.lookup)
        self.btn_create = QPushButton("Create places")
        self.btn_create.clicked.connect(self.create)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        note = QLabel("Names come from OpenStreetMap (nearby businesses and buildings via Photon, addresses via "
                      "Nominatim). Pick one or type your own and untick spots you don't want to name, then Create "
                      "places: every trip that starts or ends there gets the name.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777")

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.include)
        ll.addWidget(self.table)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.map)
        split.setSizes([700, 550])
        btns = QHBoxLayout()
        btns.addWidget(self.btn_lookup)
        btns.addWidget(self.bar, 1)
        btns.addStretch(0)
        btns.addWidget(self.btn_create)
        btns.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(note)
        lay.addWidget(split, 1)
        lay.addWidget(self.status)
        lay.addLayout(btns)
        self.load()

    # -- rows -------------------------------------------------------------------------
    def load(self) -> None:
        if self.job and self.job.isRunning():
            self.job.cancel()
            self.job.wait(5000)
        self.spots = self.db.unnamed_spots(self.include.isChecked())
        self.found = {}
        self.table.setRowCount(0)
        for r, sp in enumerate(self.spots):
            self.table.insertRow(r)
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            use.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(r, 0, use)
            n = QTableWidgetItem(str(len(sp.ends)))
            n.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            self.table.setItem(r, 1, n)
            self.table.setItem(r, 2, QTableWidgetItem(sp.label))
            for c in (1, 2):
                self.table.item(r, c).setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.lineEdit().setPlaceholderText("Look up names, or type one")
            # Typing or picking a name ticks the row.
            combo.currentTextChanged.connect(lambda text, r=r: self._named(r, text))
            self.table.setCellWidget(r, 3, combo)
            cached = self._cached(sp)
            if cached:
                self._fill(r, cached)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(2, min(self.table.columnWidth(2), 220))
        self.map.show_trips([], fit=False)
        self._update_status()
        if self.spots:
            self.table.selectRow(0)

    def _cached(self, sp: UnnamedSpot) -> list[str] | None:
        import json
        r = self.db.conn.execute("SELECT address FROM geocode_cache WHERE key=?",
                                 (f"suggest:{sp.lat:.4f},{sp.lon:.4f}",)).fetchone()
        return json.loads(r[0]) if r else None

    def _fill(self, row: int, names: list[str]) -> None:
        self.found[row] = names
        combo: QComboBox = self.table.cellWidget(row, 3)
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(names)
        combo.setEditText(names[0] if names else "")
        combo.blockSignals(False)
        self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if names else Qt.CheckState.Unchecked)
        if not names:
            combo.lineEdit().setPlaceholderText("Nothing found nearby – type a name")

    def _named(self, row: int, text: str) -> None:
        self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if text.strip() else Qt.CheckState.Unchecked)
        self._update_status()

    def _chosen(self) -> list[tuple[str, float, float]]:
        out = []
        for r, sp in enumerate(self.spots):
            name = self.table.cellWidget(r, 3).currentText().strip()
            if self.table.item(r, 0).checkState() == Qt.CheckState.Checked and name:
                out.append((name, sp.lat, sp.lon))
        return out

    def _update_status(self) -> None:
        todo = sum(1 for r in range(len(self.spots)) if r not in self.found)
        n = len(self._chosen())
        self.btn_create.setText(f"Create {n} place{'s' if n != 1 else ''}" if n else "Create places")
        self.btn_create.setEnabled(n > 0)
        self.btn_lookup.setEnabled(todo > 0 and not (self.job and self.job.isRunning()))
        self.btn_lookup.setText(f"Look up names ({min(todo, self.BATCH)} of {todo} left)" if todo else
                                "All names looked up")
        if not self.spots:
            self.status.setText("Every trip start and end already belongs to a place." +
                                ("" if self.include.isChecked() else
                                 " Tick the option above to also name spots that only show a suburb."))
        else:
            self.status.setText(f"{len(self.spots)} unnamed spot(s) with {sum(len(s.ends) for s in self.spots)} "
                                "trip starts/ends, most visited first.")

    def _show_selected(self) -> None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        if not rows:
            return
        sp = self.spots[min(rows)]
        trips = [self.db.trip(tid) for tid, _ in sp.ends[:200]]
        self.map.show_trips([{"coords": [], "color": "#888", "label": "",
                              "start": [t.start_lat, t.start_lon] if w == "start" else None,
                              "end": [t.end_lat, t.end_lon] if w == "end" else None,
                              "startLabel": f"Trip start {t.start_time.astimezone():%Y-%m-%d %H:%M}",
                              "endLabel": f"Trip end {t.end_time.astimezone():%Y-%m-%d %H:%M}"}
                             for t, (_, w) in zip(trips, sp.ends)], fit=False)
        self.map.show_cursor(sp.lat, sp.lon, pan=False)
        self.map.focus(sp.lat, sp.lon, 17)

    # -- lookup / create --------------------------------------------------------------
    def lookup(self) -> None:
        rows = [r for r in range(len(self.spots)) if r not in self.found][:self.BATCH]
        if not rows:
            return
        coords = [(r, self.spots[r].lat, self.spots[r].lon) for r in rows]
        db_path = self.db.path
        results: dict[int, list[str]] = {}

        def run(job: Job):
            local = Database(db_path)  # sqlite connections are per thread
            try:
                for i, (r, lat, lon) in enumerate(coords):
                    if job.cancelled():
                        break
                    job.progress(i, len(coords))
                    try:
                        results[r] = place_suggestions(local, lat, lon)
                    except (OSError, ValueError) as e:
                        job.say(f"Lookup failed: {e}")
                        break
                    job.progress(i + 1, len(coords))
                    time.sleep(1.1)
            finally:
                local.conn.close()
            return len(results)

        def progressed(a: int, b: int) -> None:
            self.bar.setRange(0, b)
            self.bar.setValue(a)
            for r in list(results):
                if r not in self.found:
                    self._fill(r, results[r])
            self._update_status()

        def finished() -> None:
            progressed(len(coords), len(coords))
            self.bar.setVisible(False)
            self._update_status()

        self.job = Job(run, self)
        self.job.progressed.connect(progressed)
        self.job.message.connect(self.status.setText)
        self.job.failed.connect(lambda msg: self.status.setText(f"<b>Lookup failed:</b> {msg}"))
        self.job.finished.connect(finished)
        self.bar.setVisible(True)
        self.bar.setRange(0, len(coords))
        self.bar.setValue(0)
        self.btn_lookup.setEnabled(False)
        self.status.setText(f"Looking up {len(coords)} spot(s) on OpenStreetMap…")
        self.job.start()

    def create(self) -> None:
        chosen = self._chosen()
        if not chosen:
            return
        self.db.create_places(chosen)
        self.load()
        self.status.setText(f"Created {len(chosen)} place(s); their trips are labelled now. " + self.status.text())

    def reject(self) -> None:
        if self.job and self.job.isRunning():
            self.job.cancel()
            self.job.wait(5000)
        super().reject()
