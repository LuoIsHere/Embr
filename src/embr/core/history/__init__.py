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
    if isinstance(event, ErrorEvent):
        return RecordResult.FAILED
    return RecordResult(event.status.value)


def normalize_mac(text: str) -> str:
    """Lower-case, colon-separated form; accepts "AA-BB-CC-DD-EE-FF" and
    "aabbccddeeff". Raises ValueError for anything that is not 6 hex bytes."""
    digits = text.strip().lower().replace(":", "").replace("-", "")
    if len(digits) != 12 or any(c not in "0123456789abcdef" for c in digits):
        raise ValueError(f"not a MAC address: {text!r}")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


class History:
    """Flash records over an open embr.sqlite connection (see storage.database)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def add(self, record: FlashRecord) -> None:
        """Insert a record and its entries in one transaction. The MAC is stored normalized.
        Raises ValueError if a record with this id already exists."""
        row = {name: getattr(record, name) for name in _RECORD_COLUMNS}
        row["mac"] = normalize_mac(record.mac) if record.mac else record.mac
        names = ", ".join(_RECORD_COLUMNS)
        marks = ", ".join(f":{name}" for name in _RECORD_COLUMNS)
        try:
            with self._db:
                self._db.execute(f"INSERT INTO flash_records ({names}) VALUES ({marks})", row)
                self._db.executemany(
                    "INSERT INTO record_entries"
                    " (record_id, position, name, address, sha256, size, kind, skipped)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (record.id, i, e.name, e.address, e.sha256, e.size, e.kind, e.skipped)
                        for i, e in enumerate(record.entries)
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"record {record.id} already exists") from exc

    def get(self, record_id: str) -> FlashRecord:
        """Raises KeyError if missing."""
        found = self._select("WHERE id = ?", [record_id])
        if not found:
            raise KeyError(record_id)
        return found[0]

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
        where: list[str] = []
        params: list[object] = []
        for column, value in (
            ("project_id", project_id),
            ("firmware_id", firmware_id),
            ("mac", normalize_mac(mac) if mac is not None else None),
            ("operation", operation.value if operation is not None else None),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        # ids are ULIDs, so id order is creation order
        clause += " ORDER BY timestamp DESC, id DESC"
        if limit is not None:
            clause += " LIMIT ?"
            params.append(limit)
        return self._select(clause, params)

    def _select(self, clause: str, params: list[object]) -> list[FlashRecord]:
        names = ", ".join(_RECORD_COLUMNS)
        rows = self._db.execute(f"SELECT {names} FROM flash_records {clause}", params).fetchall()
        records: list[FlashRecord] = []
        for row in rows:
            v: dict[str, object] = dict(zip(_RECORD_COLUMNS, row, strict=True))
            entries = self._db.execute(
                "SELECT name, address, sha256, size, kind, skipped FROM record_entries"
                " WHERE record_id = ? ORDER BY position",
                [v["id"]],
            ).fetchall()
            records.append(
                FlashRecord(
                    id=_str(v["id"]),
                    operation=Operation(_str(v["operation"])),
                    timestamp=_str(v["timestamp"]),
                    result=RecordResult(_str(v["result"])),
                    project_id=_opt_str(v["project_id"]),
                    project_name=_str(v["project_name"]),
                    firmware_id=_opt_str(v["firmware_id"]),
                    firmware_name=_str(v["firmware_name"]),
                    device=_str(v["device"]),
                    port=_str(v["port"]),
                    baud=_opt_int(v["baud"]),
                    connection_type=_str(v["connection_type"]),
                    reset_before=_str(v["reset_before"]),
                    reset_after=_str(v["reset_after"]),
                    chip_detected=_opt_str(v["chip_detected"]),
                    chip_revision=_opt_str(v["chip_revision"]),
                    mac=_opt_str(v["mac"]),
                    flash_size_detected=_opt_str(v["flash_size_detected"]),
                    entries=tuple(_entry(e) for e in entries),
                    refuse_reason=_opt_str(v["refuse_reason"]),
                    duration_ms=_opt_int(v["duration_ms"]) or 0,
                    esptool_version=_str(v["esptool_version"]),
                    command=_str(v["command"]),
                    log_file=_str(v["log_file"]),
                )
            )
        return records


def _entry(row: tuple[object, ...]) -> RecordEntry:
    name, address, sha256, size, kind, skipped = row
    return RecordEntry(
        _str(name),
        _opt_int(address) or 0,
        _str(sha256),
        _opt_int(size) or 0,
        _str(kind),
        bool(skipped),
    )


def _str(value: object) -> str:
    """Text column; NULL (older or partial rows) reads as ""."""
    return value if isinstance(value, str) else ""


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


_RECORD_COLUMNS = (
    "id",
    "operation",
    "timestamp",
    "result",
    "project_id",
    "project_name",
    "firmware_id",
    "firmware_name",
    "device",
    "port",
    "baud",
    "connection_type",
    "reset_before",
    "reset_after",
    "chip_detected",
    "chip_revision",
    "mac",
    "flash_size_detected",
    "refuse_reason",
    "duration_ms",
    "esptool_version",
    "command",
    "log_file",
)
