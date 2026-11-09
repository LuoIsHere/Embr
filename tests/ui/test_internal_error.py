"""An exception raised in a Qt slot reaches core.logs.crash (PySide passes it to
sys.excepthook): written to the crash log, shown as one line in the log panel, and the
first one of a run also in a dialog that can open the log folder."""

import faulthandler
import sys
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from embr.core.logs.crash import CrashLog
from embr.ui import dialogs
from embr.ui.window import EmbrWindow


def _silent(*_args: object) -> None:
    """A default hook that prints nothing (pytest's own would report the exception)."""


def test_a_slot_exception_is_logged_and_shown(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "excepthook", _silent)
    monkeypatch.setattr(threading, "excepthook", _silent)
    shown: list[str] = []

    def dialog(_parent: QWidget, crash_log: str) -> bool:
        shown.append(crash_log)
        return True  # Open log folder

    opened: list[None] = []
    monkeypatch.setattr(dialogs, "internal_error", dialog)
    monkeypatch.setattr(window, "_open_log_folder", lambda: opened.append(None))
    crash = CrashLog(tmp_path / "logs")
    crash.install()
    crash.on_error = window.report_internal_error
    try:

        def slot() -> None:
            raise ValueError("bad value")

        QTimer.singleShot(0, slot)
        QTimer.singleShot(0, slot)  # a second one in the same run: log panel only
        for _ in range(4):
            QApplication.processEvents()
    finally:
        crash.uninstall()
        faulthandler.enable()

    log = window.log_panel.log.toPlainText()
    assert log.count("[internal] unexpected error: ValueError: bad value; details in logs/") == 2
    (written,) = (tmp_path / "logs").glob("crash-*.log")
    assert "ValueError: bad value" in written.read_text(encoding="utf-8")
    assert shown == [written.name]
    assert opened == [None]
