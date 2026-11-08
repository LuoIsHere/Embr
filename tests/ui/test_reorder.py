"""Reordering projects and groups by dragging them in the sidebar (plan #9, #20): the
geometry as plain functions, then real mouse events on the sidebar of a window."""

import tomllib

import pytest
import tomli_w
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from embr.core.project import PROJECT_SCHEMA_VERSION
from embr.ui import reorder
from embr.ui.reorder import Block, Slide
from embr.ui.sidebar import FirmwareTree
from embr.ui.window import EmbrWindow

# pyright: reportPrivateUsage=false

# ---- geometry ---------------------------------------------------------------------------


def test_blocks_group_neighbouring_rows_with_one_key() -> None:
    rows = [(None, 28), ("a", 26), ("a", 40), ("b", 26), (None, 28), ("b", 40)]
    assert reorder.blocks(rows) == [
        Block("a", 1, 2, 28, 66),
        Block("b", 3, 1, 94, 26),
        Block("b", 5, 1, 148, 40),  # not next to the first "b": a block of its own
    ]


SPANS = [Block("a", 0, 1, 0, 30), Block("b", 1, 1, 30, 50), Block("c", 2, 1, 80, 20)]


@pytest.mark.parametrize(
    ("dragged", "top", "index"),
    [
        (0, 0, 0),  # where it was (middles: a 15, b 55, c 90)
        (0, 24, 0),  # a's bottom (54) not yet past b's middle
        (0, 30, 1),  # a's bottom (60) past b's middle
        (0, 70, 2),  # past c too: last
        (2, -10, 0),  # above everything: first
        (2, 50, 1),  # c's top above b's middle, below a's
        (2, 60, 2),  # c's top not above b's middle: where it was
        (1, 1000, 2),
    ],
)
def test_the_leading_edge_passes_the_middles(dragged: int, top: float, index: int) -> None:
    assert reorder.landing(SPANS, dragged, top) == index


def test_moved_and_slots() -> None:
    assert reorder.moved(["a", "b", "c"], 0, 2) == ["b", "c", "a"]
    assert reorder.moved(["a", "b", "c"], 2, 0) == ["c", "a", "b"]
    assert reorder.slots(SPANS, 0, 2) == {"b": 0, "c": 50, "a": 70}  # a's gap at the end
    assert reorder.slots(SPANS, 1, 1) == {"a": 0, "b": 30, "c": 80}  # unchanged


def test_a_slide_eases_and_restarts_from_where_it_is() -> None:
    now = [0.0]
    slide = Slide(0, lambda: now[0])
    slide.go(100)
    assert slide.moving() and slide.now() == 0
    now[0] = reorder.ANIMATION_MS / 2000  # halfway in time: OutCubic is past the middle
    halfway = slide.now()
    assert 50 < halfway < 100
    slide.go(0)  # turned back: starts from where it is, no jump
    assert slide.now() == pytest.approx(halfway)
    now[0] += reorder.ANIMATION_MS / 1000
    assert slide.now() == 0 and not slide.moving()


# ---- the sidebar ------------------------------------------------------------------------


def _shown(window: EmbrWindow) -> FirmwareTree:
    window.resize(1100, 800)
    window.show()
    window.changed()
    QApplication.processEvents()
    return window.sidebar_panel.tree


def _row(tree: FirmwareTree, kind: str, key: str) -> QTreeWidgetItem:
    """A project row by name, or a group row by name."""
    window = tree.window()
    assert isinstance(window, EmbrWindow)
    names = {p.id: p.name for p in window.library.tree().projects}
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        node = tree.node(item)
        if node is None or item is None or node.kind != kind:
            continue
        if (kind == "project" and names.get(node.project_id or "") == key) or (
            kind == "group" and node.device == key
        ):
            return item
    raise AssertionError(f"no {kind} row {key}")


