"""Use cases on projects, groups, firmware and entries, and the sidebar tree.

Every change is saved before the method returns. Entry operations apply to fixed firmware
(manual, archived); for tracking firmware they raise ValueError.
"""

import dataclasses
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.app.context import AppContext
from embr.core.firmware import model
from embr.core.firmware.entries import (
    attachment_entry,
    blank_otadata_entry,
    copy_entry,
    enabled_partition_table,
    entry_from_file,
    resolve_partitions,
    set_address,
)
from embr.core.firmware.files import (
    FileState,
    confirm_entry,
    entry_state,
    firmware_states,
    read_entry,
    relocate,
)
from embr.core.firmware.image_sniff import blank_otadata, parse_partition_table, sniff
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Entry, Firmware, Source
from embr.core.firmware.store import default_name_from_files
from embr.core.project import Project, ProjectDefaults
from embr.core.storage.ids import unique_name

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
        self._ctx = context
        self._flags: dict[str, FirmwareFlag] = {}

    # ---- sidebar ----------------------------------------------------------------------

    def tree(self, query: str = "") -> Tree:
        """Projects sorted by name with their groups and firmware (by name), then standalone
        firmware. `query` (case-insensitive substring): a project whose name matches keeps
        all its firmware; otherwise only firmware whose name matches are kept, and projects
        left with none are dropped. Flags come from the last file check of each firmware
        (check_files()), or a cheap existence check when it has not been checked yet."""
        q = query.strip().casefold()
        everything = self._ctx.firmware.all()
        rows = {f.id: self._row(f) for f in everything}

        def matches(f: Firmware) -> bool:
            return not q or q in f.name.casefold()

        projects: list[ProjectRow] = []
        for project in self._ctx.projects.all():
            members = sorted((f for f in everything if f.project_id == project.id), key=_by_name)
            if not q or q in project.name.casefold():
                shown = members
            else:
                shown = [f for f in members if matches(f)]
                if not shown:
                    continue
            groups = [
                GroupRow(name, tuple(rows[f.id] for f in shown if f.device == name))
                for name in project.devices
            ]
            ungrouped = tuple(rows[f.id] for f in shown if f.device not in project.devices)
            if ungrouped:
                groups.append(GroupRow("", ungrouped))
            projects.append(
                ProjectRow(project.id, project.name, project.notes, tuple(groups), len(members))
            )
        standalone = tuple(
            rows[f.id]
            for f in sorted(everything, key=_by_name)
            if f.project_id is None and matches(f)
        )
        return Tree(tuple(projects), standalone)

    def check_files(self, firmware_id: str) -> dict[str, FileState]:
        """Re-check every entry by name, path and hash (done when a firmware is opened and
        before flashing) and update the firmware's flag."""
        firmware = self._ctx.firmware.get(firmware_id)
        states = firmware_states(firmware, self._ctx.paths.archive)
        self._flags[firmware_id] = _flag_of(states.values())
        return states

    def flag(self, firmware_id: str) -> FirmwareFlag:
        """The flag the tree shows for this firmware."""
        cached = self._flags.get(firmware_id)
        if cached is not None:
            return cached
        return self._quick_flag(self._ctx.firmware.get(firmware_id))

    # ---- projects and groups ----------------------------------------------------------

    def project(self, project_id: str) -> Project:
        """Raises KeyError if missing."""
        return self._ctx.projects.get(project_id)

    def create_project(self, name: str, notes: str = "") -> Project:
        """Empty name becomes UNTITLED; the name is made unique."""
        return self._ctx.projects.create(name.strip() or UNTITLED, notes)

    def rename_project(self, project_id: str, name: str) -> Project:
        """ValueError for an empty name; otherwise made unique."""
        return self._ctx.projects.rename(project_id, _required(name))

    def set_project_notes(self, project_id: str, notes: str) -> Project:
        project = self._ctx.projects.get(project_id)
        project.notes = notes
        self._ctx.projects.save(project)
        return project

    def set_project_defaults(self, project_id: str, defaults: ProjectDefaults) -> Project:
        """Only firmware created afterwards are affected."""
        project = self._ctx.projects.get(project_id)
        project.defaults = defaults
        self._ctx.projects.save(project)
        return project

    def delete_project(self, project_id: str, *, delete_firmware: bool = False) -> None:
        self._ctx.projects.delete(project_id, self._ctx.firmware, delete_firmware=delete_firmware)

    def add_group(self, project_id: str, name: str) -> Project:
        """ValueError for an empty name; project.DuplicateGroupError if it exists."""
        return self._ctx.projects.add_group(project_id, _required(name))

    def rename_group(self, project_id: str, old: str, new: str) -> Project:
        """ValueError for an empty name; project.DuplicateGroupError if `new` exists."""
        return self._ctx.projects.rename_group(project_id, old, _required(new), self._ctx.firmware)

    def remove_group(self, project_id: str, name: str) -> Project:
        """Its firmware stay in the project, ungrouped."""
        return self._ctx.projects.remove_group(project_id, name, self._ctx.firmware)

    # ---- firmware ---------------------------------------------------------------------

    def firmware(self, firmware_id: str) -> Firmware:
        """Raises KeyError if missing."""
        return self._ctx.firmware.get(firmware_id)

    def project_firmware(self, project_id: str) -> list[Firmware]:
        """Firmware of a project, sorted by name."""
        return sorted(self._ctx.firmware.in_scope(project_id), key=_by_name)

    def create_firmware(
        self, name: str = "", project_id: str | None = None, device: str = ""
    ) -> Firmware:
        """New fixed firmware. In a project it copies the project defaults; standalone it
        uses default-reset / hard-reset. An empty name becomes UNTITLED."""
        name = name.strip() or UNTITLED
        if project_id is None:
            return self._ctx.firmware.create(name)
        return self._ctx.projects.create_firmware(
            self._ctx.firmware, project_id, name, device=device
        )

    def rename_firmware(self, firmware_id: str, name: str) -> Firmware:
        """ValueError for an empty name; otherwise made unique within the scope."""
        return self._ctx.firmware.rename(firmware_id, _required(name))

    def move_firmware(self, firmware_id: str, project_id: str | None, device: str = "") -> Firmware:
        """Into a project and group (unknown groups are added) or out to standalone."""
        return self._ctx.projects.move_firmware(self._ctx.firmware, firmware_id, project_id, device)

    def duplicate_firmware(self, firmware_id: str) -> Firmware:
        """Copy in the same project and group named "<name> copy" (made unique), with new
        entry ids; archived stays archived, anything else becomes a fixed (manual) copy.
        Records are not copied."""
        source = self._ctx.firmware.get(firmware_id)
        copy = self._ctx.firmware.create(
            f"{source.name} copy",
            source=Source.ARCHIVED if source.source == Source.ARCHIVED else Source.MANUAL,
            project_id=source.project_id,
            device=source.device,
            before=source.before,
            after=source.after,
            erase_all=source.erase_all,
        )
        copy.chip = source.chip
        copy.notes = source.notes
        copy.tags = list(source.tags)
        copy.flash = dataclasses.replace(source.flash)
        copy.archived_from = source.archived_from
        copy.entries = [copy_entry(e) for e in [*source.entries, *source.user_entries]]
        self._ctx.firmware.save(copy)
        return copy

    def delete_firmware(self, firmware_id: str) -> None:
        """Archive files and flash records are kept."""
        self._ctx.firmware.delete(firmware_id)
        self._flags.pop(firmware_id, None)

    def set_param(self, firmware_id: str, name: str, value: object) -> Firmware:
        """firmware.model.set_param() and save; its errors propagate."""
        firmware = self._ctx.firmware.get(firmware_id)
        model.set_param(firmware, name, value)
        self._ctx.firmware.save(firmware)
        return firmware

    # ---- entries (fixed firmware) -----------------------------------------------------

    def add_files(self, firmware_id: str, paths: Sequence[Path]) -> list[Entry]:
        """Add one entry per file, classified by image_sniff. Partition tables are added
        first so the other files get addresses from them (or from the firmware's enabled
        table). When this adds a partition table with an otadata partition and no enabled
        otadata entry exists, a blank otadata entry is added too. Entries placed by
        partition are re-resolved. A firmware still named UNTITLED with no entries takes
        the first file's name (without extension); a firmware without a chip takes the chip
        of the first ESP image added. Returns the new entries."""
        firmware = self._fixed(firmware_id)
        was_empty = not firmware.entries
        # tables first, so the files after them get addresses from the table
        ordered = sorted(paths, key=lambda p: parse_partition_table(p.read_bytes()) is None)
        added: list[Entry] = []
        for path in ordered:
            table, _ = enabled_partition_table(firmware, self._read)
            entry = entry_from_file(path, table)
            firmware.entries.append(entry)
            added.append(entry)
        if any(e.kind == EntryKind.PARTITION_TABLE for e in added):
            table, _ = enabled_partition_table(firmware, self._read)
            part = blank_otadata(table, [e.kind for e in firmware.entries if e.enabled])
            if part is not None:
                blank = blank_otadata_entry(part.offset, part.size)
                firmware.entries.append(blank)
                added.append(blank)
        if was_empty and added and _is_untitled(firmware.name):
            taken = [f.name for f in self._ctx.firmware.in_scope(firmware.project_id)]
            stem = default_name_from_files([p.name for p in paths])
            firmware.name = unique_name(stem, [n for n in taken if n != firmware.name])
        if firmware.chip is None:
            chips = (sniff(self._read(e), e.name).chip for e in added)
            firmware.chip = next((c for c in chips if c is not None), None)
        self._save_entries(firmware)
        return added

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
        firmware = self._fixed(firmware_id)
        entry = attachment_entry(path, address=address, partition=partition)
        firmware.entries.append(entry)
        self._save_entries(firmware)
        return entry

    def set_entry_address(self, firmware_id: str, entry_id: str, text: str) -> Entry:
        """entries.set_address(); AddressError propagates and nothing is saved."""
        firmware = self._fixed(firmware_id)
        entry = _entry(firmware, entry_id)
        set_address(entry, text)
        self._save_entries(firmware)
        return entry

    def set_entry_enabled(self, firmware_id: str, entry_id: str, enabled: bool) -> Entry:
        firmware = self._fixed(firmware_id)
        entry = _entry(firmware, entry_id)
        entry.enabled = enabled
        self._save_entries(firmware)
        return entry

    def set_entry_note(self, firmware_id: str, entry_id: str, note: str) -> Entry:
        firmware = self._fixed(firmware_id)
        entry = _entry(firmware, entry_id)
        entry.note = note
        self._ctx.firmware.save(firmware)
        return entry

    def remove_entry(self, firmware_id: str, entry_id: str) -> None:
        firmware = self._fixed(firmware_id)
        firmware.entries.remove(_entry(firmware, entry_id))
        self._save_entries(firmware)

    def copy_entry_to(self, firmware_id: str, entry_id: str, target_id: str) -> Entry:
        """Copy an entry (same file, address, note) into another fixed firmware."""
        entry = _entry(self._ctx.firmware.get(firmware_id), entry_id)
        target = self._fixed(target_id)
        copy = copy_entry(entry)
        target.entries.append(copy)
        self._save_entries(target)
        return copy

    def relocate_entry(self, firmware_id: str, entry_id: str, path: Path) -> Entry:
        """Point a missing or changed "path:" entry at another file."""
        firmware = self._fixed(firmware_id)
        entry = _entry(firmware, entry_id)
        relocate(entry, path)
        self._save_entries(firmware)
        self.check_files(firmware_id)
        return entry

    def confirm_entries(self, firmware_id: str) -> Firmware:
        """Accept the current content of every CHANGED "path:" entry (new hash and size)."""
        firmware = self._fixed(firmware_id)
        archive = self._ctx.paths.archive
        for entry in firmware.entries:
            if entry_state(entry, archive) == FileState.CHANGED:
                confirm_entry(entry, archive)
        self._ctx.firmware.save(firmware)
        self.check_files(firmware_id)
        return firmware

    # ---- helpers ----------------------------------------------------------------------

    def _read(self, entry: Entry) -> bytes:
        return read_entry(entry, self._ctx.paths.archive)

    def _fixed(self, firmware_id: str) -> Firmware:
        firmware = self._ctx.firmware.get(firmware_id)
        if firmware.source.tracking:
            raise ValueError(f"{firmware.name}: entries of a tracking firmware come from its build")
        return firmware

    def _save_entries(self, firmware: Firmware) -> None:
        """Re-resolve entries placed by partition, then save."""
        try:
            table, _ = enabled_partition_table(firmware, self._read)
        except OSError:  # the table file is missing; the checklist reports it
            table = None
        if table is not None:
            resolve_partitions(firmware, table)
        self._ctx.firmware.save(firmware)
        self._flags.pop(firmware.id, None)

    def _row(self, firmware: Firmware) -> FirmwareRow:
        return FirmwareRow(
            id=firmware.id,
            name=firmware.name,
            origin=_origin(firmware),
            source=firmware.source,
            chip=firmware.chip,
            flag=self._flags.get(firmware.id) or self._quick_flag(firmware),
            project_id=firmware.project_id,
            device=firmware.device,
            notes=firmware.notes,
        )

    def _quick_flag(self, firmware: Firmware) -> FirmwareFlag:
        """Existence only: no hashing."""
        if firmware.build_ref is not None and not Path(firmware.build_ref.path).exists():
            return FirmwareFlag.MISSING
        for entry in firmware.entries:
            scheme, _, rest = entry.file.partition(":")
            if scheme == "path" and not Path(rest).is_file():
                return FirmwareFlag.MISSING
            if scheme == "archive" and not (self._ctx.paths.archive / f"{rest}.bin").is_file():
                return FirmwareFlag.MISSING
        return FirmwareFlag.NONE


