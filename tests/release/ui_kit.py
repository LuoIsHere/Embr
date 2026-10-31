"""Helpers for the interface-flow release tests (test_ui_*.py), on top of release_kit.

Everything acts on the real EmbrWindow through its visible controls, as release_kit does.
Additions here:

* LANGUAGES: parametrize a test over English and Simplified Chinese;
* choose(): pick a combo box item with the keyboard (Home, Down…), which a mouse moved
  by another program on the same desktop cannot disturb (an open popup list follows the
  real cursor);
* menu(): choose an item of a popup menu, including the danger-coloured Delete… item
  (a QWidgetAction whose QAction has no text); menu_items() lists a menu's items;
* drops: drop_files() / drop_firmware() deliver Qt drag-enter / drag-move / drop events
  at a widget position, as Qt does for a Windows drag (a real OLE drag cannot be driven
  from the test process: DoDragDrop polls the physical mouse buttons);
* the entries table (entries(), entry(), set_address(), set_note(), tick()), the header,
  banner and checks list, the sidebar (row_texts(), firmware_id());
* documents on disk (firmware_doc(), project_doc(), data_snapshot());
* shown_text() (what a QLabel really displays, rich text resolved), clipped() (widgets
  cut off by their parents), english_leftovers() (untranslated English in the Chinese
  interface).
"""

from __future__ import annotations

import contextlib
import os
import re
import tomllib
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import release_kit as kit
from PySide6.QtCore import QCoreApplication, QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QWidget,
    QWidgetAction,
)

from embr.i18n import Language, tr

LANGUAGES = pytest.mark.parametrize(
    "language", [Language.ENGLISH, Language.ZH_CN], ids=["en", "zh_CN"]
)
FIRMWARE_MIME = "application/x-embr-firmware"

# release_kit internals used here and by the test_ui_* modules (one place to follow them)
plain = kit._plain  # pyright: ignore[reportPrivateUsage]
"""Display form of a text (mnemonics, menu shortcuts and markup removed)."""
dialog_stack = kit._dialog_stack  # pyright: ignore[reportPrivateUsage]
"""Visible dialogs, the active modal one last."""
layout_widgets = kit._layout_widgets  # pyright: ignore[reportPrivateUsage]
"""Every widget in a layout, nested ones included."""


# ---- combo boxes and menus --------------------------------------------------------------


def choose(combo: QComboBox, text: str, *, contains: bool = False) -> None:
    """Select the item showing `text` (English source text, see release_kit.variants):
    a click opens the list, then the keyboard (Home, Down… until the item is current,
    Enter) chooses it, so the combo box emits `activated` once. The keyboard is used in
    the list because another program moving the real mouse over an open list changes its
    current item. The combo box may be rebuilt (deleted) by the window afterwards."""
    import shiboken6

    if not combo.isVisible() or not combo.isEnabled():
        pytest.fail(f"combo box {combo.currentText()!r} is not usable")
    wanted = kit.variants(text)
    texts = [plain(combo.itemText(i)) for i in range(combo.count())]
    hits = [
        i for i, t in enumerate(texts) if (any(w in t for w in wanted) if contains else t in wanted)
    ]
    if len(hits) != 1:
        pytest.fail(f"{len(hits)} items for {text!r} in combo box; items: {texts}")
    target = hits[0]
    if combo.currentIndex() == target:
        return
    kit.click(combo)
    view = combo.view()
    kit.wait_until(view.isVisible, 5, message="combo box list did not open")
    QTest.keyClick(view, Qt.Key.Key_Home)
    for _ in range(combo.count() + 1):
        if view.currentIndex().row() == target:
            break
        QTest.keyClick(view, Qt.Key.Key_Down)
    if view.currentIndex().row() != target:
        QTest.keyClick(view, Qt.Key.Key_Escape)
        pytest.fail(f"item {text!r} cannot be reached with the keyboard; items: {texts}")
    QTest.keyClick(view, Qt.Key.Key_Return)
    kit.pump(30)
    if shiboken6.isValid(combo) and combo.currentIndex() != target:
        pytest.fail(f"combo box shows {combo.currentText()!r}, not {text!r}; items: {texts}")


def pick(driver: kit.DialogDriver, caption: str, text: str, *, contains: bool = False) -> None:
    """DialogDriver.pick() with choose()."""
    choose(driver.combo(caption), text, contains=contains)


def action_text(action: Any) -> str:
    """A menu action's text; a QWidgetAction (the danger Delete… item) shows its label."""
    text = str(action.text())
    if not text and isinstance(action, QWidgetAction):
        widget = action.defaultWidget()
        if isinstance(widget, QLabel):
            text = widget.text()
    return plain(text)


def menu_items(menu: QMenu) -> list[str]:
    return [action_text(a) for a in menu.actions() if not a.isSeparator() and a.isVisible()]


@dataclass
class MenuSeen:
    items: list[str]
    enabled: dict[str, bool]
    danger: list[str]
    """Items drawn as danger labels (QWidgetAction with property menuitem=danger)."""


@contextlib.contextmanager
def menu(*path: str, seen: list[MenuSeen] | None = None, timeout: float = 10.0) -> Generator[None]:
    """As release_kit.expect_menu, also for the Delete… item; an empty `path` only looks
    at the menu (collected into `seen`) and closes it."""
    import time

    found: list[QMenu] = []
    done = [False]
    errors: list[BaseException] = []

    def poll() -> None:
        if done[0]:
            return
        popup = QApplication.activePopupWidget()
        if not isinstance(popup, QMenu) or not popup.isVisible() or popup in found:
            return
        found.append(popup)
        done[0] = True
        try:
            act_on(popup)
        except BaseException as exc:  # pytest.fail inside a nested event loop
            errors.append(exc)
            with contextlib.suppress(RuntimeError):
                popup.close()

    def act_on(popup: QMenu) -> None:
        current = popup
        if seen is not None:
            seen.append(_seen(popup))
        if not path:
            popup.close()
            return
        for depth, text in enumerate(path):
            action = _find_action(current, text)
            if not action.isEnabled():
                pytest.fail(f"menu item {text!r} is disabled; menu: {menu_items(current)}")
            at = current.actionGeometry(action).center()
            widget = current.childAt(at) if isinstance(action, QWidgetAction) else None
            target = widget if widget is not None else current
            local = target.mapFrom(current, at) if widget is not None else at
            QTest.mouseClick(
                target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, local
            )
            if depth < len(path) - 1:
                sub = action.menu()
                if not isinstance(sub, QMenu):
                    pytest.fail(f"menu item {text!r} has no submenu")
                kit.wait_until(sub.isVisible, 5, message=f"submenu {text!r} did not open")
                current = sub

    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.setInterval(30)
    timer.timeout.connect(poll)
    timer.start()
    started = time.monotonic()
    try:
        yield
        while not done[0]:
            if time.monotonic() - started > timeout:
                pytest.fail(f"menu {' > '.join(path) or '(look)'} did not appear")
            QTest.qWait(30)
    finally:
        timer.stop()
    if errors:
        raise errors[0]


