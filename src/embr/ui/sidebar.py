"""Sidebar: search, New menu, project / group / firmware tree with drag and drop
(design doc §6.1 "左侧边栏")."""

from collections.abc import Mapping
from dataclasses import dataclass

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QAction, QDrag, QDragMoveEvent, QDropEvent, QMouseEvent
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

from embr.app.library import FirmwareFlag, FirmwareRow, Tree
from embr.core import chipinfo
from embr.core.firmware.model import Source
from embr.ui import icons
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
    """"header", "project", "group", "firmware", "placeholder"."""
    project_id: str | None = None
    device: str = ""
    firmware_id: str | None = None
    drop: str | None = None
    """Drop target type: "project", "group", "standalone" or None."""


def chip_name(chip: str | None) -> str:
    if chip is None:
        return ""
    try:
        return chipinfo.get_chip(chip).display_name
    except chipinfo.UnknownChipError:
        return chip


def chips_text(chips: tuple[str, ...]) -> str:
    """Sidebar: the chip's name, "multi" for a firmware holding files for several chips."""
    if len(chips) > 1:
        return "multi"
    return chip_name(chips[0]) if chips else ""


INDENT = 8
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
        layout.setContentsMargins(4 + indent, 3, 6, 3)
        layout.setSpacing(4)
        layout.addWidget(self.caret)
        layout.addLayout(self.text, 1)
        layout.addLayout(self.meta)


class FirmwareTree(QTreeWidget):
    """Rows are QTreeWidgetItems carrying a Node, drawn with Row widgets. Dragging a
    firmware row emits move_requested on a valid drop; the tree itself never moves items."""

    move_requested = Signal(str, object, str)
    """firmware id, project id (None = standalone), group ("" = ungrouped)."""

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
        self._drop_row: Row | None = None

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
        start = self._press
        if start is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        if (event.position().toPoint() - start).manhattanLength() < 8:
            return
        node = self.node(self.itemAt(start))
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
        if event.mimeData().hasFormat(MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        target = self._target(event.position().toPoint())
        if target is None or not event.mimeData().hasFormat(MIME):
            self._highlight(None)
            event.ignore()
            return
        self._highlight(target)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event: object) -> None:  # type: ignore[override]
        self._highlight(None)

    def dropEvent(self, event: QDropEvent) -> None:
        item = self._target(event.position().toPoint())
        self._highlight(None)
        node = self.node(item)
        if node is None or not event.mimeData().hasFormat(MIME):
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

    def _target(self, pos: QPoint) -> QTreeWidgetItem | None:
        item = self.itemAt(pos)
        node = self.node(item)
        return item if node is not None and node.drop is not None else None

    def _highlight(self, item: QTreeWidgetItem | None) -> None:
        row = self.itemWidget(item, 0) if item is not None else None
        if self._drop_row is not None and self._drop_row is not row:
            set_prop(self._drop_row, "dropping", "0")
        if isinstance(row, Row):
            set_prop(row, "dropping", "1")
            self._drop_row = row
        else:
            self._drop_row = None


class Sidebar(QWidget):
    firmware_selected = Signal(str)
    project_selected = Signal(str)
    new_firmware = Signal()
    new_project = Signal()
    import_idf = Signal()
    query_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search projects and firmware")
        self.search.setProperty("kind", "search")
        self.search.setClearButtonEnabled(True)
        self._search_icon = QAction(self.search)
        self.search.addAction(self._search_icon, QLineEdit.ActionPosition.LeadingPosition)
        self.new_button = button(f"{PLUS} New   firmware · project", "newbtn")
        self.new_menu = QMenu(self)
        self.new_menu.addAction("New firmware", self.new_firmware.emit).setToolTip(
            "fixed, add entries by hand"
        )
        self.new_menu.addAction("Import ESP-IDF build…", self.import_idf.emit).setToolTip(
            "follows a build directory"
        )
        self.new_menu.addSeparator()
        self.new_menu.addAction("New project", self.new_project.emit)
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
        self._folded: set[str] = set()
        self._data: Tree | None = None
        self._selected: tuple[str, str] | None = None
        self._dimmed: Mapping[str, str] = {}
        self._busy_id: str | None = None

    def apply_palette(self, p: Palette) -> None:
        self._search_icon.setIcon(icons.icon("search", p.fg_3))

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
        scroll = self.tree.verticalScrollBar().value()
        self.tree.clear()
        self._header("PROJECTS")
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
                self._placeholder("(no groups, no firmware)", INDENT)
            for group in project.groups:
                gkey = f"g:{project.id}:{group.name}"
                gfolded = gkey in self._folded
                grow = Row(INDENT, "▸" if gfolded else "▾")
                gname = label(group.name or "Ungrouped", tone="2" if group.name else "3")
                grow.text.addWidget(gname)
                if not group.firmware:
                    grow.meta.addWidget(label("empty", tone="3"))
                elif gfolded:
                    grow.meta.addWidget(label(str(len(group.firmware)), tone="3"))
                self._add(grow, Node("group", project.id, group.name, drop="group"), 24)
                if not gfolded:
                    for fw in group.firmware:
                        self._firmware(fw, INDENT)  # name aligned with the group name
        self._header("STANDALONE FIRMWARE", Node("header", drop="standalone"))
        if not data.standalone:
            self._placeholder(
                "(none — drop firmware here to detach it from its project)",
                INDENT,
                Node("placeholder", drop="standalone"),
            )
        for fw in data.standalone:
            self._firmware(fw, INDENT)
        self.tree.verticalScrollBar().setValue(scroll)

    def _header(self, text: str, node: Node | None = None) -> None:
        widget = QWidget()
        widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        caption = label(text)
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
        origin = elided(fw.origin, tone="3", mono=True)
        origin.setStyleSheet("font-size: 10px;")
        row.text.addWidget(name)
        row.text.addWidget(origin)
        source = {Source.IDF_BUILD: ("IDF", "info"), Source.PIO_BUILD: ("PIO", "info")}.get(
            fw.source, ("ARC", "gray") if fw.source == Source.ARCHIVED else None
        )
        if source is not None:
            row.meta.addWidget(tag(*source))
        if fw.flag == FirmwareFlag.NEEDS_CONFIRMATION:
            row.meta.addWidget(tag("!", "warn"))
        elif fw.flag in (FirmwareFlag.MISSING, FirmwareFlag.BLOCKED):
            row.meta.addWidget(tag(TIMES, "bad"))
        if fw.id == self._busy_id:
            row.meta.addWidget(tag("flashing", "info"))
        row.meta.addWidget(label(chips_text(fw.chips), tone="3"))
        reason = self._dimmed.get(fw.id)
        tip = fw.origin or fw.name
        row.setToolTip(tip if reason is None else f"{tip}\nNot for the identified board: {reason}")
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

    def _toggle(self, key: str) -> None:
        self._folded ^= {key}
        if self._data is not None:
            self.show_tree(self._data, self._selected, self._dimmed, self._busy_id)
