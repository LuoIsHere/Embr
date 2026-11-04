"""The hint above Flash is never lower than its wrapped text, so its box cannot cut it."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from embr.ui.connection import HintLabel

TEXT = "Identified. The chip stays in download mode so the next flash can start right away. " * 3


@pytest.fixture
def hint(qapp: QCoreApplication) -> Iterator[HintLabel]:
    box = QWidget()
    label = HintLabel()
    QVBoxLayout(box).addWidget(label)
    label.setText(TEXT)
    box.resize(400, 200)
    box.show()
    QApplication.processEvents()
    yield label
    box.close()


def test_the_minimum_height_follows_the_width(hint: HintLabel) -> None:
    wide = hint.minimumHeight()
    assert wide == hint.heightForWidth(hint.width())

    box = hint.parentWidget()
    assert box is not None
    box.resize(170, box.height())
    QApplication.processEvents()
    QApplication.processEvents()

    assert hint.width() < 170
    assert hint.minimumHeight() == hint.heightForWidth(hint.width()) > wide

    box.resize(400, box.height())
    QApplication.processEvents()
    QApplication.processEvents()

    assert hint.minimumHeight() == wide


def test_the_minimum_height_follows_the_text(hint: HintLabel) -> None:
    tall = hint.minimumHeight()
    hint.setText("Short.")
    hint.fit_text()
    assert hint.minimumHeight() < tall


def test_a_font_change_is_followed(hint: HintLabel) -> None:
    before = hint.minimumHeight()
    font = hint.font()
    font.setPixelSize(font.pixelSize() * 2 if font.pixelSize() > 0 else 30)
    hint.setFont(font)
    assert hint.minimumHeight() > before
