"""Small building blocks styled by theme.style_sheet() through dynamic properties."""

import html
from collections.abc import Callable

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from embr.i18n import tr


def restyle(widget: QWidget) -> None:
    """Re-apply the style sheet after a dynamic property changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def set_prop(widget: QWidget, name: str, value: str) -> None:
    if widget.property(name) != value:
        widget.setProperty(name, value)
        restyle(widget)


def plain_table(table: QTableWidget, row_px: int) -> None:
    """The look of the workspace tables (entries, versions): no row header, no grid, one line
    per row, no selection or item editing (cells hold their own fields), a context menu,
    rows of `row_px`, left-aligned header sections the user can drag."""
    table.verticalHeader().hide()
    table.setShowGrid(False)
    table.setWordWrap(False)  # one line per row; long text is elided
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    table.verticalHeader().setDefaultSectionSize(row_px)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)


def label(
    text: str = "",
    *,
    tone: str | None = None,
    mono: bool = False,
    elide: bool = False,
    wrap: bool = False,
) -> QLabel:
    """`tone`: "2" / "3" for secondary / tertiary text, "bad" for errors."""
    widget = QLabel(text)
    # plain unless a caller opts in: names and paths may contain "<", "&" (a project named
    # "<b>x</b>" must show as typed)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if tone:
        widget.setProperty("tone", tone)
    if mono:
        widget.setProperty("mono", "1")
    widget.setWordWrap(wrap)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    if elide:
        widget.setMinimumWidth(0)
    return widget


class ElidedLabel(QLabel):
    """One line that ends in "…" when it does not fit; the full text is the tooltip."""

    def __init__(
        self, text: str = "", mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight
    ) -> None:
        super().__init__()
        self._full = ""
        self._mode = mode
        self.setMinimumWidth(1)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(plain_tooltip(text))
        self._elide()

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:
        return QSize(1, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:
        return QSize(
            self.fontMetrics().horizontalAdvance(self._full) + 2, super().sizeHint().height()
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = max(self.width(), 1)
        self.setText(self.fontMetrics().elidedText(self._full, self._mode, width))


def plain_tooltip(text: str) -> str:
    """A tooltip that shows `text` as typed (Qt renders tooltips that look like HTML as
    rich text)."""
    return f"<qt>{html.escape(text)}</qt>" if "<" in text or "&" in text else text


def elided(
    text: str, *, tone: str | None = None, mono: bool = False, middle: bool = False
) -> ElidedLabel:
    mode = Qt.TextElideMode.ElideMiddle if middle else Qt.TextElideMode.ElideRight
    widget = ElidedLabel(text, mode)
    if tone:
        widget.setProperty("tone", tone)
    if mono:
        widget.setProperty("mono", "1")
    return widget


def tag(text: str, kind: str) -> QLabel:
    """Small coloured status label; kind: ok, warn, bad, info, gray. `text` goes through
    tr(), so tag texts can come from N_() constants."""
    widget = QLabel(tr(text))
    widget.setProperty("tag", kind)
    widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
    widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return widget


def button(text: str, kind: str | None = None, *, tooltip: str = "") -> QPushButton:
    """kind: None (plain), ibtn, primary, danger, newbtn, tab, addrow, danger-small,
    accent-small."""
    widget = QPushButton(text)
    if kind:
        widget.setProperty("kind", kind)
    if tooltip:
        widget.setToolTip(tooltip)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


def small[W: QWidget](widget: W) -> W:
    """Secondary text a size smaller (notes and remarks under a field)."""
    widget.setStyleSheet("font-size: 12px;")
    return widget


def field_caption(title: str) -> QLabel:
    """Caption in the left column of a two-column form, level with the field's top."""
    caption = label(title, tone="3")
    caption.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    return caption


def browse_row(field: QWidget, kind: str | None = None) -> tuple[QHBoxLayout, QPushButton]:
    """`field` stretched beside a "Browse…" button (`kind` as for button()), for path
    inputs; the caller connects the button."""
    browse = button(tr("Browse…"), kind)
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(field, 1)
    row.addWidget(browse)
    return row, browse


def warning_label(text: str) -> QLabel:
    """Wrapped text in the warning hint style (as above the Flash button)."""
    widget = label(text, wrap=True)
    widget.setProperty("hint", "warn")
    widget.setContentsMargins(9, 7, 9, 7)  # not padding: see connection.HintLabel
    return widget


def titled_box(title: str, spacing: int = 0) -> tuple[QFrame, QVBoxLayout]:
    """A framed box (at most 640 px wide) headed by `title`; its content goes into the
    returned layout, `spacing` apart."""
    frame = QFrame()
    frame.setObjectName("box")
    frame.setMaximumWidth(640)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 8)
    layout.setSpacing(spacing)
    head = QLabel(title)
    head.setObjectName("boxhead")
    layout.addWidget(head)
    return frame, layout


class ClickRow(QFrame):
    """A framed row (theme: #rec) that reacts to a press anywhere on it."""

    pressed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("rec")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.pressed.emit()


def clear_layout(layout: QLayout) -> None:
    """Remove and delete every item of a layout (nested layouts included)."""
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            break
        child = item.widget()
        if child is not None:
            child.deleteLater()
        nested = item.layout()
        if nested is not None:
            clear_layout(nested)


class _MenuItem(QLabel):
    """Body of danger_action(): looks like a menu item, triggers its action on release."""

    def __init__(self, text: str, action: QWidgetAction, menu: QMenu) -> None:
        super().__init__(text)
        self._action = action
        self._menu = menu
        self.setProperty("menuitem", "danger")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._menu.close()
            self._action.trigger()


def danger_action(menu: QMenu, text: str, slot: Callable[[], None]) -> QWidgetAction:
    """A menu item drawn in the danger colour (Delete…). Qt style sheets cannot colour one
    QAction, so the item is a label in a QWidgetAction; Enter on it triggers it as well."""
    action = QWidgetAction(menu)
    action.setDefaultWidget(_MenuItem(text, action, menu))
    action.triggered.connect(slot)
    menu.addAction(action)
    return action


class Switch(QAbstractButton):
    """An on/off switch (a checkable button drawn as a track with a knob). Colours come from
    set_colors() (the palette); toggled() as for any checkable button."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._on, self._off, self._knob, self._dim = "#c8582f", "#d9dde3", "#ffffff", "#8a92a0"

    def set_colors(self, on: str, off: str, knob: str, disabled: str) -> None:
        self._on, self._off, self._knob, self._dim = on, off, knob, disabled
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(30, 16)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        color = self._on if self.isChecked() else self._off
        if not self.isEnabled():
            color = self._dim if self.isChecked() else self._off
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        size = track.height() - 4
        x = track.right() - 2 - size if self.isChecked() else track.left() + 2
        painter.setBrush(QColor(self._knob))
        painter.drawEllipse(QRectF(x, track.top() + 2, size, size))
        painter.end()


class HelpMark(QLabel):
    """A help icon (setPixmap) with its explanation as the tooltip, shown as soon as the
    mouse is over it: an ordinary tooltip waits until the mouse has rested for a while, and
    each small movement starts the wait again, which on a small icon takes long."""

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        if self.toolTip():
            QToolTip.showText(event.globalPosition().toPoint(), self.toolTip(), self)

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        QToolTip.hideText()
