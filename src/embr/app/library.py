"""Use cases on projects, groups, firmware and entries, and the sidebar tree.

Every change is saved before the method returns. Entry operations on fixed firmware
(manual, archived) change its entries. On tracking firmware: address, enabled state and
note of a derived entry (id = tracking.entry_key()) go to entry_overrides; adding files,
attachments or copied entries raises TrackingEntriesError (entries come from the build
only, design doc §12 item 16); removing, relocating or copying into a derived entry raises
ValueError; user entries added before that rule still flash and can be removed. Every
change to a tracking firmware invalidates its tracker status.
"""

import dataclasses
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.app.chips import chip_choosable, entry_chip, firmware_chips, for_chip, image_header
from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.tracking import Tracker
from embr.core import chipinfo
from embr.core.firmware import model
from embr.core.firmware.entries import (
    attachment_entry,
    blank_otadata_entry,
    copy_entry,
    enabled_partition_table,
    entry_from_file,
    resolve_partitions,
    set_address,
    sorted_entries,
)
from embr.core.firmware.files import (
    FileState,
    confirm_entry,
    entry_state,
    read_entry,
    relocate,
)
from embr.core.firmware.image_sniff import (
    blank_otadata,
    parse_partition_table,
)
from embr.core.firmware.import_idf import IdfBuild, default_firmware_name, read_build
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import (
    AddressSource,
    BuildRef,
    Entry,
    EntryOverride,
    Firmware,
    Source,
)
from embr.core.firmware.store import default_name_from_files
from embr.core.firmware.tracking import EntryMark, ResolveState
from embr.core.flasher.checks import ChipState, revision_incompatibility
from embr.core.flasher.plan import PlanEntry
from embr.core.flasher.protocol import InfoEvent
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
    BLOCKED = "blocked"
    """Same tag: the build is incomplete or its target was switched without a rebuild."""


class TrackingEntriesError(ValueError):
    """Entries of tracking firmware come from the build; nothing can be added to it."""


class AlreadyTrackedError(PlanError):
    """The build directory is already followed by another tracking firmware."""

    def __init__(self, message: str, firmware_id: str) -> None:
        super().__init__(message)
        self.firmware_id = firmware_id


