"""The Merge to .bin dialog shows the size of the image it would write, following the
Pad option."""

import json
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel

from embr.ui.dialogs.device import size_text
from embr.ui.window import EmbrWindow

SAMPLE = Path(__file__).parents[1] / "fixtures" / "idf" / "esp32s3_hello_v61" / "build"


def _hello(window: EmbrWindow, folder: Path) -> str:
    """The sample's bootloader, partition table and app, copied below `folder`."""
    args = json.loads((SAMPLE / "flasher_args.json").read_text(encoding="utf-8"))
    copies: list[Path] = []
    for key in ("app", "bootloader", "partition-table"):  # the app's folder holds the rest
        name = args[key]["file"]
        copy = folder / name
        copy.parent.mkdir(parents=True, exist_ok=True)
        copies.append(Path(shutil.copy(SAMPLE / name, copy)))
    firmware = window.library.create_firmware("hello")
    window.library.add_files(firmware.id, copies)
    window.library.set_param(firmware.id, "flash_size", "4MB")
    return firmware.id


def _size_line(dialog: QDialog) -> str:
    shown = [
        w.text()
        for w in dialog.findChildren(QLabel)
        if w.isVisible() and w.text().startswith("Merged image size")
    ]
    return shown[0] if shown else ""


def test_the_dialog_shows_the_merged_size(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id = _hello(window, tmp_path / "build")
    window.select_firmware(firmware_id)
    window.show()
    seen: list[str] = []

    def look() -> None:
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, QDialog):
            QTimer.singleShot(20, look)
            return
        seen.append(_size_line(dialog))
        (pad,) = [b for b in dialog.findChildren(QCheckBox) if "pad" in b.text()]
        pad.setChecked(True)
        seen.append(_size_line(dialog))
        dialog.reject()

    QTimer.singleShot(0, look)
    window.library_actions.merge()

    app = (SAMPLE / json.loads((SAMPLE / "flasher_args.json").read_text())["app"]["file"]).stat()
    end = 0x10000 + app.st_size  # the app comes last
    assert seen == [
        f"Merged image size: {size_text(end)} ({end:,} bytes)",
        "Merged image size: 4 MB (4,194,304 bytes)",
    ]


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (4 * 1024 * 1024, "4 MB"),
        (1310720, "1.25 MB"),
        (0x2000, "8 KB"),
        (1536, "1.5 KB"),
        (512, "512 B"),
    ],
)
def test_size_text(size: int, text: str) -> None:
    assert size_text(size) == text
