""".embr export and import use cases (design doc §5 阶段四, §12.6).

Export: a whole project, or one or more firmware of one scope (one project, or the
standalone firmware). Each firmware goes in as it would be archived now ("归档此版本"):
tracking firmware as their current resolution (derived entries with overrides plus user
entries), as fixed firmware; "path:" entries become "archive:<sha256>"; build directory,
framework package, overrides and the last resolution are left out. Firmware that is not
flashable now (missing, incomplete, target switched, needs confirmation, a file changed)
or read-only cannot be exported.

Import: core.package.read_package() checks the whole package first; only then are files
put into the archive (existing content not stored twice, a name record added) and new
projects and firmware created with new ids, names made unique (design doc §3.1). Every
imported firmware is ARCHIVED with archived_from "Imported from <file name>, <time>".
Projects of the package come back as new projects with their groups; loose firmware go
where the import dialog says (a project and group, or standalone).
"""

import contextlib
import dataclasses
import datetime
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import esptool

from embr import __version__
from embr.app.chips import image_chip
from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.tracking import STATE_TEXT, Tracker
from embr.core.firmware.model import Entry, Firmware, FlashParams, Source
from embr.core.package import PACKAGE_SUFFIX, Package, PackageFile, read_package, write_package
from embr.core.project import Project
from embr.core.storage.ids import new_id
from embr.i18n import N_

_ARCHIVE = "archive:"
_PATH = "path:"
_NOT_IN_FILE_NAMES = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class Blocked:
    """A firmware that cannot be exported now."""

    firmware_id: str
    name: str
    reason: str
    """Short, translatable (tracking.STATE_TEXT or "written by a newer Embr")."""
    details: tuple[str, ...] = ()
    """The problems or changes behind it, as the change list shows them."""


@dataclass(frozen=True)
class ExportCheck:
    exportable: tuple[str, ...]
    """Ids of the firmware that can be exported, in the order asked."""
    blocked: tuple[Blocked, ...]
    with_dump: tuple[str, ...]
    """Names of exportable firmware with an entry read from a device (from_dump): the
    package may hold Wi-Fi credentials or other data of that device."""


@dataclass(frozen=True)
class Placement:
    """Where loose firmware of a package go: a project and group ("" = ungrouped), or
    standalone (project_id None, device ignored)."""

    project_id: str | None
    device: str = ""


@dataclass(frozen=True)
class ImportResult:
    project_ids: tuple[str, ...]
    """New projects, in package order."""
    firmware_ids: tuple[str, ...]
    """New firmware, in package order."""


