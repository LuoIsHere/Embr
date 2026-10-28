"""Projects: optional containers of firmware with a group list and defaults (design doc
§3.1, §3.2). Operations that touch firmware take the FirmwareStore explicitly; firmware
never depends on this module."""

from dataclasses import dataclass, field
from pathlib import Path

from embr.core.firmware.model import Firmware, Source
from embr.core.firmware.store import FirmwareStore

PROJECT_SCHEMA_VERSION = 1


class DuplicateGroupError(ValueError):
    """A group with that name already exists in the project."""


@dataclass
class ProjectDefaults:
    """Copied once into firmware created in the project."""

    before: str = "default-reset"
    after: str = "hard-reset"
    erase_all: bool = False


@dataclass
class Project:
    id: str
    name: str
    """Unique across all projects."""
    notes: str = ""
    tags: list[str] = field(default_factory=lambda: list[str]())
    devices: list[str] = field(default_factory=lambda: list[str]())
    """Group names, in display order; empty groups allowed."""
    defaults: ProjectDefaults = field(default_factory=ProjectDefaults)
    created_at: str = ""
    updated_at: str = ""
    read_only: bool = False
    """Loaded from a file written by a newer Embr; saving is refused."""


class ProjectStore:
    """data/projects/<id>.toml, one file per project. Firmware lists are not stored here;
    membership is the firmware's project_id."""

    def __init__(self, directory: Path, backup_root: Path) -> None:
        self.directory = directory
        self.backup_root = backup_root

    def all(self) -> list[Project]:
        """Every project, sorted by name; newer schema versions read-only."""
        raise NotImplementedError

    def get(self, project_id: str) -> Project:
        """Raises KeyError if missing."""
        raise NotImplementedError

    def create(
        self, name: str, notes: str = "", defaults: ProjectDefaults | None = None
    ) -> Project:
        """New project, saved; the name is made globally unique with " (2)" suffixes."""
        raise NotImplementedError

    def save(self, project: Project) -> None:
        """Write the TOML file and update updated_at. Raises
        embr.core.firmware.store.ReadOnlyError if read_only."""
        raise NotImplementedError

    def rename(self, project_id: str, name: str) -> Project:
        """Rename, made globally unique. Firmware keep their project_id."""
        raise NotImplementedError

    def delete(
        self, project_id: str, firmware: FirmwareStore, *, delete_firmware: bool = False
    ) -> None:
        """Delete the project. Its firmware become standalone (names made unique among
        standalone firmware), or are deleted with delete_firmware."""
        raise NotImplementedError

    def add_group(self, project_id: str, name: str) -> Project:
        """Append a group. DuplicateGroupError if it exists."""
        raise NotImplementedError

    def rename_group(self, project_id: str, old: str, new: str, firmware: FirmwareStore) -> Project:
        """Rename a group and every firmware of the project in it. DuplicateGroupError if
        `new` exists."""
        raise NotImplementedError

    def remove_group(self, project_id: str, name: str, firmware: FirmwareStore) -> Project:
        """Remove a group; its firmware stay in the project, ungrouped."""
        raise NotImplementedError

    def create_firmware(
        self,
        firmware: FirmwareStore,
        project_id: str,
        name: str,
        *,
        device: str = "",
        source: Source = Source.MANUAL,
    ) -> Firmware:
        """New firmware in the project with the project's current defaults copied in
        (IDF tracking firmware copy only erase_all). An unknown `device` is added as a group."""
        raise NotImplementedError

    def move_firmware(
        self, firmware: FirmwareStore, firmware_id: str, project_id: str | None, device: str = ""
    ) -> Firmware:
        """Move a firmware into a project (and group) or out to standalone (project_id None).
        A `device` not yet in the project's list is added to it."""
        raise NotImplementedError
