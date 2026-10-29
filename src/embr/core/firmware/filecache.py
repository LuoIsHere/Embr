"""File fingerprints and a hash cache for re-checking firmware sources cheaply
(design doc §3.4 "检测时机"): a stat per file decides whether anything needs reading,
and a file is hashed again only when its size or modification time changed."""

import hashlib
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileStamp:
    path: str
    """Absolute path as given."""
    size: int | None
    """None when the file does not exist (or is not a regular file)."""
    mtime_ns: int | None


@dataclass(frozen=True)
class Fingerprint:
    stamps: tuple[FileStamp, ...]
    """One per path, in the order given to fingerprint()."""

    @property
    def newest_mtime_ns(self) -> int | None:
        """Latest modification time among existing files, None when none exists."""
        times = [s.mtime_ns for s in self.stamps if s.mtime_ns is not None]
        return max(times, default=None)


def fingerprint(paths: Iterable[Path]) -> Fingerprint:
    """Stat every path (never reads content). Missing files give a stamp with size and
    mtime None; duplicate paths are kept once, first occurrence wins."""
    seen: set[str] = set()
    stamps: list[FileStamp] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        stamps.append(_stamp(path))
    return Fingerprint(tuple(stamps))


@dataclass
class _Cached:
    size: int
    mtime_ns: int
    sha256: str | None = None
    content: bytes | None = None


class FileCache:
    """sha256, size and content of files, cached by (path, size, mtime_ns). A cached value
    is used only while the file's stat still matches; otherwise the file is read again.
    Content is kept only for files up to `keep_bytes` (image headers and partition tables
    are read from it); larger files are re-read when needed."""

    def __init__(self, keep_bytes: int = 64 * 1024) -> None:
        self._keep = keep_bytes
        self._files: dict[str, _Cached] = {}
        self._hashed = 0

    def sha256(self, path: Path) -> str:
        """Hex sha256. Raises FileNotFoundError (OSError) when the file is missing."""
        cached = self._current(path)
        if cached.sha256 is None:
            self.read(path)
            cached = self._current(path)
        assert cached.sha256 is not None
        return cached.sha256

    def size(self, path: Path) -> int:
        """Raises FileNotFoundError when missing."""
        return self._current(path).size

    def read(self, path: Path) -> bytes:
        """Whole content (also fills the sha256 cache). Raises OSError."""
        cached = self._current(path)
        if cached.content is not None:
            return cached.content
        data = path.read_bytes()
        self._hashed += 1
        cached.sha256 = hashlib.sha256(data).hexdigest()
        if len(data) <= self._keep:
            cached.content = data
        return data

    def head(self, path: Path, size: int) -> bytes:
        """The first `size` bytes (or fewer for a shorter file). Raises OSError."""
        cached = self._current(path)
        if cached.content is not None:
            return cached.content[:size]
        with path.open("rb") as f:
            return f.read(size)

    def forget(self, path: Path) -> None:
        """Drop everything cached for a path."""
        self._files.pop(str(path), None)

    @property
    def hashed(self) -> int:
        """Number of times a file was actually read and hashed (tests use it to check
        that an unchanged file is not read again)."""
        return self._hashed

    def _current(self, path: Path) -> _Cached:
        """Cache entry matching the file's current stat (a fresh one after a change)."""
        stamp = _stamp(path)
        if stamp.size is None or stamp.mtime_ns is None:
            self._files.pop(str(path), None)
            raise FileNotFoundError(str(path))
        cached = self._files.get(stamp.path)
        if cached is None or (cached.size, cached.mtime_ns) != (stamp.size, stamp.mtime_ns):
            cached = _Cached(stamp.size, stamp.mtime_ns)
            self._files[stamp.path] = cached
        return cached


def _stamp(path: Path) -> FileStamp:
    try:
        st = path.stat()
    except OSError:
        return FileStamp(str(path), None, None)
    if not stat.S_ISREG(st.st_mode):
        return FileStamp(str(path), None, None)
    return FileStamp(str(path), st.st_size, st.st_mtime_ns)