def _at(tree: FirmwareTree, item: QTreeWidgetItem, dy: int = 0) -> QPoint:
    rect = tree.visualItemRect(item)
    return QPoint(rect.left() + 60, rect.center().y() + dy)


def _mouse(tree: FirmwareTree, kind: QEvent.Type, at: QPoint, *, held: bool = True) -> None:
    viewport = tree.viewport()
    button = Qt.MouseButton.LeftButton
    event = QMouseEvent(
        kind,
        QPointF(at),
        QPointF(viewport.mapToGlobal(at)),
        button,
        button if held else Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, event)


def _press_and_move(tree: FirmwareTree, start: QPoint, end: QPoint) -> None:
    _mouse(tree, QEvent.Type.MouseButtonPress, start)
    steps = 6
    for i in range(1, steps + 1):
        at = start + (end - start) * i / steps
        _mouse(tree, QEvent.Type.MouseMove, at)


def _drag(tree: FirmwareTree, start: QPoint, end: QPoint) -> None:
    _press_and_move(tree, start, end)
    _mouse(tree, QEvent.Type.MouseButtonRelease, end, held=False)
    QApplication.processEvents()


def _station(window: EmbrWindow) -> str:
    library = window.library
    project = library.create_project("Station")
    for name in ("Main", "Display", "Radio"):
        library.add_group(project.id, name)
        library.create_firmware(f"{name.lower()}-fw", project.id, name)
    library.create_firmware("loose", project.id)  # Ungrouped
    return project.id


def _groups(window: EmbrWindow, project_id: str) -> list[str]:
    return window.library.project(project_id).devices


def _project_names(window: EmbrWindow) -> list[str]:
    return [p.name for p in window.library.tree().projects]


def test_a_project_dragged_down_lands_below(window: EmbrWindow) -> None:
    for name in ("Alpha", "Bravo", "Charlie"):
        window.library.create_project(name)
    tree = _shown(window)
    start = _at(tree, _row(tree, "project", "Alpha"))
    _press_and_move(tree, start, _at(tree, _row(tree, "project", "Charlie"), 8))
    assert tree.sorting()
    overlay = tree._sort.overlay if tree._sort else None
    assert overlay is not None and overlay.isVisible()
    _mouse(tree, QEvent.Type.MouseButtonRelease, _at(tree, _row(tree, "project", "Charlie"), 8))
    QApplication.processEvents()

    assert not tree.sorting()
    assert _project_names(window) == ["Bravo", "Charlie", "Alpha"]
    assert [p.name for p in window.library.tree().projects] == _project_names(window)
    shown = [
        window.library.project(n.project_id).name
        for i in range(tree.topLevelItemCount())
        if (n := tree.node(tree.topLevelItem(i))) is not None
        and n.kind == "project"
        and n.project_id is not None
    ]
    assert shown == ["Bravo", "Charlie", "Alpha"]  # the sidebar was rebuilt in that order


def test_a_group_moves_within_its_project(window: EmbrWindow) -> None:
    project_id = _station(window)
    tree = _shown(window)
    _drag(tree, _at(tree, _row(tree, "group", "Radio")), _at(tree, _row(tree, "group", "Main"), -8))
    assert _groups(window, project_id) == ["Radio", "Main", "Display"]


def test_a_group_never_goes_below_ungrouped(window: EmbrWindow) -> None:
    project_id = _station(window)
    tree = _shown(window)
    ungrouped = _row(tree, "group", "")
    _drag(tree, _at(tree, _row(tree, "group", "Main")), _at(tree, ungrouped, 30))
    assert _groups(window, project_id) == ["Display", "Radio", "Main"]  # last of the named


def test_a_group_dropped_on_another_project_changes_nothing(window: EmbrWindow) -> None:
    project_id = _station(window)
    window.library.create_project("Zulu")
    tree = _shown(window)
    target = _at(tree, _row(tree, "project", "Zulu"))
    _press_and_move(tree, _at(tree, _row(tree, "group", "Main")), target)
    assert tree.viewport().cursor().shape() == Qt.CursorShape.ForbiddenCursor
    _mouse(tree, QEvent.Type.MouseButtonRelease, target, held=False)
    QApplication.processEvents()
    assert _groups(window, project_id) == ["Main", "Display", "Radio"]
    assert not tree.sorting()


