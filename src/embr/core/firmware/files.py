"""Entry file consistency: name, path and hash (design doc §3.4)."""

import hashlib
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
    scheme, _, rest = entry.file.partition(":")
    if scheme == "blank":
        return FileState.OK
    if scheme == "archive":
        path = archive_dir / f"{rest}.bin"
        if not path.is_file():
            return FileState.MISSING
        return FileState.OK if _sha256(path.read_bytes()) == rest else FileState.CHANGED
    if scheme != "path":
        return FileState.MISSING
    path = Path(rest)
    if not path.is_file():
        return FileState.MISSING
    data = path.read_bytes()
    same = path.name == entry.name and len(data) == entry.size and _sha256(data) == entry.sha256
    return FileState.OK if same else FileState.CHANGED


def firmware_states(firmware: Firmware, archive_dir: Path) -> dict[str, FileState]:
    """entry_state() of every entry (enabled or not), keyed by entry id."""
    return {e.id: entry_state(e, archive_dir) for e in firmware.entries}


def read_entry(entry: Entry, archive_dir: Path) -> bytes:
    """The bytes to write for an entry. Raises FileNotFoundError if missing."""
    scheme, _, rest = entry.file.partition(":")
    if scheme == "blank":
        return b"\xff" * entry.size
    if scheme == "archive":
        return (archive_dir / f"{rest}.bin").read_bytes()
    if scheme == "path":
        return Path(rest).read_bytes()
    raise FileNotFoundError(entry.file)


def confirm_entry(entry: Entry, archive_dir: Path) -> None:
    """Accept the current content of a CHANGED "path:" entry: record its new sha256 and
    size."""
    scheme, _, rest = entry.file.partition(":")
    if scheme != "path":
        return
    data = Path(rest).read_bytes()
    entry.sha256 = _sha256(data)
    entry.size = len(data)


def relocate(entry: Entry, path: Path) -> None:
    """Point a "path:" entry at another file: file, name, sha256 and size follow the new
    file. Address, enabled state and note are kept."""
    data = path.read_bytes()
    entry.file = f"path:{path.resolve()}"
    entry.name = path.name
    entry.sha256 = _sha256(data)
    entry.size = len(data)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