def _find_action(menu: QMenu, text: str) -> Any:
    wanted = kit.variants(text)
    hits = [a for a in menu.actions() if a.isVisible() and action_text(a) in wanted]
    if len(hits) != 1:
        pytest.fail(f"{len(hits)} menu items for {text!r}; menu: {menu_items(menu)}")
    return hits[0]


def _seen(popup: QMenu) -> MenuSeen:
    items = menu_items(popup)
    enabled = {action_text(a): a.isEnabled() for a in popup.actions() if not a.isSeparator()}
    danger: list[str] = []
    for action in popup.actions():
        if isinstance(action, QWidgetAction):
            widget = cast(QWidget | None, action.defaultWidget())
            if widget is not None and widget.property("menuitem") == "danger":
                danger.append(action_text(action))
    return MenuSeen(items, enabled, danger)


def firmware_menu(window: Any, *path: str, seen: list[MenuSeen] | None = None) -> None:
    """The header's ⋯ menu."""
    with menu(*path, seen=seen):
        kit.click(window.header.menu_button)


def context_menu_on_firmware(
    window: Any, name: str, *path: str, seen: list[MenuSeen] | None = None
) -> None:
    """Right-click a firmware row in the sidebar and choose `path`."""
    tree = window.sidebar_panel.tree
    item = kit.sidebar_row(window, name)
    tree.scrollToItem(item)
    kit.pump()
    at = tree.visualItemRect(item).center()
    with menu(*path, seen=seen):
        kit.context_menu(tree.viewport(), at)


def add_entry_menu(window: Any, item: str) -> None:
    """Entries tab: "+ Add entry" → Files… / Attachment…"""
    with menu(item):
        kit.click(window.firmware_view.entries.add_button)


# ---- dialogs ----------------------------------------------------------------------------


def press(text: str) -> Callable[[kit.DialogDriver], None]:
    def handler(driver: kit.DialogDriver) -> None:
        driver.press(text)

    return handler


def cancel(driver: kit.DialogDriver) -> None:
    driver.cancel()


def ok(driver: kit.DialogDriver) -> None:
    driver.ok()


def record_and_ok(store: list[str], *, joined: bool = False) -> Callable[[kit.DialogDriver], None]:
    """A handler that keeps the dialog's text (joined: every text incl. the title, " | ")
    in `store`, then presses OK."""

    def handler(driver: kit.DialogDriver) -> None:
        store.append(" | ".join(dialog_texts(driver)) if joined else driver.text())
        driver.ok()

    return handler


def type_and_ok(caption: str, text: str) -> Callable[[kit.DialogDriver], None]:
    """A handler that types `text` into the field `caption`, then presses OK."""

    def handler(driver: kit.DialogDriver) -> None:
        driver.type(caption, text)
        driver.ok()

    return handler


def is_danger(button: QAbstractButton) -> bool:
    """The red confirm button of a destructive dialog (style kind danger / danger-small)."""
    return str(button.property("kind") or "").startswith("danger")


@contextlib.contextmanager
def no_dialog(seconds: float = 1.5) -> Generator[list[str]]:
    """Fails when a dialog opens inside the block (it is rejected); yields the titles."""
    titles: list[str] = []
    before = {id(d) for d in dialog_stack()}

    from PySide6.QtCore import QTimer

    def poll() -> None:
        for dialog in dialog_stack():
            if id(dialog) not in before:
                titles.append(dialog.windowTitle())
                before.add(id(dialog))
                dialog.reject()

    timer = QTimer()
    timer.setInterval(30)
    timer.timeout.connect(poll)
    timer.start()
    try:
        yield titles
        QTest.qWait(int(seconds * 1000))
    finally:
        timer.stop()
    if titles:
        pytest.fail(f"unexpected dialog(s): {titles}")


# ---- sidebar ----------------------------------------------------------------------------


def node_id(window: Any, name: str, kind: str = "firmware") -> str:
    item = kit.sidebar_row(window, name, kind)
    node = window.sidebar_panel.tree.node(item)
    value = node.firmware_id if kind == "firmware" else node.project_id
    assert isinstance(value, str)
    return value


def firmware_id(window: Any, name: str) -> str:
    return node_id(window, name, "firmware")


def project_id(window: Any, name: str) -> str:
    return node_id(window, name, "project")


def row_widget(window: Any, item: Any) -> QWidget:
    widget = window.sidebar_panel.tree.itemWidget(item, 0)
    assert isinstance(widget, QWidget)
    return widget


def row_texts(window: Any, name: str, kind: str = "firmware") -> list[str]:
    """Every visible text of a sidebar row (name, origin, tags, count)."""
    kit.pump()
    return kit.all_texts(row_widget(window, kit.sidebar_row(window, name, kind)))


def group_row(window: Any, project: str, group: str) -> Any:
    """The tree item of `group` ("" = Ungrouped) under the project named `project`."""
    pid = project_id(window, project)
    for item, node, _ in kit.sidebar_rows(window):
        if node and node.kind == "group" and node.project_id == pid and node.device == group:
            return item
    shown = [(n.kind, n.device, s) for _, n, s in kit.sidebar_rows(window) if n]
    pytest.fail(f"no group {group!r} under {project!r}; rows: {shown}")


