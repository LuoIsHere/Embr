"""The status tags of an entry sit right of its address; the address column only widens for
a firmware that has any, and never cuts them. Driven offscreen on a real window."""

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

from embr.ui.window import EmbrWindow
from embr.ui.workspace import ADDRESS_FIELD_PX, ADDRESS_PX, EntriesTab


def _tags(entries: EntriesTab, row: int, column: int) -> list[QLabel]:
    cell = entries.table.cellWidget(row, column)
    return [w for w in cell.findChildren(QLabel) if w.property("tag") is not None]


def _fixed(window: EmbrWindow, tmp_path: Path) -> tuple[str, Path]:
    folder = tmp_path / "f"
    folder.mkdir()
    file = folder / "app.bin"
    file.write_bytes(b"\x00" * 64)
    firmware = window.library.create_firmware("fw")
    window.library.add_files(firmware.id, [file])
    return firmware.id, file


def _settle(window: EmbrWindow) -> None:
    window.show()
    QApplication.processEvents()
    QApplication.processEvents()


def _changed(window: EmbrWindow, firmware_id: str, file: Path) -> EntriesTab:
    file.write_bytes(b"\x01" * 64)
    os.utime(file, (file.stat().st_atime, file.stat().st_mtime + 5))
    window.library.tracker.invalidate(firmware_id)
    window.select_firmware(firmware_id)
    window.changed()
    _settle(window)
    return window.firmware_view.entries


def test_without_tags_the_address_column_keeps_its_width(
    window: EmbrWindow, tmp_path: Path
) -> None:
    firmware_id, _ = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    _settle(window)

    entries = window.firmware_view.entries
    assert not _tags(entries, 0, 1)
    assert entries.table.columnWidth(1) == ADDRESS_PX


def test_a_tag_moves_to_the_address_cell_and_widens_the_column(
    window: EmbrWindow, tmp_path: Path
) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    entries = _changed(window, firmware_id, file)

    tags = _tags(entries, 0, 1)
    assert tags
    assert not _tags(entries, 0, 3)
    assert entries.table.columnWidth(1) > ADDRESS_PX


def test_the_tag_is_whole_and_right_of_the_field(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    window.resize(1360, 760)
    entries = _changed(window, firmware_id, file)

    cell = entries.table.cellWidget(0, 1)
    field = cell.findChild(QLineEdit)
    assert field is not None
    assert field.width() == ADDRESS_FIELD_PX
    tag = _tags(entries, 0, 1)[0]
    assert tag.x() >= field.geometry().right()
    assert tag.geometry().right() < cell.width()
    assert tag.width() >= tag.sizeHint().width()


def test_rows_with_and_without_a_tag_have_the_same_field(
    window: EmbrWindow, tmp_path: Path
) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    other = file.with_name("other.bin")
    other.write_bytes(b"\x00" * 64)
    window.library.add_files(firmware_id, [other])
    window.resize(1360, 760)
    entries = _changed(window, firmware_id, file)

    fields = [entries.table.cellWidget(r, 1).findChild(QLineEdit) for r in range(2)]
    assert sum(len(_tags(entries, r, 1)) for r in range(2)) == 1
    assert [f.width() for f in fields if f is not None] == [ADDRESS_FIELD_PX] * 2


@pytest.mark.parametrize("width", [1100, 1360, 1600])
def test_the_table_never_scrolls_sideways_with_a_tag(
    window: EmbrWindow, tmp_path: Path, width: int
) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    window.resize(width, 760)
    entries = _changed(window, firmware_id, file)

    table = entries.table
    total = sum(table.columnWidth(c) for c in range(table.columnCount()))
    assert total <= table.viewport().width()
    tag = _tags(entries, 0, 1)[0]
    assert tag.width() >= tag.sizeHint().width()


def test_a_narrow_dragged_width_does_not_cut_a_tag(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    _settle(window)
    entries = window.firmware_view.entries
    entries.table.horizontalHeader().resizeSection(1, 90)  # as a user dragging the edge
    assert entries.table.columnWidth(1) == 90

    entries = _changed(window, firmware_id, file)

    tag = _tags(entries, 0, 1)[0]
    assert tag.width() >= tag.sizeHint().width()


def test_typing_in_an_address_survives_a_refresh(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id, _ = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    _settle(window)
    window.activateWindow()
    entries = window.firmware_view.entries
    field = entries.table.cellWidget(0, 1).findChild(QLineEdit)
    assert field is not None
    field.setFocus()
    if QApplication.focusWidget() is not field:
        pytest.skip("no focus on this platform")
    field.setText("0x12")

    window.changed()

    again = entries.table.cellWidget(0, 1).findChild(QLineEdit)
    assert again is not None
    assert again.text() == "0x12"


def test_a_tag_explains_itself_and_the_name_keeps_only_the_path(
    window: EmbrWindow, tmp_path: Path
) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    entries = window.firmware_view.entries
    entry = window.library.firmware(firmware_id).entries[0]

    cell = entries._address_cell(QLineEdit(), entry, None, "why it differs")  # pyright: ignore[reportPrivateUsage]

    tags = [w for w in cell.findChildren(QLabel) if w.property("tag") is not None]
    assert [w.toolTip() for w in tags] == ["why it differs"]
    assert entries.table.cellWidget(0, 3).toolTip() == str(file.resolve())
