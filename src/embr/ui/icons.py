"""Icons drawn from SVG text in the current text colour, and the application logo.

menu, panel-left and search are Lucide icons (ISC licence, see THIRD_PARTY_NOTICES); the
window-control glyphs are drawn here.
"""

from importlib.resources import files

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_LUCIDE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
)
_WINDOW = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" fill="none" '
    'stroke="{color}" stroke-width="1">{body}</svg>'
)

SHAPES: dict[str, tuple[str, str]] = {
    "menu": (
        _LUCIDE,
        '<line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="12" y2="12"/>'
        '<line x1="4" x2="20" y1="18" y2="18"/>',
    ),
    "panel-left": (_LUCIDE, '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>'),
    "search": (_LUCIDE, '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>'),
    "minimize": (_WINDOW, '<path d="M1 5h8"/>'),
    "maximize": (_WINDOW, '<rect x="1.5" y="1.5" width="7" height="7"/>'),
    "restore": (
        _WINDOW,
        '<rect x="1.5" y="3" width="5.5" height="5.5"/><path d="M3 3V1.5h5.5V7H7"/>',
    ),
    "close": (_WINDOW, '<path d="M1.5 1.5l7 7M8.5 1.5l-7 7"/>'),
}


def icon(name: str, color: str, size: int = 14, hover_color: str | None = None) -> QIcon:
    """A named shape drawn in `color` (and `hover_color` for the active state)."""
    result = QIcon()
    result.addPixmap(_pixmap(name, color, size), QIcon.Mode.Normal)
    if hover_color is not None:
        result.addPixmap(_pixmap(name, hover_color, size), QIcon.Mode.Active)
    return result


def app_icon() -> QIcon:
    """The Embr logo for the window and taskbar: the small variant (fewer cuts) at 16 and
    24 px, the full one above."""
    result = QIcon()
    logo = files("embr.resources").joinpath("logo")
    for name, sizes in (("embr-icon-small.svg", (16, 24)), ("embr-icon.svg", (32, 48, 64, 256))):
        renderer = QSvgRenderer(QByteArray(logo.joinpath(name).read_bytes()))
        for size in sizes:
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            with _painter(pixmap) as painter:
                renderer.render(painter, QRectF(0, 0, size, size))
            result.addPixmap(pixmap)
    return result


def logo_pixmap(size: int) -> QPixmap:
    """The Embr icon at `size` logical pixels, sharp on high-DPI screens."""
    name = "embr-icon-small.svg" if size <= 24 else "embr-icon.svg"
    data = files("embr.resources").joinpath("logo").joinpath(name).read_bytes()
    renderer = QSvgRenderer(QByteArray(data))
    scale = 3
    pixmap = QPixmap(QSize(size * scale, size * scale))
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    with _painter(pixmap) as painter:
        renderer.render(painter, QRectF(0, 0, size, size))
    return pixmap


KEY_HEIGHT = 14
_KEY_GAP = 3
_KEY_TEXT = (
    '<text x="{x}" y="9.6" text-anchor="middle" font-family="Segoe UI" font-size="7.5" '
    'font-weight="600" fill="{color}" stroke="none">{label}</text>'
)


def keys_pixmap(keys: tuple[str, ...], color: str) -> QPixmap:
    """Keyboard keys side by side in `color`: "Ctrl" and "Esc" as small square keys with
    their name, "Enter" as the reverse-L key shape with a return arrow."""
    parts: list[str] = []
    x = 0.0
    for key in keys:
        if key == "Enter":
            width = 20.0
            parts.append(
                f'<path d="M{x + 0.5} 0.5H{x + width - 0.5}V13.5H{x + 6.5}V7H{x + 0.5}Z"/>'
                f'<path d="M{x + 15} 3.5V10H{x + 9.5}M{x + 11.5} 8L{x + 9.5} 10L{x + 11.5} 12"/>'
            )
        else:
            width = 6.0 + 4.6 * len(key)
            parts.append(
                f'<rect x="{x + 0.5}" y="0.5" width="{width - 1}" height="13" rx="2.5"/>'
                + _KEY_TEXT.format(x=x + width / 2, color=color, label=key)
            )
        x += width + _KEY_GAP
    width = round(x - _KEY_GAP)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {KEY_HEIGHT}" '
        f'fill="none" stroke="{color}" stroke-width="1" stroke-linejoin="round" '
        f'stroke-linecap="round">{"".join(parts)}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    scale = 3
    pixmap = QPixmap(QSize(width * scale, KEY_HEIGHT * scale))
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    with _painter(pixmap) as painter:
        renderer.render(painter, QRectF(0, 0, width, KEY_HEIGHT))
    return pixmap


def _pixmap(name: str, color: str, size: int) -> QPixmap:
    template, body = SHAPES[name]
    renderer = QSvgRenderer(QByteArray(template.format(color=color, body=body).encode()))
    scale = 2  # sharp on high-DPI screens
    pixmap = QPixmap(QSize(size * scale, size * scale))
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    with _painter(pixmap) as painter:
        renderer.render(painter, QRectF(0, 0, size, size))
    return pixmap


class _painter:
    """QPainter as a context manager, so end() always runs."""

    def __init__(self, pixmap: QPixmap) -> None:
        self._painter = QPainter(pixmap)
        self._painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    def __enter__(self) -> QPainter:
        return self._painter

    def __exit__(self, *exc: object) -> None:
        self._painter.end()