def _flag_of(states: Iterable[FileState]) -> FirmwareFlag:
    found = set(states)
    if FileState.MISSING in found:
        return FirmwareFlag.MISSING
    if FileState.CHANGED in found:
        return FirmwareFlag.NEEDS_CONFIRMATION
    return FirmwareFlag.NONE


def _origin(firmware: Firmware) -> str:
    if firmware.source == Source.ARCHIVED:
        return "Archive"
    if firmware.build_ref is not None:
        env = firmware.build_ref.env
        return f"{firmware.build_ref.path} (env: {env})" if env else firmware.build_ref.path
    paths = [e for e in firmware.entries if e.file.startswith("path:")]
    first = next((e for e in paths if e.enabled), paths[0] if paths else None)
    return str(Path(first.file.removeprefix("path:")).parent) if first else ""


def _entry(firmware: Firmware, entry_id: str) -> Entry:
    for entry in firmware.entries:
        if entry.id == entry_id:
            return entry
    raise KeyError(entry_id)


def _required(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("the name cannot be empty")
    return name


def _is_untitled(name: str) -> bool:
    return name == UNTITLED or re.fullmatch(rf"{UNTITLED} \(\d+\)", name) is not None


def _by_name(firmware: Firmware) -> str:
    return firmware.name.casefold()
