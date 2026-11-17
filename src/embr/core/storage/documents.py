"""TOML documents with a schema_version: projects and firmware."""

import datetime
import shutil
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import tomli_w

from embr.core.storage.atomic import write_whole
from embr.i18n import N_

Document = dict[str, object]
Migration = Callable[[Document], Document]
"""Upgrades a document by one version; migrations[n] turns version n into n + 1."""


@dataclass(frozen=True)
class LoadedDocument:
    data: Document
    """Contents at the current version (or as found, when read_only)."""
    read_only: bool
    """True when the file has a higher schema_version than this Embr supports."""
    migrated_from: int | None
    """The file's version before migration, or None if no migration ran."""


@dataclass(frozen=True)
class DamagedDocument:
    """A project or firmware file that cannot be read: skipped, never
    changed."""

    path: Path
    reason: str


class DamagedDocumentError(KeyError):
    """get() of a document that cannot be read; a KeyError, so callers treat it as missing."""

    def __init__(self, damaged: DamagedDocument) -> None:
        super().__init__(damaged.path.stem)
        self.damaged = damaged


def damage_reason(exc: Exception) -> str:
    """One line for the user: what is wrong with the file."""
    if isinstance(exc, UnicodeDecodeError):
        return N_("not UTF-8 text")
    if isinstance(exc, tomllib.TOMLDecodeError):
        return N_(f"not valid TOML ({exc})")
    if isinstance(exc, ValueError) and "schema_version" in str(exc):
        return N_("empty, or not an Embr project or firmware file")
    return str(exc) or type(exc).__name__


def keep_damaged_copy(path: Path, backup_root: Path) -> Path | None:
    """Copy a damaged file to backup_root/<timestamp>/damaged/ unless an identical copy is
    already there; the original stays. Returns the copy (new or existing); None when the
    file cannot be read or copied."""
    try:
        content = path.read_bytes()
        for old in sorted(backup_root.glob(f"*/damaged/{path.name}")):
            if old.read_bytes() == content:
                return old
        target = backup_folder(backup_root) / "damaged" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    except OSError:
        return None
    return target


def load_document(
    path: Path, *, current: int, migrations: Mapping[int, Migration], backup_root: Path
) -> LoadedDocument:
    """Read a TOML file. A lower schema_version is migrated step by step in memory; only
    when that succeeds is the original copied to backup_root/<timestamp>/ and the migrated
    document written back. A higher version is returned read-only and the file is not
    modified. ValueError (nothing backed up or written) when schema_version is missing,
    invalid or below 1, or a migration step is missing."""
    data: Document = tomllib.loads(path.read_text(encoding="utf-8"))
    version = format_version(data, "schema_version")
    if version is None:
        raise ValueError(f"{path}: missing or invalid schema_version")
    if version > current:
        return LoadedDocument(data, read_only=True, migrated_from=None)
    if version == current:
        return LoadedDocument(data, read_only=False, migrated_from=None)
    upgraded = upgrade_document(data, current=current, migrations=migrations)
    shutil.copy2(path, backup_folder(backup_root) / path.name)
    save_document(path, upgraded, current=current)
    return LoadedDocument(upgraded, read_only=False, migrated_from=version)


class NewerDocumentError(ValueError):
    """A document has a higher schema_version than this Embr supports."""


class MissingMigrationError(ValueError):
    """No migration leads on from version `step`."""

    def __init__(self, key: str, step: int) -> None:
        super().__init__(f"no migration from {key} {step}")
        self.step = step


def format_version(data: Document, key: str) -> int | None:
    """The version number `data` states under `key`; None when it is missing, not an
    integer or below 1."""
    version = data.get(key)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None
    return version


def migrate(
    data: Document, *, key: str, version: int, current: int, migrations: Mapping[int, Migration]
) -> Document:
    """`data` at `version` (not above `current`) migrated step by step and stamped with
    `current` under `key`. MissingMigrationError when a step is missing."""
    for step in range(version, current):
        if step not in migrations:
            raise MissingMigrationError(key, step)
        data = migrations[step](data)
    return {**data, key: current}


def upgrade_document(
    data: Document, *, current: int, migrations: Mapping[int, Migration]
) -> Document:
    """In memory: a lower schema_version is migrated step by step and set to `current`.
    Raises NewerDocumentError for a higher version, ValueError when schema_version is
    missing, invalid or below 1 or a migration step is missing."""
    version = format_version(data, "schema_version")
    if version is None:
        raise ValueError("missing or invalid schema_version")
    if version > current:
        raise NewerDocumentError(f"schema_version {version} is newer than {current}")
    return migrate(
        data, key="schema_version", version=version, current=current, migrations=migrations
    )


def save_document(path: Path, data: Document, *, current: int) -> None:
    """Write `data` as TOML with schema_version = current. The write is atomic (temporary
    file in the same folder, then replace), so a crash never leaves a half-written file."""
    doc: Document = {"schema_version": current}
    doc.update((k, v) for k, v in data.items() if k != "schema_version")
    write_whole(path, tomli_w.dumps(doc).encode("utf-8"))