def standalone_header(window: Any) -> Any:
    for item, node, _ in kit.sidebar_rows(window):
        if node and node.kind == "header" and node.drop == "standalone":
            return item
    pytest.fail("no STANDALONE FIRMWARE header")


def placement_of(window: Any, name: str) -> tuple[str | None, str]:
    """(project name or None, group) of the sidebar firmware row `name`, from the tree."""
    node = window.sidebar_panel.tree.node(kit.sidebar_row(window, name))
    if node.project_id is None:
        return None, ""
    project = next(
        s
        for _, n, s in kit.sidebar_rows(window)
        if n and n.kind == "project" and n.project_id == node.project_id
    )
    return project, node.device


def search(window: Any, text: str) -> None:
    kit.type_text(window.sidebar_panel.search, text)
    kit.pump(50)


# ---- drag and drop ----------------------------------------------------------------------


def drop(widget: QWidget, at: QPoint, mime: QMimeData, action: Qt.DropAction) -> bool:
    """Drag-enter, drag-move and drop at `at` (widget coordinates), delivered through
    QApplication (which hands drag events up to the first parent that accepts drops).
    True when the drop was accepted."""
    # as QWidgetWindow does: the widget under the cursor, else the first parent that
    # accepts drops, with the position mapped into it
    while not widget.acceptDrops():
        parent = widget.parentWidget()
        if parent is None:
            return False
        at = widget.mapToParent(at)
        widget = parent
    actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
    buttons = Qt.MouseButton.LeftButton
    mods = Qt.KeyboardModifier.NoModifier
    import warnings

    with warnings.catch_warnings():  # PySide 6.12 flags the QPoint overloads, the only ones
        warnings.simplefilter("ignore", DeprecationWarning)
        enter = QDragEnterEvent(at, actions, mime, buttons, mods)
        move = QDragMoveEvent(at, actions, mime, buttons, mods)
    QCoreApplication.sendEvent(widget, enter)
    move.setAccepted(enter.isAccepted())  # as QWidgetWindow does after an accepted enter
    QCoreApplication.sendEvent(widget, move)
    kit.pump()
    if not move.isAccepted():
        return False
    drop = QDropEvent(QPointF(at), actions, mime, buttons, mods)
    drop.setDropAction(action)
    QCoreApplication.sendEvent(widget, drop)
    kit.pump()
    return drop.isAccepted()


def files_mime(paths: Sequence[Path]) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def drop_files_on_row(window: Any, item: Any, paths: Sequence[Path]) -> bool:
    """Files from Explorer dropped on a sidebar row (project, group, header, firmware)."""
    tree = window.sidebar_panel.tree
    tree.scrollToItem(item)
    kit.pump()
    at = tree.visualItemRect(item).center()
    return drop(tree.viewport(), at, files_mime(paths), Qt.DropAction.CopyAction)


