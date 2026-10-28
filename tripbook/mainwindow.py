"""Main application window."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QDate, QItemSelection, QItemSelectionModel, QModelIndex,
                            QSettings, Qt, QTimer)
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence, QPageLayout, QPageSize, QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QFileDialog, QHBoxLayout,
                               QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
                               QSplitter, QStyledItemDelegate, QTableView, QTableWidget, QTableWidgetItem,
                               QTabWidget, QTextBrowser, QToolBar, QVBoxLayout, QWidget)

from . import __version__
from .db import PLACE_RADIUS_M, Database, Trip, from_iso, to_iso
from .decode import decode_log
from .dialogs import (DeviceDialog, ExpenseDialog, Job, ManualTripDialog, MileageRateDialog, OdometerDialog,
                      ReminderDialog, RuleDialog, SettingsDialog, SplitDialog, SuggestPlacesDialog,
                      VehicleDialog)
from .exchange import (REPORT_LAYOUTS, distance_factor, export_trips_csv, import_littlelogbook_db, import_points,
                       read_gpx, report_html, reverse_geocode, summary, write_gpx, write_kml)
from .mapview import DEFAULT_TILES, MapView

PERIODS = ["This month", "Last month", "This financial year", "Last financial year", "Last 12 months",
           "All time", "Custom"]


def fmt_duration(s: int) -> str:
    h, m = divmod(round(s / 60), 60)
    return f"{h}:{m:02d}"


# --------------------------------------------------------------------------- trip table model

class TripModel(QAbstractTableModel):
    COLS = ["Date", "Start", "End", "Vehicle", "From", "To", "Distance", "Duration", "Odo start", "Odo end",
            "Type", "Reason"]
    EDITABLE = {"From", "To", "Type", "Reason"}

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.trips: list[Trip] = []
        self.types: dict[int, object] = {}
        self.vehicles: dict[int, str] = {}

    def load(self, trips: list[Trip]) -> None:
        self.beginResetModel()
        self.trips = trips
        self.types = {t["id"]: t for t in self.win.db.trip_types()}
        self.vehicles = {v["id"]: v["name"] for v in self.win.db.vehicles()}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.trips)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            col = self.COLS[section]
            unit = distance_factor(self.win.db)[1]
            return f"{col} ({unit})" if col in ("Distance", "Odo start", "Odo end") else col
        return None

    def flags(self, index):
        f = super().flags(index)
        if self.COLS[index.column()] in self.EDITABLE:
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        t = self.trips[index.row()]
        col = self.COLS[index.column()]
        f = distance_factor(self.win.db)[0]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            s, e = t.start_time.astimezone(), t.end_time.astimezone()
            tt = self.types.get(t.trip_type_id)
            return {
                "Date": s.strftime("%a %Y-%m-%d"), "Start": s.strftime("%H:%M"), "End": e.strftime("%H:%M"),
                "From": t.start_label() if role == Qt.ItemDataRole.DisplayRole else t.start_desc,
                "To": t.end_label() if role == Qt.ItemDataRole.DisplayRole else t.end_desc,
                "Distance": f"{t.distance_km * f:,.1f}", "Duration": fmt_duration(t.duration_s),
                "Odo start": f"{t.odo_start * f:,.0f}", "Odo end": f"{t.odo_end * f:,.0f}",
                "Type": (tt["name"] if tt else "–") if role == Qt.ItemDataRole.DisplayRole else t.trip_type_id,
                "Reason": t.reason, "Vehicle": self.vehicles.get(t.vehicle_id, ""),
            }[col]
        if role == Qt.ItemDataRole.TextAlignmentRole and col in ("Distance", "Odo start", "Odo end", "Duration"):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.BackgroundRole and col == "Type":
            tt = self.types.get(t.trip_type_id)
            c = QColor(tt["color"] if tt else "#cc3333")
            c.setAlpha(60 if tt else 35)
            return QBrush(c)
        if role == Qt.ItemDataRole.ForegroundRole and col in ("From", "To"):
            has = t.start_place_id if col == "From" else t.end_place_id
            if not has and not (t.start_desc if col == "From" else t.end_desc):
                return QBrush(QColor("#888888"))
        if role == Qt.ItemDataRole.ToolTipRole:
            bits = [f"Source: {t.source}"]
            if t.auto_marked:
                bits.append("Type set by an auto-mark rule")
            if t.poi_count:
                bits.append(f"{t.poi_count} button press(es)")
            return "\n".join(bits)
        return None

    def sort_key(self, col: str):
        types = {k: v["name"].lower() for k, v in self.types.items()}
        return {
            "Date": lambda t: t.start_time, "Start": lambda t: t.start_time.astimezone().time(),
            "End": lambda t: t.end_time.astimezone().time(),
            "Vehicle": lambda t: self.vehicles.get(t.vehicle_id, "").lower(),
            "From": lambda t: t.start_label().lower(), "To": lambda t: t.end_label().lower(),
            "Distance": lambda t: t.distance_km, "Duration": lambda t: t.duration_s,
            "Odo start": lambda t: t.odo_start, "Odo end": lambda t: t.odo_end,
            "Type": lambda t: types.get(t.trip_type_id, "~"), "Reason": lambda t: t.reason.lower(),
        }[col]

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.layoutAboutToBeChanged.emit()
        key = self.sort_key(self.COLS[column])
        # Stable sort on date first, so ties stay in chronological order.
        self.trips.sort(key=lambda t: t.start_time)
        self.trips.sort(key=key, reverse=order == Qt.SortOrder.DescendingOrder)
        self.layoutChanged.emit()

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole:
            return False
        t = self.trips[index.row()]
        col = self.COLS[index.column()]
        db = self.win.db
        if col == "Type":
            db.update_trip(t.id, trip_type_id=value, auto_marked=0)
            t.trip_type_id, t.auto_marked = value, False
        elif col == "Reason":
            db.update_trip(t.id, reason=str(value).strip())
            t.reason = str(value).strip()
        elif col in ("From", "To"):
            key = "start_desc" if col == "From" else "end_desc"
            text = str(value).strip()
            lat, lon = (t.start_lat, t.start_lon) if col == "From" else (t.end_lat, t.end_lon)
            if text and lat is not None:
                # Like the original: naming a trip's start or end names that location for all trips.
                db.name_place(lat, lon, text, t.start_place_id if col == "From" else t.end_place_id)
                QTimer.singleShot(0, self.win.refresh_trips)  # not while the view is still committing the edit
                return True
            db.update_trip(t.id, **{key: text})
            setattr(t, key, text)
        self.dataChanged.emit(index, index)
        self.win.update_status()
        return True


class TypeDelegate(QStyledItemDelegate):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        cb.addItem("— unmarked —", None)
        for t in self.win.db.trip_types():
            cb.addItem(t["name"], t["id"])
        return cb

    def setEditorData(self, editor, index):
        editor.setCurrentIndex(max(0, editor.findData(index.data(Qt.ItemDataRole.EditRole))))
        editor.showPopup()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData())


SORT_ROLE = Qt.ItemDataRole.UserRole + 1


class SortItem(QTableWidgetItem):
    """Table cell that sorts numbers ("R1,468.53", "42,000 km") by value and text case-insensitively."""

    def __lt__(self, other):
        a, b = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if isinstance(a, float) and isinstance(b, float):
            return a < b
        if isinstance(a, float) != isinstance(b, float):
            return isinstance(a, float)  # numbers before text
        return str(a) < str(b)


def sort_value(v):
    if isinstance(v, (int, float)):
        return float(v)
    s = "" if v is None else str(v)
    m = re.fullmatch(r"\s*[^\d\-]{0,3}?\s*(-?[\d,]*\.?\d+)\s*[a-zA-Z%]{0,3}\s*", s)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return s.lower()


def table_widget(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.verticalHeader().hide()
    t.horizontalHeader().setStretchLastSection(True)
    t.setAlternatingRowColors(True)
    t.setSortingEnabled(True)
    t.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    return t


def fill_table(t: QTableWidget, rows: list[list], ids: list[int]) -> None:
    """Replace the table contents, keeping the rows that were selected (by id) selected."""
    keep = set(selected_ids(t)) if t.rowCount() else set()
    was_blocked = t.blockSignals(True)
    t.selectionModel().blockSignals(True)
    t.setSortingEnabled(False)  # inserting while sorted would scramble rows
    t.setRowCount(0)
    for r, (vals, rid) in enumerate(zip(rows, ids)):
        t.insertRow(r)
        for c, v in enumerate(vals):
            item = SortItem("" if v is None else str(v))
            item.setData(Qt.ItemDataRole.UserRole, rid)
            item.setData(SORT_ROLE, sort_value(v))
            t.setItem(r, c, item)
    t.setSortingEnabled(True)
    t.resizeColumnsToContents()
    sel = QItemSelection()
    for r in range(t.rowCount()):
        if t.item(r, 0).data(Qt.ItemDataRole.UserRole) in keep:
            sel.select(t.model().index(r, 0), t.model().index(r, t.columnCount() - 1))
    t.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    t.selectionModel().blockSignals(False)
    t.blockSignals(was_blocked)


def selected_ids(t: QTableWidget) -> list[int]:
    return sorted({t.item(i.row(), 0).data(Qt.ItemDataRole.UserRole) for i in t.selectionModel().selectedRows()})


# --------------------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path):
        super().__init__()
        self.data_dir = data_dir
        self.db = Database(data_dir / "tripbook.db")
        self.jobs: list[Job] = []
        self.setWindowTitle("Tripbook – vehicle trip logbook")
        self.resize(1400, 860)
        self._build_toolbar()
        self._build_menu()

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_trips_tab(), "Trips")
        self.tabs.addTab(self._build_places_tab(), "Places")
        self.tabs.addTab(self._build_rules_tab(), "Auto-mark rules")
        self.tabs.addTab(self._build_expenses_tab(), "Expenses")
        self.tabs.addTab(self._build_reminders_tab(), "Reminders")
        self.tabs.addTab(self._build_report_tab(), "Report")
        self.tabs.addTab(self._build_vehicles_tab(), "Vehicles")
        self.tabs.currentChanged.connect(lambda _: self.refresh_current())
        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)

        st = QSettings("Tripbook", "Tripbook")
        if st.value("geometry"):
            self.restoreGeometry(st.value("geometry"))
        self.reload_vehicles()
        self.period.setCurrentText(st.value("period", "This month"))
        self.apply_period()
        if not self.db.vehicles():
            self.tabs.setCurrentIndex(6)
            self.statusBar().showMessage("Welcome! Start by adding your vehicle.", 15000)
        self.check_reminders()

    def closeEvent(self, e):
        st = QSettings("Tripbook", "Tripbook")
        st.setValue("geometry", self.saveGeometry())
        st.setValue("period", self.period.currentText())
        for j in self.jobs:
            j.cancel()
            j.wait(3000)
        if self.db.setting("auto_backup", "1") == "1":
            try:
                self.db.auto_backup(self.backup_dir, int(self.db.setting("backup_keep", "5")))
            except OSError as err:
                QMessageBox.warning(self, "Backup", f"The automatic backup failed: {err}")
        super().closeEvent(e)

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    # -- toolbar / filters ----------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.act_download = QAction("⬇  Download from tracker", self)
        self.act_download.setShortcut(QKeySequence("Ctrl+D"))
        self.act_download.triggered.connect(self.open_device)
        tb.addAction(self.act_download)
        act_add = QAction("＋  Add trip", self)
        act_add.triggered.connect(self.add_manual_trip)
        tb.addAction(act_add)
        tb.addSeparator()

        tb.addWidget(QLabel(" Vehicle "))
        self.vehicle = QComboBox()
        self.vehicle.setMinimumWidth(180)
        self.vehicle.currentIndexChanged.connect(lambda _: self.refresh_current())
        tb.addWidget(self.vehicle)
        tb.addWidget(QLabel("  Period "))
        self.period = QComboBox()
        self.period.addItems(PERIODS)
        self.period.currentTextChanged.connect(lambda _: self.apply_period())
        tb.addWidget(self.period)
        self.date_from = QDateEdit()
        self.date_to = QDateEdit()
        for w in (self.date_from, self.date_to):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd")
            w.dateChanged.connect(self._custom_dates)
        tb.addWidget(self.date_from)
        tb.addWidget(QLabel(" – "))
        tb.addWidget(self.date_to)
        tb.addWidget(QLabel("  Type "))
        self.type_filter = QComboBox()
        self.type_filter.currentIndexChanged.connect(lambda _: self.refresh_current())
        tb.addWidget(self.type_filter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search places / reasons")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        self.search.textChanged.connect(lambda _: self.refresh_current())
        tb.addWidget(QLabel("  "))
        tb.addWidget(self.search)

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&File")
        for text, fn in [("Import tracker dump (.bin)…", self.import_dump), ("Import GPX…", self.import_gpx),
                         ("Import Little LogBook database…", self.import_llb), (None, None),
                         ("Export selected trips as GPX…", self.export_gpx),
                         ("Export selected trips as KML (Google Earth)…", self.export_kml),
                         ("Export trips as CSV…", self.export_csv),
                         (None, None), ("Back up database…", self.backup), ("Restore from backup…", self.restore),
                         (None, None),
                         ("Settings…", self.open_settings), ("Quit", self.close)]:
            if text is None:
                m.addSeparator()
            else:
                a = m.addAction(text)
                a.triggered.connect(fn)
        h = self.menuBar().addMenu("&Help")
        h.addAction("About Tripbook").triggered.connect(lambda: QMessageBox.about(
            self, "About Tripbook",
            f"<b>Tripbook {__version__}</b><p>Vehicle trip logbook for Linux. Downloads trips from SkyTraq-based "
            "USB GPS trackers, marks them business or private and produces logbook reports.</p>"
            "<p>Map data © OpenStreetMap contributors.</p>"))

    def reload_vehicles(self) -> None:
        cur = self.vehicle.currentData()
        self.vehicle.blockSignals(True)
        self.vehicle.clear()
        self.vehicle.addItem("All vehicles", None)
        for v in self.db.vehicles():
            self.vehicle.addItem(v["name"] + (" (closed)" if v["closed"] else ""), v["id"])
        idx = self.vehicle.findData(cur) if cur is not None else -1
        if idx < 0 and self.vehicle.count() == 2:
            idx = 1  # only one vehicle: select it
        self.vehicle.setCurrentIndex(max(0, idx))
        self.vehicle.blockSignals(False)
        cur_t = self.type_filter.currentData()
        self.type_filter.blockSignals(True)
        self.type_filter.clear()
        self.type_filter.addItem("All", None)
        self.type_filter.addItem("Unmarked", -1)
        for t in self.db.trip_types():
            self.type_filter.addItem(t["name"], t["id"])
        self.type_filter.setCurrentIndex(max(0, self.type_filter.findData(cur_t)))
        self.type_filter.blockSignals(False)

    def apply_period(self) -> None:
        p = self.period.currentText()
        today = date.today()
        ys = int(self.db.setting("year_start_month", "1"))
        fy_start = date(today.year if today.month >= ys else today.year - 1, ys, 1)
        rng = {
            "This month": (today.replace(day=1), today),
            "Last month": ((today.replace(day=1) - timedelta(days=1)).replace(day=1),
                           today.replace(day=1) - timedelta(days=1)),
            "This financial year": (fy_start, today),
            "Last financial year": (fy_start.replace(year=fy_start.year - 1), fy_start - timedelta(days=1)),
            "Last 12 months": (today - timedelta(days=365), today),
            "All time": (date(2000, 1, 1), today),
        }.get(p)
        custom = p == "Custom"
        self.date_from.setEnabled(custom)
        self.date_to.setEnabled(custom)
        if rng:
            for w, d in zip((self.date_from, self.date_to), rng):
                w.blockSignals(True)
                w.setDate(QDate(d.year, d.month, d.day))
                w.blockSignals(False)
        self.refresh_current()

    def _custom_dates(self) -> None:
        if self.period.currentText() == "Custom":
            self.refresh_current()

    def period_range(self) -> tuple[datetime, datetime]:
        s = self.date_from.date().toPython()
        e = self.date_to.date().toPython() + timedelta(days=1)
        return datetime(s.year, s.month, s.day).astimezone(), datetime(e.year, e.month, e.day).astimezone()

    def period_text(self) -> str:
        return f"{self.date_from.date().toString('yyyy-MM-dd')} to {self.date_to.date().toString('yyyy-MM-dd')}"

    def filtered_trips(self) -> list[Trip]:
        s, e = self.period_range()
        return self.db.trips(self.vehicle.currentData(), s, e, self.type_filter.currentData(),
                             self.search.text().strip())

    def refresh_current(self) -> None:
        idx = self.tabs.currentIndex() if hasattr(self, "tabs") else 0
        [self.refresh_trips, self.refresh_places, self.refresh_rules, self.refresh_expenses,
         self.refresh_reminders, self.refresh_report, self.refresh_vehicles][idx]()

    def refresh_all(self) -> None:
        self.reload_vehicles()
        self.refresh_current()

    def update_status(self) -> None:
        trips = self.model.trips
        s = summary(self.db, trips, [])
        f, unit = distance_factor(self.db)
        unmarked = sum(1 for t in trips if t.trip_type_id is None)
        auto = sum(1 for t in trips if t.auto_marked)
        hours = sum(t.duration_s for t in trips) / 3600
        self.status_label.setText(
            f"{s['trips']} trips · {s['total_km'] * f:,.1f} {unit} · {hours:,.1f} h · business "
            f"{s['business_km'] * f:,.1f} {unit} ({s['business_pct']:.0f}%)"
            + (f" · {unmarked} unmarked" if unmarked else "") + (f" · {auto} auto-marked" if auto else ""))

    def tiles(self) -> str:
        return self.db.setting("tiles", DEFAULT_TILES)

    def new_map(self) -> MapView:
        mv = MapView(tiles=self.tiles(), view=self.db.setting("map_view", "Map"))
        mv.view_changed.connect(lambda name: self.db.set_setting("map_view", name))
        return mv

    # -- trips tab ------------------------------------------------------------------
    def _build_trips_tab(self) -> QWidget:
        self.model = TripModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                   QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setItemDelegateForColumn(TripModel.COLS.index("Type"), TypeDelegate(self))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.trip_menu)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._trip_selection_changed())
        self.trip_map = self.new_map()

        # Bulk edit bar: set type and/or reason on all selected trips at once.
        self.bulk_label = QLabel()
        self.bulk_type = QComboBox()
        self.bulk_type.setMinimumWidth(140)
        self.bulk_reason = QComboBox()
        self.bulk_reason.setEditable(True)
        self.bulk_reason.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.bulk_reason.setMinimumWidth(320)
        self.bulk_reason.lineEdit().setPlaceholderText("Reason (leave empty to keep current reasons)")
        self.bulk_reason.lineEdit().returnPressed.connect(self.apply_bulk)
        self.bulk_apply = QPushButton("Apply to selected")
        self.bulk_apply.clicked.connect(self.apply_bulk)
        self.bulk_clear = QPushButton("Clear reason")
        self.bulk_clear.clicked.connect(self.clear_bulk_reason)
        select_all = QPushButton("Select all")
        select_all.clicked.connect(self.table.selectAll)
        bar = QHBoxLayout()
        bar.addWidget(self.bulk_label)
        bar.addWidget(QLabel("Type"))
        bar.addWidget(self.bulk_type)
        bar.addWidget(QLabel("Reason"))
        bar.addWidget(self.bulk_reason, 1)
        bar.addWidget(self.bulk_apply)
        bar.addWidget(self.bulk_clear)
        bar.addWidget(select_all)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.trip_map)
        split.setSizes([480, 360])
        hint = QLabel("Select several trips with Ctrl+click or Shift+click, then set their type and reason above · "
                      "double-click a cell to edit (a name typed in From/To names that place for all trips) · "
                      "right-click for more")
        hint.setStyleSheet("color: #777")
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addLayout(bar)
        lay.addWidget(split)
        lay.addWidget(hint)
        return w

    def _fill_bulk_choices(self) -> None:
        cur = self.bulk_type.currentData()
        self.bulk_type.clear()
        self.bulk_type.addItem("— keep —", "keep")
        for t in self.db.trip_types():
            self.bulk_type.addItem(t["name"], t["id"])
        self.bulk_type.addItem("Unmarked", None)
        self.bulk_type.setCurrentIndex(max(0, self.bulk_type.findData(cur)))
        text = self.bulk_reason.currentText()
        self.bulk_reason.clear()
        self.bulk_reason.addItems([r[0] for r in self.db.q(
            "SELECT reason, COUNT(*) n FROM trip WHERE reason<>'' GROUP BY reason ORDER BY n DESC LIMIT 100")])
        self.bulk_reason.setEditText(text)

    def _trip_selection_changed(self) -> None:
        n = len(self.table.selectionModel().selectedRows())
        self.bulk_label.setText(f"<b>{n} selected</b>" if n else "No trips selected")
        for wdg in (self.bulk_apply, self.bulk_clear):
            wdg.setEnabled(n > 0)
        self.bulk_apply.setText(f"Apply to {n} trip{'s' if n != 1 else ''}" if n else "Apply to selected")
        self.show_selected_on_map()

    def apply_bulk(self) -> None:
        trips = self.selected_trips()
        type_id = self.bulk_type.currentData()
        reason = self.bulk_reason.currentText().strip()
        if not trips:
            return
        if type_id == "keep" and not reason:
            self.statusBar().showMessage("Choose a type and/or type a reason first.", 5000)
            return
        for t in trips:
            fields = {}
            if type_id != "keep":
                fields.update(trip_type_id=type_id, auto_marked=0)
            if reason:
                fields["reason"] = reason
            self.db.update_trip(t.id, **fields)
        self.statusBar().showMessage(f"Updated {len(trips)} trip(s).", 5000)
        self.refresh_trips()

    def clear_bulk_reason(self) -> None:
        trips = self.selected_trips()
        for t in trips:
            self.db.update_trip(t.id, reason="")
        self.statusBar().showMessage(f"Cleared the reason of {len(trips)} trip(s).", 5000)
        self.refresh_trips()

    def refresh_trips(self) -> None:
        self._fill_bulk_choices()
        sel = set(self.selected_trip_ids())
        self.model.load(self.filtered_trips())
        hdr = self.table.horizontalHeader()
        self.model.sort(hdr.sortIndicatorSection(), hdr.sortIndicatorOrder())
        self.table.resizeColumnsToContents()
        for c in ("From", "To"):
            i = TripModel.COLS.index(c)
            self.table.setColumnWidth(i, min(self.table.columnWidth(i), 260))
        self.table.setColumnHidden(TripModel.COLS.index("Vehicle"), self.vehicle.currentData() is not None)
        if sel:
            # Restore the selection in one go (row by row would redraw the map for every row).
            last = self.model.columnCount() - 1
            selection = QItemSelection()
            for r, t in enumerate(self.model.trips):
                if t.id in sel:
                    selection.select(self.model.index(r, 0), self.model.index(r, last))
            sm = self.table.selectionModel()
            sm.blockSignals(True)
            sm.select(selection, QItemSelectionModel.SelectionFlag.Select)
            sm.blockSignals(False)
            self.table.viewport().update()
        self.update_status()
        self.show_places_on(self.trip_map, fit=False, labels=False)
        self._trip_selection_changed()

    def selected_trips(self) -> list[Trip]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.trips[r] for r in rows]

    def selected_trip_ids(self) -> list[int]:
        return [t.id for t in self.selected_trips()] if hasattr(self, "table") else []

    def show_selected_on_map(self) -> None:
        trips = self.selected_trips()
        if not trips:
            trips = sorted(self.model.trips, key=lambda t: t.start_time)[-10:]
        types = {t["id"]: t for t in self.db.trip_types()}
        payload = []
        for t in trips[:200]:
            pts = self.db.trip_points(t.id)
            step = max(1, len(pts) // 1500)
            tt = types.get(t.trip_type_id)
            label = f"{t.start_time.astimezone():%Y-%m-%d %H:%M} {t.start_label()} → {t.end_label()}"
            payload.append({
                "coords": [[p.lat, p.lon] for p in pts[::step]] + ([[pts[-1].lat, pts[-1].lon]] if pts else []),
                "color": tt["color"] if tt else "#cc3333", "label": label,
                "start": [t.start_lat, t.start_lon] if t.start_lat is not None else None,
                "end": [t.end_lat, t.end_lon] if t.end_lat is not None else None,
                "startLabel": "Start: " + t.start_label(), "endLabel": "End: " + t.end_label(),
                "pois": [[p.lat, p.lon] for p in pts if p.poi],
            })
        self.trip_map.show_trips(payload, fit=True)

    def show_places_on(self, mv: MapView, fit: bool, labels: bool) -> None:
        mv.show_places([{"lat": p["lat"], "lon": p["lon"], "name": p["name"], "radius": p["radius_m"],
                         "permanent": labels} for p in self.db.places()], fit=fit)

    def trip_menu(self, pos) -> None:
        trips = self.selected_trips()
        if not trips:
            return
        m = QMenu(self)
        mark = m.addMenu("Mark as")
        for t in self.db.trip_types():
            mark.addAction(t["name"]).triggered.connect(lambda _=False, tid=t["id"]: self.mark(trips, tid))
        mark.addSeparator()
        mark.addAction("Unmarked").triggered.connect(lambda: self.mark(trips, None))
        m.addAction("Set reason…").triggered.connect(lambda: self.set_reason(trips))
        m.addSeparator()
        if len(trips) == 1 and trips[0].start_lat is not None:
            m.addAction("Name start location…").triggered.connect(lambda: self.name_location(trips[0], "start"))
            m.addAction("Name end location…").triggered.connect(lambda: self.name_location(trips[0], "end"))
        m.addAction("Look up street addresses").triggered.connect(lambda: self.geocode_trips(trips))
        m.addAction("Suggest names for unnamed places…").triggered.connect(self.suggest_places)
        m.addSeparator()
        if len(trips) > 1:
            m.addAction("Combine into one trip").triggered.connect(lambda: self.merge(trips))
        if len(trips) == 1 and trips[0].has_points:
            m.addAction("Split trip…").triggered.connect(lambda: self.split(trips[0]))
        m.addAction("Export as GPX…").triggered.connect(self.export_gpx)
        m.addAction("Export as KML…").triggered.connect(self.export_kml)
        m.addSeparator()
        m.addAction(f"Delete {len(trips)} trip(s)").triggered.connect(lambda: self.delete_trips(trips))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def mark(self, trips: list[Trip], type_id) -> None:
        for t in trips:
            self.db.update_trip(t.id, trip_type_id=type_id, auto_marked=0)
        self.refresh_trips()

    def set_reason(self, trips: list[Trip]) -> None:
        text, ok = QInputDialog.getText(self, "Reason", f"Reason for {len(trips)} trip(s):", text=trips[0].reason)
        if ok:
            for t in trips:
                self.db.update_trip(t.id, reason=text.strip())
            self.refresh_trips()

    def name_location(self, t: Trip, which: str) -> None:
        lat, lon = (t.start_lat, t.start_lon) if which == "start" else (t.end_lat, t.end_lon)
        current = t.start_desc if which == "start" else t.end_desc
        name, ok = QInputDialog.getText(self, "Name location",
                                        "Name for this location (used for all trips that start or end here):",
                                        text=current)
        if not ok or not name.strip():
            return
        self.db.name_place(lat, lon, name.strip(), t.start_place_id if which == "start" else t.end_place_id)
        self.refresh_trips()

    def merge(self, trips: list[Trip]) -> None:
        try:
            self.db.merge_trips([t.id for t in trips])
        except ValueError as e:
            QMessageBox.warning(self, "Combine trips", str(e))
        self.refresh_trips()

    def split(self, t: Trip) -> None:
        pts = self.db.trip_points(t.id)
        if len(pts) < 3:
            QMessageBox.information(self, "Split trip", "This trip has too few GPS points to split.")
            return
        d = SplitDialog(self.db, pts, f"{t.start_label()} → {t.end_label()}", self)
        if d.exec():
            try:
                self.db.split_trip(t.id, d.index)
            except ValueError as e:
                QMessageBox.warning(self, "Split trip", str(e))
            self.refresh_trips()

    def delete_trips(self, trips: list[Trip]) -> None:
        if QMessageBox.question(self, "Delete trips", f"Delete {len(trips)} trip(s)? This cannot be undone."
                                ) == QMessageBox.StandardButton.Yes:
            self.db.delete_trips([t.id for t in trips])
            self.refresh_trips()

    def add_manual_trip(self) -> None:
        if not self.db.vehicles():
            QMessageBox.information(self, "Add trip", "Add a vehicle first.")
            return
        d = ManualTripDialog(self.db, self.vehicle.currentData(), self)
        if d.exec():
            v = d.values()
            if v["end"] <= v["start"]:
                QMessageBox.warning(self, "Add trip", "The end time must be after the start time.")
                return
            self.db.add_manual_trip(**v)
            self.refresh_trips()

    # -- geocoding ------------------------------------------------------------------
    def geocode_trips(self, trips: list[Trip], quiet: bool = False) -> None:
        todo: dict[tuple[float, float], list[tuple[int, str]]] = {}
        for t in trips:
            for which, lat, lon, place, desc in (("start_desc", t.start_lat, t.start_lon, t.start_place_id,
                                                  t.start_desc),
                                                 ("end_desc", t.end_lat, t.end_lon, t.end_place_id, t.end_desc)):
                if lat is None or place or desc:
                    continue
                cached = self.db.cached_address(lat, lon)
                if cached:
                    self.db.update_trip(t.id, **{which: cached})
                else:
                    todo.setdefault((round(lat, 5), round(lon, 5)), []).append((t.id, which))
        if not todo:
            self.refresh_trips()
            return
        coords = list(todo)
        db_path = self.db.path

        def run(job: Job):
            local = Database(db_path)  # sqlite connections are per thread
            out = {}
            for i, (lat, lon) in enumerate(coords):
                if job.cancelled():
                    break
                job.progress(i, len(coords))
                try:
                    out[(lat, lon)] = reverse_geocode(local, lat, lon)
                except OSError as e:
                    job.say(f"Address lookup failed: {e}")
                    break
                time.sleep(1.1)  # Nominatim usage policy: max 1 request per second
            local.conn.close()
            return out

        def done(result):
            for key, addr in result.items():
                for tid, which in todo[key]:
                    self.db.update_trip(tid, **{which: addr})
            self.statusBar().showMessage(f"Looked up {len(result)} address(es).", 5000)
            self.refresh_trips()

        self.statusBar().showMessage(f"Looking up {len(coords)} address(es)…")
        self._run(run, done, quiet)

    def _run(self, fn, done, quiet: bool = False) -> None:
        job = Job(fn, self)
        job.succeeded.connect(done)
        if not quiet:
            job.failed.connect(lambda msg: QMessageBox.warning(self, "Tripbook", msg))
        job.message.connect(lambda s: self.statusBar().showMessage(s, 8000))
        job.finished.connect(lambda: self.jobs.remove(job) if job in self.jobs else None)
        self.jobs.append(job)
        job.start()

    # -- device ---------------------------------------------------------------------
    def open_device(self) -> None:
        d = DeviceDialog(self.db, self.data_dir, self.vehicle.currentData(), self)
        d.downloaded.connect(lambda vid, raw, path: self.import_raw(vid, raw, path, d))
        d.exec()
        self.refresh_all()

    def import_raw(self, vehicle_id: int, raw: bytes, path: str, dialog: DeviceDialog | None = None) -> int:
        pts = decode_log(raw)
        before = {t.id for t in self.db.trips(vehicle_id)}
        n = import_points(self.db, vehicle_id, pts, "device")
        self.db.x("INSERT INTO download(vehicle_id, time, path, bytes, trips_added) VALUES (?,?,?,?,?)",
                  (vehicle_id, datetime.now().isoformat(timespec="seconds"), path, len(raw), n))
        msg = f"Decoded {len(pts)} GPS points: {n} new trip(s) imported."
        if dialog:
            dialog.report(msg)
            dialog.logbox.appendPlainText(msg)
        else:
            self.statusBar().showMessage(msg, 10000)
        if n and self.db.setting("auto_geocode", "0") == "1":
            new = [t for t in self.db.trips(vehicle_id) if t.id not in before]
            self.geocode_trips(new, quiet=True)
        self.refresh_all()
        return n

    # -- import / export ------------------------------------------------------------
    def _pick_vehicle(self) -> int | None:
        vid = self.vehicle.currentData()
        if vid:
            return vid
        vs = self.db.vehicles()
        if not vs:
            QMessageBox.information(self, "Import", "Add a vehicle first.")
            return None
        if len(vs) == 1:
            return vs[0]["id"]
        name, ok = QInputDialog.getItem(self, "Import", "Import into vehicle:", [v["name"] for v in vs], 0, False)
        return next((v["id"] for v in vs if v["name"] == name), None) if ok else None

    def import_dump(self) -> None:
        vid = self._pick_vehicle()
        if not vid:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import tracker dump", str(self.data_dir / "downloads"),
                                              "Tracker dumps (*.bin *.dat *.log);;All files (*)")
        if path:
            self.import_raw(vid, Path(path).read_bytes(), path)

    def import_gpx(self) -> None:
        vid = self._pick_vehicle()
        if not vid:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Import GPX", str(Path.home()), "GPX files (*.gpx)")
        total = 0
        for p in paths:
            try:
                total += import_points(self.db, vid, read_gpx(Path(p)), "gpx")
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Import GPX", f"{p}: {e}")
        if paths:
            self.statusBar().showMessage(f"Imported {total} trip(s) from GPX.", 8000)
            self.refresh_all()

    def import_llb(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Little LogBook database", str(Path.home()),
                                              "Little LogBook databases and backups (*.s3db *.lbk *.db *.db3 *.sqlite);;All files (*)")
        if not path:
            return
        try:
            stats = import_littlelogbook_db(self.db, Path(path))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Import", f"Could not import this database:\n{e}")
            return
        QMessageBox.information(self, "Import", "Imported " + ", ".join(f"{v} {k}" for k, v in stats.items()))
        self.refresh_all()

    def export_gpx(self) -> None:
        trips = self.selected_trips() or self.model.trips
        if not trips:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export GPX", str(Path.home() / "trips.gpx"), "GPX (*.gpx)")
        if path:
            write_gpx(Path(path), [(f"{t.start_time.astimezone():%Y-%m-%d %H:%M} {t.start_label()} → "
                                    f"{t.end_label()}", self.db.trip_points(t.id)) for t in trips])

    def export_kml(self) -> None:
        self._write_kml(self.selected_trips() or self.model.trips)

    def _write_kml(self, trips: list[Trip]) -> None:
        if not trips:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export KML", str(Path.home() / "trips.kml"),
                                              "Google Earth KML (*.kml)")
        if path:
            types = {t["id"]: t["color"] for t in self.db.trip_types()}
            write_kml(Path(path), [(f"{t.start_time.astimezone():%Y-%m-%d %H:%M} {t.start_label()} → "
                                    f"{t.end_label()}", types.get(t.trip_type_id, "#cc3333"),
                                    self.db.trip_points(t.id)) for t in trips if t.has_points])
            self.statusBar().showMessage(f"Saved {path}", 8000)

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", str(Path.home() / "logbook.csv"), "CSV (*.csv)")
        if path:
            export_trips_csv(self.db, self.filtered_trips(), Path(path))

    def backup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Back up database",
                                              str(Path.home() / f"tripbook-{date.today()}.db"), "SQLite (*.db)")
        if path:
            self.db.backup_to(Path(path))
            self.statusBar().showMessage(f"Backed up to {path}", 8000)

    def restore(self) -> None:
        start = self.backup_dir if self.backup_dir.exists() else Path.home()
        path, _ = QFileDialog.getOpenFileName(self, "Restore from backup", str(start), "Tripbook backups (*.db)")
        if not path or QMessageBox.question(
                self, "Restore", "Replace all current data with this backup? The current data is backed up to "
                f"{self.backup_dir} first.") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.db.auto_backup(self.backup_dir, max(int(self.db.setting("backup_keep", "5")), 1) + 1)
            self.db.restore_from(Path(path))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Restore", str(e))
            return
        self.statusBar().showMessage(f"Restored {path}", 8000)
        self.refresh_all()

    def open_settings(self) -> None:
        d = SettingsDialog(self.db, self.data_dir, self)
        if d.exec():
            old_tiles = self.tiles()
            d.save()
            if self.tiles() != old_tiles:
                QMessageBox.information(self, "Settings", "The custom map is available after restarting Tripbook.")
            self.apply_period()

    # -- places tab -----------------------------------------------------------------
    def _build_places_tab(self) -> QWidget:
        self.places_table = table_widget(["Name", "Address", "Latitude", "Longitude", "Radius (m)", "Trips"])
        self.places_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.places_table.itemChanged.connect(self._place_edited)
        self.places_table.itemSelectionChanged.connect(self._place_selected)
        self.places_map = self.new_map()
        self.places_map.clicked.connect(self._map_add_place)
        btns = QHBoxLayout()
        for text, fn in [("Delete", self.delete_places), ("Look up address", self.geocode_places),
                         ("Suggest names for unnamed places…", self.suggest_places)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(QLabel("Click on the map to add a place · double-click a cell to edit it"))
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(self.places_table)
        ll.addLayout(btns)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.places_map)
        split.setSizes([620, 700])
        return split

    def refresh_places(self) -> None:
        counts = {r[0]: r[1] for r in self.db.q(
            "SELECT pid, COUNT(*) FROM (SELECT start_place_id pid FROM trip UNION ALL SELECT end_place_id FROM trip) "
            "WHERE pid IS NOT NULL GROUP BY pid")}
        places = self.db.places()
        self.places_table.blockSignals(True)
        fill_table(self.places_table, [[p["name"], p["address"], f"{p['lat']:.6f}", f"{p['lon']:.6f}",
                                        f"{p['radius_m']:.0f}", counts.get(p["id"], 0)] for p in places],
                   [p["id"] for p in places])
        self.places_table.blockSignals(False)
        self.show_places_on(self.places_map, fit=True, labels=True)

    def _place_edited(self, item: QTableWidgetItem) -> None:
        pid = item.data(Qt.ItemDataRole.UserRole)
        col = ["name", "address", "lat", "lon", "radius_m", None][item.column()]
        if not col:
            return
        val = item.text().strip()
        try:
            if col in ("lat", "lon", "radius_m"):
                val = float(val)
        except ValueError:
            self.refresh_places()
            return
        self.db.x(f"UPDATE place SET {col}=? WHERE id=?", (val, pid))
        self.db.resolve_places()
        self.refresh_places()

    def _place_selected(self) -> None:
        ids = selected_ids(self.places_table)
        if ids:
            p = self.db.q("SELECT * FROM place WHERE id=?", (ids[0],))[0]
            self.places_map.focus(p["lat"], p["lon"], 17)

    def _map_add_place(self, lat: float, lon: float) -> None:
        name, ok = QInputDialog.getText(self, "Add place", f"Name for the place at {lat:.5f}, {lon:.5f}:")
        if ok and name.strip():
            self.db.x("INSERT INTO place(name, lat, lon, radius_m) VALUES (?,?,?,?)",
                      (name.strip(), lat, lon, PLACE_RADIUS_M))
            self.db.resolve_places()
            self.refresh_places()

    def suggest_places(self) -> None:
        SuggestPlacesDialog(self.db, self).exec()
        self.refresh_all()

    def delete_places(self) -> None:
        ids = selected_ids(self.places_table)
        if ids and QMessageBox.question(self, "Delete places", f"Delete {len(ids)} place(s) and their rules?"
                                        ) == QMessageBox.StandardButton.Yes:
            self.db.delete_places(ids)
            self.refresh_places()

    def geocode_places(self) -> None:
        ids = selected_ids(self.places_table)
        rows = [self.db.q("SELECT * FROM place WHERE id=?", (i,))[0] for i in ids]
        coords = [(r["id"], r["lat"], r["lon"]) for r in rows]
        db_path = self.db.path

        def run(job: Job):
            local = Database(db_path)
            out = {}
            for pid, lat, lon in coords:
                out[pid] = reverse_geocode(local, lat, lon)
                time.sleep(1.1)
            local.conn.close()
            return out

        def done(result):
            for pid, addr in result.items():
                self.db.x("UPDATE place SET address=? WHERE id=?", (addr, pid))
            self.refresh_places()

        if coords:
            self._run(run, done)

    # -- rules tab ------------------------------------------------------------------
    def _build_rules_tab(self) -> QWidget:
        self.rules_table = table_widget(["Vehicle", "From", "To", "Return trip too", "Mark as", "Outbound reason",
                                         "Return reason"])
        self.rules_table.doubleClicked.connect(lambda _: self.edit_rule())
        btns = QHBoxLayout()
        for text, fn in [("Add rule…", self.add_rule), ("Edit…", self.edit_rule), ("Delete", self.delete_rules),
                         ("Re-apply rules", self.reapply_rules)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        info = QLabel("Rules automatically mark trips between two named places. Trips you mark by hand are never "
                      "changed by rules.")
        info.setStyleSheet("color:#777")
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(info)
        lay.addWidget(self.rules_table)
        lay.addLayout(btns)
        return w

    def refresh_rules(self) -> None:
        rows = self.db.q("SELECT r.*, COALESCE(v.name, 'All vehicles') AS vname, a.name AS a, b.name AS b, "
                         "t.name AS tname FROM rule r LEFT JOIN vehicle v ON v.id=r.vehicle_id "
                         "JOIN place a ON a.id=r.start_place_id JOIN place b ON b.id=r.end_place_id "
                         "JOIN trip_type t ON t.id=r.trip_type_id ORDER BY a.name")
        fill_table(self.rules_table, [[r["vname"], r["a"], r["b"], "yes" if r["reverse"] else "no", r["tname"],
                                       r["reason"], (r["reverse_reason"] or r["reason"]) if r["reverse"] else ""]
                                      for r in rows], [r["id"] for r in rows])

    def add_rule(self) -> None:
        if len(self.db.places()) < 1:
            QMessageBox.information(self, "Rules", "Name some places first (right-click a trip → Name start "
                                                   "location, or click on the Places map).")
            return
        d = RuleDialog(self.db, parent=self)
        if d.exec():
            v = d.values()
            self.db.x("INSERT INTO rule(vehicle_id, start_place_id, end_place_id, reverse, trip_type_id, reason, "
                      "reverse_reason) VALUES (:vehicle_id, :start_place_id, :end_place_id, :reverse, :trip_type_id, "
                      ":reason, :reverse_reason)", v)
            self.reapply_rules()

    def edit_rule(self) -> None:
        ids = selected_ids(self.rules_table)
        if not ids:
            return
        d = RuleDialog(self.db, self.db.q("SELECT * FROM rule WHERE id=?", (ids[0],))[0], self)
        if d.exec():
            v = d.values()
            v["id"] = ids[0]
            self.db.x("UPDATE rule SET vehicle_id=:vehicle_id, start_place_id=:start_place_id, "
                      "end_place_id=:end_place_id, reverse=:reverse, trip_type_id=:trip_type_id, reason=:reason, "
                      "reverse_reason=:reverse_reason WHERE id=:id", v)
            self.reapply_rules()

    def delete_rules(self) -> None:
        ids = selected_ids(self.rules_table)
        self.db.conn.executemany("DELETE FROM rule WHERE id=?", [(i,) for i in ids])
        self.db.conn.commit()
        self.reapply_rules()

    def reapply_rules(self) -> None:
        n = self.db.apply_rules()
        self.statusBar().showMessage(f"{n} trip(s) marked by rules.", 6000)
        self.refresh_rules()

    # -- expenses tab ---------------------------------------------------------------
    def _build_expenses_tab(self) -> QWidget:
        self.exp_table = table_widget(["Date", "Vehicle", "Category", "Description", "Amount", "Litres",
                                       "Odometer"])
        self.exp_table.doubleClicked.connect(lambda _: self.edit_expense())
        self.exp_total = QLabel()
        btns = QHBoxLayout()
        for text, fn in [("Add expense…", self.add_expense), ("Edit…", self.edit_expense),
                         ("Delete", self.delete_expenses)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.exp_total)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(self.exp_table)
        lay.addLayout(btns)
        return w

    def filtered_expenses(self):
        s, e = self.period_range()
        sql = "SELECT x.*, v.name AS vname FROM expense x JOIN vehicle v ON v.id=x.vehicle_id WHERE date>=? AND date<?"
        args = [s.date().isoformat(), e.date().isoformat()]
        if self.vehicle.currentData():
            sql += " AND vehicle_id=?"
            args.append(self.vehicle.currentData())
        return self.db.q(sql + " ORDER BY date", args)

    def refresh_expenses(self) -> None:
        rows = self.filtered_expenses()
        cur = self.db.setting("currency", "€")
        fill_table(self.exp_table, [[r["date"], r["vname"], r["category"], r["description"],
                                     f"{cur}{r['amount']:,.2f}", f"{r['volume']:.2f}" if r["volume"] else "",
                                     f"{self.db.to_unit(r['odometer']):,.0f}" if r["odometer"] else ""]
                                    for r in rows],
                   [r["id"] for r in rows])
        total = sum(r["amount"] for r in rows)
        litres = sum(r["volume"] for r in rows)
        self.exp_total.setText(f"Total {cur}{total:,.2f}" + (f" · {litres:,.1f} l fuel" if litres else ""))

    def add_expense(self) -> None:
        if not self.db.vehicles():
            QMessageBox.information(self, "Expenses", "Add a vehicle first.")
            return
        d = ExpenseDialog(self.db, vehicle_id=self.vehicle.currentData(), parent=self)
        if d.exec():
            self.db.x("INSERT INTO expense(vehicle_id, date, category, description, amount, volume, odometer) "
                      "VALUES (:vehicle_id, :date, :category, :description, :amount, :volume, :odometer)", d.values())
            self.refresh_expenses()

    def edit_expense(self) -> None:
        ids = selected_ids(self.exp_table)
        if not ids:
            return
        row = self.db.q("SELECT * FROM expense WHERE id=?", (ids[0],))[0]
        d = ExpenseDialog(self.db, row, parent=self)
        if d.exec():
            v = d.values()
            v["id"] = ids[0]
            self.db.x("UPDATE expense SET vehicle_id=:vehicle_id, date=:date, category=:category, "
                      "description=:description, amount=:amount, volume=:volume, odometer=:odometer WHERE id=:id", v)
            self.refresh_expenses()

    def delete_expenses(self) -> None:
        ids = selected_ids(self.exp_table)
        if ids and QMessageBox.question(self, "Delete", f"Delete {len(ids)} expense(s)?"
                                        ) == QMessageBox.StandardButton.Yes:
            self.db.conn.executemany("DELETE FROM expense WHERE id=?", [(i,) for i in ids])
            self.db.conn.commit()
            self.refresh_expenses()

    # -- reminders tab --------------------------------------------------------------
    def _build_reminders_tab(self) -> QWidget:
        self.rem_table = table_widget(["Status", "Vehicle", "Reminder", "Due date", "Due odometer", "Repeats"])
        self.rem_table.doubleClicked.connect(lambda _: self.edit_reminder())
        btns = QHBoxLayout()
        for text, fn in [("Add reminder…", self.add_reminder), ("Edit…", self.edit_reminder),
                         ("Mark done", self.done_reminder), ("Delete", self.delete_reminders)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(self.rem_table)
        lay.addLayout(btns)
        return w

    def refresh_reminders(self) -> None:
        items = self.db.due_reminders()
        order = {"overdue": 0, "due soon": 1, "": 2}
        items.sort(key=lambda x: (order[x[1]], x[0]["due_date"] or "9999"))
        rows = []
        for r, status in items:
            rep = []
            if r["repeat_months"]:
                rep.append(f"every {r['repeat_months']} months")
            if r["repeat_km"]:
                rep.append(f"every {self.db.to_unit(r['repeat_km']):,.0f} {self.db.unit}")
            rows.append([status.upper() or "ok", r["vehicle"], r["title"], r["due_date"] or "",
                         f"{self.db.to_unit(r['due_odometer']):,.0f} {self.db.unit}" if r["due_odometer"] else "",
                         ", ".join(rep)])
        fill_table(self.rem_table, rows, [r["id"] for r, _ in items])
        colors = {"OVERDUE": "#c0392b", "DUE SOON": "#d68910"}
        for i in range(self.rem_table.rowCount()):
            item = self.rem_table.item(i, 0)
            if item.text() in colors:
                item.setForeground(QBrush(QColor(colors[item.text()])))

    def add_reminder(self) -> None:
        if not self.db.vehicles():
            QMessageBox.information(self, "Reminders", "Add a vehicle first.")
            return
        d = ReminderDialog(self.db, vehicle_id=self.vehicle.currentData(), parent=self)
        if d.exec():
            self.db.x("INSERT INTO reminder(vehicle_id, title, due_date, due_odometer, repeat_months, repeat_km, "
                      "notice_days, notice_km) VALUES (:vehicle_id, :title, :due_date, :due_odometer, "
                      ":repeat_months, :repeat_km, :notice_days, :notice_km)", d.values())
            self.refresh_reminders()

    def edit_reminder(self) -> None:
        ids = selected_ids(self.rem_table)
        if not ids:
            return
        row = self.db.q("SELECT * FROM reminder WHERE id=?", (ids[0],))[0]
        d = ReminderDialog(self.db, row, parent=self)
        if d.exec():
            v = d.values()
            v["id"] = ids[0]
            self.db.x("UPDATE reminder SET vehicle_id=:vehicle_id, title=:title, due_date=:due_date, "
                      "due_odometer=:due_odometer, repeat_months=:repeat_months, repeat_km=:repeat_km, "
                      "notice_days=:notice_days, notice_km=:notice_km WHERE id=:id", v)
            self.refresh_reminders()

    def done_reminder(self) -> None:
        for i in selected_ids(self.rem_table):
            self.db.complete_reminder(i)
        self.refresh_reminders()

    def delete_reminders(self) -> None:
        ids = selected_ids(self.rem_table)
        self.db.conn.executemany("DELETE FROM reminder WHERE id=?", [(i,) for i in ids])
        self.db.conn.commit()
        self.refresh_reminders()

    def check_reminders(self) -> None:
        due = [(r, s) for r, s in self.db.due_reminders() if s]
        if due:
            self.statusBar().showMessage(
                "Reminders: " + "; ".join(f"{r['vehicle']}: {r['title']} ({s})" for r, s in due[:4]), 30000)

    # -- report tab -----------------------------------------------------------------
    def _build_report_tab(self) -> QWidget:
        self.report_view = QTextBrowser()
        # Report options are settings, so they survive a restart (an issue fixed in Little LogBook 2.4.001).
        self.report_layout = QComboBox()
        for key, text in REPORT_LAYOUTS.items():
            self.report_layout.addItem(text, key)
        self.report_layout.setCurrentIndex(max(0, self.report_layout.findData(
            self.db.setting("report_layout", "detailed"))))
        self.report_expenses = QCheckBox("Include expenses")
        self.report_expenses.setChecked(self.db.setting("report_expenses", "1") == "1")
        self.report_layout.currentIndexChanged.connect(self._report_options_changed)
        self.report_expenses.toggled.connect(self._report_options_changed)
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Layout"))
        opts.addWidget(self.report_layout)
        opts.addWidget(self.report_expenses)
        opts.addStretch(1)
        opts.addWidget(QLabel("The report follows the vehicle, period and type filters above."))
        btns = QHBoxLayout()
        for text, fn in [("Save as PDF…", self.report_pdf), ("Print…", self.report_print),
                         ("Export CSV…", self.export_csv), ("Export KML…", self.export_report_kml)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addLayout(opts)
        lay.addWidget(self.report_view)
        lay.addLayout(btns)
        return w

    def _report_options_changed(self) -> None:
        self.db.set_setting("report_layout", self.report_layout.currentData())
        self.db.set_setting("report_expenses", "1" if self.report_expenses.isChecked() else "0")
        self.refresh_report()

    def _report_html(self) -> str:
        vid = self.vehicle.currentData()
        v = self.db.q("SELECT * FROM vehicle WHERE id=?", (vid,))[0] if vid else None
        return report_html(self.db, v, self.filtered_trips(), self.filtered_expenses(), self.period_text(),
                           self.report_layout.currentData(), self.report_expenses.isChecked())

    def export_report_kml(self) -> None:
        self._write_kml(self.filtered_trips())

    def refresh_report(self) -> None:
        self.report_view.setHtml(self._report_html())

    def _document(self) -> QTextDocument:
        doc = QTextDocument()
        doc.setHtml(self._report_html())
        return doc

    def _printer(self) -> QPrinter:
        p = QPrinter(QPrinter.PrinterMode.HighResolution)
        p.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        p.setPageOrientation(QPageLayout.Orientation.Landscape)
        return p

    def report_pdf(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save report", str(Path.home() / "logbook.pdf"), "PDF (*.pdf)")
        if path:
            p = self._printer()
            p.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            p.setOutputFileName(path)
            self._document().print_(p)
            self.statusBar().showMessage(f"Saved {path}", 8000)

    def report_print(self) -> None:
        p = self._printer()
        if QPrintDialog(p, self).exec():
            self._document().print_(p)

    # -- vehicles tab ---------------------------------------------------------------
    def _build_vehicles_tab(self) -> QWidget:
        self.veh_table = table_widget(["Name", "Registration", "Make / model", "Year", "Opening odometer",
                                       "Current odometer", "Trips", "Status"])
        self.veh_table.doubleClicked.connect(lambda _: self.edit_vehicle())
        self.veh_table.itemSelectionChanged.connect(self.refresh_odometers)
        btns = QHBoxLayout()
        for text, fn in [("Add vehicle…", self.add_vehicle), ("Edit…", self.edit_vehicle),
                         ("Delete", self.delete_vehicle)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        self.odo_table = table_widget(["Date", "Odometer reading"])
        self.rate_table = table_widget(["Vehicle", "Valid from", "Description", "Rate"])
        self.rate_table.doubleClicked.connect(lambda _: self.edit_rate())
        rbtns = QHBoxLayout()
        for text, fn in [("Add rate…", self.add_rate), ("Edit…", self.edit_rate), ("Delete rate", self.delete_rates)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            rbtns.addWidget(b)
        rbtns.addStretch(1)
        obtns = QHBoxLayout()
        for text, fn in [("Add reading…", self.add_odometer), ("Delete reading", self.delete_odometer)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            obtns.addWidget(b)
        obtns.addStretch(1)
        info = QLabel("Odometer readings correct the logbook: later trips continue from the most recent reading.")
        info.setStyleSheet("color:#777")
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(self.veh_table, 2)
        lay.addLayout(btns)
        lay.addWidget(QLabel("<b>Odometer readings</b> (selected vehicle)"))
        lay.addWidget(info)
        lay.addWidget(self.odo_table, 1)
        lay.addLayout(obtns)
        lay.addWidget(QLabel("<b>Mileage rates</b> – the business mileage claim on reports uses the rate valid on "
                             "each trip's date"))
        lay.addWidget(self.rate_table, 1)
        lay.addLayout(rbtns)
        return w

    def refresh_vehicles(self) -> None:
        f, unit = distance_factor(self.db)
        counts = {r[0]: r[1] for r in self.db.q("SELECT vehicle_id, COUNT(*) FROM trip GROUP BY vehicle_id")}
        vs = self.db.vehicles()
        fill_table(self.veh_table, [[v["name"], v["registration"], v["make"], v["year"],
                                     f"{v['odometer_start'] * f:,.0f} {unit}",
                                     f"{self.db.current_odometer(v['id']) * f:,.0f} {unit}", counts.get(v["id"], 0),
                                     "closed" if v["closed"] else "active"] for v in vs], [v["id"] for v in vs])
        self.refresh_odometers()

    def refresh_odometers(self) -> None:
        ids = selected_ids(self.veh_table)
        rows = self.db.q("SELECT * FROM odometer WHERE vehicle_id=? ORDER BY date", (ids[0],)) if ids else []
        fill_table(self.odo_table, [[f"{from_iso(r['date']).astimezone():%Y-%m-%d %H:%M}" if "T" in r["date"]
                                     else r["date"], f"{self.db.to_unit(r['reading']):,.0f} {self.db.unit}"]
                                    for r in rows], [r["id"] for r in rows])
        rates = self.db.q("SELECT m.*, COALESCE(v.name, 'All vehicles') AS vname FROM mileage_rate m "
                          "LEFT JOIN vehicle v ON v.id=m.vehicle_id ORDER BY m.from_date")
        cur = self.db.setting("currency", "")
        fill_table(self.rate_table, [[r["vname"], r["from_date"], r["description"],
                                      f"{cur}{r['rate'] / self.db.to_unit(1.0):,.4f}/{self.db.unit}"]
                                     for r in rates], [r["id"] for r in rates])

    def add_vehicle(self) -> None:
        d = VehicleDialog(self.db, parent=self)
        if d.exec():
            self.db.x("INSERT INTO vehicle(name, registration, make, year, odometer_start, device_serial, "
                      "device_password, closed) VALUES (:name, :registration, :make, :year, :odometer_start, "
                      ":device_serial, :device_password, :closed)", d.values())
            self.refresh_all()

    def edit_vehicle(self) -> None:
        ids = selected_ids(self.veh_table)
        if not ids:
            return
        row = self.db.q("SELECT * FROM vehicle WHERE id=?", (ids[0],))[0]
        d = VehicleDialog(self.db, row, self)
        if d.exec():
            v = d.values()
            v["id"] = ids[0]
            self.db.x("UPDATE vehicle SET name=:name, registration=:registration, make=:make, year=:year, "
                      "odometer_start=:odometer_start, device_serial=:device_serial, device_password=:device_password, closed=:closed WHERE id=:id", v)
            self.refresh_all()

    def delete_vehicle(self) -> None:
        ids = selected_ids(self.veh_table)
        if not ids:
            return
        n = self.db.q("SELECT COUNT(*) FROM trip WHERE vehicle_id=?", (ids[0],))[0][0]
        if QMessageBox.question(self, "Delete vehicle", f"Delete this vehicle and its {n} trips, expenses and "
                                "reminders? This cannot be undone.") == QMessageBox.StandardButton.Yes:
            self.db.x("DELETE FROM vehicle WHERE id=?", (ids[0],))
            self.refresh_all()

    def add_odometer(self) -> None:
        ids = selected_ids(self.veh_table)
        if not ids:
            QMessageBox.information(self, "Odometer", "Select a vehicle first.")
            return
        d = OdometerDialog(self.db, self.db.current_odometer(ids[0]), self)
        if d.exec():
            when, km = d.values()
            self.db.x("INSERT INTO odometer(vehicle_id, date, reading) VALUES (?,?,?)", (ids[0], to_iso(when), km))
            self.refresh_vehicles()

    def add_rate(self) -> None:
        d = MileageRateDialog(self.db, vehicle_id=(selected_ids(self.veh_table) or [None])[0], parent=self)
        if d.exec():
            self.db.x("INSERT INTO mileage_rate(vehicle_id, from_date, description, rate) "
                      "VALUES (:vehicle_id, :from_date, :description, :rate)", d.values())
            self.refresh_odometers()

    def edit_rate(self) -> None:
        ids = selected_ids(self.rate_table)
        if not ids:
            return
        d = MileageRateDialog(self.db, self.db.q("SELECT * FROM mileage_rate WHERE id=?", (ids[0],))[0], parent=self)
        if d.exec():
            v = d.values()
            v["id"] = ids[0]
            self.db.x("UPDATE mileage_rate SET vehicle_id=:vehicle_id, from_date=:from_date, "
                      "description=:description, rate=:rate WHERE id=:id", v)
            self.refresh_odometers()

    def delete_rates(self) -> None:
        for i in selected_ids(self.rate_table):
            self.db.x("DELETE FROM mileage_rate WHERE id=?", (i,))
        self.refresh_odometers()

    def delete_odometer(self) -> None:
        for i in selected_ids(self.odo_table):
            self.db.x("DELETE FROM odometer WHERE id=?", (i,))
        self.refresh_vehicles()
