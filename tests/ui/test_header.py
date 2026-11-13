"""The workspace header: its name sits next to its tags, and the grey line shows when the
firmware's files were last updated. Driven offscreen on a real window."""

import datetime
import os
from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel

from embr.ui.widgets import ElidedLabel
from embr.ui.window import EmbrWindow
from embr.ui.workspace import CRUMB_MIN, HEADING_MIN, shrink_to, time_text

YESTERDAY_NOON = datetime.datetime.combine(
    datetime.date.today() - datetime.timedelta(days=1), datetime.time(12, 0)
)


def _label(window: EmbrWindow, index: int) -> QLabel:
    item = window.header.row.itemAt(index)
    assert item is not None
    widget = item.widget()
    assert isinstance(widget, QLabel)
    return widget


def _settle(window: EmbrWindow) -> None:
    window.show()
    QApplication.processEvents()
    QApplication.processEvents()


def _fixed(window: EmbrWindow, tmp_path: Path) -> tuple[str, Path]:
    file = tmp_path / "app.bin"
    file.write_bytes(b"\x00" * 64)
    firmware = window.library.create_firmware("fw")
    window.library.add_files(firmware.id, [file])
    return firmware.id, file


def test_a_short_name_leaves_no_gap_before_the_tags(window: EmbrWindow) -> None:
    firmware = window.library.create_firmware("fw")
    window.select_firmware(firmware.id)
    _settle(window)

    name, source_tag = _label(window, 1), _label(window, 2)
    assert name.width() < HEADING_MIN
    assert source_tag.x() - name.geometry().right() - 1 == window.header.row.spacing()


def test_a_short_project_name_leaves_no_gap_before_the_tags(window: EmbrWindow) -> None:
    project = window.library.create_project("p")
    window.select_project(project.id)
    _settle(window)

    assert _label(window, 1).width() < HEADING_MIN


def test_a_long_text_may_still_shrink_to_the_minimum(window: EmbrWindow) -> None:
    long_text = ElidedLabel("a name much longer than any minimum width of the header " * 2)
    shrink_to(long_text, HEADING_MIN)
    assert long_text.minimumWidth() == HEADING_MIN

    short_text = ElidedLabel("x")
    shrink_to(short_text, CRUMB_MIN)
    assert short_text.minimumWidth() == short_text.sizeHint().width() < CRUMB_MIN


def test_time_text_shows_the_date_on_another_day() -> None:
    moment = datetime.datetime(2026, 3, 4, 5, 6, 7)
    assert time_text(moment, today=moment.date()) == "05:06:07"
    assert time_text(moment, today=datetime.date(2026, 3, 5)) == "03-04 05:06"


def test_the_files_time_is_shown_for_a_firmware_with_files(
    window: EmbrWindow, tmp_path: Path
) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    stamp = YESTERDAY_NOON.timestamp()
    os.utime(file, (stamp, stamp))
    window.library.tracker.invalidate(firmware_id)
    window.select_firmware(firmware_id)
    _settle(window)

    label = window.header.files_updated
    assert label.isVisible()
    assert label.text() == f"Files updated {YESTERDAY_NOON:%m-%d %H:%M}"


def test_long_notes_end_in_an_ellipsis_that_is_visible(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id, _ = _fixed(window, tmp_path)
    window.library.set_param(firmware_id, "notes", "word " * 400)
    window.resize(1000, 700)  # the notes get well under NOTES_WIDTH
    window.select_firmware(firmware_id)
    _settle(window)

    notes = window.header.notes
    assert notes.text().endswith("…")
    assert notes.fontMetrics().horizontalAdvance(notes.text()) <= notes.width()
    assert notes.toolTip() == "word " * 400


def test_the_files_time_is_hidden_without_files_and_on_a_project(
    window: EmbrWindow, tmp_path: Path
) -> None:
    firmware_id, _ = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    _settle(window)
    assert window.header.files_updated.isVisible()

    empty = window.library.create_firmware("empty")
    window.select_firmware(empty.id)
    assert not window.header.files_updated.isVisible()

    project = window.library.create_project("p")
    window.select_firmware(firmware_id)
    window.select_project(project.id)
    assert not window.header.files_updated.isVisible()


def test_a_new_file_time_with_the_same_content_keeps_the_workspace(
    window: EmbrWindow, tmp_path: Path
) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    _settle(window)
    name = _label(window, 1)

    stamp = YESTERDAY_NOON.timestamp()
    os.utime(file, (stamp, stamp))
    window._recheck_selected()  # pyright: ignore[reportPrivateUsage]

    assert window.header.files_updated.text().endswith(f"{YESTERDAY_NOON:%m-%d %H:%M}")
    assert _label(window, 1) is name  # the header was not rebuilt


def _tags(window: EmbrWindow) -> list[str]:
    return [w.text() for w in window.header.findChildren(QLabel)]


def test_one_group_is_counted_in_the_singular(window: EmbrWindow) -> None:
    project = window.library.create_project("p")
    window.library.add_group(project.id, "Main")
    window.select_project(project.id)
    assert "1 group" in _tags(window)
    window.library.add_group(project.id, "Aux")
    window.select_project(project.id)
    assert "2 groups" in _tags(window)


def test_the_notes_tooltip_only_shows_a_note(window: EmbrWindow) -> None:
    # without a note the line already says it all; a note may be cut off, so it is shown whole
    project = window.library.create_project("p")
    window.select_project(project.id)
    assert window.header.notes.toolTip() == ""
    window.library.set_project_notes(project.id, "rev B board")
    window.select_project(project.id)
    assert window.header.notes.toolTip() == "rev B board"
    firmware = window.library.create_firmware("fw")
    window.select_firmware(firmware.id)
    assert window.header.notes.toolTip() == ""
