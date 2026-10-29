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
    return f"{day.isoformat()}.log"


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
    fields = [
        f"{HEADER_PREFIX}{segment_id}",
        operation,
        start.strftime("%Y-%m-%d %H:%M:%S"),
        f"port {_one_line(port)}",
        f"project {_one_line(project_name)}",
        f"firmware {_one_line(firmware_name)}",
    ]
    return " | ".join(fields)


class LogSegment:
    """An open segment. Each write() is flushed, so a crash keeps everything written."""

    file_name: str
    """Name of the file the segment lives in (the day it started)."""
    path: Path

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file_name = path.name
        self._file = path.open("a", encoding="utf-8", newline="\n")

    def write(self, text: str) -> None:
        """Append `text` as one or more lines (a newline is added; embedded newlines start
        new lines)."""
        lines = text.splitlines() or [""]
        self._file.write("".join(f"{line}\n" for line in lines))
        self._file.flush()

    def close(self) -> None:
        """Safe to call twice."""
        self._file.close()

    def __enter__(self) -> "LogSegment":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class LogBook:
    """The logs/ folder. UTF-8, "\\n" line endings, appended across operations and runs."""

    def __init__(
        self,
        directory: Path,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        """`clock` is replaceable for tests (local time)."""
        self.directory = directory
        self._clock = clock

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
        start = self._clock()
        self.directory.mkdir(parents=True, exist_ok=True)
        segment = LogSegment(self.directory / log_file_name(start.date()))
        header = header_line(
            segment_id,
            operation,
            start,
            port=port,
            project_name=project_name,
            firmware_name=firmware_name,
        )
        segment.write(header)
        return segment

    def find_segment(self, file_name: str, segment_id: str) -> int | None:
        """0-based line number of the segment's header in `file_name`, or None if the file
        or the segment is missing."""
        path = self.directory / file_name
        if not path.is_file():
            return None
        prefix = f"{HEADER_PREFIX}{segment_id} "
        with path.open(encoding="utf-8", errors="replace") as file:
            for number, line in enumerate(file):
                if line.startswith(prefix):
                    return number
        return None


def _one_line(text: str) -> str:
    return " ".join(text.splitlines()) or "-"
