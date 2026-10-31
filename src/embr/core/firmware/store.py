"""Firmware files: data/firmwares/<id>.toml, one per firmware, flat (design doc §7.6)."""

import datetime
from pathlib import Path, PurePath

from embr.core.firmware.model import FIRMWARE_SCHEMA_VERSION, Firmware, Source
from embr.core.firmware.serialize import firmware_from_doc, firmware_to_doc
from embr.core.storage.documents import (
    Document,
    Migration,
    load_document,
    save_document,
    upgrade_document,
)
from embr.core.storage.ids import new_id, unique_name


class ReadOnlyError(Exception):
    """The firmware was written by a newer Embr and cannot be saved."""


_MIGRATIONS: dict[int, Migration] = {}
"""migrations[n] upgrades a firmware document from version n to n + 1."""


def upgrade_firmware_doc(doc: Document) -> Document:
    """A firmware document upgraded to FIRMWARE_SCHEMA_VERSION in memory (see
    storage.documents.upgrade_document)."""
    return upgrade_document(doc, current=FIRMWARE_SCHEMA_VERSION, migrations=_MIGRATIONS)


def now() -> str:
    """Timestamp stored in project and firmware files."""
    return datetime.datetime.now().isoformat(timespec="seconds")


class FirmwareStore:
    def __init__(self, directory: Path, backup_root: Path) -> None:
        self.directory = directory
        self.backup_root = backup_root

    def all(self) -> list[Firmware]:
        """Every firmware, migrated to the current schema (newer ones read-only)."""
        if not self.directory.is_dir():
            return []
        return [self._load(p) for p in sorted(self.directory.glob("*.toml"))]

    def get(self, firmware_id: str) -> Firmware:
        """Raises KeyError if missing."""
        path = self._path(firmware_id)
        if not path.is_file():
            raise KeyError(firmware_id)
        return self._load(path)

    def in_scope(self, project_id: str | None) -> list[Firmware]:
        """Firmware of a project, or all standalone firmware for None."""
        return [f for f in self.all() if f.project_id == project_id]

    def create(
        self,
        name: str,
        *,
        source: Source = Source.MANUAL,
        project_id: str | None = None,
        device: str = "",
        before: str = "default-reset",
        after: str = "hard-reset",
        erase_all: bool = False,
    ) -> Firmware:
        """New firmware, saved. The name is made unique within its scope (the project, or
        all standalone firmware) with " (2)", " (3)" suffixes."""
        stamp = now()
        firmware = Firmware(
            id=new_id(),
            name=unique_name(name, self._names(project_id)),
            project_id=project_id,
            device=device if project_id is not None else "",
            source=source,
            before=before,
            after=after,
            erase_all=erase_all,
            created_at=stamp,
            updated_at=stamp,
        )
        self.save(firmware)
        return firmware

    def save(self, firmware: Firmware) -> None:
        """Write the TOML file and update updated_at. ReadOnlyError if read_only."""
        if firmware.read_only:
            raise ReadOnlyError(f"{firmware.name} was written by a newer Embr")
        firmware.updated_at = now()
        if not firmware.created_at:
            firmware.created_at = firmware.updated_at
        save_document(
            self._path(firmware.id), firmware_to_doc(firmware), current=FIRMWARE_SCHEMA_VERSION
        )

    def rename(self, firmware_id: str, name: str) -> Firmware:
        """Rename, made unique within the firmware's scope."""
        firmware = self.get(firmware_id)
        firmware.name = unique_name(name, self._names(firmware.project_id, exclude=firmware_id))
        self.save(firmware)
        return firmware

    def move(self, firmware_id: str, project_id: str | None, device: str = "") -> Firmware:
        """Set project and group (None project = standalone, which also clears the group).
        Only these fields change, plus a name suffix if the name is taken in the new scope."""
        firmware = self.get(firmware_id)
        firmware.project_id = project_id
        firmware.device = device if project_id is not None else ""
        firmware.name = unique_name(firmware.name, self._names(project_id, exclude=firmware_id))
        self.save(firmware)
        return firmware

    def delete(self, firmware_id: str) -> None:
        """Delete the firmware file. Archive files and flash records are not touched."""
        path = self._path(firmware_id)
        if not path.is_file():
            raise KeyError(firmware_id)
        path.unlink()

    def _path(self, firmware_id: str) -> Path:
        return self.directory / f"{firmware_id}.toml"

    def _load(self, path: Path) -> Firmware:
        loaded = load_document(
            path,
            current=FIRMWARE_SCHEMA_VERSION,
            migrations=_MIGRATIONS,
            backup_root=self.backup_root,
        )
        firmware = firmware_from_doc(loaded.data)
        firmware.read_only = loaded.read_only
        return firmware

    def _names(self, project_id: str | None, exclude: str | None = None) -> list[str]:
        return [f.name for f in self.in_scope(project_id) if f.id != exclude]


def default_name_from_files(file_names: list[str]) -> str:
    """Manual firmware: the first file name without its extension."""
    return PurePath(file_names[0]).stem if file_names else "untitled"


def default_name_archived(source_name: str, date: datetime.date) -> str:
    """'<source name> (archived YYYY-MM-DD)'."""
    return f"{source_name} (archived {date.isoformat()})"