@dataclass(frozen=True)
class FirmwareRow:
    id: str
    name: str
    origin: str
    """Second sidebar line: the firmware's folder (for firmware without one, the folder of
    the first enabled path entry), "Archive" for archived firmware, or the build directory
    for tracking firmware."""
    source: Source
    chips: tuple[str, ...]
    """app.chips.firmware_chips(); more than one shows as "multi"."""
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
    def __init__(self, context: AppContext, tracker: Tracker | None = None) -> None:
        """`tracker` is shared with Operations and the UI; None creates one."""
        self._ctx = context
        self._tracker = tracker or Tracker(context)

    @property
    def tracker(self) -> Tracker:
        return self._tracker

    # ---- sidebar ----------------------------------------------------------------------

    def tree(self, query: str = "") -> Tree:
        """Projects sorted by name with their groups and firmware (by name), then standalone
        firmware. `query` (case-insensitive substring): a project whose name matches keeps
        all its firmware; otherwise only firmware whose name matches are kept, and projects
        left with none are dropped. Flags come from the tracker's cached status of each
        firmware (NEEDS_CONFIRMATION -> "!", MISSING -> MISSING, INCOMPLETE and
        TARGET_SWITCHED -> BLOCKED; never a file access), or a cheap existence check when it
        has not been checked yet."""
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
        before flashing) and update the firmware's flag. Tracking firmware: Tracker.check()
        decides the flag; the returned states cover their user entries only."""
        status = self._tracker.check(firmware_id)
        firmware = status.firmware
        user = {e.id for e in firmware.user_entries}
        return {
            e.id: _MARK_STATES.get(status.marks.get(e.id, EntryMark.NEW), FileState.OK)
            for e in firmware.entries
            if not firmware.source.tracking or e.id in user
        }

    def flag(self, firmware_id: str) -> FirmwareFlag:
        """The flag the tree shows for this firmware."""
        status = self._tracker.cached(firmware_id)
        if status is not None:
            return _FLAGS[status.state]
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
        self._tracker.invalidate(firmware_id)

    def preview_idf(self, build_dir: Path) -> IdfBuild:
        """What importing `build_dir` would bring, for the import dialog. import_idf errors
        (BuildMissingError, EncryptedBuildError, IdfBuildError) propagate."""
        return read_build(build_dir)

    def import_idf(
        self,
        build_dir: Path,
        *,
        name: str = "",
        project_id: str | None = None,
        device: str = "",
    ) -> Firmware:
        """New IDF_BUILD firmware following `build_dir`: name (default
        import_idf.default_firmware_name(), made unique), project and group as given (a
        project copies only erase_all from its defaults); resolved at once and the first
        resolution accepted as last_resolved. import_idf errors propagate; PlanError when
        the build cannot be resolved (incomplete, target switched), nothing is created;
        AlreadyTrackedError when tracking_firmware_for() finds a firmware already following
        the same directory, nothing is created."""
        build = read_build(build_dir)
        tracked = self.tracking_firmware_for(build.build_dir)
        if tracked is not None:
            raise AlreadyTrackedError(
                f"{build.build_dir} is already followed by '{tracked.name}'", tracked.id
            )
        stem = name.strip() or default_firmware_name(build)
        if project_id is None:
            firmware = self._ctx.firmware.create(stem, source=Source.IDF_BUILD)
        else:
            firmware = self._ctx.projects.create_firmware(
                self._ctx.firmware, project_id, stem, device=device, source=Source.IDF_BUILD
            )
        firmware.build_ref = BuildRef(str(build.build_dir))
        self._ctx.firmware.save(firmware)
        status = self._tracker.check(firmware.id, settle=False)
        if status.state != ResolveState.OK:
            self.delete_firmware(firmware.id)
            raise PlanError(f"{build.build_dir}: " + "; ".join(status.problems))
        return self._ctx.firmware.get(firmware.id)

    def tracking_firmware_for(self, build_dir: Path) -> Firmware | None:
        """The IDF tracking firmware that follows `build_dir`, or None. Paths are compared
        after resolving them (absolute, `..` removed) and ignoring case and a trailing
        separator, as Windows does."""
        key = _path_key(build_dir)
        return next(
            (
                f
                for f in self._ctx.firmware.all()
                if f.source == Source.IDF_BUILD
                and f.build_ref is not None
                and _path_key(Path(f.build_ref.path)) == key
            ),
            None,
        )

    def mismatches(self, info: InfoEvent) -> dict[str, str]:
        """Firmware that do not fit the identified chip, by id, with the reason: the chip is
        not among the firmware's chips (app.chips.firmware_chips()), or an enabled entry for
        that chip has an image revision range that excludes the chip's revision (same rule
        as the flash check, checks.revision_incompatibility()). Tracking firmware are judged
        by the tracker's cached resolution (not resolved here; skipped when never resolved).
        Image headers are read at most once per file content. Firmware without any chip fit
        any chip."""
        state = ChipState(info.chip, info.major, info.minor, info.flash_size, info.security)
        found: dict[str, str] = {}
        for stored in self._ctx.firmware.all():
            if stored.source.tracking:
                status = self._tracker.cached(stored.id)
                if status is None:
                    continue
                firmware = status.firmware
            else:
                firmware = stored
            reason = self._mismatch(firmware, state)
            if reason is not None:
                found[stored.id] = reason
        return found

    def set_param(self, firmware_id: str, name: str, value: object) -> Firmware:
        """firmware.model.set_param() and save; its errors propagate."""
        firmware = self._ctx.firmware.get(firmware_id)
        model.set_param(firmware, name, value)
        self._save(firmware)
        return firmware

    # ---- entries ----------------------------------------------------------------------

    def create_from_files(
        self, paths: Sequence[Path], project_id: str | None = None, device: str = ""
    ) -> Firmware:
        """New fixed firmware (as create_firmware()) holding `paths`, named after the first
        file: files dropped on the sidebar (design doc §12 item 19). The folder rule is
        checked first: FolderError (or OSError for an unreadable file) and nothing is
        created."""
        raise NotImplementedError

    def add_files(self, firmware_id: str, paths: Sequence[Path]) -> list[Entry]:
        """Add one entry per file, classified by image_sniff. Partition tables are added
        first so the other files get addresses from them (or from the firmware's enabled
        table). When this adds a partition table with an otadata partition and no enabled
        otadata entry exists, a blank otadata entry is added too. Entries placed by
        partition are re-resolved. A firmware still named UNTITLED with no entries takes
        the first file's name (without extension). Returns the new entries. Folder rule
        (design doc §12 item 8): a firmware without a folder takes the folder of its first
        "path:" entry, or else of the first of `paths`; every file must be in that folder or
        below it, otherwise FolderError and nothing changes. Tracking firmware:
        TrackingEntriesError, nothing changes."""
        firmware = self._ctx.firmware.get(firmware_id)
        _refuse_tracking(firmware)
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
        larger than it is reported by the checklist, not raised. The folder rule applies as
        in add_files() (FolderError). Tracking firmware: TrackingEntriesError."""
        firmware = self._ctx.firmware.get(firmware_id)
        _refuse_tracking(firmware)
        entry = attachment_entry(path, address=address, partition=partition)
        firmware.entries.append(entry)
        self._save_entries(firmware)
        return entry

    def set_entry_address(self, firmware_id: str, entry_id: str, text: str) -> Entry:
        """entries.set_address(); AddressError propagates and nothing is saved."""
        firmware = self._ctx.firmware.get(firmware_id)
        own = _own_entry(firmware, entry_id)
        if own is not None:
            set_address(own, text)
            self._save_entries(firmware)
            return own
        entry = self._derived_entry(firmware_id, entry_id)
        set_address(entry, text)
        override = firmware.entry_overrides.setdefault(entry_id, EntryOverride())
        manual = entry.address_source == AddressSource.MANUAL
        override.address = entry.address if manual else None
        self._save_override(firmware, entry_id)
        return entry

    def set_entry_enabled(self, firmware_id: str, entry_id: str, enabled: bool) -> Entry:
        firmware = self._ctx.firmware.get(firmware_id)
        own = _own_entry(firmware, entry_id)
        if own is not None:
            own.enabled = enabled
            self._save_entries(firmware)
            return own
        entry = self._derived_entry(firmware_id, entry_id)
        entry.enabled = enabled
        override = firmware.entry_overrides.setdefault(entry_id, EntryOverride())
        override.enabled = None if enabled else False  # derived entries start enabled
        self._save_override(firmware, entry_id)
        return entry

    def set_entry_chip(self, firmware_id: str, entry_id: str, chip: str | None) -> Entry:
        """Choose the MCU of an entry whose file names no chip (None clears the choice).
        ValueError for an unknown chip, for an entry whose image header names a chip, and
        for tracking firmware (app.chips.chip_choosable())."""
        firmware = self._ctx.firmware.get(firmware_id)
        entry = _own_entry(firmware, entry_id)
        if entry is None or not chip_choosable(self._ctx, firmware, entry):
            raise ValueError(f"{firmware.name}: the MCU of this entry comes from its file")
        if chip is not None:
            try:
                chipinfo.get_chip(chip)
            except chipinfo.UnknownChipError:
                raise ValueError(f"unknown chip {chip!r}") from None
        entry.chip = chip
        self._save_entries(firmware)
        return entry

    def fit_entries(self, firmware_id: str, chip: str) -> list[Entry]:
        """After a board with `chip` was identified (design doc §12 item 13): untick and
        save every enabled entry of a fixed firmware whose MCU is another chip or none.
        Nothing changes when `chip` is not among the firmware's chips (the whole firmware is
        for other boards: the sidebar dims it, the checklist refuses), nor for tracking
        firmware. Returns the entries that were unticked."""
        firmware = self._ctx.firmware.get(firmware_id)
        if firmware.source.tracking or chip not in firmware_chips(self._ctx, firmware):
            return []
        unticked = [
            e for e in firmware.entries if e.enabled and entry_chip(self._ctx, firmware, e) != chip
        ]
        for entry in unticked:
            entry.enabled = False
        if unticked:
            self._save_entries(firmware)
        return unticked

    def set_entry_note(self, firmware_id: str, entry_id: str, note: str) -> Entry:
        firmware = self._ctx.firmware.get(firmware_id)
        own = _own_entry(firmware, entry_id)
        if own is not None:
            own.note = note
            self._save(firmware)
            return own
        entry = self._derived_entry(firmware_id, entry_id)
        entry.note = note
        override = firmware.entry_overrides.setdefault(entry_id, EntryOverride())
        override.note = note or None
        self._save_override(firmware, entry_id)
        return entry

    def remove_entry(self, firmware_id: str, entry_id: str) -> None:
        """A fixed firmware left without "path:" entries loses its folder."""
        firmware = self._ctx.firmware.get(firmware_id)
        own = _own_entry(firmware, entry_id)
        if own is None:
            raise _derived_error(firmware)
        if firmware.source.tracking:
            firmware.user_entries.remove(own)
            self._save(firmware)
        else:
            firmware.entries.remove(own)
            self._save_entries(firmware)

    def copy_entry_to(self, firmware_id: str, entry_id: str, target_id: str) -> Entry:
        """Copy an entry (same file, address, note, chosen MCU) into another firmware. A
        "path:" entry must fit the target's folder as in add_files() (FolderError). A
        tracking target raises TrackingEntriesError."""
        source = self._ctx.firmware.get(firmware_id)
        entry = _own_entry(source, entry_id) or self._derived_entry(firmware_id, entry_id)
        target = self._ctx.firmware.get(target_id)
        _refuse_tracking(target)
        copy = copy_entry(entry)
        if copy.address_source == AddressSource.AUTO:  # no detection outside its firmware
            copy.address_source = AddressSource.MANUAL
        target.entries.append(copy)
        self._save_entries(target)
        return copy

    def relocate_entry(self, firmware_id: str, entry_id: str, path: Path) -> Entry:
        """Point a missing or changed "path:" entry at another file. With a folder set, the
        file must be in it (FolderError); see relocate_folder() to move them all."""
        firmware = self._ctx.firmware.get(firmware_id)
        entry = _own_entry(firmware, entry_id)
        if entry is None:
            raise _derived_error(firmware)
        relocate(entry, path)
        if firmware.source.tracking:
            self._save(firmware)
        else:
            self._save_entries(firmware)
        self.check_files(firmware_id)
        return entry

    def relocate_folder(self, firmware_id: str, folder: Path) -> list[str]:
        """Move a fixed firmware's folder: every "path:" entry inside the old folder points
        at the same relative path under `folder` when that file exists there, and the
        folder becomes `folder`. Returns the names of entries left pointing at the old place
        (no such file under `folder`). ValueError for a firmware without a folder or a
        tracking one."""
        raise NotImplementedError

    def confirm_entries(self, firmware_id: str) -> Firmware:
        """Accept the current content of every CHANGED "path:" entry (new hash and size).
        Tracking firmware: Tracker.confirm() (accepts the changed build)."""
        firmware = self._ctx.firmware.get(firmware_id)
        if firmware.source.tracking:
            self._tracker.confirm(firmware_id)
            return self._ctx.firmware.get(firmware_id)
        archive = self._ctx.paths.archive
        for entry in firmware.entries:
            if entry_state(entry, archive) == FileState.CHANGED:
                confirm_entry(entry, archive)
        self._save(firmware)
        self.check_files(firmware_id)
        return firmware

    # ---- helpers ----------------------------------------------------------------------

    def _read(self, entry: Entry) -> bytes:
        return read_entry(entry, self._ctx.paths.archive)

    def _save(self, firmware: Firmware) -> None:
        self._ctx.firmware.save(firmware)
        self._tracker.invalidate(firmware.id)

    def _save_entries(self, firmware: Firmware) -> None:
        """Re-resolve entries placed by partition (against the table of their own chip),
        then save."""
        chips = firmware_chips(self._ctx, firmware)
        views = [for_chip(self._ctx, firmware, c) for c in chips] if len(chips) > 1 else [firmware]
        for view in views:
            try:
                table, _ = enabled_partition_table(view, self._read)
            except OSError:  # the table file is missing; the checklist reports it
                table = None
            if table is not None:
                resolve_partitions(view, table)
        self._save(firmware)

    def _save_override(self, firmware: Firmware, key: str) -> None:
        if firmware.entry_overrides.get(key) == EntryOverride():
            del firmware.entry_overrides[key]
        self._save(firmware)

    def _derived_entry(self, firmware_id: str, entry_id: str) -> Entry:
        """A copy of a derived entry of a tracking firmware, as currently resolved."""
        resolved = self._tracker.check(firmware_id).firmware
        return dataclasses.replace(_entry(resolved, entry_id))

    def _mismatch(self, firmware: Firmware, state: ChipState) -> str | None:
        chips = firmware_chips(self._ctx, firmware)
        if chips and state.chip not in chips:
            return f"firmware is for {', '.join(chips)}, the board is {state.chip}"
        if state.major < 0:  # secure download mode: the revision was not read
            return None
        for entry in sorted_entries([e for e in firmware.entries if e.enabled]):
            header = image_header(self._ctx, entry)
            if header is None or header.chip != state.chip:  # other chips are left out
                continue
            planned = PlanEntry(
                address=entry.address or 0,
                path="",
                name=entry.name,
                sha256=entry.sha256,
                size=entry.size,
                image_chip=header.chip,
                min_rev_full=header.min_rev_full,
                max_rev_full=header.max_rev_full,
                legacy_min_rev=header.legacy_min_rev,
            )
            reason = revision_incompatibility(state, planned)
            if reason is not None:
                return reason
        return None

    def _row(self, firmware: Firmware) -> FirmwareRow:
        return FirmwareRow(
            id=firmware.id,
            name=firmware.name,
            origin=_origin(firmware),
            source=firmware.source,
            chips=firmware_chips(self._ctx, firmware),
            flag=self._cached_flag(firmware),
            project_id=firmware.project_id,
            device=firmware.device,
            notes=firmware.notes,
        )

    def _cached_flag(self, firmware: Firmware) -> FirmwareFlag:
        status = self._tracker.cached(firmware.id)
        return _FLAGS[status.state] if status is not None else self._quick_flag(firmware)

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


