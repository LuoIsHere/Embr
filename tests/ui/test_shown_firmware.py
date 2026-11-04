"""The workspace shows the stored name and place of a firmware: right after a rename or a
move the Parameters tab's name field and the header's path are up to date, as the sidebar
is. Driven offscreen on a real window."""

from PySide6.QtWidgets import QLineEdit

from embr.ui.widgets import ElidedLabel
from embr.ui.window import EmbrWindow


def _path(window: EmbrWindow) -> str:
    item = window.header.row.itemAt(0)
    assert item is not None
    label = item.widget()
    assert isinstance(label, ElidedLabel)
    return label.full_text()


def _name_field(window: EmbrWindow) -> str:
    item = window.firmware_view.params.grid.itemAtPosition(0, 1)
    assert item is not None
    box = item.widget()
    assert box is not None
    field = box.findChild(QLineEdit)
    assert field is not None
    return field.text()


def test_a_firmware_moved_into_a_project_no_longer_shows_standalone(window: EmbrWindow) -> None:
    library = window.library
    project = library.create_project("Station")
    library.add_group(project.id, "Main")
    firmware = library.create_firmware("fw")
    window.select_firmware(firmware.id)
    assert _path(window) == "Standalone"

    window.sidebar_panel.tree.move_requested.emit(firmware.id, project.id, "Main")

    assert _path(window) == "Station / Main"


def test_the_name_field_follows_a_rename(window: EmbrWindow) -> None:
    firmware = window.library.create_firmware("old")
    window.select_firmware(firmware.id)
    assert _name_field(window) == "old"

    window.library.rename_firmware(firmware.id, "new")
    window.changed()

    assert _name_field(window) == "new"
