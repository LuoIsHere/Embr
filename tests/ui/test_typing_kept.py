"""Text being typed in the workspace survives a refresh: showing another page
saves it first, as leaving the field does; a refresh of the same page (a job ended, a port
changed, a check) waits until the field is left."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLayout, QLineEdit, QWidget

from embr.ui.window import EmbrWindow
from embr.ui.workspace import NotesEdit

# pyright: reportPrivateUsage=false


def _firmware(window: EmbrWindow, tmp_path: Path, name: str) -> str:
    folder = tmp_path / name
    folder.mkdir()
    file = folder / "app.bin"
    file.write_bytes(b"\x00" * 64)
    return window.library.create_from_files([file], name=name).id


def _shown(window: EmbrWindow) -> None:
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window)


def _type(field: QWidget, text: str) -> None:
    field.setFocus()
    QApplication.processEvents()
    assert QApplication.focusWidget() is field
    QTest.keyClicks(field, text)


def _name_field(window: EmbrWindow, name: str) -> QLineEdit:
    (field,) = [f for f in window.firmware_view.params.findChildren(QLineEdit) if f.text() == name]
    return field


def _params(window: EmbrWindow) -> None:
    window.firmware_view.tab_group.buttons()[1].click()  # Parameters
    QApplication.processEvents()


def test_switching_firmware_saves_what_was_typed(window: EmbrWindow, tmp_path: Path) -> None:
    first = _firmware(window, tmp_path, "first")
    second = _firmware(window, tmp_path, "second")
    _shown(window)
    window.select_firmware(first)
    _params(window)
    (notes,) = window.firmware_view.params.findChildren(NotesEdit)
    _type(notes, "rev B board")
    _type(_name_field(window, "first"), " v2")  # notes saved here, as on leaving them

    window.select_firmware(second)  # a click in the sidebar, which takes no focus
    saved = window.library.firmware(first)
    assert (saved.name, saved.notes) == ("first v2", "rev B board")


def test_a_refresh_of_the_same_page_waits_for_the_field(window: EmbrWindow, tmp_path: Path) -> None:
    first = _firmware(window, tmp_path, "first")
    _shown(window)
    window.select_firmware(first)
    _params(window)
    field = _name_field(window, "first")
    _type(field, " draft")

    window.changed()  # e.g. a job ended
    assert field.text() == "first draft" and field.isVisible()  # not rebuilt
    assert window._workspace_stale

    window.connection.port.setFocus()  # the user leaves the field
    QApplication.processEvents()
    QApplication.processEvents()
    assert window.library.firmware(first).name == "first draft"
    assert not window._workspace_stale


def test_an_entry_note_survives_a_refresh(window: EmbrWindow, tmp_path: Path) -> None:
    first = _firmware(window, tmp_path, "first")
    _shown(window)
    window.select_firmware(first)
    entries = window.firmware_view.entries
    (note,) = [f for f in entries.findChildren(QLineEdit) if f.placeholderText() == "add note"]
    _type(note, "keep me")
    window.changed()
    QTest.keyClick(note, Qt.Key.Key_Return)
    assert window.library.firmware(first).entries[0].note == "keep me"


def test_leaving_the_project_page_saves_its_name(window: EmbrWindow, tmp_path: Path) -> None:
    project = window.library.create_project("p")
    first = _firmware(window, tmp_path, "first")
    _shown(window)
    window.select_project(project.id)
    (name,) = [f for f in window.project_view.findChildren(QLineEdit) if f.text() == "p"]
    _type(name, "roject")

    window.select_firmware(first)
    assert window.library.project(project.id).name == "project"


def test_a_waiting_refresh_runs_when_an_unchanged_field_is_left(
    window: EmbrWindow, tmp_path: Path
) -> None:
    """Leaving a field that saves nothing refreshes nothing by itself."""
    first = _firmware(window, tmp_path, "first")
    _shown(window)
    window.select_firmware(first)
    _params(window)
    _name_field(window, "first").setFocus()
    QApplication.processEvents()
    window.library.rename_firmware(first, "renamed elsewhere")
    window.changed()
    assert window._workspace_stale

    window.connection.port.setFocus()
    QApplication.processEvents()
    QApplication.processEvents()
    assert not window._workspace_stale
    assert _name_field(window, "renamed elsewhere").isVisible()


def _check_texts(window: EmbrWindow) -> list[str]:
    """Texts of the rows in the Checks list now (rows replaced earlier are only deleted
    later, so they are not looked up among the panel's children)."""
    texts: list[str] = []
    pending: list[QLayout] = [window.connection.checks]
    while pending:
        layout = pending.pop()
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is None:
                continue
            widget, nested = item.widget(), item.layout()
            if isinstance(widget, QLabel):
                texts.append(widget.text())
            if nested is not None:
                pending.append(nested)
    return texts


def _type_address(window: EmbrWindow, row: int, text: str) -> None:
    """Type into the address cell of `row` and press Enter; the focus stays in the cell."""
    field = window.firmware_view.entries.table.cellWidget(row, 1).findChild(QLineEdit)
    assert field is not None
    _type(field, "")
    field.selectAll()
    QTest.keyClicks(field, text)
    QTest.keyClick(field, Qt.Key.Key_Return)
    QApplication.processEvents()


def test_the_checks_follow_an_address_typed_while_the_table_waits(
    window: EmbrWindow, tmp_path: Path
) -> None:
    """The entries table waits for its field, the checks do not: an overlap typed into it is
    refused at once and stays refused after the field is left."""
    folder = tmp_path / "f"
    folder.mkdir()
    files = [folder / "boot.bin", folder / "app.bin"]
    for file in files:
        file.write_bytes(b"\x00" * 64)
    firmware = window.library.create_firmware("fw")
    window.library.add_files(firmware.id, files)
    entries = window.library.firmware(firmware.id).entries
    window.library.set_entry_address(firmware.id, entries[0].id, "0x0")
    window.library.set_entry_address(firmware.id, entries[1].id, "0x10000")
    _shown(window)
    window.select_firmware(firmware.id)
    QApplication.processEvents()
    assert "No address overlap" in _check_texts(window)

    _type_address(window, 1, "0x0")
    assert window._workspace_stale  # the table has not been rebuilt yet
    assert "No address overlap" not in _check_texts(window)

    window.connection.port.setFocus()  # the user leaves the field
    QApplication.processEvents()
    QApplication.processEvents()
    assert not window._workspace_stale
    assert "No address overlap" not in _check_texts(window)
    assert not window.connection.flash.isEnabled()
