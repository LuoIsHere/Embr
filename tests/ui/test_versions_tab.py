"""The Versions tab of a tracking firmware, driven offscreen on a real window: shown only for
tracking firmware, names, notes and pins saved from the table, Back up this build with the
copy run at once, and the dragged column widths kept. git is never run."""

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit

from embr.app import versions as app_versions
from embr.core.versions import Origin, Version
from embr.ui import dialogs
from embr.ui.window import EmbrWindow
from embr.ui.workspace.versions import COLUMN_WIDTHS, NO_FLASHES, NOTE, PINNED, VersionsTab
from embr.ui.workspace.view import VERSIONS_TAB

# pyright: reportPrivateUsage=false

SAMPLE = Path(__file__).parents[1] / "fixtures/idf/esp32s3_hello"
NAME = 1


@pytest.fixture(autouse=True)
def no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def read_state(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(app_versions.vcs, "read_state", read_state)


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    shown: list[tuple[str, list[str]]] = []

    def notice(_parent: object, title: str, lines: list[str]) -> None:
        shown.append((title, lines))

    monkeypatch.setattr(dialogs, "notice", notice)
    return shown


def _tracked(window: EmbrWindow, tmp_path: Path) -> tuple[str, Path]:
    shutil.copytree(SAMPLE, tmp_path / "hello")
    build = tmp_path / "hello/build"
    return window.library.import_idf(build).id, build / "hello_esp32s3.bin"


def _rebuilt(window: EmbrWindow, firmware_id: str, app: Path) -> None:
    """A new app image, older than the tracker's settle time (not a build in progress)."""
    data = bytearray(app.read_bytes())
    data[-1] ^= 0xFF
    old = app.stat().st_mtime_ns
    app.write_bytes(bytes(data))
    os.utime(app, ns=(old + 10**9, old + 10**9))
    window.library.tracker.invalidate(firmware_id)


def _backup(window: EmbrWindow, firmware_id: str) -> Version:
    return window.library.versions.backup(firmware_id, origin=Origin.MANUAL)


def _shown(window: EmbrWindow, firmware_id: str) -> VersionsTab:
    window.select_firmware(firmware_id)
    return window.firmware_view.versions


def _edit(tab: VersionsTab, row: int, column: int) -> QLineEdit:
    edit = tab.table.cellWidget(row, column).findChild(QLineEdit)
    assert isinstance(edit, QLineEdit)
    return edit


def _type(edit: QLineEdit, text: str) -> None:
    edit.setText(text)
    edit.editingFinished.emit()


def _text(tab: VersionsTab, row: int, column: int) -> str:
    item = tab.table.item(row, column)
    assert item is not None
    return item.text()


def test_the_tab_is_shown_for_tracking_firmware_only(window: EmbrWindow, tmp_path: Path) -> None:
    tracked, _ = _tracked(window, tmp_path)
    fixed = window.library.create_firmware("fixed")
    view = window.firmware_view
    tab = view.tab_group.button(VERSIONS_TAB)

    window.select_firmware(tracked)
    assert not tab.isHidden() and tab.text() == "Versions"
    tab.click()
    assert view.pages.currentIndex() == VERSIONS_TAB
    assert view.versions.empty.text() == "No versions yet."

    window.select_firmware(fixed.id)
    assert tab.isHidden()
    assert view.pages.currentIndex() == 0
    assert view.tab_group.checkedId() == 0
    assert not window.header.backup_action.isVisible()


def test_rows_show_the_versions_newest_first(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id, app = _tracked(window, tmp_path)
    first = _backup(window, firmware_id)
    _rebuilt(window, firmware_id, app)
    second = _backup(window, firmware_id)

    tab = _shown(window, firmware_id)
    assert [v.id for v in tab.shown] == [second.id, first.id]
    assert [_text(tab, r, 0) for r in (0, 1)] == ["#2", "#1"]
    assert _edit(tab, 0, NAME).text() == second.name
    assert _text(tab, 0, 4) == "manual"
    assert _text(tab, 0, 5) == "hello_esp32s3.bin"  # only the app changed
    assert _text(tab, 1, 5) == ""  # the first version has nothing to compare with
    assert _text(tab, 0, 6) == NO_FLASHES
    assert tab.footer.text().startswith("2 versions; deleting all would free ")


def test_name_note_and_pin_are_saved(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id, _ = _tracked(window, tmp_path)
    version = _backup(window, firmware_id)
    tab = _shown(window, firmware_id)
    store = window.context.versions

    def stored_name() -> str:
        stored = store.get(version.id)
        assert stored is not None
        return stored.name

    _type(_edit(tab, 0, NAME), "Customer demo")
    assert stored_name() == "Customer demo"
    _type(_edit(tab, 0, NAME), "")
    assert stored_name() == version.name
    assert _edit(tab, 0, NAME).text() == version.name

    _type(_edit(tab, 0, NOTE), " for the customer ")
    pin = tab.table.item(0, PINNED)
    assert pin is not None
    pin.setCheckState(Qt.CheckState.Checked)
    stored = store.get(version.id)
    assert stored is not None
    assert (stored.note, stored.pinned) == ("for the customer", True)


def test_a_locked_firmware_cannot_be_edited(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware_id, _ = _tracked(window, tmp_path)
    _backup(window, firmware_id)

    def locked(_firmware_id: str) -> bool:
        return True

    monkeypatch.setattr(window, "is_locked", locked)
    tab = _shown(window, firmware_id)

    assert _edit(tab, 0, NAME).isReadOnly() and _edit(tab, 0, NOTE).isReadOnly()
    pin = tab.table.item(0, PINNED)
    assert pin is not None and not pin.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert not tab.backup_button.isEnabled()
    assert not window.header.backup_action.isEnabled()


def _copy_at_once(window: EmbrWindow, before: Callable[[], None] = lambda: None) -> None:
    def start(work: Callable[[], None]) -> None:
        before()
        work()

    window.library_actions.start_thread = start


def test_back_up_this_build(
    window: EmbrWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    notices: list[tuple[str, list[str]]],
) -> None:
    firmware_id, _ = _tracked(window, tmp_path)
    asked: list[str] = []

    def backup_build(_parent: object, name: str) -> str:
        asked.append(name)
        return "first"

    monkeypatch.setattr(dialogs, "backup_build", backup_build)
    _copy_at_once(window)
    tab = _shown(window, firmware_id)
    assert window.header.backup_action.isEnabled() and tab.backup_button.isEnabled()

    tab.backup_button.click()
    (version,) = window.context.versions.for_firmware(firmware_id)
    assert (version.note, version.origin) == ("first", Origin.MANUAL)
    assert asked == [window.library.firmware(firmware_id).name]
    assert [v.id for v in tab.shown] == [version.id]
    assert not window.library.versions.busy(firmware_id)
    assert tab.backup_button.text() == "Back up this build…"

    window.firmware_action("backup")  # the same content again
    assert len(asked) == 1  # no note asked for
    assert notices == [("Already backed up", [f"The current build is version #1 {version.name}."])]


def test_a_build_changed_during_the_copy_adds_no_version(
    window: EmbrWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    notices: list[tuple[str, list[str]]],
) -> None:
    firmware_id, app = _tracked(window, tmp_path)

    def backup_build(*_args: Any) -> str:
        return ""

    monkeypatch.setattr(dialogs, "backup_build", backup_build)
    _copy_at_once(window, lambda: _rebuilt(window, firmware_id, app))
    tab = _shown(window, firmware_id)

    tab.backup_button.click()
    assert window.context.versions.for_firmware(firmware_id) == []
    ((title, lines),) = notices
    assert title == "Not backed up" and "changed while it was being backed up" in lines[0]
    assert not window.library.versions.busy(firmware_id)
    assert tab.shown == []


def test_dragged_column_widths_are_kept(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id, _ = _tracked(window, tmp_path)
    _backup(window, firmware_id)
    window.resize(1400, 800)
    tab = _shown(window, firmware_id)
    view = window.firmware_view
    view.tab_group.button(VERSIONS_TAB).click()
    window.show()
    QApplication.processEvents()
    QApplication.processEvents()
    tab.table.horizontalHeader().resizeSection(NAME, 150)  # as a user dragging the edge
    assert tab.table.columnWidth(NAME) == 150

    assert window.context.settings.get("ui", COLUMN_WIDTHS) == "150,0,0,0,0,0"
    assert VersionsTab(window)._chosen == {NAME: 150}