def test_escape_puts_everything_back(window: EmbrWindow) -> None:
    for name in ("Alpha", "Bravo"):
        window.library.create_project(name)
    tree = _shown(window)
    _press_and_move(
        tree, _at(tree, _row(tree, "project", "Alpha")), _at(tree, _row(tree, "project", "Bravo"))
    )
    assert tree.sorting()
    escape = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(tree, escape)
    assert not tree.sorting()
    _mouse(tree, QEvent.Type.MouseButtonRelease, _at(tree, _row(tree, "project", "Bravo")))
    QApplication.processEvents()
    assert _project_names(window) == ["Alpha", "Bravo"]
    assert window.library.project_order() == []


def test_no_reorder_while_searching(window: EmbrWindow) -> None:
    for name in ("Alpha a", "Bravo a"):
        window.library.create_project(name)
    tree = _shown(window)
    window.sidebar_panel.search.setText("a")
    QApplication.processEvents()
    _press_and_move(
        tree,
        _at(tree, _row(tree, "project", "Alpha a")),
        _at(tree, _row(tree, "project", "Bravo a"), 8),
    )
    assert not tree.sorting()
    _mouse(tree, QEvent.Type.MouseButtonRelease, QPoint(5, 5), held=False)
    assert window.library.project_order() == []


def test_ungrouped_and_a_newer_projects_groups_stay_put(window: EmbrWindow) -> None:
    project_id = _station(window)
    future = window.library.create_project("Future")
    window.library.add_group(future.id, "A")
    window.library.add_group(future.id, "B")
    file = window.library._ctx.paths.projects / f"{future.id}.toml"
    doc = tomllib.loads(file.read_text(encoding="utf-8"))
    doc["schema_version"] = PROJECT_SCHEMA_VERSION + 1
    file.write_text(tomli_w.dumps(doc), encoding="utf-8")
    tree = _shown(window)

    _press_and_move(
        tree, _at(tree, _row(tree, "group", "")), _at(tree, _row(tree, "group", "Main"))
    )
    assert not tree.sorting()  # Ungrouped is fixed
    _mouse(tree, QEvent.Type.MouseButtonRelease, QPoint(5, 5), held=False)
    _press_and_move(tree, _at(tree, _row(tree, "group", "B")), _at(tree, _row(tree, "group", "A")))
    assert not tree.sorting()  # a newer Embr's project: its file is not written
    _mouse(tree, QEvent.Type.MouseButtonRelease, QPoint(5, 5), held=False)
    QApplication.processEvents()
    _drag(
        tree,
        _at(tree, _row(tree, "project", "Future")),
        _at(tree, _row(tree, "project", "Station"), -10),
    )
    assert _project_names(window) == ["Future", "Station"]  # its place is not in its file
    assert _groups(window, project_id) == ["Main", "Display", "Radio"]


def test_folds_survive_a_reorder(window: EmbrWindow) -> None:
    project_id = _station(window)
    window.library.create_project("Alpha")
    tree = _shown(window)
    window.sidebar_panel._toggle(f"g:{project_id}:Display")
    window.sidebar_panel._toggle(f"p:{window.library.tree().projects[0].id}")  # Alpha folded
    QApplication.processEvents()
    _drag(
        tree, _at(tree, _row(tree, "project", "Alpha")), _at(tree, _row(tree, "group", "Radio"), 30)
    )
    assert _project_names(window) == ["Station", "Alpha"]
    assert window.sidebar_panel._folded == {
        f"g:{project_id}:Display",
        f"p:{window.library.tree().projects[1].id}",
    }


