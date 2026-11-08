"""Sidebar: search, New menu, project / group / firmware tree with drag and drop
(design doc §6.1 "左侧边栏")."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from PySide6.QtCore import QEvent, QMargins, QMimeData, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QContextMenuEvent,
    QDrag,
    QDragMoveEvent,
    QDropEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from embr.app.library import ARCHIVE_ORIGIN, FirmwareFlag, FirmwareRow, Tree
from embr.core import chipinfo
from embr.core.firmware.model import Source
from embr.i18n import tr
from embr.ui import icons, reorder
from embr.ui.reorder import ANIMATION_MS, EDGE, SCROLL_STEP, Block, Colors, Overlay
from embr.ui.theme import Palette
from embr.ui.widgets import button, elided, label, set_prop, tag

MIME = "application/x-embr-firmware"
PLUS = chr(0xFF0B)
"""Full-width plus, as in the UI mock."""
TIMES = chr(0xD7)
"""Multiplication sign: tag for a missing source."""
DIMMED = 0.45


@dataclass(frozen=True)
class Node:
    """What a tree row stands for."""

    kind: str
    """"header", "project", "group", "firmware", "placeholder", "spare" (the empty row
    at the end)."""
    project_id: str | None = None
    device: str = ""
    firmware_id: str | None = None
    drop: str | None = None
    """Drop target type: "project", "group", "standalone" or None."""


@dataclass
class _Sort:
    """A reorder in progress (plan #9, #20)."""

    project_id: str | None
    """The project whose groups are reordered; None: the projects are."""
    spans: list[Block]
    dragged: int
    grip: int
    """Content y of the mouse within the dragged block when it was picked up."""
    allowed: tuple[int, int]
    """Content y range the mouse may be in for a drop (groups: their own project)."""
    overlay: Overlay
    index: int
    valid: bool = True
    at: QPoint | None = None
    """Last mouse position in the viewport."""


def chip_name(chip: str | None) -> str:
    if chip is None:
        return ""
    try:
        return chipinfo.get_chip(chip).display_name
    except chipinfo.UnknownChipError:
        return chip


def chips_text(mcus: tuple[str, ...]) -> str:
    """Sidebar: the MCU label, "multi" for a firmware holding files for several MCUs."""
    if len(mcus) > 1:
        return "multi"
    return mcus[0] if mcus else ""


INDENT = 8
SPARE_MIN = 40
"""Height of the spare row at the end when the rows fill the list (a firmware row's)."""
NAME_MIN = 72
"""Firmware rows: below this much room for the name, optional meta gives way."""
"""Tree: indent of groups (and of firmware, whose names line up with group names)."""


class Row(QWidget):
    """Content of one tree row: caret, label (one or two lines), meta on the right."""

    def __init__(self, indent: int, caret: str = "") -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("background: transparent;")
        self.caret = label(caret, tone="3")
        self.caret.setFixedWidth(10)
        self.text = QVBoxLayout()
        self.text.setContentsMargins(0, 0, 0, 0)
        self.text.setSpacing(0)
        self.meta = QHBoxLayout()
        self.meta.setContentsMargins(0, 0, 0, 0)
        self.meta.setSpacing(4)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4 + indent, 5, 6, 5)
        layout.setSpacing(4)
        layout.addWidget(self.caret)
        layout.addLayout(self.text, 1)
        layout.addLayout(self.meta)
        self._optional: list[tuple[int, QWidget]] = []
        """(rank, widget): meta widgets that give way when the row is narrow."""

    def add_optional(self, widget: QWidget, rank: int) -> None:
        """A meta widget, shown in place, that gives way to the name in a narrow sidebar;
        a lower rank is hidden first."""
        self.meta.addWidget(widget)
        self._optional.append((rank, widget))
        self._optional.sort(key=lambda item: item[0])

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if not self._optional:
            return
        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else QMargins()
        fixed = margins.left() + margins.right() + self.caret.width() + 8
        shown: list[QWidget] = []
        for i in range(self.meta.count()):
            item = self.meta.itemAt(i)
            widget = item.widget() if item is not None else None
            if widget is not None:
                shown.append(widget)
        for _, widget in self._optional:
            widget.setVisible(True)
        for _, widget in self._optional:
            meta = sum(w.sizeHint().width() + 4 for w in shown if not w.isHidden())
            if self.width() - fixed - meta >= NAME_MIN:
                break
            widget.setVisible(False)


