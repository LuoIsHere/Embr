"""Entry file consistency: name, path and hash (design doc §3.4)."""

from enum import StrEnum
from pathlib import Path

from embr.core.firmware.model import Entry, Firmware


class FileState(StrEnum):
    OK = "ok"
    CHANGED = "changed"
    """File present but its content (sha256 or size) differs: needs confirmation."""
    MISSING = "missing"
    """No file at the recorded path or in the archive."""


def entry_state(entry: Entry, archive_dir: Path) -> FileState:
    """ "path:" entries: the file must exist, its name must equal entry.name, and its sha256
    and size must match. "archive:" entries: archive_dir/<sha256>.bin must exist with that
    hash. "blank:" entries are always OK."""
    raise NotImplementedError


def firmware_states(firmware: Firmware, archive_dir: Path) -> dict[str, FileState]:
    """entry_state() of every entry (enabled or not), keyed by entry id."""
    raise NotImplementedError


def read_entry(entry: Entry, archive_dir: Path) -> bytes:
    """The bytes to write for an entry. Raises FileNotFoundError if missing."""
    raise NotImplementedError


def confirm_entry(entry: Entry, archive_dir: Path) -> None:
    """Accept the current content of a CHANGED "path:" entry: record its new sha256 and
    size."""
    raise NotImplementedError


def relocate(entry: Entry, path: Path) -> None:
    """Point a "path:" entry at another file: file, name, sha256 and size follow the new
    file. Address, enabled state and note are kept."""
    raise NotImplementedError