def test_a_refresh_during_a_drag_waits_for_the_drop(window: EmbrWindow) -> None:
    for name in ("Alpha", "Bravo"):
        window.library.create_project(name)
    tree = _shown(window)
    first = tree.topLevelItem(1)
    _press_and_move(
        tree,
        _at(tree, _row(tree, "project", "Alpha")),
        _at(tree, _row(tree, "project", "Bravo"), 8),
    )
    window.library.create_project("Charlie")
    window.changed()  # e.g. a refresh round
    assert tree.topLevelItem(1) is first  # the rows under the drag stay
    _mouse(tree, QEvent.Type.MouseButtonRelease, _at(tree, _row(tree, "project", "Bravo"), 8))
    QApplication.processEvents()
    assert _project_names(window) == ["Bravo", "Alpha", "Charlie"]
    kinds = [tree.node(tree.topLevelItem(i)) for i in range(tree.topLevelItemCount())]
    assert sum(1 for n in kinds if n is not None and n.kind == "project") == 3


def test_a_firmware_row_is_not_reordered(window: EmbrWindow) -> None:
    project_id = _station(window)
    tree = _shown(window)
    firmware = next(
        tree.node(tree.topLevelItem(i))
        for i in range(tree.topLevelItemCount())
        if (n := tree.node(tree.topLevelItem(i))) is not None and n.kind == "firmware"
    )
    assert firmware is not None and not tree._can_sort(firmware)
    assert _groups(window, project_id) == ["Main", "Display", "Radio"]


def test_a_drag_keeps_the_selection(window: EmbrWindow) -> None:
    """The press on the dragged project row does not select it (a drag is not a click): the
    firmware shown stays selected, before and after the drop."""
    project_id = _station(window)
    window.library.create_project("Alpha")
    tree = _shown(window)
    shown = window.library.project_firmware(project_id)[0]
    window.select_firmware(shown.id)
    QApplication.processEvents()
    selected = tree.currentItem()
    assert selected is not None and tree.node(selected).firmware_id == shown.id  # type: ignore[union-attr]

    _press_and_move(
        tree, _at(tree, _row(tree, "project", "Alpha")), _at(tree, _row(tree, "group", "Radio"), 30)
    )
    assert tree.sorting() and tree.currentItem() is selected
    _mouse(tree, QEvent.Type.MouseButtonRelease, QPoint(-5, -5), held=False)  # outside: back
    QApplication.processEvents()
    current = tree.currentItem()
    assert current is not None and tree.node(current).firmware_id == shown.id  # type: ignore[union-attr]


def test_a_selected_row_off_screen_keeps_its_colour(window: EmbrWindow) -> None:
    for index in range(12):
        project = window.library.create_project(f"P{index:02}")
        window.library.create_firmware(f"fw {index:02}", project.id)
    tree = _shown(window)
    last = window.library.project_firmware(window.library.tree().projects[-1].id)[0]
    window.select_firmware(last.id)
    tree.verticalScrollBar().setValue(0)
    QApplication.processEvents()
    row = next(
        tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
        if (n := tree.node(tree.topLevelItem(i))) is not None and n.firmware_id == last.id
    )
    assert row is not None and row.isSelected()
    assert not tree.viewport().rect().contains(tree.visualItemRect(row))  # off screen

    _press_and_move(
        tree, _at(tree, _row(tree, "project", "P00")), _at(tree, _row(tree, "project", "P01"), 20)
    )
    sort = tree._sort
    assert sort is not None
    block = next(b for b in sort.spans if b.key == last.project_id)
    rows = [tree.topLevelItem(i) for i in range(block.first, tree.indexOfTopLevelItem(row))]
    above = sum(tree.indexRowSizeHint(tree.indexFromItem(r)) for r in rows if r is not None)
    picture = sort.overlay._pictures[block.key].toImage()
    colour = picture.pixelColor(8, above + 4).name()
    assert colour == window._palette.sel.lower()
    _mouse(tree, QEvent.Type.MouseButtonRelease, QPoint(-5, -5), held=False)
