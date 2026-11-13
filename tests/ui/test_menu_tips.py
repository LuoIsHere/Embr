"""Menus whose items carry a tooltip show it: a QMenu shows item tooltips only when asked."""

from pathlib import Path

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QApplication, QMenu

from embr.ui.window import EmbrWindow

# pyright: reportPrivateUsage=false


def test_the_new_menu_shows_its_tips(window: EmbrWindow) -> None:
    menu = window.sidebar_panel.new_menu
    assert menu.toolTipsVisible()
    assert menu.actions()[0].toolTip() == "fixed, add entries by hand"


def test_the_entry_menu_shows_its_tips(window: EmbrWindow, tmp_path: Path) -> None:
    file = tmp_path / "app.bin"
    file.write_bytes(b"\x00" * 64)
    firmware = window.library.create_firmware("fw")
    window.library.add_files(firmware.id, [file])
    window.select_firmware(firmware.id)
    window.show()
    QApplication.processEvents()
    shown: list[bool] = []

    def look_and_close() -> None:
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        shown.append(menu.toolTipsVisible())
        menu.close()

    QTimer.singleShot(0, look_and_close)
    entries = window.firmware_view.entries
    entries._context_menu(QPoint(5, entries.table.rowViewportPosition(0) + 5))

    assert shown == [True]
