"""Firmware files: data/firmwares/<id>.toml, one per firmware, flat (design doc §7.6)."""

import datetime
from pathlib import Path

from embr.core.firmware.model import Firmware, Source


class ReadOnlyError(Exception):
    """The firmware was written by a newer Embr and cannot be saved."""


class FirmwareStore:
    def __init__(self, directory: Path, backup_root: Path) -> None:
        self.directory = directory
        self.backup_root = backup_root

    def all(self) -> list[Firmware]:
        """Every firmware, migrated to the current schema (newer ones read-only)."""
        raise NotImplementedError

    def get(self, firmware_id: str) -> Firmware:
        """Raises KeyError if missing."""
        raise NotImplementedError

    def in_scope(self, project_id: str | None) -> list[Firmware]:
        """Firmware of a project, or all standalone firmware for None."""
        raise NotImplementedError

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
        raise NotImplementedError

    def save(self, firmware: Firmware) -> None:
        """Write the TOML file and update updated_at. ReadOnlyError if read_only."""
        raise NotImplementedError

    def rename(self, firmware_id: str, name: str) -> Firmware:
        """Rename, made unique within the firmware's scope."""
        raise NotImplementedError

    def move(self, firmware_id: str, project_id: str | None, device: str = "") -> Firmware:
        """Set project and group (None project = standalone, which also clears the group).
        Only these fields change, plus a name suffix if the name is taken in the new scope."""
        raise NotImplementedError

    def delete(self, firmware_id: str) -> None:
        """Delete the firmware file. Archive files and flash records are not touched."""
        raise NotImplementedError


def default_name_from_files(file_names: list[str]) -> str:
    """Manual firmware: the first file name without its extension."""
    raise NotImplementedError


def default_name_archived(source_name: str, date: datetime.date) -> str:
    """'<source name> (archived YYYY-MM-DD)'."""
    raise NotImplementedError