def drop_files_on_entries(window: Any, paths: Sequence[Path]) -> bool:
    table = window.firmware_view.entries.table
    at = QPoint(table.viewport().width() // 2, max(table.viewport().height() - 4, 2))
    return drop(table.viewport(), at, files_mime(paths), Qt.DropAction.CopyAction)


def drag_firmware(window: Any, name: str, target: Any) -> bool:
    """A firmware row dragged onto `target` (a tree item: project, group, header, spare)."""
    tree = window.sidebar_panel.tree
    mime = QMimeData()
    mime.setData(FIRMWARE_MIME, firmware_id(window, name).encode())
    tree.scrollToItem(target)
    kit.pump()
    at = tree.visualItemRect(target).center()
    return drop(tree.viewport(), at, mime, Qt.DropAction.MoveAction)


# ---- the workspace ----------------------------------------------------------------------


def open_tab(window: Any, name: str) -> None:
    """Entries / Parameters / Records."""
    view = window.firmware_view
    for button in view.tab_group.buttons():
        if kit.same_text(button.text(), name):
            kit.click(button)
            return
    pytest.fail(f"no tab {name!r}")


def header_texts(window: Any) -> list[str]:
    kit.pump()  # widgets added to a shown layout become visible on the next pass
    return kit.all_texts(window.header)


def header_tags(window: Any) -> list[str]:
    kit.pump()
    return [
        plain(w.text())
        for w in kit.find_all(window.header, QLabel)
        if w.property("tag") is not None
    ]


def banner(window: Any) -> str:
    """The banner's text (display form), "" when hidden."""
    kit.pump()
    view = window.firmware_view
    if not view.banner.isVisible():
        return ""
    return shown_text(view.banner.text)


def banner_button(window: Any) -> QAbstractButton:
    return cast(QAbstractButton, window.firmware_view.banner.action)


@dataclass
class Check:
    state: str
    title: str
    detail: str


def checks(window: Any) -> list[Check]:
    """The right column's checks list: (PASS / WARN / REFUSE / LATER, title, detail)."""
    kit.pump()
    layout = window.connection.checks
    found: list[Check] = []
    for index in range(layout.count()):
        row = layout.itemAt(index)
        inner = row.layout() if row is not None else None
        if inner is None:
            continue
        labels = [w for w in layout_widgets(inner) if isinstance(w, QLabel)]
        texts = [plain(w.text()) for w in labels]
        if texts:
            found.append(Check(texts[0], texts[1] if len(texts) > 1 else "", " ".join(texts[2:])))
    return found


def check(window: Any, title: str) -> Check:
    hits = [c for c in checks(window) if kit.same_text(c.title, title)]
    if len(hits) != 1:
        pytest.fail(f"{len(hits)} checks titled {title!r}: {checks(window)}")
    return hits[0]


def has_check(window: Any, title: str, state: str | None = None) -> bool:
    return any(
        kit.same_text(c.title, title) and (state is None or c.state == state)
        for c in checks(window)
    )


def wait_check(window: Any, title: str, state: str, timeout: float = 10.0) -> None:
    kit.wait_until(
        lambda: has_check(window, title, state),
        timeout,
        message=lambda: f"no {state} {title!r}: {checks(window)}",
    )


@dataclass
class EntryRow:
    row: int
    enabled: bool
    checkable: bool
    address: str
    address_read_only: bool
    mcu: str
    mcu_choosable: bool
    name: str
    tags: list[str]
    kind: str
    source: str
    requires: str
    note: str
    tooltip: str


def entries(window: Any) -> list[EntryRow]:
    """Rows of the entries table, top to bottom."""
    kit.pump()
    from embr.ui.widgets import ElidedLabel

    table = window.firmware_view.entries.table
    rows: list[EntryRow] = []
    for r in range(table.rowCount()):
        check_item = table.item(r, 0)
        address = table.cellWidget(r, 1).findChild(QLineEdit)
        mcu_cell = table.cellWidget(r, 2)
        combo = mcu_cell.findChild(QComboBox)
        mcu_label = mcu_cell.findChild(QLabel)
        mcu = combo.currentText() if combo is not None else (mcu_label.text() if mcu_label else "")
        name_cell = table.cellWidget(r, 3)
        names = name_cell.findChildren(ElidedLabel)
        tags = [
            plain(w.text()) for w in name_cell.findChildren(QLabel) if w.property("tag") is not None
        ]
        note = table.cellWidget(r, 7).findChild(QLineEdit)
        rows.append(
            EntryRow(
                r,
                check_item.checkState() == Qt.CheckState.Checked,
                bool(check_item.flags() & Qt.ItemFlag.ItemIsUserCheckable),
                address.text(),
                address.isReadOnly(),
                mcu,
                combo is not None,
                names[0].full_text() if names else "",
                tags,
                table.item(r, 4).text(),
                table.item(r, 5).text(),
                table.item(r, 6).text(),
                note.text(),
                name_cell.toolTip(),
            )
        )
    return rows


def entry(window: Any, name: str) -> EntryRow:
    hits = [e for e in entries(window) if e.name == name]
    if len(hits) != 1:
        pytest.fail(f"{len(hits)} entries named {name!r}: {[e.name for e in entries(window)]}")
    return hits[0]


def entry_names(window: Any) -> list[str]:
    return [e.name for e in entries(window)]


def _cell_edit(window: Any, name: str, column: int) -> QLineEdit:
    table = window.firmware_view.entries.table
    row = entry(window, name).row
    edit = table.cellWidget(row, column).findChild(QLineEdit)
    assert isinstance(edit, QLineEdit)
    return edit


def set_address(window: Any, name: str, text: str) -> None:
    """Type into the entry's address cell and press Enter (editingFinished)."""
    kit.click(_cell_edit(window, name, 1))
    kit.pump(30)  # focus leaving another cell may refresh the table: find the cell again
    edit = _cell_edit(window, name, 1)
    edit.setFocus()
    QTest.keyClick(edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(edit, Qt.Key.Key_Delete)
    if text:
        QTest.keyClicks(edit, text)
    QTest.keyClick(edit, Qt.Key.Key_Return)
    kit.pump(50)


def set_note(window: Any, name: str, text: str) -> None:
    kit.click(_cell_edit(window, name, 7))
    kit.pump(30)
    edit = _cell_edit(window, name, 7)
    edit.setFocus()
    kit.type_text(edit, text, finish="return")
    kit.pump(50)


def tick(window: Any, name: str, on: bool) -> None:
    """Click the entry's check box (column 0) when its state differs from `on`."""
    table = window.firmware_view.entries.table
    row = entry(window, name)
    if row.enabled == on:
        return
    rect = table.visualRect(table.model().index(row.row, 0))
    QTest.mouseClick(
        table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center()
    )
    kit.pump(50)
    if entry(window, name).enabled != on:
        pytest.fail(f"entry {name!r} did not toggle")


def entry_context_menu(
    window: Any, name: str, *path: str, seen: list[MenuSeen] | None = None
) -> None:
    table = window.firmware_view.entries.table
    row = entry(window, name).row
    rect = table.visualRect(table.model().index(row, 4))
    with menu(*path, seen=seen):
        kit.context_menu(table.viewport(), rect.center())


def choose_entry_mcu(window: Any, name: str, text: str) -> None:
    table = window.firmware_view.entries.table
    combo = table.cellWidget(entry(window, name).row, 2).findChild(QComboBox)
    if not isinstance(combo, QComboBox):
        pytest.fail(f"entry {name!r} has no MCU chooser")
    choose(combo, text)
    kit.pump(50)


def param_widget(window: Any, caption: str) -> QWidget:
    """The widget right of a caption on the Parameters tab."""
    grid = window.firmware_view.params.grid
    wanted = kit.variants(caption)
    for index in range(grid.count()):
        item = grid.itemAt(index)
        widget = item.widget() if item is not None else None
        if isinstance(widget, QLabel) and plain(widget.text()) in wanted:
            row, _, _, _ = grid.getItemPosition(index)
            cell = grid.itemAtPosition(row, 1)
            if cell is not None and cell.widget() is not None:
                return cast(QWidget, cell.widget())
    pytest.fail(f"no parameter {caption!r}")


def param_combos(window: Any, caption: str) -> list[QComboBox]:
    return [c for c in param_widget(window, caption).findChildren(QComboBox) if c.isVisible()]


def param_edit(window: Any, caption: str) -> QLineEdit:
    widget = param_widget(window, caption)
    edit = widget if isinstance(widget, QLineEdit) else widget.findChild(QLineEdit)
    assert isinstance(edit, QLineEdit)
    return edit


def param_texts(window: Any, caption: str) -> list[str]:
    return kit.all_texts(param_widget(window, caption))


# ---- files and documents ----------------------------------------------------------------


def firmware_doc(root: Path, firmware: str) -> dict[str, Any]:
    return tomllib.loads((root / "data" / "firmwares" / f"{firmware}.toml").read_text("utf-8"))


def project_doc(root: Path, project: str) -> dict[str, Any]:
    return tomllib.loads((root / "data" / "projects" / f"{project}.toml").read_text("utf-8"))


def firmware_docs(root: Path) -> list[dict[str, Any]]:
    folder = root / "data" / "firmwares"
    return [tomllib.loads(p.read_text("utf-8")) for p in sorted(folder.glob("*.toml"))]


def project_docs(root: Path) -> list[dict[str, Any]]:
    folder = root / "data" / "projects"
    return [tomllib.loads(p.read_text("utf-8")) for p in sorted(folder.glob("*.toml"))]


def doc_named(docs: Iterable[dict[str, Any]], name: str) -> dict[str, Any]:
    hits = [d for d in docs if d.get("name") == name]
    if len(hits) != 1:
        pytest.fail(f"{len(hits)} documents named {name!r}: {[d.get('name') for d in docs]}")
    return hits[0]


def archive_files(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "data" / "archive").glob("*.bin"))


def data_snapshot(root: Path) -> kit.Snapshot:
    """Projects, firmware and archive files (not the database, which records sessions)."""
    found: kit.Snapshot = {}
    for folder in ("projects", "firmwares", "archive"):
        for rel, value in kit.snapshot(root / "data" / folder).items():
            found[f"{folder}/{rel}"] = value
    return found


def copy_files(sources: Iterable[Path], folder: Path) -> list[Path]:
    import shutil

    folder.mkdir(parents=True, exist_ok=True)
    return [Path(shutil.copy(s, folder / s.name)) for s in sources]


def make_old(path: Path, age: float = 3600) -> None:
    import os
    import time

    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


# ---- what is shown ----------------------------------------------------------------------


def _rich(text: str) -> bool:
    """Qt::mightBeRichText (exposed on QtGui's Qt namespace in PySide6)."""
    import PySide6.QtGui as gui

    return bool(gui.Qt.mightBeRichText(text))  # type: ignore[attr-defined]


def shown_text(label: QLabel) -> str:
    """What a label displays: its text, or for rich text the document's plain text."""
    text = label.text()
    rich = label.textFormat() == Qt.TextFormat.RichText or (
        label.textFormat() == Qt.TextFormat.AutoText and _rich(text)
    )
    if not rich:
        return " ".join(text.split())
    doc = QTextDocument()
    doc.setHtml(text)
    return " ".join(doc.toPlainText().split())


def visible_widgets(root: QWidget) -> list[QWidget]:
    return [w for w in [root, *root.findChildren(QWidget)] if w.isVisible()]


def clipped(
    root: QWidget,
    *,
    kinds: tuple[type[QWidget], ...] = (QLabel, QAbstractButton, QLineEdit, QComboBox, QCheckBox),
) -> list[str]:
    """Visible widgets of `kinds` under `root` that stick out of a visible ancestor up to
    `root` (cut off), or whose text needs more room than they have (labels that do not
    elide or wrap, buttons). Scroll area contents are measured against their viewport."""
    from PySide6.QtWidgets import QAbstractScrollArea

    from embr.ui.widgets import ElidedLabel

    problems: list[str] = []
    for widget in visible_widgets(root):
        if not isinstance(widget, kinds) or widget.width() <= 0:
            continue
        name = f"{type(widget).__name__} {plain(kit.text_of(widget))!r}"
        rect = QRect(widget.mapTo(root, QPoint(0, 0)), widget.size())
        parent = widget.parentWidget()
        while parent is not None and parent is not root.parentWidget():
            if isinstance(parent, QAbstractScrollArea) or isinstance(
                parent.parentWidget(), QAbstractScrollArea
            ):
                break  # a scroll area or its viewport: the contents may scroll
            outer = QRect(parent.mapTo(root, QPoint(0, 0)), parent.size())
            if not outer.contains(rect):
                problems.append(f"{name} outside {type(parent).__name__} {outer} ({rect})")
                break
            if parent is root:
                break
            parent = parent.parentWidget()
        if isinstance(widget, ElidedLabel) or (isinstance(widget, QLabel) and widget.wordWrap()):
            continue
        if isinstance(widget, QLabel) and not widget.pixmap().isNull() and not widget.text():
            continue
        if isinstance(widget, (QLabel, QAbstractButton)) and kit.text_of(widget):
            text = plain(kit.text_of(widget))
            if isinstance(widget, QLabel) and (
                widget.textFormat() == Qt.TextFormat.RichText or _rich(widget.text())
            ):
                text = shown_text(widget)
            need = widget.fontMetrics().horizontalAdvance(text)
            room = widget.contentsRect().width()
            if isinstance(widget, QAbstractButton):
                room -= 8  # padding inside the frame
            if need > room + 1 and "\n" not in kit.text_of(widget):
                problems.append(f"{name} needs {need} px, has {room}")
    return problems


KEEP_ENGLISH = {
    # design doc §12 item 10, internal_docs/m7_glossary.md "保持英文"
    "pass",
    "warn",
    "refuse",
    "later",
    "ok",
    "refused",
    "failed",
    "cancelled",
    "idf",
    "pio",
    "arc",
    "derived",
    "in",
    "download",
    "mode",
    "flash",
    "multi",
    "chip",
    "version",
    "default-reset",
    "hard-reset",
    "watchdog-reset",
    "no-reset",
    "usb-reset",
    "dio",
    "qio",
    "qout",
    "dout",
    "keep",
    "detect",
    "usb-serial/jtag",
    "usb-otg",
    "usb",
    "otg",
    "serial",
    "jtag",
    "embr",
    "esp-idf",
    "platformio",
    "esptool",
    "env",
    "mcu",
    "mac",
    "idf.py",
    "flasher_args.json",
    "platformio.ini",
    "idedata.json",
    "sdkconfig",
    "boot_app0.bin",
    "bootloader",
    "app",
    "partition_table",
    "otadata",
    "fs",
    "merged",
    "attachment",
    "unknown",
    "ctrl",
    "enter",
    "esc",
    "f2",
    "english",
    "hash",
    "uart",
    "nvs",
    "wi-fi",
    "auto",
    "manual",
    "partition",
    "erase_all",
    "sha256",
    "bin",
    "copy",
    "gpl",
    "or",
    "github",
    "com",
    "luoishere",
    "releases",
    "harmonyos",
    "sans",
    "third_party_notices",
    "huawei",
    "device",
    "co",
    "ltd",
    "license",
    "agreement",
    "fonts",
    "copyright",
    "data",
    "logs",
    "temp",
    "settings.ini",
    "build",
    "pio\\build",
    "framework-arduinoespressif32",
    "esp",
    "secure",
    "boot",
    "spiffs",
    "littlefs",
    "fatfs",
    "app0",
    "lnk",
    "elf",
    "ff",
    "x",
    "s",
    "kb",
    "mb",
    "zip",
    "rev",
    "v",
    "pkg",
    "framework_pkg",
    "dump",
    "record",
    "id",
    "ulid",
    "before",
    "after",
    "release",
    "s3",
    "write-flash",
    "skip-flashed",
    "erase-all",
    "arduino",
    "s3-main-v1.3",
}
"""Words the glossary keeps in English, file and product names, units (lower case)."""

WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-./\\]*")
CHIP_WORD = re.compile(r"^esp32[a-z0-9\-]*$|^v\d", re.IGNORECASE)


