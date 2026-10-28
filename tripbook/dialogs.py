"""Edit dialogs, settings, and the device download dialog."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QDate, QDateTime, QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDateTimeEdit, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QVBoxLayout)

from . import device as D
from .db import EXPENSE_CATEGORIES, Database
from .mapview import DEFAULT_TILES


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


class VehicleDialog(FormDialog):
    def __init__(self, db: Database, row=None, parent=None):
        super().__init__("Vehicle", parent)
        g = (lambda k, d="": row[k] if row and row[k] is not None else d)
        self.name = QLineEdit(g("name"))
        self.reg = QLineEdit(g("registration"))
        self.make = QLineEdit(g("make"))
        self.year = QLineEdit(g("year"))
        self.odo = spin(value=g("odometer_start", 0), suffix=" km")
        self.odo_date = QDateEdit(qdate(g("odometer_start_date", None)))
        self.odo_date.setCalendarPopup(True)
        self.serial = QLineEdit(g("device_serial"))
        self.password = QLineEdit(g("device_password"))
        self.password.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.password.setMaxLength(16)
        self.closed = QCheckBox("Vehicle sold / no longer in use")
        self.closed.setChecked(bool(g("closed", 0)))
        for label, w in [("Name", self.name), ("Registration", self.reg), ("Make / model", self.make),
                         ("Year", self.year), ("Opening odometer", self.odo), ("Odometer date", self.odo_date),
                         ("Tracker serial", self.serial), ("Tracker password", self.password), ("", self.closed)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        return dict(name=self.name.text().strip() or self.reg.text().strip() or "Vehicle",
                    registration=self.reg.text().strip(), make=self.make.text().strip(),
                    year=self.year.text().strip(), odometer_start=self.odo.value(),
                    odometer_start_date=self.odo_date.date().toString("yyyy-MM-dd"),
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
        self.dist = spin(suffix=" km")
        self.type = type_combo(db)
        self.reason = QLineEdit()
        for label, w in [("Vehicle", self.vehicle), ("Start", self.start), ("End", self.end), ("From", self.frm),
                         ("To", self.to), ("Distance", self.dist), ("Type", self.type), ("Reason", self.reason)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        return dict(vehicle_id=self.vehicle.currentData(),
                    start=self.start.dateTime().toPython().astimezone(),
                    end=self.end.dateTime().toPython().astimezone(),
                    start_desc=self.frm.text().strip(), end_desc=self.to.text().strip(),
                    distance_km=self.dist.value(), trip_type_id=self.type.currentData(),
                    reason=self.reason.text().strip())


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
        self.odo = spin(value=g("odometer", 0), suffix=" km")
        for label, w in [("Vehicle", self.vehicle), ("Date", self.date), ("Category", self.cat),
                         ("Description", self.desc), ("Amount", self.amount), ("Fuel volume", self.volume),
                         ("Odometer (optional)", self.odo)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        return dict(vehicle_id=self.vehicle.currentData(), date=self.date.date().toString("yyyy-MM-dd"),
                    category=self.cat.currentText().strip() or "Other", description=self.desc.text().strip(),
                    amount=self.amount.value(), volume=self.volume.value(), odometer=self.odo.value() or None)


class ReminderDialog(FormDialog):
    def __init__(self, db: Database, row=None, vehicle_id=None, parent=None):
        super().__init__("Reminder", parent)
        g = (lambda k, d=None: row[k] if row and row[k] is not None else d)
        self.vehicle = vehicle_combo(db, g("vehicle_id", vehicle_id))
        self.title = QLineEdit(g("title", ""))
        self.title.setPlaceholderText("e.g. Service, Licence renewal, Tyres")
        self.use_date = QCheckBox("Due on date")
        self.use_date.setChecked(row is None or bool(g("due_date")))
        self.date = QDateEdit(qdate(g("due_date")))
        self.date.setCalendarPopup(True)
        self.odo = spin(value=g("due_odometer", 0), suffix=" km")
        self.rep_m = QSpinBox()
        self.rep_m.setRange(0, 120)
        self.rep_m.setValue(g("repeat_months", 0))
        self.rep_m.setSuffix(" months")
        self.rep_km = spin(value=g("repeat_km", 0), suffix=" km")
        drow = QHBoxLayout()
        drow.addWidget(self.use_date)
        drow.addWidget(self.date)
        for label, w in [("Vehicle", self.vehicle), ("Title", self.title)]:
            self.form.addRow(label, w)
        self.form.addRow("Due date", drow)
        for label, w in [("Due at odometer (0 = none)", self.odo), ("Repeat every", self.rep_m),
                         ("…or every", self.rep_km)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        return dict(vehicle_id=self.vehicle.currentData(), title=self.title.text().strip() or "Reminder",
                    due_date=self.date.date().toString("yyyy-MM-dd") if self.use_date.isChecked() else None,
                    due_odometer=self.odo.value() or None, repeat_months=self.rep_m.value(),
                    repeat_km=self.rep_km.value())


class RuleDialog(FormDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__("Auto-mark rule", parent)
        self.vehicle = vehicle_combo(db, include_all=True)
        self.start = place_combo(db)
        self.end = place_combo(db)
        self.reverse = QCheckBox("Also apply to the return trip")
        self.reverse.setChecked(True)
        self.type = type_combo(db, allow_none=False)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Reason filled into matching trips (optional)")
        for label, w in [("Vehicle", self.vehicle), ("From place", self.start), ("To place", self.end),
                         ("", self.reverse), ("Mark as", self.type), ("Reason", self.reason)]:
            self.form.addRow(label, w)

    def values(self) -> dict:
        return dict(vehicle_id=self.vehicle.currentData(), start_place_id=self.start.currentData(),
                    end_place_id=self.end.currentData(), reverse=int(self.reverse.isChecked()),
                    trip_type_id=self.type.currentData(), reason=self.reason.text().strip())


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
        self.tiles = QLineEdit(db.setting("tiles", DEFAULT_TILES))
        self.geocode = QCheckBox("Look up street addresses for new trips (OpenStreetMap Nominatim)")
        self.geocode.setChecked(db.setting("auto_geocode", "0") == "1")
        info = QLabel(f"Data folder: {data_dir}")
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for label, w in [("Split trips after a stop of", self.stop), ("Distance unit", self.units),
                         ("Currency symbol", self.currency), ("Financial year starts in", self.year_start),
                         ("Map tile URL", self.tiles), ("", self.geocode), ("", info)]:
            self.form.addRow(label, w)
        self.db = db

    def save(self) -> None:
        db = self.db
        db.set_setting("stop_interval_min", self.stop.value())
        db.set_setting("units", self.units.currentData())
        db.set_setting("currency", self.currency.text().strip())
        db.set_setting("year_start_month", self.year_start.currentData())
        db.set_setting("tiles", self.tiles.text().strip() or DEFAULT_TILES)
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
        self.btn_download = QPushButton("Download trips")
        self.btn_download.setDefault(True)
        self.btn_cancel = QPushButton("Close")
        self.btn_info.clicked.connect(self.do_info)
        self.btn_assign.clicked.connect(self.do_assign)
        self.btn_download.clicked.connect(self.do_download)
        self.btn_cancel.clicked.connect(self.cancel_or_close)
        brow = QHBoxLayout()
        for b in (self.btn_info, self.btn_assign):
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
        for b in (self.btn_info, self.btn_assign, self.btn_download):
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
        for b in (self.btn_info, self.btn_assign, self.btn_download):
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
