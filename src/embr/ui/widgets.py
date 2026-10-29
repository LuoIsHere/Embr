"""Small building blocks styled by theme.style_sheet() through dynamic properties."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLayout, QPushButton, QVBoxLayout, QWidget


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


def tag(text: str, kind: str) -> QLabel:
    """Small coloured status label; kind: ok, warn, bad, info, gray."""
    widget = QLabel(text)
    widget.setProperty("tag", kind)
    widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