_Stamp = tuple[int, int, int]
"""st_mtime_ns, st_size, st_ino of a document file."""


class DocumentStore[T](ABC):
    """<id>.toml files in one folder, each a document with a schema_version (projects,
    firmware). Parsed documents are kept by the file's stat (modification time, size, file
    id): a file is parsed again only after it changed on disk (a save replaces it). Every
    read builds new objects from them with _build(), so changing one never touches what is
    kept."""

    def __init__(
        self,
        directory: Path,
        backup_root: Path,
        *,
        current: int,
        migrations: Mapping[int, Migration],
    ) -> None:
        self.directory = directory
        self.backup_root = backup_root
        self._current = current
        self._migrations = migrations
        self._damaged: list[DamagedDocument] = []
        self._parsed: dict[Path, tuple[_Stamp, LoadedDocument]] = {}

    @abstractmethod
    def _build(self, loaded: LoadedDocument) -> T:
        """A new object from a document. ValueError, KeyError or TypeError for one that
        does not convert: its file counts as damaged."""

    def damaged(self) -> list[DamagedDocument]:
        """Files the last all() could not read."""
        return list(self._damaged)

    def get(self, item_id: str) -> T:
        """Raises KeyError if missing (DamagedDocumentError, a KeyError, if unreadable)."""
        path = self._path(item_id)
        if not path.is_file():
            raise KeyError(item_id)
        return self._load(path)

    def _all(self) -> list[T]:
        """Every readable document in file name order, migrated to the current schema
        (newer ones read-only). Files that cannot be read are skipped and listed by
        damaged()."""
        found: list[T] = []
        damaged: list[DamagedDocument] = []
        paths = sorted(self.directory.glob("*.toml")) if self.directory.is_dir() else []
        for path in paths:
            try:
                found.append(self._load(path))
            except DamagedDocumentError as exc:
                damaged.append(exc.damaged)
        self._damaged = damaged
        kept = set(paths)
        self._parsed = {p: v for p, v in self._parsed.items() if p in kept}
        return found

    def _write(self, item_id: str, data: Document) -> None:
        save_document(self._path(item_id), data, current=self._current)

    def _delete_file(self, item_id: str) -> None:
        """Remove the document. KeyError when there is none."""
        path = self._path(item_id)
        if not path.is_file():
            raise KeyError(item_id)
        path.unlink()
        self._parsed.pop(path, None)

    def _path(self, item_id: str) -> Path:
        return self.directory / f"{item_id}.toml"

    def _load(self, path: Path) -> T:
        """A new object from the file's document, parsed again when its stat changed.
        DamagedDocumentError when the file cannot be read or parsed."""
        try:
            info = path.stat()
        except OSError as exc:
            raise DamagedDocumentError(DamagedDocument(path, damage_reason(exc))) from exc
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        kept = self._parsed.get(path)
        if kept is None or kept[0] != stamp:
            # stat taken before the read: a change during the read gives a later stat, so
            # the next call parses again
            kept = (stamp, self._parse(path))
            self._parsed[path] = kept
        return self._build(kept[1])

    def _parse(self, path: Path) -> LoadedDocument:
        """DamagedDocumentError when the file cannot be read or parsed."""
        try:
            loaded = load_document(
                path,
                current=self._current,
                migrations=self._migrations,
                backup_root=self.backup_root,
            )
            self._build(loaded)  # a document that does not convert is damaged
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DamagedDocumentError(DamagedDocument(path, damage_reason(exc))) from exc
        return loaded


def backup_folder(backup_root: Path) -> Path:
    """A new backup/<timestamp>/ folder (shared by everything backed up in that second)."""
    folder = backup_root / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def doc_str(doc: Document, key: str, default: str = "") -> str:
    value = doc.get(key, default)
    return value if isinstance(value, str) else default


def doc_opt_str(doc: Document, key: str) -> str | None:
    value = doc.get(key)
    return value if isinstance(value, str) else None


def doc_bool(doc: Document, key: str, default: bool = False) -> bool:
    value = doc.get(key, default)
    return value if isinstance(value, bool) else default


def doc_int(doc: Document, key: str, default: int = 0) -> int:
    value = doc.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def doc_strs(doc: Document, key: str) -> list[str]:
    value = doc.get(key, [])
    return (
        [v for v in cast(list[object], value) if isinstance(v, str)]
        if isinstance(value, list)
        else []
    )


def doc_table(doc: Document, key: str) -> Document | None:
    value = doc.get(key)
    return cast(Document, value) if isinstance(value, dict) else None


def doc_tables(doc: Document, key: str) -> list[Document]:
    value = doc.get(key, [])
    if not isinstance(value, list):
        return []
    return [cast(Document, v) for v in cast(list[object], value) if isinstance(v, dict)]