def english_leftovers(texts: Iterable[str], data: Iterable[str] = ()) -> list[str]:
    """Texts of the Chinese interface holding English words that are not kept in English
    by the glossary, not file names, paths, numbers or chip names, and not part of the
    test's own data (`data`: names, paths, file names the test created)."""
    known = sorted({d for d in data if d}, key=len, reverse=True)
    found: list[str] = []
    for text in texts:
        if text.startswith("esptool "):
            continue  # an equivalent command, as esptool spells it
        rest = text
        for value in known:  # the test's own paths first (they may hold spaces)
            if "\\" in value or " " in value:
                rest = rest.replace(value, " ")
        rest = re.sub(r"[A-Za-z]:\\[^\s，。；：]*", " ", rest)  # Windows paths  # noqa: RUF001
        for value in known:
            rest = rest.replace(value, " ")
        rest = re.sub(r"\S*…", " ", rest)  # elided text: a cut-off name
        rest = re.sub(r"data/archive/\S*", " ", rest)
        rest = re.sub(r"0x[0-9A-Fa-f]+", " ", rest)
        rest = re.sub(r"\bCOM\d+\b|\b[0-9A-F]{4}:[0-9A-F]{4}\b", " ", rest)  # ports, VID:PID
        rest = re.sub(r"\blogs/[\w/.]*", " ", rest)
        rest = re.sub(r"\b[\w\-.]+\.(bin|json|ini|embr|toml|log|txt|csv|elf|py)\b", " ", rest)
        words = [
            w.strip(".-/\\")
            for w in WORD.findall(rest)
            if w.strip(".-/\\").lower() not in KEEP_ENGLISH and not CHIP_WORD.match(w)
        ]
        words = [w for w in words if (len(w) > 1 and not w.isupper()) or len(w) > 4]
        if words:
            found.append(f"{text!r}: {', '.join(words)}")
    return found


