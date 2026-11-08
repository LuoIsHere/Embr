"""The spare row at the end of the sidebar fills the empty space below the other rows, so
dropping a firmware or files anywhere there shows the highlight on all of it; when the list
scrolls it keeps a firmware row's height (plan #11)."""

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from embr.ui.sidebar import SPARE_MIN, FirmwareTree
from embr.ui.window import EmbrWindow

# pyright: reportPrivateUsage=false


def _shown(window: EmbrWindow, height: int) -> FirmwareTree:
    window.resize(1100, height)
    window.show()
    window.changed()
    QApplication.processEvents()
    return window.sidebar_panel.tree


def _spare(tree: FirmwareTree) -> QTreeWidgetItem:
    item = tree._spare_row()
    assert item is not None
    return item


def test_a_short_list_fills_the_rest_with_the_spare_row(window: EmbrWindow) -> None:
    window.library.create_firmware("one")
    tree = _shown(window, 700)
    spare = tree.visualItemRect(_spare(tree))
    viewport = tree.viewport().height()

    assert spare.height() > SPARE_MIN
    assert spare.bottom() + 1 == viewport  # down to the bottom edge, no gap
    assert tree.verticalScrollBar().maximum() == 0  # and no scroll bar for it
    below = QPoint(20, viewport - 3)
    assert tree._target(below) is _spare(tree)
    tree._highlight(tree._target(below))
    row = tree.itemWidget(_spare(tree), 0)
    assert row is not None and row.property("dropping") == "1"
    assert row.height() == spare.height()  # the highlight covers the whole space


def test_a_long_list_keeps_one_row_and_scrolls(window: EmbrWindow) -> None:
    for index in range(30):
        window.library.create_firmware(f"fw {index:02}")
    tree = _shown(window, 500)

    assert _spare(tree).sizeHint(0).height() == SPARE_MIN
    assert tree.verticalScrollBar().maximum() > 0
    tree.verticalScrollBar().setValue(tree.verticalScrollBar().maximum())
    QApplication.processEvents()
    spare = tree.visualItemRect(_spare(tree))
    assert spare.bottom() + 1 == tree.viewport().height()  # still there at the very end


def test_the_spare_row_follows_the_window_height(window: EmbrWindow) -> None:
    window.library.create_firmware("one")
    tree = _shown(window, 600)
    before = _spare(tree).sizeHint(0).height()

    window.resize(1100, 800)
    QApplication.processEvents()
    assert _spare(tree).sizeHint(0).height() > before
    assert tree.visualItemRect(_spare(tree)).bottom() + 1 == tree.viewport().height()


def test_folding_a_project_grows_the_spare_row(window: EmbrWindow) -> None:
    project = window.library.create_project("Station")
    window.library.add_group(project.id, "Main")
    for name in ("a", "b", "c"):
        window.library.create_firmware(name, project.id, "Main")
    tree = _shown(window, 700)
    open_height = _spare(tree).sizeHint(0).height()
    folded_away = sum(  # the group row and its three firmware rows, as laid out
        tree.visualItemRect(item).height()
        for index in range(tree.topLevelItemCount())
        if (item := tree.topLevelItem(index)) is not None
        and (node := tree.node(item)) is not None
        and node.kind in ("group", "firmware")
    )

    window.sidebar_panel._toggle(f"p:{project.id}")
    QApplication.processEvents()
    assert _spare(tree).sizeHint(0).height() == open_height + folded_away
    assert tree.visualItemRect(_spare(tree)).bottom() + 1 == tree.viewport().height()
