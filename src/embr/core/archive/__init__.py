"""Content-addressed archive (design doc §3.4 "归档", §7.6): data/archive/<sha256>.bin plus
an index in embr.sqlite (archive_files: one row per content; archive_names: one row per
time something was archived under a name). Files are copied, never moved; the source is
never modified. Nothing is deleted except by delete() ("Clean archive").
"""

import contextlib
import datetime
import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from embr.core.storage.ids import new_id


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
        return self.directory / f"{sha256}.bin"

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
        data = source if isinstance(source, bytes) else source.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        stamp = self._clock().isoformat(timespec="seconds")
        target = self.path(sha)
        if not target.is_file():
            self.directory.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(f".{os.getpid()}.part")
            partial.write_bytes(data)
            os.replace(partial, target)
        name = ArchiveName(
            id=new_id(),
            sha256=sha,
            original_name=original_name,
            original_path=original_path,
            note=note,
            archived_at=stamp,
            source_firmware=source_firmware,
        )
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO archive_files (sha256, size, first_archived)"
                " VALUES (?, ?, ?)",
                (sha, len(data), stamp),
            )
            self._db.execute(
                "INSERT INTO archive_names (id, sha256, original_name, original_path, note,"
                " archived_at, source_firmware) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name.id,
                    sha,
                    original_name,
                    original_path,
                    note,
                    stamp,
                    source_firmware,
                ),
            )
        return name

    def has(self, sha256: str) -> bool:
        """The file exists in the archive folder."""
        return self.path(sha256).is_file()

    def files(self) -> list[ArchiveFile]:
        """Every indexed file, oldest first."""
        rows = self._db.execute(
            "SELECT sha256, size, first_archived FROM archive_files ORDER BY first_archived, rowid"
        )
        return [ArchiveFile(r[0], int(r[1]), r[2]) for r in rows]

    def names(self, sha256: str) -> list[ArchiveName]:
        """Name records of a content, oldest first."""
        rows = self._db.execute(
            "SELECT id, sha256, original_name, original_path, note, archived_at,"
            " source_firmware FROM archive_names WHERE sha256 = ? ORDER BY archived_at, rowid",
            (sha256,),
        )
        return [ArchiveName(*(str(v or "") for v in r)) for r in rows]

    def unreferenced(self, referenced: Iterable[str]) -> list[ArchiveFile]:
        """Indexed files whose sha256 is not in `referenced`."""
        keep = set(referenced)
        return [f for f in self.files() if f.sha256 not in keep]

    def delete(self, sha256s: Iterable[str]) -> None:
        """Remove the files and their index rows (file and names). Unknown hashes are
        ignored."""
        for sha in set(sha256s):
            with self._db:
                self._db.execute("DELETE FROM archive_names WHERE sha256 = ?", (sha,))
                self._db.execute("DELETE FROM archive_files WHERE sha256 = ?", (sha,))
            with contextlib.suppress(FileNotFoundError):
                self.path(sha).unlink()
