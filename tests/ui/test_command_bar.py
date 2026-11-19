"""The equivalent command bar: a long command is elided to the width left for it, and the
Copy button keeps its width whatever the window width and the command."""

from PySide6.QtWidgets import QApplication

from embr.ui.theme import COPY_BUTTON_WIDTH
from embr.ui.window import EmbrWindow

LONG = "esptool --chip esp32s3 -p COM5 write-flash" + " 0x10000 build\\app\\bootloader\\app.bin" * 8
COPY_WIDTH = COPY_BUTTON_WIDTH


def _settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def test_copy_keeps_its_width_when_the_window_narrows(window: EmbrWindow) -> None:
    bar = window.cmdbar
    window.resize(1600, 800)
    window.show()
    bar.set_command(LONG)
    _settle()
    assert bar.copy_button.width() == COPY_WIDTH

    window.resize(window.minimumWidth(), 800)
    _settle()

    assert bar.copy_button.width() == COPY_WIDTH
    assert bar.copy_button.geometry().right() < bar.width()  # inside the bar, not cut off
    assert bar.width() <= window.width()
    shown = bar.text.text()
    assert shown != LONG and shown.endswith("…")
    assert bar.text.fontMetrics().horizontalAdvance(shown) <= bar.text.width()
    assert bar.text.toolTip() == LONG


def test_a_short_command_is_shown_whole(window: EmbrWindow) -> None:
    bar = window.cmdbar
    window.resize(1400, 800)
    window.show()
    bar.set_command("esptool --chip esp32 -p COM3 read-mac")
    _settle()

    assert bar.text.text() == "esptool --chip esp32 -p COM3 read-mac"
    assert bar.copy_button.width() == COPY_WIDTH
