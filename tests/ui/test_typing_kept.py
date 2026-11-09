"""Text being typed in the workspace survives a refresh (review H3): showing another page
saves it first, as leaving the field does; a refresh of the same page (a job ended, a port
changed, a check) waits until the field is left."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

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
