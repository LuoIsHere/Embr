"""core/package: the .embr zip (design doc §5 阶段四, §12.6) and in-memory document upgrades.

Packages are written with write_package() and then taken apart and rebuilt with zipfile
to get malformed ones. The manifest's key names are not part of the interface; the
helpers that edit it look for values (a file's sha256 and size, the format version)
rather than for fixed keys.
"""

import hashlib
import tomllib
import warnings
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import tomli_w

from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import (
    FIRMWARE_SCHEMA_VERSION,
    AddressSource,
    Entry,
    Firmware,
    Source,
)
from embr.core.firmware.serialize import firmware_from_doc, firmware_to_doc
from embr.core.firmware.store import upgrade_firmware_doc
from embr.core.package import (
    MANIFEST_NAME,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_SUFFIX,
    Manifest,
    NewerPackageError,
    PackageError,
    PackageFile,
    PackageItem,
    read_manifest,
    read_package,
    write_package,
)
from embr.core.project import (
    PROJECT_SCHEMA_VERSION,
    Project,
    ProjectDefaults,
    project_from_doc,
    project_to_doc,
    upgrade_project_doc,
)
from embr.core.storage.documents import Document, NewerDocumentError, upgrade_document
from embr.core.storage.ids import new_id

EXPORTED_AT = "2026-10-31T12:00:00"
STAMP = "2026-10-01T09:30:00"
UNLISTED_SHA = "0" * 64

Members = list[tuple[str, bytes]]


# ---- sample package ---------------------------------------------------------------------


@dataclass
class Sample:
    project: Project
    firmwares: list[Firmware]
    files: list[tuple[PackageFile, Path]]
    temp: Path

    @property
    def shas(self) -> list[str]:
        return [f.sha256 for f, _ in self.files]


def _file(folder: Path, name: str, data: bytes, note: str = "") -> tuple[PackageFile, Path]:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return PackageFile(hashlib.sha256(data).hexdigest(), len(data), name, note), path


def _archived_entry(
    file: PackageFile,
    kind: EntryKind,
    address: int,
    *,
    source: AddressSource = AddressSource.AUTO,
    partition: str | None = None,
    enabled: bool = True,
    note: str = "",
) -> Entry:
    return Entry(
        id=new_id(),
        name=file.original_name,
        file=f"archive:{file.sha256}",
        sha256=file.sha256,
        size=file.size,
        kind=kind,
        address=address,
        auto_address=address,
        address_source=source,
        partition=partition,
        enabled=enabled,
        note=note,
    )


def _blank_entry(address: int, size: int) -> Entry:
    return Entry(
        id=new_id(),
        name="otadata (blank)",
        file="blank:",
        sha256=hashlib.sha256(b"\xff" * size).hexdigest(),
        size=size,
        kind=EntryKind.OTADATA,
        address=address,
        auto_address=address,
        address_source=AddressSource.AUTO,
        auto_added=True,
    )


def _sample(tmp_path: Path) -> Sample:
    """A project with groups (one empty) and one firmware in it, plus a loose firmware
    keeping its group, sharing the app file: three files, one blank entry."""
    source = tmp_path / "source"
    app = _file(source, "app.bin", b"\xe9app" * 1000, "main app")
    assets = _file(source, "assets.bin", b"\x01" * 4096, "assets")
    calib = _file(source, "calib.bin", b"\x02" * 4096)
    project = Project(
        id=new_id(),
        name="Alpha",
        notes="project notes",
        tags=["lab"],
        devices=["board", "spare"],
        defaults=ProjectDefaults("no-reset", "watchdog-reset", True),
        created_at=STAMP,
        updated_at=STAMP,
    )
    in_project = Firmware(
        id=new_id(),
        name="main",
        project_id=project.id,
        device="board",
        source=Source.ARCHIVED,
        notes="fw notes",
        tags=["release"],
        erase_all=True,
        entries=[
            _archived_entry(app[0], EntryKind.APP, 0x10000, note="main app"),
            _archived_entry(
                assets[0],
                EntryKind.ATTACHMENT,
                0x1A000,
                source=AddressSource.PARTITION,
                partition="assets",
            ),
            _blank_entry(0xD000, 0x2000),
            _archived_entry(
                calib[0],
                EntryKind.ATTACHMENT,
                0x3FF000,
                source=AddressSource.MANUAL,
                enabled=False,
                note="calibration",
            ),
        ],
        archived_from="main, archived 2026-10-01T09:30:00",
        created_at=STAMP,
        updated_at=STAMP,
    )
    loose = Firmware(
        id=new_id(),
        name="loose",
        project_id=None,
        device="spare",
        source=Source.ARCHIVED,
        entries=[_archived_entry(app[0], EntryKind.APP, 0x10000)],
        created_at=STAMP,
        updated_at=STAMP,
    )
    temp = tmp_path / "temp"
    temp.mkdir()
    return Sample(project, [in_project, loose], [app, assets, calib], temp)