class Packager:
    def __init__(
        self,
        context: AppContext,
        tracker: Tracker,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        self._ctx = context
        self._tracker = tracker
        self._clock = clock

    def check_export(self, firmware_ids: Sequence[str]) -> ExportCheck:
        """Which of the firmware can be exported now and why the others cannot (the
        reasons archiving would give, or "written by a newer Embr"). KeyError for an
        unknown id."""
        exportable: list[str] = []
        blocked: list[Blocked] = []
        with_dump: list[str] = []
        for firmware_id in firmware_ids:
            firmware = self._ctx.firmware.get(firmware_id)
            if firmware.read_only:
                reason = N_("written by a newer Embr")
                blocked.append(Blocked(firmware_id, firmware.name, reason))
                continue
            status = self._tracker.check(firmware_id)
            if not status.state.flashable:
                details = status.problems or tuple(c.message for c in status.changes)
                reason = STATE_TEXT[status.state]
                blocked.append(Blocked(firmware_id, firmware.name, reason, details))
                continue
            exportable.append(firmware_id)
            if any(e.from_dump for e in status.firmware.entries):
                with_dump.append(firmware.name)
        return ExportCheck(tuple(exportable), tuple(blocked), tuple(with_dump))

    def export_project(self, project_id: str, destination: Path) -> None:
        """The project (name, notes, tags, groups including empty ones, defaults) and all
        its firmware into one package. PlanError naming every blocked firmware when any
        cannot be exported (nothing is written); KeyError for an unknown project."""
        project = self._ctx.projects.get(project_id)
        firmware_ids = [f.id for f in self._ctx.firmware.in_scope(project_id)]
        self._export([project], firmware_ids, destination, project_id=project_id)

    def export_firmware(self, firmware_ids: Sequence[str], destination: Path) -> None:
        """The firmware as loose firmware (no project in the package; each keeps its group
        name in `device`). ValueError when the list is empty or spans more than one scope;
        PlanError naming every blocked firmware when any cannot be exported (nothing is
        written)."""
        if not firmware_ids:
            raise ValueError("no firmware to export")
        scopes = {self._ctx.firmware.get(i).project_id for i in firmware_ids}
        if len(scopes) > 1:
            raise ValueError("the firmware to export must be in the same project")
        self._export([], list(dict.fromkeys(firmware_ids)), destination, project_id=None)

    def open(self, source: Path) -> Package:
        """Read and check a package (core.package.read_package) into a new folder under
        temp/. The caller discards it (Package.discard) when done. PackageError /
        NewerPackageError / OSError as read_package()."""
        return read_package(source, self._ctx.paths.temp / f"package-{new_id()}")

    def import_package(
        self, package: Package, file_name: str, placement: Placement
    ) -> ImportResult:
        """Write a checked package: files into the archive, then new projects (with their
        groups) and firmware. Loose firmware go to `placement`; an unknown placement
        project is KeyError, a group not in that project is ValueError (no group is
        created). When writing fails part way, the projects and firmware created so far
        are deleted again before the error propagates (archive files stay; they are
        content-addressed and cleaned by "Clean archive")."""
        if placement.project_id is not None:
            target = self._ctx.projects.get(placement.project_id)
            if placement.device and placement.device not in target.devices:
                raise ValueError(f"{target.name} has no group {placement.device}")
        stamp = self._clock().isoformat(timespec="seconds")
        origin = f"Imported from {file_name}, {stamp}"
        projects: dict[str, str] = {}
        firmware_ids: list[str] = []
        try:
            self._archive_files(package)
            for project in package.projects:
                projects[project.id] = self._create_project(project).id
            for firmware in package.firmwares:
                if firmware.project_id is None:
                    project_id = placement.project_id
                    device = placement.device if project_id is not None else ""
                else:
                    project_id, device = projects[firmware.project_id], firmware.device
                    if device and device not in self._ctx.projects.get(project_id).devices:
                        self._ctx.projects.add_group(project_id, device)
                created = self._create_firmware(firmware, project_id, device, origin)
                firmware_ids.append(created.id)
        except BaseException:
            for firmware_id in firmware_ids:
                with contextlib.suppress(KeyError, OSError):
                    self._ctx.firmware.delete(firmware_id)
            for project_id in projects.values():
                with contextlib.suppress(KeyError, OSError):
                    self._ctx.projects.delete(project_id, self._ctx.firmware, delete_firmware=True)
            raise
        return ImportResult(tuple(projects.values()), tuple(firmware_ids))

    def _export(
        self,
        projects: list[Project],
        firmware_ids: list[str],
        destination: Path,
        *,
        project_id: str | None,
    ) -> None:
        check = self.check_export(firmware_ids)
        if check.blocked:
            names = "; ".join(f"{b.name}: {b.reason}" for b in check.blocked)
            raise PlanError(f"Cannot export: {names}")
        firmwares: list[Firmware] = []
        files: dict[str, tuple[PackageFile, Path]] = {}
        for firmware_id in firmware_ids:
            resolved = self._tracker.check(firmware_id).firmware
            entries = [self._packed_entry(e, resolved, files) for e in resolved.entries]
            firmwares.append(_packed_firmware(resolved, entries, project_id))
        try:
            write_package(
                destination,
                projects=projects,
                firmwares=firmwares,
                files=list(files.values()),
                embr_version=__version__,
                esptool_version=esptool.__version__,
                exported_at=self._clock().isoformat(timespec="seconds"),
                temp=self._ctx.paths.temp,
            )
        except ValueError as exc:  # a file changed after the check
            raise PlanError(f"Cannot export: {exc}") from exc

    def _packed_entry(
        self, entry: Entry, firmware: Firmware, files: dict[str, tuple[PackageFile, Path]]
    ) -> Entry:
        """A copy referring to "archive:<sha256>"; its file recorded in `files` (first
        name and note win). A tracking firmware's chip stays with files that name none, as
        archiving does."""
        copy = dataclasses.replace(entry)
        if firmware.source.tracking and copy.chip is None and image_chip(self._ctx, entry) is None:
            copy.chip = firmware.chip
        if entry.file.startswith(_PATH):
            source = Path(entry.file.removeprefix(_PATH))
        elif entry.file.startswith(_ARCHIVE):
            source = self._ctx.archive.path(entry.file.removeprefix(_ARCHIVE))
        else:
            return copy
        copy.file = _ARCHIVE + entry.sha256
        files.setdefault(
            entry.sha256, (PackageFile(entry.sha256, entry.size, entry.name, entry.note), source)
        )
        return copy

    def _archive_files(self, package: Package) -> None:
        users: dict[str, str] = {}
        for firmware in package.firmwares:
            for entry in firmware.entries:
                users.setdefault(entry.file.removeprefix(_ARCHIVE), firmware.name)
        for listed in package.manifest.files:
            self._ctx.archive.add(
                package.files[listed.sha256],
                original_name=listed.original_name,
                note=listed.note,
                source_firmware=users.get(listed.sha256, ""),
            )

    def _create_project(self, project: Project) -> Project:
        created = self._ctx.projects.create(
            project.name, notes=project.notes, defaults=dataclasses.replace(project.defaults)
        )
        created.tags = list(project.tags)
        created.devices = list(project.devices)
        self._ctx.projects.save(created)
        return created

    def _create_firmware(
        self, firmware: Firmware, project_id: str | None, device: str, origin: str
    ) -> Firmware:
        created = self._ctx.firmware.create(
            firmware.name,
            source=Source.ARCHIVED,
            project_id=project_id,
            device=device,
            before=firmware.before,
            after=firmware.after,
            erase_all=firmware.erase_all,
        )
        created.notes = firmware.notes
        created.tags = list(firmware.tags)
        created.flash = dataclasses.replace(firmware.flash)
        created.entries = [dataclasses.replace(e) for e in firmware.entries]
        created.archived_from = origin
        self._ctx.firmware.save(created)
        return created


def _packed_firmware(resolved: Firmware, entries: list[Entry], project_id: str | None) -> Firmware:
    return Firmware(
        id=resolved.id,
        name=resolved.name,
        project_id=project_id,
        device=resolved.device,
        source=Source.ARCHIVED,
        notes=resolved.notes,
        tags=list(resolved.tags),
        flash=FlashParams(resolved.flash.mode, resolved.flash.freq, resolved.flash.size),
        before=resolved.before,
        after=resolved.after,
        erase_all=resolved.erase_all,
        entries=entries,
        archived_from=resolved.archived_from,
        created_at=resolved.created_at,
        updated_at=resolved.updated_at,
    )


def default_group(package: Package, project_groups: Sequence[str]) -> str:
    """Default group for the loose firmware of `package` in a project with these groups:
    the group they all had, when they had the same one and the project has it; else ""
    (ungrouped)."""
    groups = {f.device for f in package.firmwares if f.project_id is None}
    if len(groups) == 1 and (group := groups.pop()) in project_groups:
        return group
    return ""


def default_package_name(name: str) -> str:
    """File name for a package exported under `name` (a project or firmware name):
    characters Windows does not allow in file names replaced by "_", trailing dots and
    spaces removed, PACKAGE_SUFFIX added; "package.embr" when nothing is left."""
    stem = _NOT_IN_FILE_NAMES.sub("_", name).rstrip(" .")
    return (stem or "package") + PACKAGE_SUFFIX
