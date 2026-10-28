"""Application entry point."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def data_dir() -> Path:
    """Where the logbook lives: $TRIPBOOK_DATA, else the platform's usual place for application data."""
    if os.environ.get("TRIPBOOK_DATA"):
        return Path(os.environ["TRIPBOOK_DATA"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Tripbook"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Tripbook"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "tripbook"


def main() -> None:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(d / "tripbook.log", maxBytes=2_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("tripbook")
    root.setLevel(logging.DEBUG if os.environ.get("TRIPBOOK_DEBUG") else logging.INFO)
    root.addHandler(handler)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .mainwindow import MainWindow

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("Tripbook")
    app.setOrganizationName("Tripbook")
    app.setDesktopFileName("tripbook")
    icon = Path(__file__).with_name("tripbook.svg")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow(d)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
