"""Build src/embr/resources/logo/embr.ico (the Embr.exe file icon) from the logo SVGs.

    uv run python scripts/make_icon.py

Sizes 16 and 24 use embr-icon-small.svg (fewer cuts, readable when tiny), larger sizes
embr-icon.svg, the same rule as the window icon. Every size is stored as PNG inside the
ICO (supported since Windows Vista). Re-run after changing the logo.
"""

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QImageWriter, QPainter
from PySide6.QtSvg import QSvgRenderer

LOGO = Path(__file__).resolve().parents[1] / "src" / "embr" / "resources" / "logo"
SIZES = (16, 24, 32, 48, 64, 128, 256)
SMALL = 24
"""Largest size drawn from the small variant."""


def render_png(svg: Path, size: int) -> bytes:
    renderer = QSvgRenderer(QByteArray(svg.read_bytes()))
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    QImageWriter(buffer, QByteArray(b"png")).write(image)
    return bytes(buffer.data().data())


def ico(images: list[tuple[int, bytes]]) -> bytes:
    """ICONDIR, one ICONDIRENTRY per image, then the PNG data."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, data in images:
        side = 0 if size >= 256 else size  # 0 means 256 in an ICONDIRENTRY
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(data for _, data in images)


def main() -> int:
    app = QGuiApplication(sys.argv)
    images = [
        (
            size,
            render_png(LOGO / ("embr-icon-small.svg" if size <= SMALL else "embr-icon.svg"), size),
        )
        for size in SIZES
    ]
    out = LOGO / "embr.ico"
    out.write_bytes(ico(images))
    print(f"wrote {out} ({out.stat().st_size} bytes, sizes {', '.join(map(str, SIZES))})")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
