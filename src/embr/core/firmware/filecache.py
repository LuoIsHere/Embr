"""File fingerprints and a hash cache for re-checking firmware sources cheaply
(design doc §3.4 "检测时机"): a stat per file decides whether anything needs reading,
and a file is hashed again only when its size or modification time changed."""

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
        raise NotImplementedError


def fingerprint(paths: Iterable[Path]) -> Fingerprint:
    """Stat every path (never reads content). Missing files give a stamp with size and
    mtime None; duplicate paths are kept once, first occurrence wins."""
    raise NotImplementedError


class FileCache:
    """sha256, size and content of files, cached by (path, size, mtime_ns). A cached value
    is used only while the file's stat still matches; otherwise the file is read again.
    Content is kept only for files up to `keep_bytes` (image headers and partition tables
    are read from it); larger files are re-read when needed."""

    def __init__(self, keep_bytes: int = 64 * 1024) -> None:
        raise NotImplementedError

    def sha256(self, path: Path) -> str:
        """Hex sha256. Raises FileNotFoundError (OSError) when the file is missing."""
        raise NotImplementedError

    def size(self, path: Path) -> int:
        """Raises FileNotFoundError when missing."""
        raise NotImplementedError

    def read(self, path: Path) -> bytes:
        """Whole content (also fills the sha256 cache). Raises OSError."""
        raise NotImplementedError

    def head(self, path: Path, size: int) -> bytes:
        """The first `size` bytes (or fewer for a shorter file). Raises OSError."""
        raise NotImplementedError

    def forget(self, path: Path) -> None:
        """Drop everything cached for a path."""
        raise NotImplementedError

    @property
    def hashed(self) -> int:
        """Number of times a file was actually read and hashed (tests use it to check
        that an unchanged file is not read again)."""
        raise NotImplementedError
