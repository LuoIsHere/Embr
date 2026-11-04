"""The project page offers only reset modes as defaults for new firmware: there is no
erase-all box, and an erase_all default stored by v0.1.0 no longer reaches new firmware.
Driven offscreen on a real window."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QCheckBox, QComboBox

from embr.app.context import open_context
from embr.core.project import ProjectDefaults
from embr.core.storage.paths import AppPaths, prepare
from embr.i18n import Language, current, set_language
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


def test_the_project_page_has_no_erase_all_box(window: EmbrWindow) -> None:
    project = window.library.create_project("Station")
    window.select_project(project.id)
    assert not window.project_view.findChildren(QCheckBox)


def test_a_stored_erase_all_default_stays_off_for_new_firmware(window: EmbrWindow) -> None:
    library = window.library
    project = library.create_project("Station")
    library.set_project_defaults(project.id, ProjectDefaults("no-reset", "no-reset", True))
    firmware = library.create_firmware("fw", project.id)
    assert (firmware.before, firmware.erase_all) == ("no-reset", False)
    window.select_firmware(firmware.id)
    assert not window.connection.erase_all.isChecked()


def test_changing_a_reset_mode_on_the_page_saves_it(window: EmbrWindow) -> None:
    library = window.library
    project = library.create_project("Station")
    window.select_project(project.id)

    before = window.project_view.findChildren(QComboBox)[0]
    before.setCurrentText("no-reset")
    before.activated.emit(before.currentIndex())

    assert library.project(project.id).defaults == ProjectDefaults("no-reset", "hard-reset")
