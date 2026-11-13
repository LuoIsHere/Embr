"""Entry file consistency: name, path and hash."""

import hashlib
from enum import StrEnum
from pathlib import Path

from embr.core.firmware.filecache import FileCache
from embr.core.firmware.model import Entry, Firmware


class FileState(StrEnum):
    OK = "ok"
    CHANGED = "changed"
    """File present but its content (sha256 or size) differs: needs confirmation."""
    MISSING = "missing"
    """No file at the recorded path or in the archive."""


def entry_file(entry: Entry, archive_dir: Path) -> Path | None:
    """The file holding the entry's bytes: the path of a "path:" entry,
    archive_dir/<sha256>.bin for an "archive:" one; None for "blank:" (and unknown schemes)."""
    scheme, _, rest = entry.file.partition(":")
    if scheme == "path":
        return Path(rest)
    if scheme == "archive":
        return archive_dir / f"{rest}.bin"
    return None


def entry_state(entry: Entry, archive_dir: Path, cache: FileCache | None = None) -> FileState:
    """ "path:" entries: the file must exist, its name must equal entry.name, and its sha256
    and size must match. "archive:" entries: archive_dir/<sha256>.bin must exist with that
    hash. "blank:" entries are always OK. With `cache`, a file is hashed again only after
    its size or modification time changed."""
    scheme, _, rest = entry.file.partition(":")
    if scheme == "blank":
        return FileState.OK
    path = entry_file(entry, archive_dir)
    if path is None or not path.is_file():
        return FileState.MISSING
    if cache is not None:
        sha, size = cache.digest(path)
    else:
        data = path.read_bytes()
        sha, size = _sha256(data), len(data)
    if scheme == "archive":
        return FileState.OK if sha == rest else FileState.CHANGED
    same = path.name == entry.name and size == entry.size and sha == entry.sha256
    return FileState.OK if same else FileState.CHANGED


def firmware_states(firmware: Firmware, archive_dir: Path) -> dict[str, FileState]:
    """entry_state() of every entry (enabled or not), keyed by entry id."""
    return {e.id: entry_state(e, archive_dir) for e in firmware.entries}


def read_entry(entry: Entry, archive_dir: Path, cache: FileCache | None = None) -> bytes:
    """The bytes to write for an entry. Raises FileNotFoundError if missing. With `cache`,
    small files (partition tables) come from it while they are unchanged."""
    if entry.file.startswith("blank:"):
        return b"\xff" * entry.size
    path = entry_file(entry, archive_dir)
    if path is None:
        raise FileNotFoundError(entry.file)
    return cache.read(path) if cache is not None else path.read_bytes()


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
