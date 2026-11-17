"""Entry file consistency: name, path and hash."""

import hashlib
import os
from enum import StrEnum
from pathlib import Path

from embr.core.firmware.filecache import FileCache
from embr.core.firmware.model import Entry

PATH = "path:"
"""Entry.file of a file on disk: "path:<absolute path>"."""
ARCHIVE = "archive:"
"""Entry.file of a copy in the archive: "archive:<sha256>" (stored as <sha256>.bin)."""
BLANK = "blank:"
"""Entry.file of `size` bytes of 0xFF, kept in no file."""


def path_ref(path: Path | str) -> str:
    return f"{PATH}{path}"


def archive_ref(sha256: str) -> str:
    return f"{ARCHIVE}{sha256}"


def local_file(entry: Entry) -> Path | None:
    """The file of a "path:" entry; None for archived, blank and unknown ones."""
    return Path(entry.file.removeprefix(PATH)) if entry.file.startswith(PATH) else None


def archived_sha(entry: Entry) -> str | None:
    """The sha256 an "archive:" entry refers to; None for other entries."""
    return entry.file.removeprefix(ARCHIVE) if entry.file.startswith(ARCHIVE) else None


def is_blank(entry: Entry) -> bool:
    return entry.file.startswith(BLANK)


def path_key(path: Path) -> str:
    """Key under which two paths name the same file: absolute, `..` removed, and on Windows
    case and separators folded."""
    return os.path.normcase(str(path.resolve()))


class FileState(StrEnum):
    OK = "ok"
    CHANGED = "changed"
    """File present but its content (sha256 or size) differs: needs confirmation."""
    MISSING = "missing"
    """No file at the recorded path or in the archive."""


def entry_file(entry: Entry, archive_dir: Path) -> Path | None:
    """The file holding the entry's bytes: the path of a "path:" entry,
    archive_dir/<sha256>.bin for an "archive:" one; None for "blank:" (and unknown schemes)."""
    if (sha := archived_sha(entry)) is not None:
        return archive_dir / f"{sha}.bin"
    return local_file(entry)


def entry_state(entry: Entry, archive_dir: Path, cache: FileCache | None = None) -> FileState:
    """ "path:" entries: the file must exist, its name must equal entry.name, and its sha256
    and size must match. "archive:" entries: archive_dir/<sha256>.bin must exist with that
    hash. "blank:" entries are always OK. A file that cannot be read is MISSING. With
    `cache`, a file is hashed again only after its size or modification time changed."""
    if is_blank(entry):
        return FileState.OK
    path = entry_file(entry, archive_dir)
    if path is None:
        return FileState.MISSING
    try:
        if cache is not None:
            sha, size = cache.digest(path)
        else:
            data = path.read_bytes()
            sha, size = _sha256(data), len(data)
    except OSError:
        return FileState.MISSING
    if (archived := archived_sha(entry)) is not None:
        return FileState.OK if sha == archived else FileState.CHANGED
    same = path.name == entry.name and size == entry.size and sha == entry.sha256
    return FileState.OK if same else FileState.CHANGED


def read_entry(entry: Entry, archive_dir: Path, cache: FileCache | None = None) -> bytes:
    """The bytes to write for an entry. Raises FileNotFoundError if missing. With `cache`,
    small files (partition tables) come from it while they are unchanged."""
    if is_blank(entry):
        return b"\xff" * entry.size
    path = entry_file(entry, archive_dir)
    if path is None:
        raise FileNotFoundError(entry.file)
    return cache.read(path) if cache is not None else path.read_bytes()


def confirm_entry(entry: Entry, archive_dir: Path) -> None:
    """Accept the current content of a CHANGED "path:" entry: record its new sha256 and
    size."""
    path = local_file(entry)
    if path is None:
        return
    data = path.read_bytes()
    entry.sha256 = _sha256(data)
    entry.size = len(data)


def relocate(entry: Entry, path: Path) -> None:
    """Point a "path:" entry at another file: file, name, sha256 and size follow the new
    file. Address, enabled state and note are kept."""
    data = path.read_bytes()
    entry.file = path_ref(path.resolve())
    entry.name = path.name
    entry.sha256 = _sha256(data)
    entry.size = len(data)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
