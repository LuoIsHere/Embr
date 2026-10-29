"""core/logs: daily log files, segments and per-board files (design doc §7.6)."""

import datetime
from pathlib import Path

import pytest

from embr.core.logs import (
    BOARDS_FOLDER,
    HEADER_PREFIX,
    LogBook,
    board_log_name,
    header_line,
    log_file_name,
)
from embr.core.storage.ids import new_id

DAY = datetime.date(2026, 10, 29)


class Clock:
    """Replaceable LogBook clock; tests move `now` by hand."""

    def __init__(self, now: datetime.datetime) -> None:
        self.now = now

    def __call__(self) -> datetime.datetime:
        return self.now


def _at(day: datetime.date, hour: int, minute: int = 0, second: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute, second))


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_same_day_segments_append_to_one_file(tmp_path: Path) -> None:
    clock = Clock(_at(DAY, 9))
    book = LogBook(tmp_path, clock)
    identify_id, flash_id = new_id(), new_id()  # identify: an id of its own, no record
    with book.open_segment(identify_id, "identify", port="COM3") as segment:
        segment.write("chip esp32")
    first_start = clock.now
    clock.now = _at(DAY, 10, 30)
    with book.open_segment(
        flash_id, "flash", port="COM3", project_name="Station", firmware_name="Release"
    ) as segment:
        segment.write("written")

    name = log_file_name(DAY)
    assert name == "2026-10-29.log"
    assert segment.file_name == name
    assert [p.name for p in tmp_path.iterdir()] == [name]
    lines = _lines(tmp_path / name)
    first = book.find_segment(name, identify_id)
    second = book.find_segment(name, flash_id)
    assert (first, second) == (0, 2)
    assert lines[0] == header_line(
        identify_id, "identify", first_start, port="COM3", project_name="", firmware_name=""
    )
    assert lines[2] == header_line(
        flash_id,
        "flash",
        clock.now,
        port="COM3",
        project_name="Station",
        firmware_name="Release",
    )
    for line, segment_id, operation in (
        (lines[0], identify_id, "identify"),
        (lines[2], flash_id, "flash"),
    ):
        assert line.startswith(HEADER_PREFIX)
        assert segment_id in line
        assert operation in line
    assert lines[1] == "chip esp32"
    assert lines[3] == "written"


def test_next_day_writes_new_file(tmp_path: Path) -> None:
    clock = Clock(_at(DAY, 18))
    book = LogBook(tmp_path, clock)
    first, second = new_id(), new_id()
    with book.open_segment(first, "erase", port="COM3"):
        pass
    clock.now = _at(DAY + datetime.timedelta(days=1), 8)
    with book.open_segment(second, "flash", port="COM3") as segment:
        pass

    assert segment.file_name == "2026-10-30.log"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-10-29.log", "2026-10-30.log"]
    assert book.find_segment("2026-10-30.log", second) == 0
    assert book.find_segment("2026-10-29.log", second) is None


def test_segment_past_midnight_stays_in_start_file(tmp_path: Path) -> None:
    clock = Clock(_at(DAY, 23, 59, 59))
    book = LogBook(tmp_path, clock)
    segment_id = new_id()
    segment = book.open_segment(segment_id, "read_flash", port="COM3")
    clock.now = _at(DAY + datetime.timedelta(days=1), 0, 0, 5)
    segment.write("still reading")
    segment.close()
    segment.close()  # safe twice

    assert segment.file_name == "2026-10-29.log"
    assert [p.name for p in tmp_path.iterdir()] == ["2026-10-29.log"]
    assert _lines(tmp_path / "2026-10-29.log")[1:] == ["still reading"]


def test_find_segment_unknown_id_or_missing_file(tmp_path: Path) -> None:
    book = LogBook(tmp_path, Clock(_at(DAY, 9)))
    with book.open_segment(new_id(), "flash", port="COM3"):
        pass
    assert book.find_segment("2026-10-29.log", new_id()) is None
    assert book.find_segment("2026-10-28.log", new_id()) is None


def test_header_line_marks_empty_names_and_flattens_line_breaks() -> None:
    line = header_line(
        "01JC0000000000000000000001",
        "erase",
        _at(DAY, 8, 5, 3),
        port="COM3",
        project_name="",
        firmware_name="Line one\nLine two",
    )
    assert line.startswith(HEADER_PREFIX)
    assert "\n" not in line
    assert "\r" not in line
    for part in ("01JC0000000000000000000001", "erase", "2026-10-29", "08:05:03", "COM3"):
        assert part in line
    assert "Line one Line two" in line
    assert "-" in line.split()  # the empty project name


def test_multiline_write_and_utf8(tmp_path: Path) -> None:
    book = LogBook(tmp_path, Clock(_at(DAY, 9)))
    with book.open_segment(new_id(), "flash", port="COM3", firmware_name="固件 Grüße") as segment:
        segment.write("first\nsecond")
    raw = (tmp_path / "2026-10-29.log").read_bytes()
    assert b"\r\n" not in raw
    lines = raw.decode("utf-8").splitlines()
    assert "固件 Grüße" in lines[0]
    assert lines[1:] == ["first", "second"]


@pytest.mark.parametrize("mac", ["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aabbccddeeff"])
def test_board_log_name_is_a_windows_safe_mac(mac: str) -> None:
    name = board_log_name(mac)
    assert name == "aa-bb-cc-dd-ee-ff.log"
    assert ":" not in name


@pytest.mark.parametrize("mac", ["", "aa:bb:cc:dd:ee", "gg:hh:ii:jj:kk:ll"])
def test_board_log_name_rejects_a_non_mac(mac: str) -> None:
    with pytest.raises(ValueError):
        board_log_name(mac)


def test_segment_lines_hold_header_and_written_lines(tmp_path: Path) -> None:
    book = LogBook(tmp_path, Clock(_at(DAY, 9)))
    segment_id = new_id()
    with book.open_segment(segment_id, "flash", port="COM1") as segment:
        segment.write("first\nsecond")
        lines = segment.lines
    header = header_line(
        segment_id, "flash", _at(DAY, 9), port="COM1", project_name="", firmware_name=""
    )
    assert lines == [header, "first", "second"]
    assert lines == _lines(tmp_path / "2026-10-29.log")


def test_copy_to_board_appends_segments_in_order(tmp_path: Path) -> None:
    clock = Clock(_at(DAY, 9))
    book = LogBook(tmp_path, clock)
    mac = "aa:bb:cc:dd:ee:ff"
    first = book.open_segment(new_id(), "flash", port="COM1")
    first.write("flashed")
    clock.now = _at(DAY, 10)
    with book.open_segment(new_id(), "erase", port="COM1") as second:
        second.write("erased")
    daily = tmp_path / "2026-10-29.log"
    before = daily.read_bytes()

    assert not (tmp_path / BOARDS_FOLDER).exists()
    path = book.copy_to_board(first, mac)  # still open: lines written so far
    first.close()
    assert book.copy_to_board(second, mac) == path

    assert path == book.board_log_path(mac) == tmp_path / BOARDS_FOLDER / "aa-bb-cc-dd-ee-ff.log"
    assert _lines(path) == first.lines + second.lines
    assert _lines(path)[1::2] == ["flashed", "erased"]
    assert b"\r\n" not in path.read_bytes()
    assert daily.read_bytes() == before
