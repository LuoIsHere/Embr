"""Selecting what the workspace shows, and the merge dialog's replace question: a duplicated
or deleted firmware moves the selection the way a click does, selecting the shown project
again keeps the last job's hint, and replacing a file names the firmware using it. Driven
offscreen on a real window."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from embr.app.operations import JobKind, JobOutcome
from embr.core.history import RecordResult
from embr.i18n import Language, set_language, tr
from embr.ui import dialogs
from embr.ui.window import EmbrWindow
from embr.ui.workspace import MCU_WIDEST

# pyright: reportPrivateUsage=false

OUTCOME = JobOutcome(JobKind.IDENTIFY, RecordResult.OK, "", None, "", "", False)


def _fixed(window: EmbrWindow, tmp_path: Path, name: str = "fw") -> str:
    file = tmp_path / f"{name}.bin"
    file.write_bytes(b"\x00" * 64)
    firmware = window.library.create_firmware(name)
    window.library.add_files(firmware.id, [file])
    return firmware.id


def test_selecting_the_shown_project_again_keeps_the_last_job_hint(window: EmbrWindow) -> None:
    project = window.library.create_project("p")
    window.select_project(project.id)
    window._last_outcome = OUTCOME
    window.select_project(project.id)  # a click on its sidebar row
    assert window._last_outcome is OUTCOME

    window.select_project(window.library.create_project("q").id)
    assert window._last_outcome is None


def test_a_duplicate_is_selected_like_a_click(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware_id = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    window._last_outcome = OUTCOME
    seen: list[str] = []
    real = window.library.tracker.mark_seen

    def mark_seen(seen_id: str) -> None:
        seen.append(seen_id)
        real(seen_id)

    monkeypatch.setattr(window.library.tracker, "mark_seen", mark_seen)
    window.firmware_action("duplicate")

    selected = window._selected_firmware_id()
    assert selected is not None and selected != firmware_id
    assert seen == [firmware_id]  # leaving the original ends its "Build updated" state
    assert window._last_outcome is None


def test_deleting_the_last_firmware_clears_the_selection(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware_id = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    window._last_outcome = OUTCOME

    def yes(*_args: object, **_kwargs: object) -> tuple[bool, bool]:
        return True, False

    monkeypatch.setattr(dialogs, "confirm", yes)
    window.firmware_action("delete")
    assert window._selection is None
    assert window._last_outcome is None


def test_replacing_a_file_names_the_firmware_using_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: EmbrWindow
) -> None:
    asked: list[list[str]] = []

    def answer(_parent: QWidget, _title: str, lines: list[str], _ok: str) -> tuple[bool, bool]:
        asked.append(lines)
        return False, False

    monkeypatch.setattr(dialogs, "confirm", answer)
    output = tmp_path / "fw_merged.bin"
    assert dialogs._replace_ok(window, output, ["fw (merged)"])  # nothing to replace yet
    output.write_bytes(b"first merge")
    assert not dialogs._replace_ok(window, output, ["fw (merged)", "<b>x</b>"])
    (question, users) = asked[0]
    assert "<b>fw_merged.bin</b> already exists" in question
    assert "<b>fw (merged)</b>, <b>&lt;b&gt;x&lt;/b&gt;</b>" in users  # names as typed
    assert not dialogs._replace_ok(window, output)
    assert len(asked[1]) == 1  # no firmware uses it: the question alone


def test_the_mcu_column_shows_its_widest_choice_in_chinese(
    window: EmbrWindow, tmp_path: Path
) -> None:
    set_language(Language.ZH_CN)  # the fixture restores the language
    firmware_id = _fixed(window, tmp_path)
    window.resize(1800, 900)
    window.select_firmware(firmware_id)
    window.show()
    QApplication.processEvents()
    QApplication.processEvents()
    table = window.firmware_view.entries.table
    widest = tr(f"auto ({MCU_WIDEST})")
    assert widest.startswith("自动")
    assert table.columnWidth(2) >= table.fontMetrics().horizontalAdvance(widest) + 34
