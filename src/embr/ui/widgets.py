"""Small building blocks styled by theme.style_sheet() through dynamic properties."""

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QPushButton,
    QSizePolicy,
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
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
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


def hbox(*items: QWidget | QLayout | int | None, margins: int = 0, spacing: int = 6) -> QHBoxLayout:
    """Row layout; an int adds that much fixed space, None adds a stretch."""
    layout = QHBoxLayout()
    _fill(layout, items, margins, spacing)
    return layout


def vbox(*items: QWidget | QLayout | int | None, margins: int = 0, spacing: int = 6) -> QVBoxLayout:
    layout = QVBoxLayout()
    _fill(layout, items, margins, spacing)
    return layout


def _fill(
    layout: QHBoxLayout | QVBoxLayout,
    items: tuple[QWidget | QLayout | int | None, ...],
    margins: int,
    spacing: int,
) -> None:
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    for item in items:
        if item is None:
            layout.addStretch(1)
        elif isinstance(item, int):
            layout.addSpacing(item)
        elif isinstance(item, QLayout):
            layout.addLayout(item)
        else:
            layout.addWidget(item)


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
