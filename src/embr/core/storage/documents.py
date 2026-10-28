"""TOML documents with a schema_version: projects and firmware (design doc §3.5)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

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


def load_document(
    path: Path, *, current: int, migrations: Mapping[int, Migration], backup_root: Path
) -> LoadedDocument:
    """Read a TOML file. A lower schema_version is migrated step by step: the original file
    is copied to backup_root/<timestamp>/ first, then the migrated document is written back.
    A higher version is returned read-only and the file is not modified."""
    raise NotImplementedError


def save_document(path: Path, data: Document, *, current: int) -> None:
    """Write `data` as TOML with schema_version = current. The write is atomic (temporary
    file in the same folder, then replace), so a crash never leaves a half-written file."""
    raise NotImplementedError
