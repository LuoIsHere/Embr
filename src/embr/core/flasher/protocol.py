"""Worker protocol: one task JSON on stdin, one JSON event per line on stdout.

The protocol is one-way: the worker never waits for input after the task.
"""

from dataclasses import dataclass
from enum import StrEnum

from embr.core.flasher.checks import CheckLevel, SecurityState
from embr.core.flasher.plan import FlashPlan, IdentifyPlan


class ProtocolError(ValueError):
    """A task or event line that cannot be decoded."""


class ResultStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    """Only produced by the runner after cancel(); flash content may be incomplete."""


class LogLevel(StrEnum):
    INFO = "info"
    NOTE = "note"
    WARNING = "warning"
    ERROR = "error"
    STAGE = "stage"
    """Stage marker from esptool's logger; text is "begin" or "end"."""


class EntryStatus(StrEnum):
    WRITTEN = "written"
    SKIPPED = "skipped"
    """Flash already held identical content (skip_flashed)."""


@dataclass(frozen=True)
class InfoEvent:
    chip: str
    """esptool chip name."""
    revision: str
    """e.g. "v1.0"."""
    major: int
    minor: int
    mac: str
    """"aa:bb:cc:dd:ee:ff"."""
    flash_size: str | None
    security: SecurityState
    in_download_mode: bool
    """True when the job leaves the chip in ROM download mode (after = no-reset)."""


@dataclass(frozen=True)
class CheckEvent:
    name: str
    level: CheckLevel
    message: str


@dataclass(frozen=True)
class LogEvent:
    level: LogLevel
    text: str


@dataclass(frozen=True)
class ProgressEvent:
    entry_index: int
    address: int
    """Address of the entry being written; comes from the worker, not from esptool."""
    cur: int
    total: int
    text: str


@dataclass(frozen=True)
class EntryEvent:
    entry_index: int
    address: int
    status: EntryStatus


@dataclass(frozen=True)
class ResultEvent:
    status: ResultStatus
    message: str


@dataclass(frozen=True)
class ErrorEvent:
    message: str
    details: str
    """Traceback or stderr text."""


Event = InfoEvent | CheckEvent | LogEvent | ProgressEvent | EntryEvent | ResultEvent | ErrorEvent
Task = IdentifyPlan | FlashPlan


def encode_task(task: Task) -> str:
    """Task as a single JSON document (no trailing newline)."""
    raise NotImplementedError


def decode_task(text: str) -> Task:
    """Inverse of encode_task(). Raises ProtocolError."""
    raise NotImplementedError


def encode_event(event: Event) -> str:
    """Event as one JSON line without the newline; includes a "type" field."""
    raise NotImplementedError


def decode_event(line: str) -> Event:
    """Inverse of encode_event(). Raises ProtocolError."""
    raise NotImplementedError