class FirmwareTree(QTreeWidget):
    """Rows are QTreeWidgetItems carrying a Node, drawn with Row widgets. Dragging a
    firmware row emits move_requested on a valid drop; the tree itself never moves items.
    Files dragged in from Explorer emit files_dropped (design doc §12 item 19): on a
    project, group or the standalone header, or on a firmware row. Dragging a project row,
    or a named group row within its project, reorders them (plan #9, #20; module reorder):
    projects_reordered / groups_reordered on a drop that changes the order. Not while
    `sortable` is off (a search shows part of the list) nor for the groups of a project in
    `read_only`; Esc or a drop outside the allowed area puts everything back."""

    move_requested = Signal(str, object, str)
    """firmware id, project id (None = standalone), group ("" = ungrouped)."""
    files_dropped = Signal(list, object)
    """local paths (list[str]), the Node they were dropped on."""
    menu_requested = Signal(str, QPoint)
    """firmware id, global position: right-click on a firmware row."""
    projects_reordered = Signal(list)
    """project ids (list[str]) in their new order."""
    groups_reordered = Signal(str, list)
    """project id, its group names (list[str]) in their new order."""
    sort_finished = Signal()
    """A reorder ended (dropped or cancelled); the rows may be shown again."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("tree")
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setUniformRowHeights(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._press: QPoint | None = None
        self._drop_row: QWidget | None = None
        self.sortable = True
        self.read_only: frozenset[str] = frozenset()
        self.colors = Colors("#f6f7f9", "#ffffff", "#d9dde3")
        self._sort: _Sort | None = None
        self._scroller = QTimer(self)
        self._scroller.setInterval(16)
        self._scroller.timeout.connect(self._edge_scroll)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        node = self.node(self.itemAt(event.pos()))
        if node is not None and node.firmware_id is not None:
            self.menu_requested.emit(node.firmware_id, event.globalPos())

    def node(self, item: QTreeWidgetItem | None) -> Node | None:
        if item is None:
            return None
        value = item.data(0, Qt.ItemDataRole.UserRole)
        return value if isinstance(value, Node) else None

    # ---- dragging firmware rows -------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._sort is not None:
            self._sort_to(event.position().toPoint())
            return
        start = self._press
        if start is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        if (event.position().toPoint() - start).manhattanLength() < 8:
            return
        node = self.node(self.itemAt(start))
        if node is not None and self._can_sort(node):
            self._press = None
            self._begin_sort(node, start)
            if self._sort is not None:
                self._sort_to(event.position().toPoint())
            return
        if node is None or node.firmware_id is None:
            return
        self._press = None
        mime = QMimeData()
        mime.setData(MIME, node.firmware_id.encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)
        self._highlight(None)

    def dragEnterEvent(self, event: QDragMoveEvent) -> None:  # type: ignore[override]
        if event.mimeData().hasFormat(MIME) or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        mime = event.mimeData()
        target = self._target(event.position().toPoint(), files=not mime.hasFormat(MIME))
        if target is None or not (mime.hasFormat(MIME) or mime.hasUrls()):
            self._highlight(None)
            event.ignore()
            return
        self._highlight(target)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event: object) -> None:  # type: ignore[override]
        self._highlight(None)

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        files = not mime.hasFormat(MIME) and mime.hasUrls()
        item = self._target(event.position().toPoint(), files=files)
        self._highlight(None)
        node = self.node(item)
        if node is not None and files:
            event.acceptProposedAction()
            paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
            self.files_dropped.emit(paths, node)
            return
        if node is None or not mime.hasFormat(MIME):
            event.ignore()
            return
        firmware_id = bytes(event.mimeData().data(MIME).data()).decode()
        event.acceptProposedAction()
        if node.drop == "standalone":
            self.move_requested.emit(firmware_id, None, "")
        elif node.drop == "project":
            self.move_requested.emit(firmware_id, node.project_id, "")
        elif node.drop == "group":
            self.move_requested.emit(firmware_id, node.project_id, node.device)

    def _target(self, pos: QPoint, *, files: bool = False) -> QTreeWidgetItem | None:
        """The row a drop lands on: a project, group or the standalone header; for files
        also a firmware row. The empty space below the rows counts as the spare row at the
        end, a standalone drop target (design doc §12 item 19)."""
        item = self.itemAt(pos)
        if item is None:
            return self._spare_row()
        node = self.node(item)
        if node is None:
            return None
        return item if node.drop is not None or (files and node.kind == "firmware") else None

    def _spare_row(self) -> QTreeWidgetItem | None:
        last = self.topLevelItem(self.topLevelItemCount() - 1)
        node = self.node(last)
        return last if node is not None and node.kind == "spare" else None

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.fit_spare()

    def fit_spare(self) -> None:
        """The spare row fills the empty space below the other rows, so a drag over any of
        it shows the drop highlight on all of it; at least SPARE_MIN high, also when the
        list scrolls. Content never exceeds the viewport because of it (no scroll bar)."""
        spare = self._spare_row()
        if spare is None:
            return
        # the heights the view lays out: a row is as tall as its widget needs (a wrapped
        # placeholder) when that is more than its item's size hint
        count = self.topLevelItemCount() - 1
        rows = (self.topLevelItem(i) for i in range(count))
        others = sum(self.indexRowSizeHint(self.indexFromItem(r)) for r in rows if r is not None)
        height = max(SPARE_MIN, self.viewport().height() - others)
        size = spare.sizeHint(0)
        if size.height() != height:
            size.setHeight(height)
            spare.setSizeHint(0, size)

    def _highlight(self, item: QTreeWidgetItem | None) -> None:
        row = self.itemWidget(item, 0) if item is not None else None
        if self._drop_row is not None and self._drop_row is not row:
            set_prop(self._drop_row, "dropping", "0")
        if row is not None:  # a Row, or the STANDALONE FIRMWARE header
            set_prop(row, "dropping", "1")
            self._drop_row = row
        else:
            self._drop_row = None

    # ---- reordering projects and groups -----------------------------------------------

    def sorting(self) -> bool:
        return self._sort is not None

    def _can_sort(self, node: Node) -> bool:
        if not self.sortable or node.project_id is None:
            return False
        if node.kind == "project":
            return True
        return node.kind == "group" and bool(node.device) and node.project_id not in self.read_only

    def _keys(self, project_id: str | None) -> list[tuple[str | None, int]]:
        """(block key, height) of every row: the project id for the rows of a project
        (project_id None), or the group name for a named group of `project_id` and its
        firmware rows; None for the rest."""
        rows: list[tuple[str | None, int]] = []
        project: str | None = None
        key: str | None = None
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            node = self.node(item)
            kind = node.kind if node is not None else ""
            if kind == "project" and node is not None:
                project = node.project_id
                key = project if project_id is None else None
            elif kind in ("header", "spare"):
                project = key = None
            elif kind == "group" and node is not None and project_id is not None:
                key = node.device if project == project_id and node.device else None
            elif kind == "placeholder" and project_id is not None:
                key = None
            height = self.indexRowSizeHint(self.indexFromItem(item)) if item else 0
            rows.append((key, height))
        return rows

    def _begin_sort(self, node: Node, press: QPoint) -> None:
        project_id = node.project_id if node.kind == "group" else None
        rows = self._keys(project_id)
        spans = reorder.blocks(rows)
        picked = node.device if node.kind == "group" else node.project_id
        dragged = next((i for i, b in enumerate(spans) if b.key == picked), None)
        if dragged is None or len(spans) < 2:
            return
        scroll = self.verticalScrollBar().value()
        allowed = (spans[0].top, spans[-1].top + spans[-1].height)
        if project_id is not None:  # the project's own rows: its row down to its last
            tops = [sum(h for _, h in rows[:i]) for i in range(len(rows) + 1)]
            first = next(
                i
                for i in range(len(rows))
                if (n := self.node(self.topLevelItem(i))) is not None
                and n.kind == "project"
                and n.project_id == project_id
            )
            end = next(
                (
                    i
                    for i in range(first + 1, len(rows))
                    if (n := self.node(self.topLevelItem(i))) is not None
                    and n.kind in ("project", "header")
                ),
                len(rows),
            )
            allowed = (tops[first], tops[end])
        pictures = {b.key: self._picture(b) for b in spans}
        overlay = Overlay(
            self.viewport(), spans, pictures, self.verticalScrollBar().value, self.colors
        )
        block = spans[dragged]
        grip = press.y() + scroll - block.top
        self._sort = _Sort(project_id, spans, dragged, grip, allowed, overlay, dragged)
        overlay.lift(dragged, block.top)
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        self.grabKeyboard()
        self._scroller.start()

    def _picture(self, block: Block) -> QPixmap:
        """The block's rows as shown: grabbed from the viewport when on screen (with the
        selection), from the row widgets otherwise."""
        width = self.viewport().width()
        picture = QPixmap(width, block.height)
        picture.fill(Qt.GlobalColor.transparent)
        painter = QPainter(picture)
        y = 0
        screen = self.viewport().rect()
        for index in range(block.first, block.first + block.count):
            item = self.topLevelItem(index)
            if item is None:
                continue
            height = self.indexRowSizeHint(self.indexFromItem(item))
            rect = self.visualItemRect(item)
            widget = self.itemWidget(item, 0)
            if screen.contains(rect) and rect.height() == height:
                painter.drawPixmap(0, y, self.viewport().grab(rect))
            else:
                widget.resize(width, height)
                painter.drawPixmap(0, y, widget.grab())
            y += height
        painter.end()
        return picture

    def _sort_to(self, at: QPoint) -> None:
        sort = self._sort
        if sort is None:
            return
        sort.at = at
        scroll = self.verticalScrollBar().value()
        block = sort.spans[sort.dragged]
        mouse = at.y() + scroll
        low, high = sort.allowed
        inside = 0 <= at.x() < self.viewport().width() and 0 <= at.y() < self.viewport().height()
        sort.valid = inside and low <= mouse < high
        top = mouse - sort.grip
        region_top, region_bottom = sort.overlay.region
        within = min(max(top, region_top - block.height), region_bottom)
        sort.index = (
            reorder.landing(sort.spans, sort.dragged, within) if sort.valid else sort.dragged
        )
        sort.overlay.lift(sort.dragged, top)
        sort.overlay.place(reorder.slots(sort.spans, sort.dragged, sort.index))
        cursor = Qt.CursorShape.ClosedHandCursor if sort.valid else Qt.CursorShape.ForbiddenCursor
        self.viewport().setCursor(cursor)

    def _edge_scroll(self) -> None:
        sort = self._sort
        if sort is None or sort.at is None:
            self._scroller.stop()
            return
        bar = self.verticalScrollBar()
        if sort.at.y() < EDGE:
            step = -SCROLL_STEP
        elif sort.at.y() >= self.viewport().height() - EDGE:
            step = SCROLL_STEP
        else:
            return
        before = bar.value()
        bar.setValue(before + step)
        if bar.value() != before:
            self._sort_to(sort.at)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._sort is None:
            super().mouseReleaseEvent(event)
            return
        sort = self._sort
        self._sort_to(event.position().toPoint())
        if sort.valid and sort.index != sort.dragged:
            keys = reorder.moved([b.key for b in sort.spans], sort.dragged, sort.index)
            self._end_sort(cancelled=False)
            if sort.project_id is None:
                self.projects_reordered.emit(keys)
            else:
                self.groups_reordered.emit(sort.project_id, keys)
        else:
            self._end_sort(cancelled=True)

    def event(self, event: QEvent) -> bool:
        """Esc during a reorder cancels it (the tree takes no focus; the keyboard is grabbed
        while dragging). The shortcut override is accepted first, or a window shortcut on Esc
        would take the key before it gets here."""
        kind = event.type()
        if (
            getattr(self, "_sort", None) is not None  # events come during __init__ too
            and kind in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress)
            and cast(QKeyEvent, event).key() == Qt.Key.Key_Escape
        ):
            event.accept()
            if kind == QEvent.Type.KeyPress:
                self._end_sort(cancelled=True)
            return True
        return super().event(event)

    def _end_sort(self, *, cancelled: bool) -> None:
        """Dropped: the overlay goes now (the rows are rebuilt in the new order). Cancelled:
        everything slides back first."""
        sort = self._sort
        if sort is None:
            return
        self._sort = None
        self._scroller.stop()
        self.releaseKeyboard()
        self.viewport().unsetCursor()
        overlay = sort.overlay
        if cancelled:
            overlay.drop_back()
            QTimer.singleShot(ANIMATION_MS + 20, overlay.deleteLater)
        else:
            overlay.hide()
            overlay.deleteLater()
        self.sort_finished.emit()


class Sidebar(QWidget):
    firmware_selected = Signal(str)
    project_selected = Signal(str)
    new_firmware = Signal()
    new_project = Signal()
    import_idf = Signal()
    import_pio = Signal()
    import_package = Signal()
    query_changed = Signal(str)
    files_dropped = Signal(list, object)
    """See FirmwareTree.files_dropped."""
    projects_reordered = Signal(list)
    """See FirmwareTree.projects_reordered."""
    groups_reordered = Signal(str, list)
    """See FirmwareTree.groups_reordered."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search projects and firmware"))
        self.search.setProperty("kind", "search")
        self.search.setClearButtonEnabled(True)
        self._search_icon = QAction(self.search)
        self.search.addAction(self._search_icon, QLineEdit.ActionPosition.LeadingPosition)
        self.new_button = button(tr(f"{PLUS} New   firmware · project"), "newbtn")
        self.new_menu = QMenu(self)
        self.new_menu.addAction(tr("New firmware"), self.new_firmware.emit).setToolTip(
            tr("fixed, add entries by hand")
        )
        self.new_menu.addAction(tr("Import ESP-IDF build…"), self.import_idf.emit).setToolTip(
            tr("follows a build directory")
        )
        self.new_menu.addAction(tr("Import PlatformIO build…"), self.import_pio.emit).setToolTip(
            tr("follows the build folders of PlatformIO envs")
        )
        self.new_menu.addAction(tr("Import .embr package…"), self.import_package.emit).setToolTip(
            tr("projects and firmware exported from Embr")
        )
        self.new_menu.addSeparator()
        self.new_menu.addAction(tr("New project"), self.new_project.emit)
        self.new_button.clicked.connect(self._open_new_menu)
        self.tree = FirmwareTree()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 4, 0)
        layout.setSpacing(6)
        layout.addWidget(self.search)
        layout.addWidget(self.new_button)
        layout.addWidget(self.tree, 1)
        self.search.textChanged.connect(self.query_changed.emit)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.files_dropped.connect(self.files_dropped.emit)
        self.tree.projects_reordered.connect(self.projects_reordered.emit)
        self.tree.groups_reordered.connect(self.groups_reordered.emit)
        self.tree.sort_finished.connect(self._sort_finished)
        self._folded: set[str] = set()
        self._data: Tree | None = None
        self._selected: tuple[str, str] | None = None
        self._dimmed: Mapping[str, str] = {}
        self._busy_id: str | None = None

    def apply_palette(self, p: Palette) -> None:
        self._search_icon.setIcon(icons.icon("search", p.fg_3))
        self.tree.colors = Colors(p.panel_2, p.panel, p.line)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """A narrow sidebar shows "+ New" instead of the whole button text (a button cannot
        elide; its text would be cut on both sides)."""
        super().resizeEvent(event)
        full, short = tr(f"{PLUS} New   firmware · project"), tr(f"{PLUS} New")
        room = self.new_button.width() - 26  # padding and border
        fits = self.new_button.fontMetrics().horizontalAdvance(full) <= room
        self.new_button.setText(full if fits else short)

    def _open_new_menu(self) -> None:
        self.new_menu.popup(self.new_button.mapToGlobal(QPoint(0, self.new_button.height() + 4)))

    # ---- content ----------------------------------------------------------------------

    def show_tree(
        self,
        data: Tree,
        selected: tuple[str, str] | None,
        dimmed: Mapping[str, str],
        busy_id: str | None,
    ) -> None:
        """selected: ("firmware" | "project", id). dimmed: firmware that do not fit the
        identified chip (chip or revision) with the reason, shown on hover. busy_id:
        firmware being flashed."""
        self._data, self._selected, self._dimmed, self._busy_id = (
            data,
            selected,
            dimmed,
            busy_id,
        )
        if self.tree.sorting():  # the rows being dragged stay; shown when the drag ends
            return
        self.tree.sortable = not self.search.text().strip()
        self.tree.read_only = frozenset(p.id for p in data.projects if p.read_only)
        scroll = self.tree.verticalScrollBar().value()
        self.tree.clear()
        self._header(tr("PROJECTS"))
        for project in data.projects:
            key = f"p:{project.id}"
            folded = key in self._folded
            row = Row(0, "▸" if folded else "▾")
            name = elided(project.name)
            name.setStyleSheet("font-weight: 600;")
            row.text.addWidget(name)
            row.meta.addWidget(label(str(project.firmware_count), tone="3"))
            row.setToolTip(project.notes or project.name)
            item = self._add(row, Node("project", project.id, drop="project"), 26)
            if selected == ("project", project.id):
                self.tree.setCurrentItem(item)
            if folded:
                continue
            if not project.groups:
                self._placeholder(tr("(no groups, no firmware)"), INDENT)
            for group in project.groups:
                gkey = f"g:{project.id}:{group.name}"
                gfolded = gkey in self._folded
                grow = Row(INDENT, "▸" if gfolded else "▾")
                gname = elided(group.name or tr("Ungrouped"), tone="2" if group.name else "3")
                grow.text.addWidget(gname)
                if not group.firmware:
                    grow.meta.addWidget(label(tr("empty"), tone="3"))
                else:
                    grow.meta.addWidget(label(str(len(group.firmware)), tone="3"))
                self._add(grow, Node("group", project.id, group.name, drop="group"), 24)
                if not gfolded:
                    for fw in group.firmware:
                        self._firmware(fw, INDENT)  # name aligned with the group name
        self._header(tr("STANDALONE FIRMWARE"), Node("header", drop="standalone"))
        if not data.standalone:
            self._placeholder(
                tr("(none — drop firmware here to detach it from its project)"),
                INDENT,
                Node("placeholder", drop="standalone"),
            )
        for fw in data.standalone:
            self._firmware(fw, INDENT)
        # the empty space at the very end (a firmware row's worth when the list scrolls), to
        # drop files or firmware on as standalone
        spare = self._add(Row(INDENT), Node("spare", drop="standalone"), SPARE_MIN)
        spare.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
        self.tree.fit_spare()
        self.tree.verticalScrollBar().setValue(scroll)

    def _header(self, text: str, node: Node | None = None) -> None:
        widget = QWidget()
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)  # drop highlight
        widget.setStyleSheet("background: transparent;")
        caption = elided(text)  # narrow sidebar: "STANDALONE FIRMW…", not cut off
        caption.setProperty("group", "1")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 2)
        layout.addWidget(caption)
        item = self._add(widget, node or Node("header"), 28)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)

    def _placeholder(self, text: str, indent: int, node: Node | None = None) -> None:
        row = Row(indent)
        caption = label(text, tone="3")
        caption.setWordWrap(True)
        row.text.addWidget(caption)
        item = self._add(row, node or Node("placeholder"), 24)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)

    def _firmware(self, fw: FirmwareRow, indent: int) -> None:
        row = Row(indent)
        name = elided(fw.name)
        # a path, or the fixed text of archived firmware (translated)
        origin_text = tr(fw.origin) if fw.origin == ARCHIVE_ORIGIN else fw.origin
        origin = elided(origin_text, tone="3", mono=True)
        origin.setStyleSheet("font-size: 11px;")
        row.text.addWidget(name)
        if fw.origin:  # no second line: the name stays centred in the row
            row.text.addWidget(origin)
        source = {Source.IDF_BUILD: ("IDF", "info"), Source.PIO_BUILD: ("PIO", "info")}.get(
            fw.source, ("ARC", "gray") if fw.source == Source.ARCHIVED else None
        )
        if source is not None:
            row.add_optional(tag(*source), 2)
        if fw.flag == FirmwareFlag.NEEDS_CONFIRMATION:
            row.meta.addWidget(tag("!", "warn"))
        elif fw.flag in (FirmwareFlag.MISSING, FirmwareFlag.BLOCKED):
            row.meta.addWidget(tag(TIMES, "bad"))
        if fw.id == self._busy_id:
            row.meta.addWidget(tag(tr("flashing"), "info"))
        if fw.mcus:
            row.add_optional(label(chips_text(fw.mcus), tone="3"), 1)  # hidden before the tag
        reason = self._dimmed.get(fw.id)
        tip = origin_text or fw.name
        row.setToolTip(
            tip if reason is None else tr(f"{tip}\nNot for the identified board: {tr(reason)}")
        )
        dimmed = reason is not None
        if fw.flag == FirmwareFlag.MISSING or dimmed:
            effect = QGraphicsOpacityEffect(row)
            effect.setOpacity(DIMMED if dimmed else 0.6)
            row.setGraphicsEffect(effect)
        node = Node("firmware", fw.project_id, fw.device, fw.id)
        item = self._add(row, node, 40)
        if self._selected == ("firmware", fw.id):
            self.tree.setCurrentItem(item)

    def _add(self, widget: QWidget, node: Node, height: int) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self.tree)
        item.setData(0, Qt.ItemDataRole.UserRole, node)
        item.setSizeHint(0, widget.sizeHint().expandedTo(widget.minimumSizeHint()))
        size = item.sizeHint(0)
        size.setHeight(height)
        item.setSizeHint(0, size)
        flags = Qt.ItemFlag.ItemIsEnabled
        if node.kind in ("project", "firmware"):
            flags |= Qt.ItemFlag.ItemIsSelectable
        if node.kind == "firmware":
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        if node.drop is not None:
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        item.setFlags(flags)
        self.tree.setItemWidget(item, 0, widget)
        return item

    def _clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        node = self.tree.node(item)
        if node is None:
            return
        if node.kind == "firmware" and node.firmware_id is not None:
            self.firmware_selected.emit(node.firmware_id)
        elif node.kind == "group" and node.project_id is not None:
            self._toggle(f"g:{node.project_id}:{node.device}")
        elif node.kind == "project" and node.project_id is not None:
            row = self.tree.itemWidget(item, 0)
            pos = self.tree.viewport().mapFromGlobal(self.cursor().pos())
            on_caret = isinstance(row, Row) and pos.x() < row.caret.geometry().right() + 6
            if on_caret:
                self._toggle(f"p:{node.project_id}")
            else:
                self.project_selected.emit(node.project_id)

    def _sort_finished(self) -> None:
        """Shown again after a reorder: what came meanwhile, and the press on a project row
        no longer shows it selected (a drag is not a click)."""
        if self._data is not None:
            self.show_tree(self._data, self._selected, self._dimmed, self._busy_id)

    def _toggle(self, key: str) -> None:
        self._folded ^= {key}
        if self._data is not None:
            self.show_tree(self._data, self._selected, self._dimmed, self._busy_id)
