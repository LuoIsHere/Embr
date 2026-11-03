"""The workspace shows the stored name and place of a firmware: right after a rename or a
move the Parameters tab's name field and the header's path are up to date, as the sidebar
is. Driven offscreen on a real window."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QLineEdit

from embr.app.context import open_context
from embr.core.storage.paths import AppPaths, prepare
from embr.i18n import Language, current, set_language
from embr.ui.widgets import ElidedLabel
from embr.ui.window import EmbrWindow


@pytest.fixture
def window(qapp: QCoreApplication, tmp_path: Path) -> Iterator[EmbrWindow]:
    language = current()
    set_language(Language.ENGLISH)
    paths = AppPaths(tmp_path / "Embr")
    prepare(paths)
    context = open_context(paths)
    shown = EmbrWindow(context)
    yield shown
    shown.close()
    context.close()
    set_language(language)


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