def window_texts(window: Any) -> list[str]:
    """Visible texts of the window: labels (shown form), buttons, check boxes, line-edit
    placeholders, tab and table header texts, combo items, tooltips of visible widgets."""
    texts: list[str] = []
    kit.pump()  # widgets added to a shown layout become visible on the next pass
    command = window.cmdbar.text  # the equivalent command stays as esptool spells it
    for widget in visible_widgets(window):
        if widget is command:
            continue
        if isinstance(widget, QLabel):
            texts.append(shown_text(widget))
        elif isinstance(widget, QAbstractButton):
            texts.append(plain(kit.text_of(widget)))
        elif isinstance(widget, QLineEdit):
            texts.append(widget.placeholderText())
        elif isinstance(widget, QComboBox):
            texts += [widget.itemText(i) for i in range(widget.count())]
        if widget.toolTip():
            texts.append(plain(widget.toolTip()))
    table = window.firmware_view.entries.table
    if table.isVisible():
        texts += [
            table.horizontalHeaderItem(c).text()
            for c in range(table.columnCount())
            if table.horizontalHeaderItem(c) is not None
        ]
        texts += [
            table.item(r, c).toolTip()
            for r in range(table.rowCount())
            for c in range(table.columnCount())
            if table.item(r, c) is not None and table.item(r, c).toolTip()
        ]
    return [t for t in texts if t and t.strip()]


def dialog_texts(driver: kit.DialogDriver) -> list[str]:
    texts = [driver.title]
    for widget in visible_widgets(driver.dialog):
        if isinstance(widget, QLabel):
            texts.append(shown_text(widget))
        elif isinstance(widget, QAbstractButton):
            texts.append(plain(kit.text_of(widget)))
        elif isinstance(widget, QLineEdit):
            texts.append(widget.placeholderText())
        elif isinstance(widget, QComboBox):
            texts += [widget.itemText(i) for i in range(widget.count())]
    return [t for t in texts if t and t.strip()]


def in_chinese() -> bool:
    from embr.i18n import current

    return current() == Language.ZH_CN


def t(text: str) -> str:
    """tr() for building expected texts."""
    return tr(text)


def layout_texts(layout: QLayout) -> list[str]:
    return [
        plain(kit.text_of(w))
        for w in layout_widgets(layout)
        if isinstance(w, (QLabel, QAbstractButton)) and w.isVisible()
    ]


# ---- common flows through the New menu and the entries tab -------------------------------

STANDALONE = "Standalone (no project)"
UNGROUPED = "(ungrouped)"


def new_project(window: Any, name: str, notes: str = "") -> None:
    def fill(driver: kit.DialogDriver) -> None:
        driver.type("Name (unique)", name)
        if notes:
            driver.type("Notes", notes)
        driver.ok()

    with kit.expect_dialog(fill, title="New project"):
        kit.new_menu(window, "New project")


def new_firmware(
    window: Any, name: str, place: str | None = None, group: str | None = None
) -> None:
    """New menu → New firmware. place: a project name or STANDALONE (None keeps the
    default, the current project); group: a group name or UNGROUPED."""

    def fill(driver: kit.DialogDriver) -> None:
        driver.type("Name", name)
        if place is not None:
            pick(driver, "Place in", place)
        if group is not None:
            pick(driver, "Group", group)
        driver.ok()

    with kit.expect_dialog(fill, title="New firmware"):
        kit.new_menu(window, "New firmware")


def add_files(window: Any, dialogs: kit.FileDialogs, paths: Sequence[Path]) -> None:
    """Entries tab: + Add entry → Files… with `paths` from the file dialog."""
    dialogs.queue_open_many(list(paths))
    add_entry_menu(window, "Files…")
    kit.pump(50)


