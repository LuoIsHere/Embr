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

import datetime
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from embr.app.context import AppContext
from embr.app.tracking import Tracker
from embr.core.package import Package


@dataclass(frozen=True)
class Blocked:
    """A firmware that cannot be exported now."""

    firmware_id: str
    name: str
    reason: str


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
        raise NotImplementedError

    def export_project(self, project_id: str, destination: Path) -> None:
        """The project (name, notes, tags, groups including empty ones, defaults) and all
        its firmware into one package. PlanError naming every blocked firmware when any
        cannot be exported (nothing is written); KeyError for an unknown project."""
        raise NotImplementedError

    def export_firmware(self, firmware_ids: Sequence[str], destination: Path) -> None:
        """The firmware as loose firmware (no project in the package; each keeps its group
        name in `device`). ValueError when the list is empty or spans more than one scope;
        PlanError naming every blocked firmware when any cannot be exported (nothing is
        written)."""
        raise NotImplementedError

    def open(self, source: Path) -> Package:
        """Read and check a package (core.package.read_package) into a new folder under
        temp/. The caller discards it (Package.discard) when done. PackageError /
        NewerPackageError / OSError as read_package()."""
        raise NotImplementedError

    def import_package(
        self, package: Package, file_name: str, placement: Placement
    ) -> ImportResult:
        """Write a checked package: files into the archive, then new projects (with their
        groups) and firmware. Loose firmware go to `placement`; an unknown placement
        project is KeyError, a group not in that project is ValueError (no group is
        created). When writing fails part way, the projects and firmware created so far
        are deleted again before the error propagates (archive files stay; they are
        content-addressed and cleaned by "Clean archive")."""
        raise NotImplementedError


def default_group(package: Package, project_groups: Sequence[str]) -> str:
    """Default group for the loose firmware of `package` in a project with these groups:
    the group they all had, when they had the same one and the project has it; else ""
    (ungrouped)."""
    raise NotImplementedError


def default_package_name(name: str) -> str:
    """File name for a package exported under `name` (a project or firmware name):
    characters Windows does not allow in file names replaced by "_", trailing dots and
    spaces removed, PACKAGE_SUFFIX added; "package.embr" when nothing is left."""
    raise NotImplementedError
