"""Daily log files logs/YYYY-MM-DD.log (design doc §7.6).

Every operation writes one segment that starts with a header line carrying the record id
(or, for identify, an id of its own), so a record can be traced to its log. Segments are
appended, never rewritten; nothing is deleted automatically.
"""

import datetime
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

HEADER_PREFIX = "=== "


def log_file_name(day: datetime.date) -> str:
    """File name for a day, e.g. "2026-10-29.log"."""
    raise NotImplementedError


def header_line(
    segment_id: str,
    operation: str,
    start: datetime.datetime,
    *,
    port: str,
    project_name: str,
    firmware_name: str,
) -> str:
    """One line, no newline: HEADER_PREFIX, the id, the operation (identify / flash / erase /
    read_flash), the start time to the second, then port, project and firmware names
    (empty ones shown as "-"). Line breaks inside names are replaced by spaces."""
    raise NotImplementedError


class LogSegment:
    """An open segment. Each write() is flushed, so a crash keeps everything written."""

    file_name: str
    """Name of the file the segment lives in (the day it started)."""
    path: Path

    def write(self, text: str) -> None:
        """Append `text` as one or more lines (a newline is added; embedded newlines start
        new lines)."""
        raise NotImplementedError

    def close(self) -> None:
        """Safe to call twice."""
        raise NotImplementedError

    def __enter__(self) -> "LogSegment":
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class LogBook:
    """The logs/ folder. UTF-8, "\\n" line endings, appended across operations and runs."""

    def __init__(
        self,
        directory: Path,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        """`clock` is replaceable for tests (local time)."""
        raise NotImplementedError

    def open_segment(
        self,
        segment_id: str,
        operation: str,
        *,
        port: str,
        project_name: str = "",
        firmware_name: str = "",
    ) -> LogSegment:
        """Start a segment in today's file (created if missing) and write its header. The
        whole segment stays in this file even if the operation runs past midnight."""
        raise NotImplementedError

    def find_segment(self, file_name: str, segment_id: str) -> int | None:
        """0-based line number of the segment's header in `file_name`, or None if the file
        or the segment is missing."""
        raise NotImplementedError