def _write(sample: Sample, destination: Path) -> Manifest:
    return write_package(
        destination,
        projects=[sample.project],
        firmwares=sample.firmwares,
        files=sample.files,
        embr_version="0.1.0",
        esptool_version="5.0.0",
        exported_at=EXPORTED_AT,
        temp=sample.temp,
    )


@pytest.fixture
def sample(tmp_path: Path) -> Sample:
    return _sample(tmp_path)


def _package(sample: Sample, tmp_path: Path) -> Path:
    destination = tmp_path / f"alpha{PACKAGE_SUFFIX}"
    _write(sample, destination)
    return destination


# ---- zip helpers ------------------------------------------------------------------------


def _members(path: Path) -> Members:
    with zipfile.ZipFile(path) as archive:
        return [(info.filename, archive.read(info)) for info in archive.infolist()]


def _rezip(path: Path, members: Members) -> None:
    """Write `members` exactly as named (no separator conversion), duplicates allowed."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about duplicate names
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members:
                info = zipfile.ZipInfo("placeholder", date_time=(2026, 10, 31, 12, 0, 0))
                info.filename = name  # ZipInfo() would turn backslashes into slashes
                archive.writestr(info, data)


def _toml(data: bytes) -> dict[str, Any]:
    return tomllib.loads(data.decode("utf-8"))


def _replace(members: Members, name: str, data: bytes) -> Members:
    assert name in [n for n, _ in members], name
    return [(n, data if n == name else d) for n, d in members]


def _drop(members: Members, name: str) -> Members:
    assert name in [n for n, _ in members], name
    return [(n, d) for n, d in members if n != name]


def _member(members: Members, name: str) -> bytes:
    return next(d for n, d in members if n == name)


def _edit_toml(members: Members, name: str, change: Callable[[dict[str, Any]], None]) -> Members:
    doc = _toml(_member(members, name))
    change(doc)
    return _replace(members, name, tomli_w.dumps(doc).encode("utf-8"))


def _set_format_version(doc: dict[str, Any], value: object) -> None:
    """Set the manifest's format version: the one top-level integer key naming a format."""
    keys = [
        k
        for k, v in doc.items()
        if "format" in k and isinstance(v, int) and not isinstance(v, bool)
    ]
    assert len(keys) == 1, f"no single format version key in the manifest: {sorted(doc)}"
    doc[keys[0]] = value


def _bump_listed_size(doc: dict[str, Any], sha: str) -> None:
    """Add 1 to the size the manifest lists for `sha` (the integer next to it)."""
    bumped = 0

    def bump(table: dict[str, object]) -> None:
        nonlocal bumped
        for key, value in table.items():
            if isinstance(value, int) and not isinstance(value, bool):
                table[key] = value + 1
                bumped += 1

    def walk(node: object) -> None:
        if isinstance(node, dict):
            table = cast(dict[str, object], node)
            if sha in table.values():
                bump(table)
            inner = table.get(sha)
            if isinstance(inner, dict):
                bump(cast(dict[str, object], inner))
            for value in list(table.values()):
                walk(value)
        elif isinstance(node, list):
            for item in cast(list[object], node):
                walk(item)

    walk(doc)
    assert bumped == 1, f"no single size listed for {sha}"


