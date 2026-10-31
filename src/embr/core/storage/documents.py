"""TOML documents with a schema_version: projects and firmware (design doc §3.5)."""

import datetime
import os
import shutil
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import tomli_w

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
    """A project or firmware file that cannot be read (design doc §12.8): skipped, never
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
    """Read a TOML file. A lower schema_version is migrated step by step: the original file
    is copied to backup_root/<timestamp>/ first, then the migrated document is written back.
    A higher version is returned read-only and the file is not modified."""
    data: Document = tomllib.loads(path.read_text(encoding="utf-8"))
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"{path}: missing or invalid schema_version")
    if version > current:
        return LoadedDocument(data, read_only=True, migrated_from=None)
    if version == current:
        return LoadedDocument(data, read_only=False, migrated_from=None)
    shutil.copy2(path, backup_folder(backup_root) / path.name)
    for step in range(version, current):
        data = migrations[step](data)
    save_document(path, data, current=current)
    data["schema_version"] = current
    return LoadedDocument(data, read_only=False, migrated_from=version)


class NewerDocumentError(ValueError):
    """A document has a higher schema_version than this Embr supports."""


def upgrade_document(
    data: Document, *, current: int, migrations: Mapping[int, Migration]
) -> Document:
    """In memory: a lower schema_version is migrated step by step and set to `current`.
    Raises NewerDocumentError for a higher version, ValueError when schema_version is
    missing, invalid or below 1."""
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("missing or invalid schema_version")
    if version > current:
        raise NewerDocumentError(f"schema_version {version} is newer than {current}")
    for step in range(version, current):
        if step not in migrations:
            raise ValueError(f"no migration from schema_version {step}")
        data = migrations[step](data)
    return {**data, "schema_version": current}


def save_document(path: Path, data: Document, *, current: int) -> None:
    """Write `data` as TOML with schema_version = current. The write is atomic (temporary
    file in the same folder, then replace), so a crash never leaves a half-written file."""
    doc: Document = {"schema_version": current}
    doc.update((k, v) for k, v in data.items() if k != "schema_version")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(tomli_w.dumps(doc), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


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