def add_group(window: Any, name: str) -> None:
    """Project overview: type into the new-group field and click Add."""
    edit = new_group_edit(window)
    kit.type_text(edit, name)
    add = [b for b in kit.find_all(window.project_view, QAbstractButton, "Add")]
    if len(add) != 1:
        pytest.fail(f"{len(add)} Add buttons in the project overview")
    kit.click(add[0])
    kit.pump(50)


def found[T](find: Callable[[], T | None], what: str, timeout: float = 5.0) -> T:
    """Wait (event loop running: widgets added to a shown layout appear on the next pass)
    until `find()` gives something."""
    return kit.wait_until(lambda: find(), timeout, message=f"{what} not shown")


def new_group_edit(window: Any) -> QLineEdit:
    def find() -> QLineEdit | None:
        hits = [
            e
            for e in kit.find_all(window.project_view, QLineEdit)
            if kit.same_text(e.placeholderText(), "new group, e.g. Main board")
        ]
        return hits[0] if len(hits) == 1 else None

    return found(find, "the new-group field")


def group_edits(window: Any) -> list[QLineEdit]:
    """The group name fields of the project overview, in order."""
    return [
        e
        for e in kit.find_all(window.project_view, QLineEdit)
        if e.width() == 260 and not e.placeholderText()
    ]


def group_edit(window: Any, name: str) -> QLineEdit:
    def find() -> QLineEdit | None:
        hits = [e for e in group_edits(window) if e.text() == name]
        return hits[0] if len(hits) == 1 else None

    return found(find, f"group field {name!r}")


def overview_name(window: Any) -> QLineEdit:
    def find() -> QLineEdit | None:
        edits = [e for e in kit.find_all(window.project_view, QLineEdit) if e.maximumWidth() == 420]
        return edits[0] if edits else None

    return found(find, "the project name field")


