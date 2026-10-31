"""The .embr package (design doc §5 阶段四, §12.6): a zip file holding projects, firmware
and every file they refer to, for moving them to another computer.

    manifest.toml
    projects/<id>.toml
    firmwares/<id>.toml
    files/<sha256>.bin

Entries refer to files only as "archive:<sha256>" (or "blank:"); a package never holds a
local path. Reading validates everything before anything is handed out: a package that
fails any check is rejected as a whole. Only the expected names are read; other members,
and names that would point outside the package, are ignored. Nothing is ever extracted
under a name taken from the zip.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from embr.core.firmware.model import Firmware
from embr.core.project import Project
from embr.core.storage.documents import Migration

PACKAGE_FORMAT_VERSION = 1
"""Version of the package layout and manifest; independent of the schema_version of the
project and firmware documents inside."""

PACKAGE_SUFFIX = ".embr"
MANIFEST_NAME = "manifest.toml"

_MIGRATIONS: dict[int, Migration] = {}
"""migrations[n] upgrades a manifest from format version n to n + 1."""


class PackageError(Exception):
    """The file is not a valid package (unreadable zip, bad manifest, a file missing or not
    matching the manifest, a reference to a file the package does not hold, ...). The
    message names the first problem found."""


class NewerPackageError(PackageError):
    """The package format version, or the schema_version of a project or firmware in it,
    is higher than this Embr supports."""


@dataclass(frozen=True)
class PackageFile:
    """One file of files/ as listed in the manifest."""

    sha256: str
    size: int
    original_name: str
    note: str = ""


@dataclass(frozen=True)
class PackageItem:
    """A project or firmware as listed in the manifest. project_id: the project a firmware
    belongs to inside the package; None for projects and loose firmware."""

    id: str
    name: str
    project_id: str | None = None


@dataclass(frozen=True)
class Manifest:
    format_version: int
    embr_version: str
    esptool_version: str
    exported_at: str
    """ISO 8601 local time with seconds."""
    projects: tuple[PackageItem, ...]
    firmwares: tuple[PackageItem, ...]
    files: tuple[PackageFile, ...]


@dataclass
class Package:
    """A package that passed every check, with its files copied out to `folder`."""

    manifest: Manifest
    projects: list[Project]
    """In manifest order, documents upgraded to the current schema."""
    firmwares: list[Firmware]
    """In manifest order, documents upgraded to the current schema; project_id is a
    project of this package or None (loose firmware; `device` keeps the group it had)."""
    files: dict[str, Path]
    """sha256 -> its copy under `folder` (the content checked against the manifest)."""
    folder: Path

    def discard(self) -> None:
        """Remove `folder` and the copies in it."""
        raise NotImplementedError


def write_package(
    destination: Path,
    *,
    projects: Sequence[Project],
    firmwares: Sequence[Firmware],
    files: Sequence[tuple[PackageFile, Path]],
    embr_version: str,
    esptool_version: str,
    exported_at: str,
    temp: Path,
) -> Manifest:
    """Write a package to `destination` (replaced if it exists). The zip is built in
    `temp` and moved into place only when complete, so a failure never leaves a partial
    package at the destination. Projects and firmware are written as given (ids, names,
    groups, entries); a firmware's project_id must be one of `projects` or None. Every
    "archive:" entry must be in `files`, whose sha256 and size are those of the source
    path. Raises ValueError for a "path:" entry, an entry whose file is not in `files`, a
    firmware of an unknown project, or a source file that does not match its PackageFile;
    OSError when a file cannot be read or the destination written. Returns the manifest
    written."""
    raise NotImplementedError


def read_manifest(source: Path) -> Manifest:
    """Only the manifest (for showing what a package holds before reading it in full).
    Raises PackageError / NewerPackageError as read_package() does for the manifest."""
    raise NotImplementedError


def read_package(source: Path, folder: Path) -> Package:
    """Read and check a whole package, copying its files into `folder` (created; left
    empty and removed again on failure). Checks, any of which rejects the package:
    - a readable zip with manifest.toml that parses; format version supported (lower ->
      migrated, higher -> NewerPackageError);
    - every listed project and firmware has its document, whose id matches; documents
      upgraded to the current schema_version (higher -> NewerPackageError);
    - a firmware's project_id is a listed project or absent;
    - every listed file is present with the listed size and sha256;
    - every entry refers to "archive:<sha256>" of a listed file, or is "blank:";
    - no expected member appears twice.
    Raises PackageError (or OSError when `source` cannot be read)."""
    raise NotImplementedError