_FLAGS = {
    ResolveState.OK: FirmwareFlag.NONE,
    ResolveState.UPDATED: FirmwareFlag.NONE,
    ResolveState.BUILDING: FirmwareFlag.NONE,
    ResolveState.NEEDS_CONFIRMATION: FirmwareFlag.NEEDS_CONFIRMATION,
    ResolveState.MISSING: FirmwareFlag.MISSING,
    ResolveState.INCOMPLETE: FirmwareFlag.BLOCKED,
    ResolveState.TARGET_SWITCHED: FirmwareFlag.BLOCKED,
}

_MARK_STATES = {EntryMark.CHANGED: FileState.CHANGED, EntryMark.MISSING: FileState.MISSING}


def _origin(firmware: Firmware) -> str:
    if firmware.source == Source.ARCHIVED:
        return "Archive"
    if firmware.build_ref is not None:
        env = firmware.build_ref.env
        return f"{firmware.build_ref.path} (env: {env})" if env else firmware.build_ref.path
    paths = [e for e in firmware.entries if e.file.startswith("path:")]
    first = next((e for e in paths if e.enabled), paths[0] if paths else None)
    return str(Path(first.file.removeprefix("path:")).parent) if first else ""


def _refuse_tracking(firmware: Firmware) -> None:
    if firmware.source.tracking:
        raise TrackingEntriesError(
            f"entries of '{firmware.name}' come from its build directory; archive this "
            "version or create a fixed firmware to add files"
        )


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _own_entry(firmware: Firmware, entry_id: str) -> Entry | None:
    """The firmware's stored entry: any entry of a fixed firmware, a user entry of a
    tracking one; None for derived entries."""
    if not firmware.source.tracking:
        return _entry(firmware, entry_id)
    return next((e for e in firmware.user_entries if e.id == entry_id), None)


def _derived_error(firmware: Firmware) -> ValueError:
    return ValueError(f"{firmware.name}: this entry comes from the build output")


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