def _files_under(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()


# ---- write and read ---------------------------------------------------------------------


def test_round_trip_keeps_projects_firmware_and_files(sample: Sample, tmp_path: Path) -> None:
    destination = tmp_path / "out" / "alpha.embr"
    destination.parent.mkdir()
    written = _write(sample, destination)

    project = sample.project
    in_project, loose = sample.firmwares
    assert written.format_version == PACKAGE_FORMAT_VERSION
    assert (written.embr_version, written.esptool_version, written.exported_at) == (
        "0.1.0",
        "5.0.0",
        EXPORTED_AT,
    )
    assert written.projects == (PackageItem(project.id, "Alpha"),)
    assert written.firmwares == (
        PackageItem(in_project.id, "main", project.id),
        PackageItem(loose.id, "loose", None),
    )
    assert set(written.files) == {f for f, _ in sample.files}
    assert read_manifest(destination) == written

    folder = tmp_path / "read"
    package = read_package(destination, folder)
    assert package.manifest == written
    assert package.folder == folder
    assert [project_to_doc(p) for p in package.projects] == [project_to_doc(project)]
    assert [firmware_to_doc(f) for f in package.firmwares] == [
        firmware_to_doc(f) for f in sample.firmwares
    ]
    assert not any(f.read_only for f in package.firmwares)
    assert set(package.files) == set(sample.shas)
    for file, source in sample.files:
        copy = package.files[file.sha256]
        assert copy.is_relative_to(folder)
        assert copy.read_bytes() == source.read_bytes()


def test_package_layout_holds_each_file_once(sample: Sample, tmp_path: Path) -> None:
    package_file = _package(sample, tmp_path)
    names = [n for n, _ in _members(package_file)]
    expected = {
        MANIFEST_NAME,
        f"projects/{sample.project.id}.toml",
        *(f"firmwares/{f.id}.toml" for f in sample.firmwares),
        *(f"files/{sha}.bin" for sha in sample.shas),
    }
    assert sorted(names) == sorted(expected)  # the shared app file is stored once
    members = _members(package_file)
    for sha, (_, source) in zip(sample.shas, sample.files, strict=True):
        assert _member(members, f"files/{sha}.bin") == source.read_bytes()
    project_doc = _toml(_member(members, f"projects/{sample.project.id}.toml"))
    assert project_doc["schema_version"] == PROJECT_SCHEMA_VERSION
    for firmware in sample.firmwares:
        doc = _toml(_member(members, f"firmwares/{firmware.id}.toml"))
        assert doc["schema_version"] == FIRMWARE_SCHEMA_VERSION


def test_write_package_replaces_an_existing_destination(sample: Sample, tmp_path: Path) -> None:
    destination = tmp_path / "alpha.embr"
    destination.write_bytes(b"an older package")
    _write(sample, destination)
    assert read_manifest(destination).projects == (PackageItem(sample.project.id, "Alpha"),)


def test_discard_removes_the_folder(sample: Sample, tmp_path: Path) -> None:
    package_file = _package(sample, tmp_path)
    folder = tmp_path / "read"
    package = read_package(package_file, folder)
    assert folder.is_dir()
    package.discard()
    assert not folder.exists()


# ---- write_package refusals -------------------------------------------------------------


def _with_path_entry(sample: Sample) -> None:
    source = sample.files[0][1]
    sample.firmwares[0].entries[0].file = f"path:{source.resolve()}"


def _with_unknown_file(sample: Sample) -> None:
    sample.files.pop(2)  # calib.bin is still referenced


def _with_wrong_size(sample: Sample) -> None:
    file, path = sample.files[1]
    sample.files[1] = (PackageFile(file.sha256, file.size + 1, file.original_name), path)


def _with_wrong_sha(sample: Sample) -> None:
    file, path = sample.files[1]
    other = path.with_name("other.bin")
    other.write_bytes(b"" * file.size)  # same size, other content
    sample.files[1] = (file, other)


def _with_unknown_project(sample: Sample) -> None:
    sample.firmwares[1].project_id = new_id()


REFUSED_WRITES: dict[str, Callable[[Sample], None]] = {
    "path_entry": _with_path_entry,
    "file_not_in_files": _with_unknown_file,
    "size_mismatch": _with_wrong_size,
    "sha256_mismatch": _with_wrong_sha,
    "unknown_project": _with_unknown_project,
}


@pytest.mark.parametrize("case", REFUSED_WRITES)
def test_write_package_refuses_and_leaves_no_destination(
    sample: Sample, tmp_path: Path, case: str
) -> None:
    REFUSED_WRITES[case](sample)
    destination = tmp_path / "alpha.embr"
    with pytest.raises(ValueError):
        _write(sample, destination)
    assert not destination.exists()


def test_failed_write_keeps_the_previous_destination(sample: Sample, tmp_path: Path) -> None:
    destination = tmp_path / "alpha.embr"
    destination.write_bytes(b"previous package")
    _with_path_entry(sample)
    with pytest.raises(ValueError):
        _write(sample, destination)
    assert destination.read_bytes() == b"previous package"


# ---- read_package refusals --------------------------------------------------------------


def _flip_file(members: Members, s: Sample) -> Members:
    name = f"files/{s.shas[0]}.bin"
    data = bytearray(_member(members, name))
    data[-1] ^= 0xFF
    return _replace(members, name, bytes(data))


def _drop_file(members: Members, s: Sample) -> Members:
    return _drop(members, f"files/{s.shas[1]}.bin")


def _wrong_size(members: Members, s: Sample) -> Members:
    return _edit_toml(members, MANIFEST_NAME, lambda d: _bump_listed_size(d, s.shas[2]))


def _broken_manifest(members: Members, s: Sample) -> Members:
    return _replace(members, MANIFEST_NAME, b"format_version = [unterminated")


def _no_manifest(members: Members, s: Sample) -> Members:
    return _drop(members, MANIFEST_NAME)


def _drop_project_doc(members: Members, s: Sample) -> Members:
    return _drop(members, f"projects/{s.project.id}.toml")


def _drop_firmware_doc(members: Members, s: Sample) -> Members:
    return _drop(members, f"firmwares/{s.firmwares[1].id}.toml")


def _firmware_id_mismatch(members: Members, s: Sample) -> Members:
    name = f"firmwares/{s.firmwares[0].id}.toml"
    return _edit_toml(members, name, lambda d: d.update(id=new_id()))


def _project_id_mismatch(members: Members, s: Sample) -> Members:
    name = f"projects/{s.project.id}.toml"
    return _edit_toml(members, name, lambda d: d.update(id=new_id()))


def _firmware_of_unlisted_project(members: Members, s: Sample) -> Members:
    name = f"firmwares/{s.firmwares[1].id}.toml"
    return _edit_toml(members, name, lambda d: d.update(project_id=new_id()))


def _first_archive_entry(doc: dict[str, Any]) -> dict[str, Any]:
    entries = cast(list[dict[str, Any]], doc["entries"])
    return next(e for e in entries if str(e["file"]).startswith("archive:"))


def _entry_to_unlisted_file(members: Members, s: Sample) -> Members:
    def change(doc: dict[str, Any]) -> None:
        _first_archive_entry(doc).update(file=f"archive:{UNLISTED_SHA}", sha256=UNLISTED_SHA)

    return _edit_toml(members, f"firmwares/{s.firmwares[0].id}.toml", change)


def _entry_to_local_path(members: Members, s: Sample) -> Members:
    def change(doc: dict[str, Any]) -> None:
        _first_archive_entry(doc).update(file="path:C:/firmware/app.bin")

    return _edit_toml(members, f"firmwares/{s.firmwares[1].id}.toml", change)


def _duplicate_file_member(members: Members, s: Sample) -> Members:
    name = f"files/{s.shas[0]}.bin"
    return [*members, (name, _member(members, name))]  # even with the same content


def _duplicate_manifest(members: Members, s: Sample) -> Members:
    return [*members, (MANIFEST_NAME, _member(members, MANIFEST_NAME))]


def _duplicate_firmware_doc(members: Members, s: Sample) -> Members:
    name = f"firmwares/{s.firmwares[0].id}.toml"
    return [*members, (name, _member(members, name))]


def _format_version_zero(members: Members, s: Sample) -> Members:
    return _edit_toml(members, MANIFEST_NAME, lambda d: _set_format_version(d, 0))


TAMPERED: dict[str, Callable[[Members, Sample], Members]] = {
    "file_content_changed": _flip_file,
    "file_missing": _drop_file,
    "listed_size_wrong": _wrong_size,
    "manifest_not_toml": _broken_manifest,
    "manifest_missing": _no_manifest,
    "project_document_missing": _drop_project_doc,
    "firmware_document_missing": _drop_firmware_doc,
    "firmware_id_mismatch": _firmware_id_mismatch,
    "project_id_mismatch": _project_id_mismatch,
    "firmware_of_unlisted_project": _firmware_of_unlisted_project,
    "entry_refers_to_unlisted_file": _entry_to_unlisted_file,
    "entry_refers_to_local_path": _entry_to_local_path,
    "duplicate_file_member": _duplicate_file_member,
    "duplicate_manifest": _duplicate_manifest,
    "duplicate_firmware_document": _duplicate_firmware_doc,
    "format_version_zero": _format_version_zero,
}


@pytest.mark.parametrize("case", TAMPERED)
def test_tampered_package_is_rejected_as_a_whole(sample: Sample, tmp_path: Path, case: str) -> None:
    package_file = _package(sample, tmp_path)
    _rezip(package_file, TAMPERED[case](_members(package_file), sample))
    folder = tmp_path / "read"
    before = _files_under(tmp_path)

    with pytest.raises(PackageError) as raised:
        read_package(package_file, folder)

    assert not isinstance(raised.value, NewerPackageError)
    assert not folder.exists()  # created, emptied and removed again
    assert _files_under(tmp_path) == before


def test_file_that_is_not_a_zip_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "broken.embr"
    source.write_bytes(b"this is not a zip file")
    with pytest.raises(PackageError):
        read_manifest(source)
    with pytest.raises(PackageError):
        read_package(source, tmp_path / "read")
    assert not (tmp_path / "read").exists()


def test_missing_source_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        read_package(tmp_path / "missing.embr", tmp_path / "read")
    assert not (tmp_path / "read").exists()


def test_read_manifest_rejects_a_broken_manifest(sample: Sample, tmp_path: Path) -> None:
    package_file = _package(sample, tmp_path)
    _rezip(package_file, _broken_manifest(_members(package_file), sample))
    with pytest.raises(PackageError):
        read_manifest(package_file)


# ---- newer packages ---------------------------------------------------------------------


def _newer_format(members: Members, s: Sample) -> Members:
    def change(doc: dict[str, Any]) -> None:
        _set_format_version(doc, PACKAGE_FORMAT_VERSION + 1)

    return _edit_toml(members, MANIFEST_NAME, change)


def _newer_project(members: Members, s: Sample) -> Members:
    name = f"projects/{s.project.id}.toml"
    return _edit_toml(members, name, lambda d: d.update(schema_version=PROJECT_SCHEMA_VERSION + 1))


def _newer_firmware(members: Members, s: Sample) -> Members:
    name = f"firmwares/{s.firmwares[1].id}.toml"
    return _edit_toml(members, name, lambda d: d.update(schema_version=FIRMWARE_SCHEMA_VERSION + 1))


NEWER: dict[str, Callable[[Members, Sample], Members]] = {
    "format_version": _newer_format,
    "project_schema": _newer_project,
    "firmware_schema": _newer_firmware,
}


@pytest.mark.parametrize("case", NEWER)
def test_newer_package_is_rejected(sample: Sample, tmp_path: Path, case: str) -> None:
    package_file = _package(sample, tmp_path)
    _rezip(package_file, NEWER[case](_members(package_file), sample))
    folder = tmp_path / "read"
    with pytest.raises(NewerPackageError):
        read_package(package_file, folder)
    assert not folder.exists()


def test_read_manifest_rejects_a_newer_format(sample: Sample, tmp_path: Path) -> None:
    package_file = _package(sample, tmp_path)
    _rezip(package_file, _newer_format(_members(package_file), sample))
    with pytest.raises(NewerPackageError):
        read_manifest(package_file)


# ---- names inside the zip ---------------------------------------------------------------

HOSTILE_NAMES = (
    "../evil_dotdot.bin",
    "files/../../evil_nested.bin",
    "projects/../../evil_project.toml",
    "/evil_root_slash.bin",
    "C:\\evil_drive_backslash.bin",
    "C:/evil_drive_slash.bin",
    "..\\evil_dotdot_backslash.bin",
    "files\\..\\..\\evil_nested_backslash.bin",
    "notes/readme.txt",
    "projects/readme.txt",
)


def _outside_places(name: str) -> list[Path]:
    """Places outside the test folder a careless extraction of `name` could write to."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return [Path("/") / base, Path("C:/") / base, Path.cwd() / base, Path.cwd().parent / base]


def test_hostile_member_names_are_ignored(sample: Sample, tmp_path: Path) -> None:
    package_file = _package(sample, tmp_path)
    members = _members(package_file)
    _rezip(package_file, [*members, *((n, b"evil " + n.encode()) for n in HOSTILE_NAMES)])
    folder = tmp_path / "deep" / "er" / "read"
    before = _files_under(tmp_path)

    package = read_package(package_file, folder)

    assert [firmware_to_doc(f) for f in package.firmwares] == [
        firmware_to_doc(f) for f in sample.firmwares
    ]
    # nothing is extracted under a name taken from the zip: only the checked copies
    assert _files_under(folder) == set(package.files.values())
    assert _files_under(tmp_path) - _files_under(folder) == before
    for name in HOSTILE_NAMES:
        for place in _outside_places(name):
            assert not place.exists(), place
    package.discard()


def test_package_of_hostile_names_only_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "evil.embr"
    _rezip(source, [(n, b"evil") for n in HOSTILE_NAMES])
    before = _files_under(tmp_path)
    with pytest.raises(PackageError):
        read_package(source, tmp_path / "read")
    assert _files_under(tmp_path) == before
    for name in HOSTILE_NAMES:
        for place in _outside_places(name):
            assert not place.exists(), place


# ---- document upgrades ------------------------------------------------------------------


def test_upgrade_document_migrates_step_by_step() -> None:
    steps: list[int] = []

    def v1_to_v2(doc: Document) -> Document:
        steps.append(1)
        return {k: v for k, v in doc.items() if k != "old"} | {"mid": doc["old"]}

    def v2_to_v3(doc: Document) -> Document:
        steps.append(2)
        return doc | {"added": True}

    migrations = {1: v1_to_v2, 2: v2_to_v3}
    result = upgrade_document({"schema_version": 1, "old": "x"}, current=3, migrations=migrations)
    assert result == {"schema_version": 3, "mid": "x", "added": True}
    assert steps == [1, 2]

    steps.clear()
    result = upgrade_document({"schema_version": 2, "mid": "y"}, current=3, migrations=migrations)
    assert result == {"schema_version": 3, "mid": "y", "added": True}
    assert steps == [2]


def test_upgrade_document_keeps_a_current_document() -> None:
    def never(doc: Document) -> Document:
        raise AssertionError("no migration for a current document")

    doc: Document = {"schema_version": 2, "name": "x"}
    assert upgrade_document(doc, current=2, migrations={1: never, 2: never}) == {
        "schema_version": 2,
        "name": "x",
    }


def test_upgrade_document_refuses_a_newer_document() -> None:
    with pytest.raises(NewerDocumentError) as raised:
        upgrade_document({"schema_version": 4}, current=3, migrations={})
    assert isinstance(raised.value, ValueError)


@pytest.mark.parametrize("version", [None, "1", True, 1.0, 0, -1])
def test_upgrade_document_refuses_an_invalid_version(version: object) -> None:
    doc: Document = {"name": "x"} if version is None else {"schema_version": version}
    with pytest.raises(ValueError) as raised:
        upgrade_document(doc, current=1, migrations={})
    assert not isinstance(raised.value, NewerDocumentError)


def _project() -> Project:
    return Project(
        id=new_id(),
        name="Alpha",
        notes="n",
        tags=["t"],
        devices=["board", "spare"],
        created_at=STAMP,
        updated_at=STAMP,
    )


def test_upgrade_project_doc() -> None:
    project = _project()
    doc = project_to_doc(project) | {"schema_version": PROJECT_SCHEMA_VERSION}
    upgraded = upgrade_project_doc(doc)
    assert upgraded["schema_version"] == PROJECT_SCHEMA_VERSION
    assert project_from_doc(upgraded) == project
    with pytest.raises(NewerDocumentError):
        upgrade_project_doc(doc | {"schema_version": PROJECT_SCHEMA_VERSION + 1})
    with pytest.raises(ValueError):
        upgrade_project_doc(project_to_doc(project))  # no schema_version
    with pytest.raises(ValueError):
        upgrade_project_doc(doc | {"schema_version": 0})


def test_upgrade_firmware_doc(sample: Sample) -> None:
    firmware = sample.firmwares[0]
    doc = firmware_to_doc(firmware) | {"schema_version": FIRMWARE_SCHEMA_VERSION}
    upgraded = upgrade_firmware_doc(doc)
    assert upgraded["schema_version"] == FIRMWARE_SCHEMA_VERSION
    assert firmware_to_doc(firmware_from_doc(upgraded)) == firmware_to_doc(firmware)
    with pytest.raises(NewerDocumentError):
        upgrade_firmware_doc(doc | {"schema_version": FIRMWARE_SCHEMA_VERSION + 1})
    with pytest.raises(ValueError):
        upgrade_firmware_doc(firmware_to_doc(firmware))  # no schema_version
    with pytest.raises(ValueError):
        upgrade_firmware_doc(doc | {"schema_version": 0})
