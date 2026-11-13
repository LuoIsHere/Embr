"""Reordering projects and groups by dragging them in the sidebar.

No QDrag: the sidebar handles the mouse itself. When a drag starts, each block (a project
with its rows, or a group with its firmware) is grabbed as a picture and drawn by an overlay
on the tree's viewport, over the real rows. The dragged block follows the mouse and the
others slide (OUT_CUBIC, ANIMATION_MS) to leave a gap where it would land. On release the
overlay goes and the tree is rebuilt in the new order. Positions are kept in content
coordinates, so the overlay follows when the list scrolls under it.

The geometry (blocks, landing index, slot positions) is plain functions, tested without a
window."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEasingCurve, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import QWidget

ANIMATION_MS = 150
EDGE = 28
"""Height of the zone at the top and bottom of the list that scrolls it during a drag."""
SCROLL_STEP = 10
"""Pixels scrolled per tick (16 ms) in that zone."""


@dataclass(frozen=True)
class Block:
    key: str
    """Project id, or group name."""
    first: int
    """Index of its first row (top-level item)."""
    count: int
    top: int
    """Content y of its first row."""
    height: int


def blocks(rows: Sequence[tuple[str | None, int]]) -> list[Block]:
    """Blocks from the rows of the list in order: (block key or None, row height). Neighbouring
    rows with the same key form one block; None rows belong to none."""
    found: list[Block] = []
    top = 0
    for index, (key, height) in enumerate(rows):
        last = found[-1] if found else None
        if key is not None and last is not None and last.key == key and _next(last) == index:
            found[-1] = Block(key, last.first, last.count + 1, last.top, last.height + height)
        elif key is not None:
            found.append(Block(key, index, 1, top, height))
        top += height
    return found


def _next(block: Block) -> int:
    return block.first + block.count


def landing(spans: Sequence[Block], dragged: int, top: float) -> int:
    """Where the dragged block (index in `spans`) lands when its top is at `top` (content
    y): a block it started below stays above it until the dragged block's top rises past
    that block's middle; a block it started above stays below until its bottom sinks past
    that block's middle. Original positions only, so the answer depends on `top` alone (no
    flicker while the others slide)."""
    bottom = top + spans[dragged].height
    above = 0
    for i, block in enumerate(spans):
        middle = block.top + block.height / 2
        if (i < dragged and top > middle) or (i > dragged and bottom > middle):
            above += 1
    return above


def moved(keys: Sequence[str], dragged: int, index: int) -> list[str]:
    """`keys` with the one at `dragged` moved to `index` among the others."""
    others = [k for i, k in enumerate(keys) if i != dragged]
    others.insert(index, keys[dragged])
    return others


def slots(spans: Sequence[Block], dragged: int, index: int) -> dict[str, int]:
    """Content y of each block with the dragged one at `index`: the others slide to leave
    its gap. The dragged block's entry is its gap."""
    order = moved([b.key for b in spans], dragged, index)
    heights = {b.key: b.height for b in spans}
    top = spans[0].top if spans else 0
    found: dict[str, int] = {}
    for key in order:
        found[key] = top
        top += heights[key]
    return found


class Slide:
    """One block's animated y: from where it is to its target, restarted from the current
    position when the target changes (no jump)."""

    def __init__(self, y: float, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._from = self._to = y
        self._start = clock()

    @property
    def target(self) -> float:
        return self._to

    def go(self, y: float) -> None:
        if y == self._to:
            return
        self._from = self.now()
        self._to = y
        self._start = self._clock()

    def now(self) -> float:
        done = min(1.0, (self._clock() - self._start) * 1000 / ANIMATION_MS)
        eased = QEasingCurve(QEasingCurve.Type.OutCubic).valueForProgress(done)
        return self._from + (self._to - self._from) * eased

    def moving(self) -> bool:
        return self.now() != self._to


@dataclass
class Colors:
    background: str
    lifted: str
    """The dragged block's background."""
    edge: str
    selected: str
    """A selected row (the tree's own selection colour), for rows grabbed off screen."""


class Overlay(QWidget):
    """Draws the blocks of one reorder over the viewport. `region`: content y range the
    blocks fill (the real rows there are covered); `scroll`: the list's scroll offset now."""

    def __init__(
        self,
        viewport: QWidget,
        spans: Sequence[Block],
        pictures: dict[str, QPixmap],
        scroll: Callable[[], int],
        colors: Colors,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(viewport)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(viewport.rect())
        self.spans = list(spans)
        self._pictures = pictures
        self._scroll = scroll
        self._colors = colors
        self._clock = clock
        self._slides = {b.key: Slide(b.top, clock) for b in spans}
        self.dragged: int | None = None
        self.dragged_top = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self.show()
        self.raise_()

    @property
    def region(self) -> tuple[int, int]:
        top = self.spans[0].top
        last = self.spans[-1]
        return top, last.top + last.height

    def target(self, key: str) -> float:
        return self._slides[key].target

    def place(self, tops: dict[str, int]) -> None:
        """Slide every block (but the dragged one) to its y in `tops`."""
        for key, y in tops.items():
            if self.dragged is None or key != self.spans[self.dragged].key:
                self._slides[key].go(y)
        self._tick()

    def lift(self, dragged: int, top: float) -> None:
        self.dragged, self.dragged_top = dragged, top
        self.update()

    def drop_back(self) -> None:
        """Cancel: everything, the dragged block too, slides back to where it started."""
        if self.dragged is not None:
            key = self.spans[self.dragged].key
            self._slides[key] = Slide(self.dragged_top, self._clock)
            self.dragged = None
        for block in self.spans:
            self._slides[block.key].go(block.top)
        self._tick()

    def moving(self) -> bool:
        return any(s.moving() for s in self._slides.values())

    def _tick(self) -> None:
        if self.moving():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        offset = -self._scroll()
        top, bottom = self.region
        painter = QPainter(self)
        painter.fillRect(
            QRect(0, top + offset, self.width(), bottom - top), QColor(self._colors.background)
        )
        lifted = self.spans[self.dragged].key if self.dragged is not None else None
        for block in self.spans:
            if block.key != lifted:
                y = round(self._slides[block.key].now()) + offset
                painter.drawPixmap(0, y, self._pictures[block.key])
        if lifted is not None:
            picture = self._pictures[lifted]
            y = round(self.dragged_top) + offset
            box = QRectF(1.5, y + 0.5, self.width() - 3, picture.height() - 1)
            painter.setPen(QPen(QColor(self._colors.edge), 1))
            painter.setBrush(QColor(self._colors.lifted))
            painter.drawRoundedRect(box, 5, 5)
            painter.drawPixmap(0, y, picture)
        painter.end()
