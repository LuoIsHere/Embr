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

import contextlib
import hashlib
import os
import re
import shutil
import tomllib
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import tomli_w

from embr.core.firmware.model import FIRMWARE_SCHEMA_VERSION, Firmware
from embr.core.firmware.serialize import firmware_from_doc, firmware_to_doc
from embr.core.firmware.store import upgrade_firmware_doc
from embr.core.project import (
    PROJECT_SCHEMA_VERSION,
    Project,
    project_from_doc,
    project_to_doc,
    upgrade_project_doc,
)
from embr.core.storage.documents import (
    Document,
    Migration,
    NewerDocumentError,
    doc_opt_str,
    doc_str,
    doc_tables,
)
from embr.core.storage.ids import is_id, new_id
from embr.i18n import N_

PACKAGE_FORMAT_VERSION = 1
"""Version of the package layout and manifest; independent of the schema_version of the
project and firmware documents inside."""

PACKAGE_SUFFIX = ".embr"
MANIFEST_NAME = "manifest.toml"

_MIGRATIONS: dict[int, Migration] = {}
"""migrations[n] upgrades a manifest from format version n to n + 1."""

_PROJECT = re.compile(r"projects/([^/]+)\.toml")
_FIRMWARE = re.compile(r"firmwares/([^/]+)\.toml")
_FILE = re.compile(r"files/([0-9a-f]{64})\.bin")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ARCHIVE = "archive:"
_BLANK = "blank:"
_CHUNK = 1 << 20


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
        shutil.rmtree(self.folder, ignore_errors=True)


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
    project_ids = {p.id for p in projects}
    listed = {f.sha256: f for f, _ in files}
    for firmware in firmwares:
        if firmware.project_id is not None and firmware.project_id not in project_ids:
            raise ValueError(f"{firmware.name}: its project is not in the package")
        if firmware.source.tracking:
            raise ValueError(f"{firmware.name}: a tracking firmware cannot go into a package")
        for entry in firmware.entries:
            if entry.file == _BLANK:
                continue
            if not entry.file.startswith(_ARCHIVE):
                raise ValueError(f"{entry.name}: only archive: and blank: entries can be packed")
            if entry.file.removeprefix(_ARCHIVE) not in listed:
                raise ValueError(f"{entry.name}: its file is not in the package")
    for listed_file, path in files:
        if _hash_file(path) != (listed_file.sha256, listed_file.size):
            raise ValueError(f"{listed_file.original_name}: {path.name} does not match")
    manifest = Manifest(
        format_version=PACKAGE_FORMAT_VERSION,
        embr_version=embr_version,
        esptool_version=esptool_version,
        exported_at=exported_at,
        projects=tuple(PackageItem(p.id, p.name) for p in projects),
        firmwares=tuple(PackageItem(f.id, f.name, f.project_id) for f in firmwares),
        files=tuple(listed.values()),
    )
    sources = {f.sha256: path for f, path in files}
    temp.mkdir(parents=True, exist_ok=True)
    partial = temp / f"{new_id()}{PACKAGE_SUFFIX}.part"
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_NAME, tomli_w.dumps(_manifest_to_doc(manifest)))
            for project in projects:
                doc = {"schema_version": PROJECT_SCHEMA_VERSION, **project_to_doc(project)}
                zf.writestr(f"projects/{project.id}.toml", tomli_w.dumps(doc))
            for firmware in firmwares:
                doc = {"schema_version": FIRMWARE_SCHEMA_VERSION, **firmware_to_doc(firmware)}
                zf.writestr(f"firmwares/{firmware.id}.toml", tomli_w.dumps(doc))
            for sha, path in sources.items():
                zf.write(path, f"files/{sha}.bin")
        _place(partial, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
    return manifest


def read_manifest(source: Path) -> Manifest:
    """Only the manifest (for showing what a package holds before reading it in full).
    Raises PackageError / NewerPackageError as read_package() does for the manifest."""
    with _open_zip(source) as zf:
        return _read_manifest(zf, _members(zf))


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
    folder.mkdir(parents=True)
    try:
        with _open_zip(source) as zf:
            members = _members(zf)
            manifest = _read_manifest(zf, members)
            projects = [
                _parse(project_from_doc, _document(zf, members.projects, p, upgrade_project_doc))
                for p in manifest.projects
            ]
            firmwares = [
                _parse(firmware_from_doc, _document(zf, members.firmwares, f, upgrade_firmware_doc))
                for f in manifest.firmwares
            ]
            _check_firmwares(manifest, firmwares)
            files = {f.sha256: _extract(zf, members.files, f, folder) for f in manifest.files}
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return Package(manifest, projects, firmwares, files, folder)


@dataclass
class _Members:
    manifest: zipfile.ZipInfo | None
    projects: dict[str, zipfile.ZipInfo]
    firmwares: dict[str, zipfile.ZipInfo]
    files: dict[str, zipfile.ZipInfo]


def _open_zip(source: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(source)
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise PackageError(N_(f"{source.name} is not an Embr package ({exc})")) from exc


def _members(zf: zipfile.ZipFile) -> _Members:
    """The expected members by id / sha256; everything else is ignored. An expected name
    that appears twice rejects the package."""
    members = _Members(None, {}, {}, {})
    seen: set[str] = set()
    for info in zf.infolist():
        name = info.filename
        table: dict[str, zipfile.ZipInfo] | None = None
        key = name
        if info.is_dir():
            continue
        if name != MANIFEST_NAME:
            for pattern, candidate in (
                (_PROJECT, members.projects),
                (_FIRMWARE, members.firmwares),
                (_FILE, members.files),
            ):
                if m := pattern.fullmatch(name):
                    table, key = candidate, m.group(1)
                    break
            else:
                continue
        if name in seen:
            raise PackageError(N_(f"{name} appears more than once in the package"))
        seen.add(name)
        if table is None:
            members.manifest = info
        else:
            table[key] = info
    return members


def _read_manifest(zf: zipfile.ZipFile, members: _Members) -> Manifest:
    if members.manifest is None:
        raise PackageError(N_(f"{MANIFEST_NAME} is missing from the package"))
    try:
        doc: Document = tomllib.loads(_read(zf, members.manifest).decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PackageError(N_(f"{MANIFEST_NAME} cannot be read ({exc})")) from exc
    version = doc.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PackageError(N_(f"{MANIFEST_NAME}: missing or invalid format_version"))
    if version > PACKAGE_FORMAT_VERSION:
        raise NewerPackageError(
            N_(f"package format {version} is newer than this Embr supports; update Embr")
        )
    for step in range(version, PACKAGE_FORMAT_VERSION):
        if step not in _MIGRATIONS:
            raise PackageError(N_(f"package format {step} is not supported"))
        doc = _MIGRATIONS[step](doc)
    return _manifest_from_doc({**doc, "format_version": PACKAGE_FORMAT_VERSION})


def _manifest_to_doc(manifest: Manifest) -> Document:
    def item(i: PackageItem) -> Document:
        d: Document = {"id": i.id, "name": i.name}
        if i.project_id is not None:
            d["project_id"] = i.project_id
        return d

    return {
        "format_version": manifest.format_version,
        "embr_version": manifest.embr_version,
        "esptool_version": manifest.esptool_version,
        "exported_at": manifest.exported_at,
        "projects": [item(p) for p in manifest.projects],
        "firmwares": [item(f) for f in manifest.firmwares],
        "files": [
            {"sha256": f.sha256, "size": f.size, "original_name": f.original_name, "note": f.note}
            for f in manifest.files
        ],
    }


def _manifest_from_doc(doc: Document) -> Manifest:
    """Strict: the version fields are strings, every listed item has its keys with the
    right types, nothing is listed twice."""
    for key in ("embr_version", "esptool_version", "exported_at"):
        if not isinstance(doc.get(key), str):
            raise PackageError(N_(f"{MANIFEST_NAME}: missing {key}"))
    for key in ("projects", "firmwares", "files"):
        value = doc.get(key, [])
        if not isinstance(value, list) or len(doc_tables(doc, key)) != len(
            cast(list[object], value)
        ):
            raise PackageError(N_(f"{MANIFEST_NAME}: invalid {key}"))
    projects = tuple(_item(d, "projects") for d in doc_tables(doc, "projects"))
    firmwares = tuple(_item(d, "firmwares") for d in doc_tables(doc, "firmwares"))
    if any(p.project_id is not None for p in projects):
        raise PackageError(N_(f"{MANIFEST_NAME}: a project cannot belong to a project"))
    files: list[PackageFile] = []
    for d in doc_tables(doc, "files"):
        sha, size = d.get("sha256"), d.get("size")
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise PackageError(N_(f"{MANIFEST_NAME}: invalid sha256 in files"))
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise PackageError(N_(f"{MANIFEST_NAME}: invalid size for {sha}"))
        files.append(PackageFile(sha, size, doc_str(d, "original_name"), doc_str(d, "note")))
    for message, keys in (
        (N_(f"{MANIFEST_NAME}: a project is listed twice"), [p.id for p in projects]),
        (N_(f"{MANIFEST_NAME}: a firmware is listed twice"), [f.id for f in firmwares]),
        (N_(f"{MANIFEST_NAME}: a file is listed twice"), [f.sha256 for f in files]),
    ):
        if len(set(keys)) != len(keys):
            raise PackageError(message)
    return Manifest(
        format_version=PACKAGE_FORMAT_VERSION,
        embr_version=doc_str(doc, "embr_version"),
        esptool_version=doc_str(doc, "esptool_version"),
        exported_at=doc_str(doc, "exported_at"),
        projects=projects,
        firmwares=firmwares,
        files=tuple(files),
    )


def _item(doc: Document, key: str) -> PackageItem:
    item_id, name, project_id = doc.get("id"), doc.get("name"), doc.get("project_id")
    if not isinstance(item_id, str) or not is_id(item_id) or not isinstance(name, str):
        raise PackageError(N_(f"{MANIFEST_NAME}: invalid item in {key}"))
    if project_id is not None and not isinstance(project_id, str):
        raise PackageError(N_(f"{MANIFEST_NAME}: invalid project_id in {key}"))
    return PackageItem(item_id, name, doc_opt_str(doc, "project_id"))


def _document(
    zf: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    item: PackageItem,
    upgrade: Callable[[Document], Document],
) -> Document:
    info = members.get(item.id)
    if info is None:
        raise PackageError(N_(f"{item.name}: its document is missing from the package"))
    try:
        doc: Document = tomllib.loads(_read(zf, info).decode("utf-8"))
        doc = upgrade(doc)
    except NewerDocumentError as exc:
        raise NewerPackageError(N_(f"{item.name} was written by a newer Embr ({exc})")) from exc
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise PackageError(N_(f"{info.filename} cannot be read ({exc})")) from exc
    if doc.get("id") != item.id:
        raise PackageError(N_(f"{info.filename}: id does not match the manifest"))
    return doc


def _parse[T](parse: Callable[[Document], T], doc: Document) -> T:
    """A model from a document, with bad values (unknown kinds, sources, ...) as
    PackageError."""
    try:
        return parse(doc)
    except (ValueError, KeyError, TypeError) as exc:
        raise PackageError(
            N_(f"{doc_str(doc, 'name') or doc_str(doc, 'id')}: invalid value ({exc})")
        ) from exc


def _check_firmwares(manifest: Manifest, firmwares: list[Firmware]) -> None:
    projects = {p.id for p in manifest.projects}
    files = {f.sha256: f for f in manifest.files}
    for item, firmware in zip(manifest.firmwares, firmwares, strict=True):
        if firmware.project_id != item.project_id:
            raise PackageError(N_(f"{item.name}: its project does not match the manifest"))
        if firmware.project_id is not None and firmware.project_id not in projects:
            raise PackageError(N_(f"{item.name}: its project is not in the package"))
        if firmware.source.tracking:
            raise PackageError(N_(f"{item.name}: a tracking firmware cannot come from a package"))
        for entry in firmware.entries:
            if entry.file == _BLANK:
                continue
            listed = files.get(entry.file.removeprefix(_ARCHIVE))
            if not entry.file.startswith(_ARCHIVE) or listed is None:
                raise PackageError(
                    N_(f"{item.name}: {entry.name} refers to a file not in the package")
                )
            if (entry.sha256, entry.size) != (listed.sha256, listed.size):
                raise PackageError(N_(f"{item.name}: {entry.name} does not match its file"))


def _extract(
    zf: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], listed: PackageFile, folder: Path
) -> Path:
    """Copy one file out as folder/<sha256>.bin, checking size and sha256 on the way (never
    reading more than the listed size)."""
    name = listed.original_name or listed.sha256
    info = members.get(listed.sha256)
    if info is None:
        raise PackageError(N_(f"{name}: missing from the package"))
    if info.file_size != listed.size:
        raise PackageError(N_(f"{name}: size does not match the manifest"))
    target = folder / f"{listed.sha256}.bin"
    digest = hashlib.sha256()
    size = 0
    try:
        with zf.open(info) as src, target.open("wb") as out:
            while chunk := src.read(_CHUNK):
                size += len(chunk)
                if size > listed.size:
                    break
                digest.update(chunk)
                out.write(chunk)
    except (zipfile.BadZipFile, EOFError, RuntimeError) as exc:
        raise PackageError(N_(f"{name}: cannot be read from the package ({exc})")) from exc
    if size != listed.size or digest.hexdigest() != listed.sha256:
        raise PackageError(N_(f"{name}: content does not match the manifest"))
    return target


def _read(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        return zf.read(info)
    except (zipfile.BadZipFile, EOFError, RuntimeError) as exc:
        raise PackageError(N_(f"{info.filename} cannot be read ({exc})")) from exc


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _place(partial: Path, destination: Path) -> None:
    """Move the finished zip to `destination`; across drives through a .part copy next to
    it, so the destination only ever holds a complete package."""
    try:
        os.replace(partial, destination)
    except OSError:
        staged = destination.with_name(destination.name + ".part")
        try:
            shutil.copyfile(partial, staged)
            os.replace(staged, destination)
        finally:
            with contextlib.suppress(FileNotFoundError):
                staged.unlink()
