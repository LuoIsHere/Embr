"""Use cases on projects, groups, firmware and entries, and the sidebar tree.

Every change is saved before the method returns. Entry operations on fixed firmware
(manual, archived) change its entries. On tracking firmware: address, enabled state and
note of a derived entry (id = tracking.entry_key()) go to entry_overrides; adding files,
attachments or copied entries raises TrackingEntriesError (entries come from the build
only, design doc §12 item 16); removing, relocating or copying into a derived entry raises
ValueError. Every change to a tracking firmware invalidates its tracker status.
"""

import dataclasses
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.app.chips import (
    board_revision,
    chip_choosable,
    entry_mcu,
    firmware_chips,
    firmware_mcus,
    fits,
    for_chip,
    image_header,
)
from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.pio import FRAMEWORK_PKG_KEY as FRAMEWORK_PKG_KEY
from embr.app.pio import PIO_SECTION as PIO_SECTION
from embr.app.tracking import Tracker
from embr.core import chipinfo
from embr.core.firmware import model
from embr.core.firmware.entries import (
    FolderError,
    attachment_entry,
    blank_otadata_entry,
    copy_entry,
    enabled_partition_table,
    entry_from_file,
    in_folder,
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
from embr.core.firmware.import_pio import (
    EnvRef,
    PioBuild,
    PioBuildError,
    env_ref,
    ini_envs,
    is_built,
    locate_package,
    read_env,
)
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
from embr.core.storage.documents import DamagedDocument, keep_damaged_copy
from embr.core.storage.ids import unique_name
from embr.i18n import N_

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


ARCHIVE_ORIGIN = N_("Archive")
"""FirmwareRow.origin of archived firmware (shown translated)."""


@dataclass(frozen=True)
class FirmwareRow:
    id: str
    name: str
    origin: str
    """Second sidebar line: the firmware's folder, "Archive" for archived firmware, or the
    build directory for tracking firmware."""
    source: Source
    chips: tuple[str, ...]
    """app.chips.firmware_chips()."""
    mcus: tuple[str, ...]
    """Labels of app.chips.firmware_mcus() ("ESP32-P4 v3"); more than one shows as
    "multi"."""
    flag: FirmwareFlag
    project_id: str | None
    device: str
    notes: str


@dataclass(frozen=True)
class PioEnvChoice:
    """One env of the project in the PIO import dialog."""

    name: str
    built: bool
    """import_pio.is_built(); unbuilt envs are shown greyed and cannot be chosen."""
    tracked_by: str | None
    """Id of the firmware already following this project + env (cannot be chosen)."""
    board: str | None
    """From `pio project config`; None when unknown."""
    build: PioBuild | None
    """read_env() of a built env; None when unbuilt or unreadable."""
    problem: str | None
    """Why a built env could not be read."""


@dataclass(frozen=True)
class PioPreview:
    picked: EnvRef
    """The env whose build folder the user picked (checked by default)."""
    envs: tuple[PioEnvChoice, ...]
    """Every env of platformio.ini, in file order."""
    config_error: str | None
    """Why `pio project config` gave nothing (boards are then unknown); None on success."""


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
        readable = self._ctx.projects.all()
        for project in readable:
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
        # firmware whose project cannot be read (or is gone) stay usable: shown standalone
        known = {p.id for p in readable}
        standalone = tuple(
            rows[f.id]
            for f in sorted(everything, key=_by_name)
            if (f.project_id is None or f.project_id not in known) and matches(f)
        )
        return Tree(tuple(projects), standalone)

    def damaged(self) -> list[DamagedDocument]:
        """Project and firmware files the last tree() could not read (design doc §12.8)."""
        return self._ctx.projects.damaged() + self._ctx.firmware.damaged()

    def keep_damaged(self, damaged: list[DamagedDocument]) -> Path | None:
        """Copy each damaged file to data/backup/<timestamp>/damaged/ (once per content;
        the originals stay). Returns the folder of the copies, None if none was made."""
        copies = [keep_damaged_copy(d.path, self._ctx.paths.backup) for d in damaged]
        made = [c for c in copies if c is not None]
        return made[-1].parent if made else None

    def check_files(self, firmware_id: str) -> dict[str, FileState]:
        """Re-check every entry by name, path and hash (done when a firmware is opened and
        before flashing) and update the firmware's flag. Tracking firmware: Tracker.check()
        decides the flag and nothing is returned."""
        status = self._tracker.check(firmware_id)
        firmware = status.firmware
        if firmware.source.tracking:
            return {}
        return {
            e.id: _MARK_STATES.get(status.marks.get(e.id, EntryMark.NEW), FileState.OK)
            for e in firmware.entries
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
        copy.folder = source.folder
        copy.entries = [copy_entry(e) for e in source.entries]
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

    def preview_pio(self, build_dir: Path) -> PioPreview:
        """What importing the env folder `build_dir` (`<project>/.pio/build/<env>`) and its
        sibling envs would bring, for the import dialog. Blocking: runs
        PioConfigs.load() (about two seconds), so call it off the UI thread. EnvDirError
        propagates; pio problems go into config_error, env read errors into problem."""
        picked = env_ref(build_dir)
        root = picked.project_root
        pio = self._tracker.pio
        config = pio.load(root)
        manual = self.framework_pkg_default()
        choices: list[PioEnvChoice] = []
        for name in ini_envs(root):
            ref = EnvRef(root, name)
            built = is_built(ref)
            tracked = self.tracking_firmware_for_env(root, name)
            env_config = config.envs.get(name)
            build: PioBuild | None = None
            problem: str | None = None
            if built:
                try:
                    build = read_env(
                        ref, core=pio.core_dir, manual_package=manual, config=env_config
                    )
                except (PioBuildError, OSError) as exc:
                    problem = str(exc)
            choices.append(
                PioEnvChoice(
                    name=name,
                    built=built,
                    tracked_by=tracked.id if tracked is not None else None,
                    board=env_config.board if env_config is not None else None,
                    build=build,
                    problem=problem,
                )
            )
        return PioPreview(picked=picked, envs=tuple(choices), config_error=config.error)

    def import_pio(
        self,
        project_root: Path,
        envs: Sequence[str],
        *,
        project_id: str | None = None,
        device: str = "",
    ) -> list[Firmware]:
        """New PIO_BUILD firmware, one per env in `envs` (design doc §12.5): named after the
        env (made unique), build_ref = (project root, env), all in the same project and group
        (a project copies its defaults: reset modes and erase_all, as PIO builds give no reset
        modes, design doc §3.1), resolved at once with the first
        resolution accepted. All or nothing: AlreadyTrackedError when tracking_firmware_for_env()
        finds one of them followed already, PlanError (naming the env) when one cannot be
        resolved (unbuilt, incomplete, target switched); nothing is created then. ValueError
        for an empty `envs`."""
        if not envs:
            raise ValueError("no env to import")
        root = project_root.resolve()
        for env in envs:
            tracked = self.tracking_firmware_for_env(root, env)
            if tracked is not None:
                raise AlreadyTrackedError(
                    f"{root} env {env} is already followed by '{tracked.name}'", tracked.id
                )
        created: list[Firmware] = []
        try:
            for env in envs:
                ref = EnvRef(root, env)
                if not is_built(ref):
                    raise PlanError(f"{env}: not built (no .bin in {ref.build_dir})")
                if project_id is None:
                    firmware = self._ctx.firmware.create(env, source=Source.PIO_BUILD)
                else:
                    firmware = self._ctx.projects.create_firmware(
                        self._ctx.firmware, project_id, env, device=device, source=Source.PIO_BUILD
                    )
                created.append(firmware)
                firmware.build_ref = BuildRef(str(root), env)
                self._ctx.firmware.save(firmware)
                status = self._tracker.check(firmware.id, settle=False)
                if status.state != ResolveState.OK:
                    raise PlanError(
                        f"{env}: " + "; ".join(status.problems or ("cannot be resolved",))
                    )
        except BaseException:
            for firmware in created:
                self.delete_firmware(firmware.id)
            raise
        return [self._ctx.firmware.get(f.id) for f in created]

    def tracking_firmware_for_env(self, project_root: Path, env: str) -> Firmware | None:
        """The PIO tracking firmware following `env` of `project_root`, or None. Roots are
        compared as tracking_firmware_for() compares build directories; env names exactly."""
        key = _path_key(project_root)
        return next(
            (
                f
                for f in self._ctx.firmware.all()
                if f.source == Source.PIO_BUILD
                and f.build_ref is not None
                and f.build_ref.env == env
                and _path_key(Path(f.build_ref.path)) == key
            ),
            None,
        )

    def framework_pkg_default(self) -> str:
        """settings.ini [pio] framework_pkg; "" means auto-detect."""
        return self._ctx.settings.get(PIO_SECTION, FRAMEWORK_PKG_KEY) or ""

    def detected_framework_pkg(self) -> str:
        """The framework package auto-detection finds under the PIO core folder (no env
        preference), for the Settings placeholder; "" when there is none."""
        found = locate_package(manual="", listed=None, config=None, core=self._tracker.pio.core_dir)
        return str(found.path) if found.path is not None else ""

    def set_framework_pkg_default(self, path: str) -> None:
        """Store (stripped; "" removes the key) and re-check every PIO firmware that has no
        path of its own."""
        path = path.strip()
        if path:
            self._ctx.settings.set(PIO_SECTION, FRAMEWORK_PKG_KEY, path)
        else:
            self._ctx.settings.remove(PIO_SECTION, FRAMEWORK_PKG_KEY)
        self._ctx.settings.save()
        for firmware in self._ctx.firmware.all():
            if firmware.source == Source.PIO_BUILD and not firmware.framework_pkg:
                self._tracker.invalidate(firmware.id)

    def set_framework_pkg(self, firmware_id: str, path: str) -> Firmware:
        """The firmware's own framework package path (stripped; "" falls back to the global
        default). Saved and re-checked. ValueError for a firmware that is not PIO_BUILD."""
        firmware = self._ctx.firmware.get(firmware_id)
        if firmware.source != Source.PIO_BUILD:
            raise ValueError(f"{firmware.name} is not a PlatformIO firmware")
        firmware.framework_pkg = path.strip()
        self._ctx.firmware.save(firmware)
        self._tracker.invalidate(firmware_id)
        return firmware

    def mismatches(self, info: InfoEvent) -> dict[str, str]:
        """Firmware that do not fit the identified chip, by id, with the reason: no MCU of
        the firmware fits the chip and its revision (app.chips.fits()), or an enabled entry
        that fits has an image revision range that excludes the chip's revision (same rule
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
        self,
        paths: Sequence[Path],
        project_id: str | None = None,
        device: str = "",
        name: str = "",
    ) -> Firmware:
        """New fixed firmware (as create_firmware()) holding `paths`, named `name` or after
        the first file: files dropped on the sidebar (design doc §12 item 19). The folder
        rule is checked first: FolderError (or OSError for an unreadable file) and nothing is
        created."""
        if not paths:
            raise ValueError("no files to add")
        _check_folder(str(Path(paths[0]).resolve().parent), paths)
        for path in paths:
            path.read_bytes()  # unreadable files fail here, before anything is created
        stem = name.strip() or default_name_from_files([p.name for p in paths])
        firmware = self.create_firmware(stem, project_id, device)
        self.add_files(firmware.id, paths)
        return self._ctx.firmware.get(firmware.id)

    def add_files(self, firmware_id: str, paths: Sequence[Path]) -> list[Entry]:
        """Add one entry per file, classified by image_sniff. Partition tables are added
        first so the other files get addresses from them (or from the firmware's enabled
        table). When this adds a partition table with an otadata partition and no enabled
        otadata entry exists, a blank otadata entry is added too. Entries placed by
        partition are re-resolved. A firmware still named UNTITLED with no entries takes
        the first file's name (without extension). Returns the new entries. Folder rule
        (design doc §12 item 8): a firmware without a folder takes the folder of the first
        of `paths`; every file must be in that folder or below it, otherwise FolderError and
        nothing changes. Tracking firmware:
        TrackingEntriesError, nothing changes."""
        firmware = self._ctx.firmware.get(firmware_id)
        _refuse_tracking(firmware)
        if paths:
            firmware.folder = _check_folder(_root(firmware, Path(paths[0])), paths)
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
        firmware.folder = _check_folder(_root(firmware, path), [path])
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

    def set_entry_chip(
        self, firmware_id: str, entry_id: str, chip: str | None, line: str = ""
    ) -> Entry:
        """Choose the MCU of an entry whose file names no chip (None clears the choice and
        the line). `line` is the chip's revision line (chipinfo.revision_lines(); required
        for chips that have lines, "" for others). ValueError for an unknown chip or line,
        for an entry whose image header names a chip, and for tracking firmware
        (app.chips.chip_choosable())."""
        firmware = self._ctx.firmware.get(firmware_id)
        entry = _own_entry(firmware, entry_id)
        if entry is None or not chip_choosable(self._ctx, firmware, entry):
            raise ValueError(f"{firmware.name}: the MCU of this entry comes from its file")
        if chip is not None:
            try:
                chipinfo.get_chip(chip)
            except chipinfo.UnknownChipError:
                raise ValueError(f"unknown chip {chip!r}") from None
            lines = [ln.name for ln in chipinfo.revision_lines(chip)]
            if (line or lines) and line not in lines:
                raise ValueError(f"{chip}: choose one of the revision lines {lines}")
        entry.chip = chip
        entry.chip_line = line if chip is not None else ""
        self._save_entries(firmware)
        return entry

    def fit_entries(self, firmware_id: str, chip: str, revision: int | None = None) -> list[Entry]:
        """After a board with `chip` and `revision` was identified (design doc §12 item 13):
        untick and save every enabled entry of a fixed firmware that does not fit it
        (app.chips.fits(): another chip, another revision line, or no MCU).
        Nothing changes when `chip` is not among the firmware's chips (the whole firmware is
        for other boards: the sidebar dims it, the checklist refuses), nor for tracking
        firmware. Returns the entries that were unticked."""
        firmware = self._ctx.firmware.get(firmware_id)
        if firmware.source.tracking or chip not in firmware_chips(self._ctx, firmware):
            return []
        unticked = [
            e
            for e in firmware.entries
            if e.enabled and not fits(entry_mcu(self._ctx, firmware, e), chip, revision)
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
        """The folder of a fixed firmware stays, also when its first file is removed."""
        firmware = self._ctx.firmware.get(firmware_id)
        own = _own_entry(firmware, entry_id)
        if own is None:
            raise _derived_error(firmware)
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
        if entry.file.startswith("path:"):
            file = Path(entry.file.removeprefix("path:"))
            target.folder = _check_folder(_root(target, file), [file])
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
        if firmware.folder:
            _check_folder(firmware.folder, [path])
        relocate(entry, path)
        self._save_entries(firmware)
        self.check_files(firmware_id)
        return entry

    def relocate_folder(self, firmware_id: str, folder: Path) -> list[str]:
        """Move a fixed firmware's folder: every "path:" entry inside the old folder points
        at the same relative path under `folder` when that file exists there, and the
        folder becomes `folder`. Returns the names of entries left pointing at the old place
        (no such file under `folder`). ValueError for a firmware without a folder or a
        tracking one."""
        firmware = self._ctx.firmware.get(firmware_id)
        _refuse_tracking(firmware)
        if not firmware.folder:
            raise ValueError(f"{firmware.name} has no folder")
        old, new = Path(firmware.folder), folder.resolve()
        left: list[str] = []
        for entry in firmware.entries:
            current = Path(entry.file.removeprefix("path:"))
            if not entry.file.startswith("path:") or not in_folder(str(old), current):
                continue
            moved = new / current.resolve().relative_to(old.resolve())
            if moved.is_file():
                entry.file = f"path:{moved}"
            else:
                left.append(entry.name)
        firmware.folder = str(new)
        self._save_entries(firmware)
        self.check_files(firmware_id)
        return left

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
        mcus = firmware_mcus(self._ctx, firmware)
        revision = board_revision(state.major, state.minor)
        if mcus and not any(fits(m, state.chip, revision) for m in mcus):
            names = ", ".join(m.label for m in mcus)
            return f"firmware is for {names}, the board is {state.chip} {_revision_text(state)}"
        if revision is None:  # secure download mode: the revision was not read
            return None
        for entry in sorted_entries([e for e in firmware.entries if e.enabled]):
            header = image_header(self._ctx, entry)
            mcu = entry_mcu(self._ctx, firmware, entry)
            if header is None or not fits(mcu, state.chip, revision):  # others are left out
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
            mcus=tuple(m.label for m in firmware_mcus(self._ctx, firmware)),
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
        return ARCHIVE_ORIGIN
    if firmware.build_ref is not None:
        env = firmware.build_ref.env
        return f"{firmware.build_ref.path} (env: {env})" if env else firmware.build_ref.path
    return firmware.folder


def _root(firmware: Firmware, first_new: Path) -> str:
    """The folder files of a fixed firmware must be in: its folder, else the folder of the
    first file being added (design doc §12 item 8)."""
    return firmware.folder or str(first_new.resolve().parent)


def _check_folder(folder: str, paths: Sequence[Path]) -> str:
    """`folder` when every path is in it or below it, else FolderError."""
    outside = [p for p in paths if not in_folder(folder, p)]
    if outside:
        raise FolderError(folder, outside)
    return folder


def _revision_text(state: ChipState) -> str:
    return f"v{state.major}.{state.minor}" if state.major >= 0 else ""


def _refuse_tracking(firmware: Firmware) -> None:
    if firmware.source.tracking:
        raise TrackingEntriesError(
            f"entries of '{firmware.name}' come from its build directory; archive this "
            "version or create a fixed firmware to add files"
        )


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _own_entry(firmware: Firmware, entry_id: str) -> Entry | None:
    """The firmware's stored entry: any entry of a fixed firmware; None for the derived
    entries of a tracking one."""
    return None if firmware.source.tracking else _entry(firmware, entry_id)


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
