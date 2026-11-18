"""The sidebar builds its rows again only when what they show changed: an edit of the
firmware page that leaves the sidebar as it is (an entry unticked) keeps its rows."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItem, QWidget

from embr.ui.widgets import ElidedLabel
from embr.ui.window import EmbrWindow


def _firmware(window: EmbrWindow, tmp_path: Path) -> str:
    folder = tmp_path / "f"
    folder.mkdir()
    files = [folder / "boot.bin", folder / "app.bin"]
    for file in files:
        file.write_bytes(b"\x00" * 64)
    return window.library.create_from_files(files, name="fw").id


def _row(window: EmbrWindow, firmware_id: str) -> tuple[QTreeWidgetItem, QWidget]:
    tree = window.sidebar_panel.tree
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        node = tree.node(item)
        if item is not None and node is not None and node.firmware_id == firmware_id:
            widget = tree.itemWidget(item, 0)
            assert widget is not None
            return item, widget
    raise AssertionError(f"no sidebar row for {firmware_id}")


def _untick_first_entry(window: EmbrWindow) -> None:
    item = window.firmware_view.entries.table.item(0, 0)
    assert item is not None
    item.setCheckState(Qt.CheckState.Unchecked)  # the table's own handler saves it
    QApplication.processEvents()


def test_unticking_an_entry_keeps_the_sidebar_rows(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id = _firmware(window, tmp_path)
    window.select_firmware(firmware_id)
    _, before = _row(window, firmware_id)

    _untick_first_entry(window)

    assert not window.library.firmware(firmware_id).entries[0].enabled
    assert _row(window, firmware_id)[1] is before


def test_a_rename_builds_the_rows_again(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id = _firmware(window, tmp_path)
    window.select_firmware(firmware_id)
    _, before = _row(window, firmware_id)

    window.library.rename_firmware(firmware_id, "renamed")
    window.changed()

    _, after = _row(window, firmware_id)
    assert after is not before
    assert "renamed" in [label.text() for label in after.findChildren(ElidedLabel)]


def test_the_selected_row_is_current_again_after_the_cursor_left_it(
    window: EmbrWindow, tmp_path: Path
) -> None:
    """Keys move the current row without selecting: the next refresh puts it back."""
    firmware_id = _firmware(window, tmp_path)
    window.select_firmware(firmware_id)
    tree = window.sidebar_panel.tree
    selected, _ = _row(window, firmware_id)
    first = tree.topLevelItem(0)
    assert first is not None
    tree.setCurrentItem(first)
    assert tree.currentItem() is not selected

    window.changed()

    current = tree.node(tree.currentItem())
    assert current is not None and current.firmware_id == firmware_id
