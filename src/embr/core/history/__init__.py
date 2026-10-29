"""Flash records in embr.sqlite (design doc §3.3).

A record is a snapshot taken when the operation ran: it never refers to the current
content of a project or firmware, so renaming, moving or deleting them leaves it intact.
"""

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from embr.core.flasher.protocol import ErrorEvent, ResultEvent


class Operation(StrEnum):
    FLASH = "flash"
    ERASE = "erase"
    """No entries and no firmware."""
    READ_FLASH = "read_flash"
    """One entry (the region read); firmware is the dump firmware created from it."""


class RecordResult(StrEnum):
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUSED = "refused"


@dataclass(frozen=True)
class RecordEntry:
    name: str
    address: int
    sha256: str
    size: int
    kind: str
    """EntryKind value, e.g. "app"."""
    skipped: bool = False
    """Flash already held identical content (skip_flashed); nothing was written."""


@dataclass(frozen=True)
class FlashRecord:
    id: str
    """ULID; created before the job starts so the log segment header can carry it."""
    operation: Operation
    timestamp: str
    """Start time, ISO 8601 local time with seconds."""
    result: RecordResult
    project_id: str | None = None
    project_name: str = ""
    firmware_id: str | None = None
    firmware_name: str = ""
    device: str = ""
    """Group of the firmware at that time."""
    port: str = ""
    baud: int | None = None
    """None on native USB."""
    connection_type: str = ""
    """ConnectionType value as detected for this job."""
    reset_before: str = ""
    reset_after: str = ""
    """The reset strategy actually used (after adjustment for the connection type)."""
    chip_detected: str | None = None
    chip_revision: str | None = None
    mac: str | None = None
    """Normalized, e.g. "aa:bb:cc:dd:ee:ff"."""
    flash_size_detected: str | None = None
    entries: tuple[RecordEntry, ...] = ()
    refuse_reason: str | None = None
    duration_ms: int = 0
    esptool_version: str = ""
    command: str = ""
    """Equivalent esptool command line."""
    log_file: str = ""
    """"YYYY-MM-DD.log" of the day the operation started."""


def result_from_event(event: ResultEvent | ErrorEvent) -> RecordResult:
    """Record result for a job's terminal event. The worker never sends a FAILED result: a
    failure ends with an ErrorEvent, which maps to FAILED; ResultEvent statuses map to the
    record result of the same name."""
    raise NotImplementedError


def normalize_mac(text: str) -> str:
    """Lower-case, colon-separated form; accepts "AA-BB-CC-DD-EE-FF" and
    "aabbccddeeff". Raises ValueError for anything that is not 6 hex bytes."""
    raise NotImplementedError


class History:
    """Flash records over an open embr.sqlite connection (see storage.database)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        raise NotImplementedError

    def add(self, record: FlashRecord) -> None:
        """Insert a record and its entries in one transaction. The MAC is stored normalized.
        Raises ValueError if a record with this id already exists."""
        raise NotImplementedError

    def get(self, record_id: str) -> FlashRecord:
        """Raises KeyError if missing."""
        raise NotImplementedError

    def query(
        self,
        *,
        project_id: str | None = None,
        firmware_id: str | None = None,
        mac: str | None = None,
        operation: Operation | None = None,
        limit: int | None = None,
    ) -> list[FlashRecord]:
        """Records matching every filter given (None = no filter), newest first. `mac` is
        normalized first, so any accepted spelling matches."""
        raise NotImplementedError
