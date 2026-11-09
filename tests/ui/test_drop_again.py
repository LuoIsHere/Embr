"""Files dropped again on the firmware that already has them, in the workspace or on its
sidebar row: no second entry; changed content is accepted, unchanged files are named. A
refresh of the sidebar during a drag keeps the drop target."""

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QTreeWidgetItem, QTreeWidgetItemIterator

from embr.ui import dialogs
from embr.ui.window import EmbrWindow

# pyright: reportPrivateUsage=false


def _fixed(window: EmbrWindow, tmp_path: Path) -> tuple[str, list[Path]]:
    folder = tmp_path / "fw"
    folder.mkdir()
    files = [folder / "a.bin", folder / "b.bin"]
    for i, file in enumerate(files):
        file.write_bytes(bytes([i]) * 64)
    firmware = window.library.create_from_files(files, name="fw")
    window.select_firmware(firmware.id)
    return firmware.id, files


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    shown: list[list[str]] = []

    def notice(_parent: object, _title: str, lines: list[str]) -> None:
        shown.append(lines)

    monkeypatch.setattr(dialogs, "notice", notice)
    return shown


def _names(window: EmbrWindow, firmware_id: str) -> list[str]:
    return [e.name for e in window.library.firmware(firmware_id).entries]


def test_a_sidebar_drop_of_the_same_files_adds_nothing(
    window: EmbrWindow, tmp_path: Path, notices: list[list[str]]
) -> None:
    firmware_id, files = _fixed(window, tmp_path)
    window._drop_on_firmware(firmware_id, files, None, [])
    assert sorted(_names(window, firmware_id)) == ["a.bin", "b.bin"]
    assert notices == [["Already in this firmware, content unchanged: a.bin, b.bin"]]


def test_a_dropped_changed_file_is_confirmed_not_added(
    window: EmbrWindow, tmp_path: Path, notices: list[list[str]]
) -> None:
    firmware_id, files = _fixed(window, tmp_path)
    files[0].write_bytes(b"\x07" * 80)  # overwritten on disk

    window._drop_on_firmware(firmware_id, files, None, [])
    assert notices == []  # something was done: the log says what
    log = window.log_panel.log.toPlainText()
    assert "a.bin: changed content accepted" in log
    assert "already in this firmware, unchanged: b.bin" in log

    window.firmware_view.entries._add_paths([files[0]])  # the workspace drop
    assert sorted(_names(window, firmware_id)) == ["a.bin", "b.bin"]
    assert not window.firmware_view.banner.isVisible()


def test_a_rebuild_during_a_drag_keeps_the_drop_target(
    window: EmbrWindow, tmp_path: Path, notices: list[list[str]]
) -> None:
    """A check while files are dragged in may rebuild the sidebar under the cursor."""
    firmware_id, files = _fixed(window, tmp_path)
    window.show()
    QApplication.processEvents()
    tree = window.sidebar_panel.tree
    extra = files[0].with_name("c.bin")
    extra.write_bytes(b"\x05" * 64)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(extra))])

    def row() -> QTreeWidgetItem:
        found = QTreeWidgetItemIterator(tree)
        while item := found.value():  # None after the last row
            node = tree.node(item)
            if node is not None and node.firmware_id == firmware_id:
                return item
            found += 1
        raise AssertionError("no firmware row")

    def event[E: QDropEvent](kind: type[E]) -> E:
        at = QPointF(tree.visualItemRect(row()).center())
        return kind(
            at,
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    tree.dragMoveEvent(event(QDragMoveEvent))
    window.changed()  # the rows are rebuilt mid-drag
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert tree.itemWidget(row(), 0).property("dropping") == "1"

    tree.dropEvent(event(QDropEvent))
    assert sorted(_names(window, firmware_id)) == ["a.bin", "b.bin", "c.bin"]
    assert notices == []


def test_copying_an_entry_to_a_firmware_that_has_its_file_says_so(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware_id, files = _fixed(window, tmp_path)
    other = window.library.create_from_files(files[:1], name="other")

    def first(*_args: object) -> int:
        return 0

    monkeypatch.setattr(dialogs, "ask_choice", first)
    source = window.library.firmware(firmware_id)
    (entry,) = [e for e in source.entries if e.name == "a.bin"]

    window.firmware_view.entries._copy_to(source, entry)
    assert "[entry] not copied: a.bin is already in 'other'" in window.log_panel.log.toPlainText()
    assert _names(window, other.id) == ["a.bin"]


def test_re_locating_outside_the_folder_says_why(
    window: EmbrWindow, tmp_path: Path, notices: list[list[str]]
) -> None:
    firmware_id, _files = _fixed(window, tmp_path)
    elsewhere = tmp_path / "elsewhere" / "a.bin"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"\x09" * 64)
    source = window.library.firmware(firmware_id)
    (entry,) = [e for e in source.entries if e.name == "a.bin"]

    assert not window.firmware_view.entries._relocate_to(source, entry, elsewhere)
    folder = str(tmp_path / "fw")
    assert notices == [[f"a.bin is not in this firmware's folder {folder}."]]
    (kept,) = [e for e in window.library.firmware(firmware_id).entries if e.id == entry.id]
    assert kept.file == entry.file


def test_copying_to_a_firmware_with_another_folder_says_why(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notices: list[list[str]]
) -> None:
    firmware_id, _files = _fixed(window, tmp_path)
    other_file = tmp_path / "other" / "c.bin"
    other_file.parent.mkdir()
    other_file.write_bytes(b"\x03" * 64)
    window.library.create_from_files([other_file], name="other")

    def first(*_args: object) -> int:
        return 0

    monkeypatch.setattr(dialogs, "ask_choice", first)
    source = window.library.firmware(firmware_id)
    window.firmware_view.entries._copy_to(source, source.entries[0])
    folder = str(tmp_path / "other")
    assert notices == [[f"{source.entries[0].name} is not in this firmware's folder {folder}."]]
