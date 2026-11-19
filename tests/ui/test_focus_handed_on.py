"""A button that has the focus and is then disabled or hidden (Flash when the build changes
or a job ends) hands the focus to the next widget, often an address field of the entry
table. Nobody types there, so the page does not wait for that field:
later refreshes show the firmware as it is. A field the user went into still waits."""

import os
import time
from pathlib import Path

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from embr.ui.window import EmbrWindow

# pyright: reportPrivateUsage=false


def _shown(window: EmbrWindow, tmp_path: Path) -> list[Path]:
    folder = tmp_path / "f"
    folder.mkdir()
    files = [folder / "boot.bin", folder / "app.bin"]
    for file in files:
        file.write_bytes(b"\x00" * 64)
    firmware_id = window.library.create_from_files(files, name="fw").id
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window)
    window.select_firmware(firmware_id)
    QApplication.processEvents()
    return files


def _focus(widget: QWidget) -> None:
    widget.setFocus()
    QApplication.processEvents()
    assert QApplication.focusWidget() is widget


def _change(file: Path) -> None:
    file.write_bytes(b"\x01" * 64)
    later = time.time() + 5
    os.utime(file, (later, later))


def _address_field(window: EmbrWindow) -> QLineEdit:
    fields = window.firmware_view.entries.table.findChildren(QLineEdit)
    assert fields
    return fields[0]


def _focus_in_page(window: EmbrWindow) -> bool:
    return window._page_field(QApplication.focusWidget()) is not None


def test_a_disabled_button_does_not_leave_the_page_waiting(
    window: EmbrWindow, tmp_path: Path
) -> None:
    files = _shown(window, tmp_path)
    flash = window.connection.flash
    flash.setEnabled(True)
    _focus(flash)

    flash.setEnabled(False)  # as the right panel does when the build changes
    QApplication.processEvents()

    assert not _focus_in_page(window)
    _change(files[1])
    window.changed()
    assert not window._workspace_stale
    assert window.firmware_view.banner.isVisible()  # the page shows the changed file


def test_a_hidden_button_does_not_leave_the_page_waiting(
    window: EmbrWindow, tmp_path: Path
) -> None:
    files = _shown(window, tmp_path)
    flash = window.connection.flash
    flash.setEnabled(True)
    _focus(flash)

    flash.hide()
    QApplication.processEvents()

    assert not _focus_in_page(window)
    _change(files[1])
    window.changed()
    assert not window._workspace_stale
    assert window.firmware_view.banner.isVisible()


def test_a_field_the_user_went_into_still_waits(window: EmbrWindow, tmp_path: Path) -> None:
    _shown(window, tmp_path)
    field = _address_field(window)
    _focus(field)

    window.changed()

    assert window._workspace_stale
    assert QApplication.focusWidget() is field