def commit_notes(edit: Any, text: str, away: QWidget) -> None:
    """Type into a multi-line notes field, then click `away` (a line edit elsewhere): the
    field loses the focus and saves."""
    from PySide6.QtWidgets import QPlainTextEdit

    assert isinstance(edit, QPlainTextEdit)
    kit.click(edit)
    edit.setFocus()
    QTest.keyClick(edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(edit, Qt.Key.Key_Delete)
    for chunk in re.findall(r"[\x20-\x7e]+|[^\x20-\x7e]+", text):
        if chunk.isascii():
            QTest.keyClicks(edit, chunk)
        else:
            from PySide6.QtGui import QInputMethodEvent

            commit = QInputMethodEvent("", [])
            commit.setCommitString(chunk)
            QCoreApplication.sendEvent(edit, commit)
    kit.click(away)
    away.setFocus()  # a click focuses a line edit; make sure even if the window is inactive
    kit.pump(50)  # the field commits on focus out; the page may be rebuilt (edit deleted)


def notes_edit(root: QWidget) -> Any:
    from PySide6.QtWidgets import QPlainTextEdit

    def find() -> QPlainTextEdit | None:
        edits = [e for e in root.findChildren(QPlainTextEdit) if e.isVisible()]
        return edits[0] if len(edits) == 1 else None

    return found(find, "the notes field")


STUCK_SECONDS = 45.0


@pytest.fixture
def watchdog(qapp: Any) -> Generator[None]:
    """A dialog or popup menu nobody handles would block the test forever (a product
    dialog the test did not expect): after STUCK_SECONDS it is closed and the test fails
    naming it. Use with pytestmark = pytest.mark.usefixtures("watchdog")."""
    import time

    from PySide6.QtCore import QTimer

    seen: dict[int, float] = {}
    stuck: list[str] = []

    def poll() -> None:
        now = time.monotonic()
        widgets: list[QWidget] = list(dialog_stack())
        popup = QApplication.activePopupWidget()
        if popup is not None:
            widgets.append(popup)
        for widget in widgets:
            first = seen.setdefault(id(widget), now)
            if now - first > STUCK_SECONDS and widget.isVisible():
                stuck.append(
                    f"{type(widget).__name__} {widget.windowTitle()!r}: {kit.all_texts(widget)}"
                )
                with contextlib.suppress(RuntimeError):
                    reject = getattr(widget, "reject", None)
                    reject() if callable(reject) else widget.close()

    timer = QTimer()
    timer.setInterval(500)
    timer.timeout.connect(poll)
    timer.start()
    try:
        yield
    finally:
        timer.stop()
    if stuck:
        pytest.fail(f"left open (closed by the watchdog): {stuck}")


def no_problems(problems: Sequence[str]) -> None:
    """Fail with every collected problem, one per line (a test reports all it saw)."""
    if problems:
        pytest.fail("\n".join(["problems:", *problems]), pytrace=False)


@contextlib.contextmanager
def dialogs_in_order(
    *steps: tuple[str, Callable[[kit.DialogDriver], None]], timeout: float = 30.0
) -> Generator[None]:
    """Handle modal dialogs that open one after another inside the block (e.g. two
    QInputDialogs with the same title): step i handles the i-th dialog whose title is
    steps[i][0] (English source text; "" = any title). Each handler must close its dialog.
    Fails when a step's dialog did not appear."""
    import time

    from PySide6.QtCore import QTimer

    state = {"index": 0, "busy": False}
    handled: set[int] = set()
    errors: list[BaseException] = []

    before = {id(d) for d in dialog_stack()}

    def poll() -> None:
        if state["busy"] or errors:
            return
        if state["index"] >= len(steps):  # one more dialog than expected: close it
            for dialog in dialog_stack():
                if id(dialog) not in handled and id(dialog) not in before:
                    handled.add(id(dialog))
                    texts = kit.all_texts(dialog)
                    errors.append(
                        AssertionError(f"unexpected dialog {dialog.windowTitle()!r}: {texts}")
                    )
                    dialog.reject()
            return
        title, handler = steps[int(state["index"])]
        for dialog in reversed(dialog_stack()):
            if id(dialog) in handled:
                continue
            if title and plain(dialog.windowTitle()) not in kit.variants(title):
                continue
            handled.add(id(dialog))
            state["busy"] = True
            try:
                handler(kit.DialogDriver(dialog))
            except BaseException as exc:
                errors.append(exc)
                with contextlib.suppress(RuntimeError):
                    if dialog.isVisible():
                        dialog.reject()
            finally:
                state["busy"] = False
                state["index"] = int(state["index"]) + 1
            return

    timer = QTimer()
    timer.setInterval(30)
    timer.timeout.connect(poll)
    timer.start()
    started = time.monotonic()
    try:
        yield
        while int(state["index"]) < len(steps) and not errors:
            if time.monotonic() - started > timeout:
                step = int(state["index"])
                pytest.fail(f"dialog {steps[step][0]!r} (step {step + 1}) did not appear")
            QTest.qWait(30)
    finally:
        timer.stop()
    if errors:
        raise errors[0]


def fixed_firmware(
    window: Any,
    dialogs: kit.FileDialogs,
    name: str,
    files: Sequence[Path],
    place: str | None = STANDALONE,
    group: str | None = None,
) -> str:
    """New fixed firmware `name` holding `files` (Add entry → Files…); returns its id."""
    new_firmware(window, name, place, group)
    kit.wait_log(window, f"[new] fixed firmware '{name}' created")
    add_files(window, dialogs, files)
    kit.wait_log(window, "[entry] added ")
    kit.pump(50)
    return firmware_id(window, name)


S3_FILES = ["bootloader.bin", "partition-table.bin", "hello_esp32s3.bin"]
"""Names of release_kit.hello_files("esp32s3_hello", …) in address order."""


def reselect(window: Any, name: str, other: str) -> None:
    """Click another firmware, then `name` again: a full check on selection (design doc
    §3.4 检测时机)."""
    click_row(window, other)
    click_row(window, name)
    kit.pump(50)


def click_row(window: Any, name: str, kind: str = "firmware") -> None:
    """release_kit.click_sidebar, robust against the tree being rebuilt between finding
    the row and clicking it (a re-check refreshing the window): the row is looked up
    again right before the click."""
    from PySide6.QtGui import QCursor

    tree = window.sidebar_panel.tree
    kit.pump(20)
    tree.scrollToItem(kit.sidebar_row(window, name, kind))
    kit.pump(20)
    item = kit.sidebar_row(window, name, kind)
    rect = tree.visualItemRect(item)
    at = QPoint(rect.left() + min(rect.width() // 2, 60), rect.center().y())
    saved = QCursor.pos()
    QCursor.setPos(tree.viewport().mapToGlobal(at))
    try:
        kit.click(tree.viewport(), at)
    finally:
        QCursor.setPos(saved)


def flip_byte(path: Path, offset: int | None = None) -> None:
    """Change one byte (in the middle by default), keeping the size."""
    data = bytearray(path.read_bytes())
    at = len(data) // 2 if offset is None else offset
    data[at] ^= 0x5A
    path.write_bytes(bytes(data))


@contextlib.contextmanager
def collect(problems: list[str], what: str) -> Generator[None]:
    """An assertion failing inside the block becomes one entry of `problems` (the test
    goes on and reports everything at the end)."""
    try:
        yield
    except AssertionError as exc:
        problems.append(f"{what}: {exc}")
    except pytest.fail.Exception as exc:
        problems.append(f"{what}: {exc}")


# ---- importing builds (tracking firmware) ------------------------------------------


def import_idf(
    window: Any,
    dialogs: kit.FileDialogs,
    folder: Path,
    *,
    name: str = "",
    place: str | None = STANDALONE,
    group: str | None = None,
    use_build: bool = False,
    seen: list[str] | None = None,
) -> None:
    """New menu → Import ESP-IDF build…, Browse… → `folder` (the build folder, or the
    project folder with use_build=True: the dialog offers "Use the build folder")."""
    dialogs.queue_directory(folder)

    def fill(driver: kit.DialogDriver) -> None:
        driver.press("Browse…")
        if use_build:
            kit.wait_until(
                lambda: kit.find_all(driver.dialog, QAbstractButton, "Use the build folder"),
                15,
                message=lambda: f"no 'Use the build folder': {driver.texts()}",
            )
            if seen is not None:
                seen.append(driver.text())
            driver.press("Use the build folder")
        driver.wait_text("ESP-IDF")
        if seen is not None:
            seen.append(driver.text())
        if name:
            driver.type("Name", name)
        if place is not None:
            pick(driver, "Place in", place)
        if group is not None:
            pick(driver, "Group", group)
        driver.ok()

    with kit.expect_dialog(fill, title="Import ESP-IDF build"):
        kit.new_menu(window, "Import ESP-IDF build…")
    kit.wait_log(window, "[import] '")


@pytest.fixture
def pio_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """PLATFORMIO_CORE_DIR at a fake core in tmp_path; pio not on PATH."""
    core = tmp_path / "pio_core"
    core.mkdir()
    keep = [
        p
        for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and not (Path(p) / "pio.exe").is_file()
    ]
    monkeypatch.setenv("PATH", os.pathsep.join(keep))
    monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(core))
    return core


def import_pio(
    window: Any,
    dialogs: kit.FileDialogs,
    env_dir: Path,
    *,
    also: tuple[str, ...] = (),
    place: str | None = STANDALONE,
    group: str | None = None,
    boxes: list[tuple[str, bool, bool]] | None = None,
) -> None:
    dialogs.queue_directory(env_dir)

    def fill(driver: kit.DialogDriver) -> None:
        driver.press("Browse…")
        kit.wait_until(
            lambda: any(b.isEnabled() and b.isChecked() for b in driver.checkboxes()),
            60,
            message=lambda: f"no env to import: {driver.texts()}",
        )
        kit.pump(100)
        if boxes is not None:
            boxes.extend(
                (kit.text_of(b), b.isEnabled(), b.isChecked()) for b in driver.checkboxes()
            )
        for env in also:
            hits = [b for b in driver.checkboxes() if kit.text_of(b).startswith(f"{env} ·")]
            assert len(hits) == 1, [kit.text_of(b) for b in driver.checkboxes()]
            kit.set_checked(hits[0], True)
        if place is not None:
            pick(driver, "Place in", place)
        if group is not None:
            pick(driver, "Group", group)
        driver.ok()

    with kit.expect_dialog(fill, title="Import PlatformIO build", timeout=90):
        kit.new_menu(window, "Import PlatformIO build…")
    kit.wait_log(window, "[import] '")
