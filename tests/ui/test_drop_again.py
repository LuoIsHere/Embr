"""Files dropped again on the firmware that already has them, in the workspace or on its
sidebar row: no second entry; changed content is accepted, unchanged files are named."""

from pathlib import Path

import pytest

from embr.ui import dialogs
from embr.ui.window import EmbrWindow

# pyright: reportPrivateUsage=false


def _fixed(window: EmbrWindow, tmp_path: Path) -> tuple[str, list[Path]]:
    folder = tmp_path / "fw"
    folder.mkdir()
    files = [folder / "a.bin", folder / "b.bin"]
    for i, file in enumerate(files):
        file.write_bytes(bytes([i]) * 64)
    firmware = window.library.create_from_files(files, name="fw")
    window.select_firmware(firmware.id)
    return firmware.id, files


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    shown: list[list[str]] = []

    def notice(_parent: object, _title: str, lines: list[str]) -> None:
        shown.append(lines)

    monkeypatch.setattr(dialogs, "notice", notice)
    return shown


def _names(window: EmbrWindow, firmware_id: str) -> list[str]:
    return [e.name for e in window.library.firmware(firmware_id).entries]


def test_a_sidebar_drop_of_the_same_files_adds_nothing(
    window: EmbrWindow, tmp_path: Path, notices: list[list[str]]
) -> None:
    firmware_id, files = _fixed(window, tmp_path)
    window._drop_on_firmware(firmware_id, files, None, [])
    assert sorted(_names(window, firmware_id)) == ["a.bin", "b.bin"]
    assert notices == [["Already in this firmware, content unchanged: a.bin, b.bin"]]


def test_a_dropped_changed_file_is_confirmed_not_added(
    window: EmbrWindow, tmp_path: Path, notices: list[list[str]]
) -> None:
    firmware_id, files = _fixed(window, tmp_path)
    files[0].write_bytes(b"\x07" * 80)  # overwritten on disk

    window._drop_on_firmware(firmware_id, files, None, [])
    assert notices == []  # something was done: the log says what
    log = window.log_panel.log.toPlainText()
    assert "a.bin: changed content accepted" in log
    assert "already in this firmware, unchanged: b.bin" in log

    window.firmware_view.entries._add_paths([files[0]])  # the workspace drop
    assert sorted(_names(window, firmware_id)) == ["a.bin", "b.bin"]
    assert not window.firmware_view.banner.isVisible()
