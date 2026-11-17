"""Firmware files: data/firmwares/<id>.toml, one per firmware, flat."""

import copy
import datetime
from collections.abc import Sequence
from pathlib import Path, PurePath

from embr.core.firmware.model import FIRMWARE_SCHEMA_VERSION, Firmware, Source
from embr.core.firmware.serialize import firmware_from_doc, firmware_to_doc
from embr.core.storage.documents import (
    Document,
    DocumentStore,
    LoadedDocument,
    Migration,
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


class FirmwareStore(DocumentStore[Firmware]):
    """data/firmwares/<id>.toml, one file per firmware (see DocumentStore for caching)."""

    def __init__(self, directory: Path, backup_root: Path) -> None:
        super().__init__(
            directory, backup_root, current=FIRMWARE_SCHEMA_VERSION, migrations=_MIGRATIONS
        )

    def all(self) -> list[Firmware]:
        """Every readable firmware, migrated to the current schema (newer ones read-only).
        Files that cannot be read are skipped and listed by damaged()."""
        return self._all()

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
        self._write(firmware.id, firmware_to_doc(firmware))

    def rename(self, firmware_id: str, name: str) -> Firmware:
        """Rename, made unique within the firmware's scope."""
        firmware = self.get(firmware_id)
        firmware.name = unique_name(name, self._names(firmware.project_id, exclude=firmware_id))
        self.save(firmware)
        return firmware

    def move(self, firmware_id: str, project_id: str | None, device: str = "") -> Firmware:
        """Set project and group (None project = standalone, which also clears the group).
        Only these fields change, plus a name suffix if the name is taken in the new scope."""
        return self.move_many([firmware_id], project_id, device)[0]

    def move_many(
        self, firmware_ids: Sequence[str], project_id: str | None, device: str = ""
    ) -> list[Firmware]:
        """move() for each firmware in turn (each name made unique against the scope as the
        earlier ones left it), reading the firmware files once. KeyError for an unknown
        id; ReadOnlyError for a firmware of a newer Embr (the ones before it are moved)."""
        everything = {f.id: f for f in self.all()}
        moved: list[Firmware] = []
        for firmware_id in firmware_ids:
            if firmware_id not in everything:
                raise KeyError(firmware_id)
            firmware = everything[firmware_id]
            taken = [
                f.name
                for f in everything.values()
                if f.project_id == project_id and f.id != firmware_id
            ]
            firmware.project_id = project_id
            firmware.device = device if project_id is not None else ""
            firmware.name = unique_name(firmware.name, taken)
            self.save(firmware)
            moved.append(firmware)
        return moved

    def delete(self, firmware_id: str) -> None:
        """Delete the firmware file. Archive files and flash records are not touched."""
        self._delete_file(firmware_id)

    def _build(self, loaded: LoadedDocument) -> Firmware:
        """firmware_from_doc() builds new objects for everything but last_resolved, a table
        of the document itself: that one is copied, so the kept document never changes."""
        firmware = firmware_from_doc(loaded.data)
        firmware.last_resolved = copy.deepcopy(firmware.last_resolved)
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
