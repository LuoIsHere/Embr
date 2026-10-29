"""Use cases on projects, groups, firmware and entries, and the sidebar tree.

Every change is saved before the method returns. Entry operations apply to fixed firmware
(manual, archived); for tracking firmware they raise ValueError.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.app.context import AppContext
from embr.core.firmware.files import FileState
from embr.core.firmware.model import Entry, Firmware, Source
from embr.core.project import Project, ProjectDefaults

UNTITLED = "untitled"
"""Name of a firmware created without one; replaced by the first added file's name."""


class FirmwareFlag(StrEnum):
    NONE = "none"
    NEEDS_CONFIRMATION = "needs_confirmation"
    """Tag "!": an entry file's content changed since it was recorded."""
    MISSING = "missing"
    """Tag with the multiplication sign: an entry file (or the build directory) is gone."""


@dataclass(frozen=True)
class FirmwareRow:
    id: str
    name: str
    origin: str
    """Second sidebar line: files folder (folder of the first enabled path entry), "Archive"
    for archived firmware, or the build directory for tracking firmware."""
    source: Source
    chip: str | None
    flag: FirmwareFlag
    project_id: str | None
    device: str
    notes: str


@dataclass(frozen=True)
class GroupRow:
    name: str
    """"" is the Ungrouped bucket."""
    firmware: tuple[FirmwareRow, ...]


@dataclass(frozen=True)
class ProjectRow:
    id: str
    name: str
    notes: str
    groups: tuple[GroupRow, ...]
    """Declared groups in order (empty ones included), then Ungrouped when it has firmware."""
    firmware_count: int
    """All firmware of the project, whatever the search."""


@dataclass(frozen=True)
class Tree:
    projects: tuple[ProjectRow, ...]
    standalone: tuple[FirmwareRow, ...]


class Library:
    def __init__(self, context: AppContext) -> None:
        raise NotImplementedError

    # ---- sidebar ----------------------------------------------------------------------

    def tree(self, query: str = "") -> Tree:
        """Projects sorted by name with their groups and firmware (by name), then standalone
        firmware. `query` (case-insensitive substring): a project whose name matches keeps
        all its firmware; otherwise only firmware whose name matches are kept, and projects
        left with none are dropped. Flags come from the last file check of each firmware
        (check_files()), or a cheap existence check when it has not been checked yet."""
        raise NotImplementedError

    def check_files(self, firmware_id: str) -> dict[str, FileState]:
        """Re-check every entry by name, path and hash (done when a firmware is opened and
        before flashing) and update the firmware's flag."""
        raise NotImplementedError

    def flag(self, firmware_id: str) -> FirmwareFlag:
        """The flag the tree shows for this firmware."""
        raise NotImplementedError

    # ---- projects and groups ----------------------------------------------------------

    def project(self, project_id: str) -> Project:
        """Raises KeyError if missing."""
        raise NotImplementedError

    def create_project(self, name: str, notes: str = "") -> Project:
        """Empty name becomes UNTITLED; the name is made unique."""
        raise NotImplementedError

    def rename_project(self, project_id: str, name: str) -> Project:
        """ValueError for an empty name; otherwise made unique."""
        raise NotImplementedError

    def set_project_notes(self, project_id: str, notes: str) -> Project:
        raise NotImplementedError

    def set_project_defaults(self, project_id: str, defaults: ProjectDefaults) -> Project:
        """Only firmware created afterwards are affected."""
        raise NotImplementedError

    def delete_project(self, project_id: str, *, delete_firmware: bool = False) -> None:
        raise NotImplementedError

    def add_group(self, project_id: str, name: str) -> Project:
        """ValueError for an empty name; project.DuplicateGroupError if it exists."""
        raise NotImplementedError

    def rename_group(self, project_id: str, old: str, new: str) -> Project:
        """ValueError for an empty name; project.DuplicateGroupError if `new` exists."""
        raise NotImplementedError

    def remove_group(self, project_id: str, name: str) -> Project:
        """Its firmware stay in the project, ungrouped."""
        raise NotImplementedError

    # ---- firmware ---------------------------------------------------------------------

    def firmware(self, firmware_id: str) -> Firmware:
        """Raises KeyError if missing."""
        raise NotImplementedError

    def project_firmware(self, project_id: str) -> list[Firmware]:
        """Firmware of a project, sorted by name."""
        raise NotImplementedError

    def create_firmware(
        self, name: str = "", project_id: str | None = None, device: str = ""
    ) -> Firmware:
        """New fixed firmware. In a project it copies the project defaults; standalone it
        uses default-reset / hard-reset. An empty name becomes UNTITLED."""
        raise NotImplementedError

    def rename_firmware(self, firmware_id: str, name: str) -> Firmware:
        """ValueError for an empty name; otherwise made unique within the scope."""
        raise NotImplementedError

    def move_firmware(self, firmware_id: str, project_id: str | None, device: str = "") -> Firmware:
        """Into a project and group (unknown groups are added) or out to standalone."""
        raise NotImplementedError

    def duplicate_firmware(self, firmware_id: str) -> Firmware:
        """Copy in the same project and group named "<name> copy" (made unique), with new
        entry ids; archived stays archived, anything else becomes a fixed (manual) copy.
        Records are not copied."""
        raise NotImplementedError

    def delete_firmware(self, firmware_id: str) -> None:
        """Archive files and flash records are kept."""
        raise NotImplementedError

    def set_param(self, firmware_id: str, name: str, value: object) -> Firmware:
        """firmware.model.set_param() and save; its errors propagate."""
        raise NotImplementedError

    # ---- entries (fixed firmware) -----------------------------------------------------

    def add_files(self, firmware_id: str, paths: Sequence[Path]) -> list[Entry]:
        """Add one entry per file, classified by image_sniff. Partition tables are added
        first so the other files get addresses from them (or from the firmware's enabled
        table). When this adds a partition table with an otadata partition and no enabled
        otadata entry exists, a blank otadata entry is added too. Entries placed by
        partition are re-resolved. A firmware still named UNTITLED with no entries takes
        the first file's name (without extension); a firmware without a chip takes the chip
        of the first ESP image added. Returns the new entries."""
        raise NotImplementedError

    def add_attachment(
        self,
        firmware_id: str,
        path: Path,
        *,
        address: int | None = None,
        partition: str | None = None,
    ) -> Entry:
        """Attachment placed by address or by partition (exactly one). By partition: the
        address is resolved from the enabled partition table; a missing partition or a file
        larger than it is reported by the checklist, not raised."""
        raise NotImplementedError

    def set_entry_address(self, firmware_id: str, entry_id: str, text: str) -> Entry:
        """entries.set_address(); AddressError propagates and nothing is saved."""
        raise NotImplementedError

    def set_entry_enabled(self, firmware_id: str, entry_id: str, enabled: bool) -> Entry:
        raise NotImplementedError

    def set_entry_note(self, firmware_id: str, entry_id: str, note: str) -> Entry:
        raise NotImplementedError

    def remove_entry(self, firmware_id: str, entry_id: str) -> None:
        raise NotImplementedError

    def copy_entry_to(self, firmware_id: str, entry_id: str, target_id: str) -> Entry:
        """Copy an entry (same file, address, note) into another fixed firmware."""
        raise NotImplementedError

    def relocate_entry(self, firmware_id: str, entry_id: str, path: Path) -> Entry:
        """Point a missing or changed "path:" entry at another file."""
        raise NotImplementedError

    def confirm_entries(self, firmware_id: str) -> Firmware:
        """Accept the current content of every CHANGED "path:" entry (new hash and size)."""
        raise NotImplementedError
