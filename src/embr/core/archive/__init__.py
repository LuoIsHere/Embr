"""Content-addressed archive (design doc §3.4 "归档", §7.6): data/archive/<sha256>.bin plus
an index in embr.sqlite (archive_files: one row per content; archive_names: one row per
time something was archived under a name). Files are copied, never moved; the source is
never modified. Nothing is deleted except by delete() ("Clean archive").
"""

import datetime
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArchiveFile:
    sha256: str
    size: int
    first_archived: str
    """ISO 8601 local time with seconds."""


@dataclass(frozen=True)
class ArchiveName:
    id: str
    sha256: str
    original_name: str
    original_path: str
    note: str
    archived_at: str
    source_firmware: str
    """Name of the firmware it was archived from ("" when unknown)."""


class Archive:
    def __init__(
        self,
        directory: Path,
        connection: sqlite3.Connection,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        self.directory = directory
        self._db = connection
        self._clock = clock

    def path(self, sha256: str) -> Path:
        """directory/<sha256>.bin (whether or not it exists)."""
        raise NotImplementedError

    def add(
        self,
        source: Path | bytes,
        *,
        original_name: str,
        original_path: str = "",
        note: str = "",
        source_firmware: str = "",
    ) -> ArchiveName:
        """Copy the content into the archive (written to a temporary name, then renamed, so
        a crash never leaves a partial <sha256>.bin) unless that content is already stored,
        and add a name record. Returns the name record. Raises OSError when the source
        cannot be read."""
        raise NotImplementedError

    def has(self, sha256: str) -> bool:
        """The file exists in the archive folder."""
        raise NotImplementedError

    def files(self) -> list[ArchiveFile]:
        """Every indexed file, oldest first."""
        raise NotImplementedError

    def names(self, sha256: str) -> list[ArchiveName]:
        """Name records of a content, oldest first."""
        raise NotImplementedError

    def unreferenced(self, referenced: Iterable[str]) -> list[ArchiveFile]:
        """Indexed files whose sha256 is not in `referenced`."""
        raise NotImplementedError

    def delete(self, sha256s: Iterable[str]) -> None:
        """Remove the files and their index rows (file and names). Unknown hashes are
        ignored."""
        raise NotImplementedError
