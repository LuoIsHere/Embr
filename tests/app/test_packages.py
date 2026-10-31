"""app/packages: .embr export and import (design doc §5 阶段四, §12.6). Packages go from
the `context` data folder to `other`, a second data folder standing in for another
computer, unless a test needs both ends in one folder."""

import datetime
import hashlib
import re
import shutil
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import esptool
import pytest
import tomli_w
from app_testkit import (
    REPO,
    Clock,
    FakePio,
    build_files,
    copy_files,
    fixed_firmware,
    flip_last_byte,
    idf_project,
    make_package,
    move_app,
    pio_build,
    pio_configs,
    pio_project,
    root_first,
)

import embr
from embr.app.archiving import Archiver
from embr.app.chips import firmware_chips
from embr.app.context import AppContext, open_context
from embr.app.flashplan import PlanError
from embr.app.library import Library
from embr.app.packages import (
    ImportResult,
    Packager,
    Placement,
    default_group,
    default_package_name,
)
from embr.app.tracking import Tracker
from embr.core.firmware.import_pio import BOOT_APP0_IN_PACKAGE
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import (
    FIRMWARE_SCHEMA_VERSION,
    AddressSource,
    Entry,
    Firmware,
    FlashParams,
    Source,
)
from embr.core.firmware.store import FirmwareStore
from embr.core.firmware.tracking import ResolveState
from embr.core.package import (
    MANIFEST_NAME,
    PACKAGE_FORMAT_VERSION,
    Manifest,
    NewerPackageError,
    Package,
    PackageError,
    read_manifest,
)
from embr.core.project import ProjectDefaults
from embr.core.storage import paths as storage_paths
from embr.core.storage.ids import new_id
from embr.core.storage.paths import AppPaths

NOW = datetime.datetime(2026, 10, 31, 12, 0, 0)
ATTACHMENTS = REPO / "tests" / "fixtures" / "synthetic" / "attachments"
HELLO_APP = "app:hello_esp32s3.bin"
"""Derived entry id of the esp32s3_hello app."""


@dataclass
class Rig:
    context: AppContext
    tracker: Tracker
    library: Library
    packager: Packager


def _rig(context: AppContext) -> Rig:
    tracker = Tracker(context, clock=Clock())
    return Rig(
        context, tracker, Library(context, tracker), Packager(context, tracker, clock=lambda: NOW)
    )


@pytest.fixture
def rig(context: AppContext) -> Rig:
    return _rig(context)


@pytest.fixture
def other(context: AppContext, tmp_path: Path) -> Iterator[Rig]:
    """A second data folder ("another computer")."""
    saved = tempfile.tempdir
    paths = AppPaths(tmp_path / "other")
    storage_paths.prepare(paths)
    ctx = open_context(paths)
    yield _rig(ctx)
    ctx.close()
    tempfile.tempdir = saved


# ---- firmware to export -----------------------------------------------------------------


def _full_firmware(
    rig: Rig, folder: Path, project_id: str | None, device: str = "", name: str = "ota"
) -> Firmware:
    """Fixed firmware with the esp32s3_ota_custom build (blank otadata added), an
    attachment placed by partition and a disabled one placed by address, a manual app
    address and a note, files copied to `folder`."""
    files = copy_files(build_files("esp32s3_ota_custom"), folder)
    assets, calib = copy_files(
        [ATTACHMENTS / "assets_fits.bin", ATTACHMENTS / "calib_at_address.bin"], folder
    )
    library = rig.library
    firmware = library.create_firmware(name, project_id, device)
    library.add_files(firmware.id, root_first(files))
    library.add_attachment(firmware.id, assets, partition="assets")
    calibration = library.add_attachment(firmware.id, calib, address=0x3FF000)
    library.set_entry_enabled(firmware.id, calibration.id, False)
    library.set_entry_note(firmware.id, calibration.id, "calibration")
    app = next(e for e in library.firmware(firmware.id).entries if e.kind == EntryKind.APP)
    library.set_entry_address(firmware.id, app.id, "0x30000")
    library.set_entry_note(firmware.id, app.id, "main app")
    return library.firmware(firmware.id)


def _tracking_firmware(
    rig: Rig, folder: Path, project_id: str | None, device: str = ""
) -> tuple[Firmware, Path]:
    """IDF tracking firmware of esp32s3_hello copied to `folder`, with a manual app
    address, an app note and the bootloader disabled. Returns it and its build dir."""
    build = idf_project("esp32s3_hello", folder)
    firmware = rig.library.import_idf(build, project_id=project_id, device=device)
    rig.library.set_entry_address(firmware.id, HELLO_APP, "0x20000")
    rig.library.set_entry_note(firmware.id, HELLO_APP, "tracked app")
    resolved = rig.tracker.check(firmware.id).firmware
    boot = next(e for e in resolved.entries if e.kind == EntryKind.BOOTLOADER)
    rig.library.set_entry_enabled(firmware.id, boot.id, False)
    return rig.library.firmware(firmware.id), build


def _resolved_entries(rig: Rig, firmware_id: str) -> list[Entry]:
    """What an export holds: the current resolution (stored entries for fixed firmware)."""
    return rig.tracker.check(firmware_id).firmware.entries


def _view(entry: Entry) -> tuple[object, ...]:
    """Everything an entry keeps through export and import, except its file reference and
    its chip (see test_tracking_firmware_is_exported_as_fixed)."""
    return (
        entry.kind,
        entry.address,
        entry.auto_address,
        entry.address_source,
        entry.partition,
        entry.enabled,
        entry.note,
        entry.sha256,
        entry.size,
        entry.from_dump,
        entry.auto_added,
        entry.file == "blank:",
    )


def _views(entries: list[Entry]) -> dict[str, tuple[object, ...]]:
    views = {e.name: _view(e) for e in entries}
    assert len(views) == len(entries), "entry names repeat"
    return views


def _make_read_only(context: AppContext, firmware_id: str) -> None:
    """As if the firmware had been written by a newer Embr."""
    file = context.paths.firmwares / f"{firmware_id}.toml"
    doc = tomllib.loads(file.read_text(encoding="utf-8"))
    doc["schema_version"] = FIRMWARE_SCHEMA_VERSION + 1
    file.write_text(tomli_w.dumps(doc), encoding="utf-8")


def _import(rig: Rig, source: Path, placement: Placement) -> ImportResult:
    package = rig.packager.open(source)
    try:
        return rig.packager.import_package(package, source.name, placement)
    finally:
        package.discard()


def _get(rig: Rig, ids: tuple[str, ...]) -> list[Firmware]:
    return [rig.context.firmware.get(i) for i in ids]


def _files_under(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@dataclass(frozen=True)
class State:
    projects: list[str]
    firmwares: list[str]
    archive: list[str]
    temp: list[str]
    archive_files: list[tuple[str, int]]
    archive_names: int


def _state(rig: Rig) -> State:
    paths = rig.context.paths
    names = rig.context.database.execute("SELECT COUNT(*) FROM archive_names")
    return State(
        projects=_files_under(paths.projects),
        firmwares=_files_under(paths.firmwares),
        archive=_files_under(paths.archive),
        temp=_files_under(paths.temp),
        archive_files=[(f.sha256, f.size) for f in rig.context.archive.files()],
        archive_names=int(names.fetchone()[0]),
    )


# ---- export a project, import it elsewhere ----------------------------------------------


def _alpha(rig: Rig, tmp_path: Path) -> tuple[str, dict[str, tuple[str, list[Entry]]]]:
    """Project "Alpha": groups board, spare (empty) and other; notes, tags, defaults; a
    full fixed firmware in board, a tracking one in other and an empty ungrouped one.
    Returns the project id and, by firmware name, the group and entries each firmware
    should arrive with."""
    library = rig.library
    project = library.create_project("Alpha", "project notes")
    for group in ("board", "spare", "other"):
        library.add_group(project.id, group)
    library.set_project_defaults(project.id, ProjectDefaults("no-reset", "watchdog-reset", True))
    stored = rig.context.projects.get(project.id)
    stored.tags = ["lab"]
    rig.context.projects.save(stored)
    fixed = _full_firmware(rig, tmp_path / "ota", project.id, "board")
    tracked, _ = _tracking_firmware(rig, tmp_path / "hello", project.id, "other")
    empty = library.create_firmware("empty", project.id)
    expected: dict[str, tuple[str, list[Entry]]] = {
        fixed.name: ("board", _resolved_entries(rig, fixed.id)),
        tracked.name: ("other", _resolved_entries(rig, tracked.id)),
        empty.name: ("", []),
    }
    return project.id, expected


def test_exported_project_comes_back_whole(rig: Rig, other: Rig, tmp_path: Path) -> None:
    project_id, expected = _alpha(rig, tmp_path)
    original = rig.context.projects.get(project_id)
    original_ids = {f.id for f in rig.context.firmware.all()}
    destination = tmp_path / "alpha.embr"

    rig.packager.export_project(project_id, destination)
    result = _import(other, destination, Placement(None))

    (new_project,) = result.project_ids
    assert new_project != project_id
    project = other.context.projects.get(new_project)
    assert (project.name, project.notes, project.tags, project.devices, project.defaults) == (
        "Alpha",
        "project notes",
        ["lab"],
        ["board", "spare", "other"],  # the empty group too, in order
        original.defaults,
    )
    assert len(result.firmware_ids) == 3
    assert not set(result.firmware_ids) & original_ids
    imported = {f.name: f for f in _get(other, result.firmware_ids)}
    assert {name: (f.project_id, f.device) for name, f in imported.items()} == {
        name: (new_project, device) for name, (device, _) in expected.items()
    }
    for name, (_, entries) in expected.items():
        firmware = imported[name]
        assert firmware.source == Source.ARCHIVED
        assert firmware.archived_from.startswith("Imported from alpha.embr, ")
        assert _views(firmware.entries) == _views(entries)
        for entry in firmware.entries:
            assert entry.file in ("blank:", f"archive:{entry.sha256}")
        for entry in entries:  # file contents identical, attachments included
            if entry.file.startswith("path:"):
                source = Path(entry.file.removeprefix("path:"))
                assert other.context.archive.path(entry.sha256).read_bytes() == source.read_bytes()
    for firmware_id in result.firmware_ids:  # everything needed came along
        assert other.tracker.check(firmware_id).state is ResolveState.OK


def test_export_project_leaves_the_source_alone(rig: Rig, tmp_path: Path) -> None:
    project_id, _ = _alpha(rig, tmp_path)
    before = {f.id: (f.entry_overrides, f.build_ref, f.entries) for f in rig.context.firmware.all()}
    temp = _files_under(rig.context.paths.temp)

    rig.packager.export_project(project_id, tmp_path / "alpha.embr")

    after = {f.id: (f.entry_overrides, f.build_ref, f.entries) for f in rig.context.firmware.all()}
    assert after == before
    assert _files_under(rig.context.paths.temp) == temp  # the zip was moved out of temp/


def test_export_records_versions_and_time(rig: Rig, tmp_path: Path) -> None:
    firmware = fixed_firmware(rig.library, copy_files(build_files("esp32s3_hello"), tmp_path / "f"))
    destination = tmp_path / "fw.embr"
    rig.packager.export_firmware([firmware.id], destination)

    manifest = read_manifest(destination)
    assert manifest.format_version == PACKAGE_FORMAT_VERSION
    assert (manifest.embr_version, manifest.esptool_version) == (
        embr.__version__,
        esptool.__version__,
    )
    assert manifest.exported_at == NOW.isoformat(timespec="seconds")
    assert manifest.projects == ()
    assert [(f.id, f.name, f.project_id) for f in manifest.firmwares] == [
        (firmware.id, firmware.name, None)
    ]


def test_export_keeps_firmware_settings(rig: Rig, other: Rig, tmp_path: Path) -> None:
    firmware = fixed_firmware(rig.library, copy_files(build_files("esp32s3_hello"), tmp_path / "f"))
    for name, value in (
        ("flash_mode", "qio"),
        ("flash_freq", "40m"),
        ("flash_size", "4MB"),
        ("before", "no-reset"),
        ("after", "watchdog-reset"),
        ("erase_all", True),
        ("tags", ["release"]),
        ("notes", "first release"),
    ):
        rig.library.set_param(firmware.id, name, value)
    destination = tmp_path / "fw.embr"

    rig.packager.export_firmware([firmware.id], destination)
    (imported,) = _get(other, _import(other, destination, Placement(None)).firmware_ids)

    assert (imported.flash, imported.before, imported.after, imported.erase_all) == (
        FlashParams("qio", "40m", "4MB"),
        "no-reset",
        "watchdog-reset",
        True,
    )
    assert (imported.tags, imported.notes) == (["release"], "first release")


# ---- tracking firmware ------------------------------------------------------------------


def test_tracking_firmware_is_exported_as_fixed(rig: Rig, tmp_path: Path) -> None:
    tracked, build = _tracking_firmware(rig, tmp_path / "hello", None)
    resolved = rig.tracker.check(tracked.id).firmware
    files = {p: p.read_bytes() for p in build.rglob("*") if p.is_file()}
    destination = tmp_path / "hello.embr"

    rig.packager.export_firmware([tracked.id], destination)
    package = rig.packager.open(destination)
    try:
        (exported,) = package.firmwares
        assert not exported.source.tracking
        assert (
            exported.build_ref,
            exported.entry_overrides,
            exported.last_resolved,
            exported.framework_pkg,
            exported.folder,
        ) == (None, {}, None, "", "")
        assert _views(exported.entries) == _views(resolved.entries)
        app = next(e for e in exported.entries if e.kind == EntryKind.APP)
        assert (app.address, app.address_source, app.note) == (
            0x20000,
            AddressSource.MANUAL,
            "tracked app",
        )
        boot = next(e for e in exported.entries if e.kind == EntryKind.BOOTLOADER)
        assert not boot.enabled
        assert exported.flash == resolved.flash
        result = rig.packager.import_package(package, destination.name, Placement(None))
    finally:
        package.discard()

    (imported,) = _get(rig, result.firmware_ids)
    assert imported.source == Source.ARCHIVED
    # as with "archive this version", the build's chip stays with files that name none
    assert firmware_chips(rig.context, imported) == ("esp32s3",)
    assert {p: p.read_bytes() for p in files} == files
    stored = rig.context.firmware.get(tracked.id)
    assert (stored.source, stored.entry_overrides) == (Source.IDF_BUILD, tracked.entry_overrides)


# ---- PlatformIO Arduino: boot_app0.bin from the framework package ------------------------

PIO_ESP32, PIO_S3 = "esp32dev", "esp32-s3-devkitc-1"
PIO_PACKAGE = Path("packages") / "framework-arduinoespressif32"
"""The framework package in the fake PIO core of the samples."""


def _pio_rig(context: AppContext, tmp_path: Path, sample: str) -> tuple[Rig, Path, Path]:
    """A rig whose tracker knows PIO configs (FakePio, as tests/app/test_pio_tracking.py)
    for a copy of `sample`. Returns it, the project root and the fake PIO core."""
    root, core = tmp_path / "project", tmp_path / "core"
    pio_project(sample, root, core)
    configs = pio_configs(core, FakePio())
    configs.load(root)
    tracker = Tracker(context, clock=Clock(), pio=configs)
    packager = Packager(context, tracker, clock=lambda: NOW)
    return Rig(context, tracker, Library(context, tracker), packager), root, core


def _member_texts(path: Path) -> dict[str, str]:
    return {n: d.decode("utf-8") for n, d in _members(path) if n.endswith(".toml")}


def _assert_no_text_names(texts: dict[str, str], places: list[Path]) -> None:
    locals_ = {t.lower() for p in places for t in (str(p), p.as_posix())}
    for name, text in texts.items():
        assert "path:" not in text, name
        assert not any(local in text.lower() for local in locals_), name
        for value in _strings(tomllib.loads(text)):
            assert not any(local in value.lower() for local in locals_), (name, value)


# (sample, env, otadata address, manual framework package): the default package in the
# PIO core, a manual one (set_framework_pkg), and a table with otadata away from 0xE000
FRAMEWORK_CASES: dict[str, tuple[str, str, int, bool]] = {
    "core_package": ("multi_env", PIO_ESP32, 0xE000, False),
    "manual_package": ("multi_env", PIO_ESP32, 0xE000, True),
    "otadata_moved": ("arduino_esp32s3_otadata", PIO_S3, 0xD000, False),
}


@pytest.mark.parametrize("case", FRAMEWORK_CASES)
def test_pio_boot_app0_from_the_framework_package_travels_with_the_firmware(
    context: AppContext, other: Rig, tmp_path: Path, case: str
) -> None:
    sample, env, address, manual = FRAMEWORK_CASES[case]
    rig, root, core = _pio_rig(context, tmp_path, sample)
    (firmware,) = rig.library.import_pio(root, [env])
    package_dir = core / PIO_PACKAGE
    if manual:
        package_dir = make_package(tmp_path / "own", "2.0.0", b"\x5a" * 0x2000)
        rig.library.set_framework_pkg(firmware.id, str(package_dir))
    boot_file = package_dir / BOOT_APP0_IN_PACKAGE
    boot_bytes = boot_file.read_bytes()
    boot_sha = hashlib.sha256(boot_bytes).hexdigest()
    build = pio_build(root, env)
    assert not (build / "boot_app0.bin").exists()  # not in the build folder
    status = rig.tracker.check(firmware.id)
    assert status.state.flashable, status.problems
    boot = next(e for e in status.firmware.entries if e.kind == EntryKind.OTADATA)
    assert (boot.name, boot.address, boot.sha256, boot.file) == (
        "boot_app0.bin",
        address,
        boot_sha,
        f"path:{boot_file}",
    )
    destination = tmp_path / "pio.embr"

    rig.packager.export_firmware([firmware.id], destination)

    members = dict(_members(destination))
    assert members[f"files/{boot_sha}.bin"] == boot_bytes
    listed = {f.sha256: f for f in read_manifest(destination).files}
    assert listed[boot_sha].original_name == "boot_app0.bin"
    assert listed[boot_sha].size == len(boot_bytes)
    _assert_no_text_names(_member_texts(destination), [package_dir, core, package_dir.resolve()])

    (imported,) = _get(other, _import(other, destination, Placement(None)).firmware_ids)
    otadata = [e for e in imported.entries if e.kind == EntryKind.OTADATA]
    assert [(e.name, e.address, e.file, e.enabled, e.sha256) for e in otadata] == [
        ("boot_app0.bin", address, f"archive:{boot_sha}", True, boot_sha)
    ]
    assert other.context.archive.path(boot_sha).read_bytes() == boot_bytes
    back = other.tracker.check(imported.id)
    assert back.state.flashable, back.problems
    assert back.state is ResolveState.OK


@pytest.mark.parametrize(
    ("sample", "env", "address"),
    [("multi_env", PIO_ESP32, 0xE000), ("arduino_esp32s3_otadata", PIO_S3, 0xD000)],
    ids=["otadata_at_e000", "otadata_moved"],
)  # otadata partitions are 0x2000 bytes in both tables
def test_pio_blank_otadata_stays_blank(
    context: AppContext, other: Rig, tmp_path: Path, sample: str, env: str, address: int
) -> None:
    rig, root, core = _pio_rig(context, tmp_path, sample)
    shutil.rmtree(core / "packages")  # no framework package anywhere
    (firmware,) = rig.library.import_pio(root, [env])
    status = rig.tracker.check(firmware.id)
    assert status.state.flashable, status.problems
    blank = next(e for e in status.firmware.entries if e.kind == EntryKind.OTADATA)
    assert (blank.file, blank.size, blank.address) == ("blank:", 0x2000, address)
    destination = tmp_path / "pio.embr"

    rig.packager.export_firmware([firmware.id], destination)

    package = other.packager.open(destination)
    try:
        (exported,) = package.firmwares
        (packed,) = [e for e in exported.entries if e.kind == EntryKind.OTADATA]
        assert (packed.file, packed.size, packed.address) == ("blank:", 0x2000, address)
    finally:
        package.discard()
    others = {e.sha256 for e in status.firmware.entries if e.file != "blank:"}
    listed = {f.sha256 for f in read_manifest(destination).files}
    assert listed == others  # nothing added for the blank entry
    files = sorted(n for n, _ in _members(destination) if n.startswith("files/"))
    assert files == sorted(f"files/{s}.bin" for s in others)
    _assert_no_text_names(_member_texts(destination), [core])

    (imported,) = _get(other, _import(other, destination, Placement(None)).firmware_ids)
    otadata = [e for e in imported.entries if e.kind == EntryKind.OTADATA]
    assert [(e.file, e.size, e.address, e.enabled) for e in otadata] == [
        ("blank:", 0x2000, address, True)
    ]
    assert other.tracker.check(imported.id).state is ResolveState.OK


def _build_missing(rig: Rig, tmp_path: Path) -> str:
    firmware, build = _tracking_firmware(rig, tmp_path / "hello", None)
    shutil.rmtree(build)
    return firmware.id


def _needs_confirmation(rig: Rig, tmp_path: Path) -> str:
    firmware, build = _tracking_firmware(rig, tmp_path / "hello", None)
    move_app(build, 0x30000)
    return firmware.id


def _fixed_file_missing(rig: Rig, tmp_path: Path) -> str:
    files = copy_files(build_files("esp32s3_hello"), tmp_path / "missing")
    firmware = fixed_firmware(rig.library, files, "missing")
    files[2].unlink()
    return firmware.id


def _fixed_file_changed(rig: Rig, tmp_path: Path) -> str:
    files = copy_files(build_files("esp32s3_hello"), tmp_path / "changed")
    firmware = fixed_firmware(rig.library, files, "changed")
    flip_last_byte(files[2])
    return firmware.id


def _read_only(rig: Rig, tmp_path: Path) -> str:
    files = copy_files(build_files("esp32s3_hello"), tmp_path / "newer")
    firmware = fixed_firmware(rig.library, files, "newer")
    _make_read_only(rig.context, firmware.id)
    return firmware.id


BLOCKED: dict[str, Callable[[Rig, Path], str]] = {
    "build_missing": _build_missing,
    "needs_confirmation": _needs_confirmation,
    "fixed_file_missing": _fixed_file_missing,
    "fixed_file_changed": _fixed_file_changed,
    "read_only": _read_only,
}


@pytest.mark.parametrize("case", BLOCKED)
def test_firmware_that_is_not_flashable_cannot_be_exported(
    rig: Rig, tmp_path: Path, case: str
) -> None:
    blocked_id = BLOCKED[case](rig, tmp_path)
    blocked_name = rig.context.firmware.get(blocked_id).name
    ok = fixed_firmware(rig.library, copy_files(build_files("esp32_hello"), tmp_path / "ok"), "ok")

    check = rig.packager.check_export([blocked_id, ok.id])
    assert check.exportable == (ok.id,)
    assert [(b.firmware_id, b.name) for b in check.blocked] == [(blocked_id, blocked_name)]
    assert check.blocked[0].reason

    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(PlanError) as raised:
        rig.packager.export_firmware([blocked_id, ok.id], out / "fw.embr")
    assert blocked_name in str(raised.value)
    assert list(out.iterdir()) == []


def test_export_project_is_refused_when_any_firmware_is_blocked(rig: Rig, tmp_path: Path) -> None:
    library = rig.library
    project = library.create_project("p")
    gone, build = _tracking_firmware(rig, tmp_path / "hello", project.id)
    shutil.rmtree(build)
    files = copy_files(build_files("esp32_hello"), tmp_path / "fixed")
    changed = library.create_from_files(files, project.id, name="changed")
    flip_last_byte(files[2])
    library.create_from_files(
        copy_files(build_files("esp32s3_hello"), tmp_path / "ok"), project.id, name="ok"
    )
    destination = tmp_path / "out" / "p.embr"
    destination.parent.mkdir()

    with pytest.raises(PlanError) as raised:
        rig.packager.export_project(project.id, destination)

    assert gone.name in str(raised.value) and changed.name in str(raised.value)
    assert list(destination.parent.iterdir()) == []


def test_check_export_keeps_the_order_and_warns_about_dumps(rig: Rig, tmp_path: Path) -> None:
    clean = fixed_firmware(rig.library, copy_files(build_files("esp32_hello"), tmp_path / "c"), "c")
    dumped = fixed_firmware(
        rig.library, copy_files(build_files("esp32s3_hello"), tmp_path / "d"), "dumped"
    )
    stored = rig.context.firmware.get(dumped.id)
    stored.entries[0].from_dump = True
    rig.context.firmware.save(stored)
    rig.tracker.invalidate(dumped.id)

    check = rig.packager.check_export([dumped.id, clean.id])

    assert check.exportable == (dumped.id, clean.id)
    assert check.blocked == ()
    assert check.with_dump == ("dumped",)


def test_unknown_ids_raise_key_error(rig: Rig, tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        rig.packager.check_export([new_id()])
    with pytest.raises(KeyError):
        rig.packager.export_project(new_id(), tmp_path / "x.embr")
    assert not (tmp_path / "x.embr").exists()


def test_export_firmware_needs_one_scope(rig: Rig, tmp_path: Path) -> None:
    library = rig.library
    first, second = library.create_project("a"), library.create_project("b")
    in_first = library.create_firmware("x", first.id)
    in_second = library.create_firmware("y", second.id)
    alone = library.create_firmware("z")
    destination = tmp_path / "x.embr"

    for ids in ([], [in_first.id, alone.id], [in_first.id, in_second.id]):
        with pytest.raises(ValueError):
            rig.packager.export_firmware(ids, destination)
        assert not destination.exists()


# ---- no local paths ---------------------------------------------------------------------

_ABSOLUTE = re.compile(r"^([A-Za-z]:[\\/]|[\\/])")


def _strings(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in cast(dict[str, object], node).items():
            yield key
            yield from _strings(value)
    elif isinstance(node, list):
        for item in cast(list[object], node):
            yield from _strings(item)


def test_package_holds_no_local_path(rig: Rig, tmp_path: Path) -> None:
    project_id, _ = _alpha(rig, tmp_path)
    destination = tmp_path / "alpha.embr"
    rig.packager.export_project(project_id, destination)
    locals_ = {
        text.lower()
        for place in (tmp_path, REPO, tmp_path.resolve())
        for text in (str(place), place.as_posix())
    }

    with zipfile.ZipFile(destination) as archive:
        members = [(i.filename, archive.read(i)) for i in archive.infolist()]
    texts = [(n, d) for n, d in members if n.endswith(".toml")]
    assert {n for n, _ in texts} >= {MANIFEST_NAME}
    assert len(texts) == 1 + 1 + 3  # manifest, the project, its three firmware
    for name, _ in members:
        assert not _ABSOLUTE.match(name) and ".." not in name
    for name, data in texts:
        text = data.decode("utf-8")
        assert "path:" not in text, name
        for value in _strings(tomllib.loads(text)):
            assert not _ABSOLUTE.match(value), (name, value)
            assert not any(local in value.lower() for local in locals_), (name, value)
    for file in read_manifest(destination).files:
        assert "/" not in file.original_name and "\\" not in file.original_name


# ---- loose firmware and placements ------------------------------------------------------


def _two_loose(rig: Rig, tmp_path: Path) -> tuple[Path, list[str]]:
    """Firmware a and b of project P, both in group g1, exported as loose firmware."""
    library = rig.library
    project = library.create_project("P")
    library.add_group(project.id, "g1")
    library.add_group(project.id, "g2")
    a = library.create_from_files(
        copy_files(build_files("esp32s3_hello"), tmp_path / "a"), project.id, "g1", "a"
    )
    b = library.create_from_files(
        copy_files(build_files("esp32_hello"), tmp_path / "b"), project.id, "g1", "b"
    )
    destination = tmp_path / "ab.embr"
    rig.packager.export_firmware([a.id, b.id], destination)
    return destination, [a.id, b.id]


def test_loose_firmware_keep_their_group_in_the_package(
    rig: Rig, other: Rig, tmp_path: Path
) -> None:
    source, ids = _two_loose(rig, tmp_path)
    package = other.packager.open(source)
    try:
        assert package.projects == []
        assert {(f.id, f.name, f.project_id, f.device) for f in package.firmwares} == {
            (ids[0], "a", None, "g1"),
            (ids[1], "b", None, "g1"),
        }
        assert default_group(package, ["x", "g1"]) == "g1"
        assert default_group(package, ["x"]) == ""
    finally:
        package.discard()


def test_loose_firmware_go_where_the_placement_says(rig: Rig, other: Rig, tmp_path: Path) -> None:
    source, _ = _two_loose(rig, tmp_path)
    q = other.library.create_project("Q")
    other.library.add_group(q.id, "h")
    other.library.add_group(q.id, "g1")

    def placed(result: ImportResult) -> set[tuple[str, str | None, str]]:
        assert result.project_ids == ()
        return {(f.name, f.project_id, f.device) for f in _get(other, result.firmware_ids)}

    assert placed(_import(other, source, Placement(q.id, "h"))) == {
        ("a", q.id, "h"),
        ("b", q.id, "h"),
    }
    assert placed(_import(other, source, Placement(None, "h"))) == {  # group ignored
        ("a", None, ""),
        ("b", None, ""),
    }
    assert placed(_import(other, source, Placement(q.id))) == {  # names unique in Q
        ("a (2)", q.id, ""),
        ("b (2)", q.id, ""),
    }
    assert other.context.projects.get(q.id).devices == ["h", "g1"]  # no group created
    assert len(other.context.projects.all()) == 1


def test_bad_placement_creates_nothing(rig: Rig, other: Rig, tmp_path: Path) -> None:
    source, _ = _two_loose(rig, tmp_path)
    q = other.library.create_project("Q")
    other.library.add_group(q.id, "h")
    package = other.packager.open(source)
    try:
        with pytest.raises(KeyError):
            other.packager.import_package(package, source.name, Placement(new_id()))
        with pytest.raises(ValueError):
            other.packager.import_package(package, source.name, Placement(q.id, "nope"))
    finally:
        package.discard()
    assert other.context.projects.get(q.id).devices == ["h"]
    assert [p.id for p in other.context.projects.all()] == [q.id]
    assert other.context.firmware.all() == []


# ---- names, archive and rollback --------------------------------------------------------


def test_imported_names_are_made_unique(rig: Rig, other: Rig, tmp_path: Path) -> None:
    project_id, expected = _alpha(rig, tmp_path)
    destination = tmp_path / "alpha.embr"
    rig.packager.export_project(project_id, destination)

    first = _import(other, destination, Placement(None))
    second = _import(other, destination, Placement(None))

    names = [other.context.projects.get(i).name for i in (*first.project_ids, *second.project_ids)]
    assert names == ["Alpha", "Alpha (2)"]
    for result in (first, second):  # unique within each project already
        assert {f.name for f in _get(other, result.firmware_ids)} == set(expected)

    alone = fixed_firmware(rig.library, copy_files(build_files("esp32_hello"), tmp_path / "s"), "s")
    loose = tmp_path / "s.embr"
    rig.packager.export_firmware([alone.id], loose)
    (back,) = _get(rig, _import(rig, loose, Placement(None)).firmware_ids)
    assert (back.name, back.project_id) == ("s (2)", None)  # among standalone firmware


def test_archive_content_is_stored_once_and_named_again(rig: Rig, tmp_path: Path) -> None:
    firmware = fixed_firmware(rig.library, copy_files(build_files("esp32s3_hello"), tmp_path / "f"))
    app = next(e for e in firmware.entries if e.kind == EntryKind.APP)
    Archiver(rig.context, rig.tracker).archive_entry(firmware.id, app.id)
    known = {f.sha256 for f in rig.context.archive.files()}
    names_before = rig.context.archive.names(app.sha256)
    destination = tmp_path / "fw.embr"
    rig.packager.export_firmware([firmware.id], destination)
    listed = {f.sha256: f for f in read_manifest(destination).files}
    assert app.sha256 in listed and listed[app.sha256].original_name == app.name

    _import(rig, destination, Placement(None))

    files = [f.sha256 for f in rig.context.archive.files()]
    assert sorted(files) == sorted(known | set(listed))  # one row per content
    assert _files_under(rig.context.paths.archive) == sorted(f"{s}.bin" for s in files)
    names = rig.context.archive.names(app.sha256)
    assert names[:-1] == names_before
    record = listed[app.sha256]
    assert (names[-1].original_name, names[-1].note) == (record.original_name, record.note)


def test_importing_twice_stores_files_once(rig: Rig, other: Rig, tmp_path: Path) -> None:
    project_id, _ = _alpha(rig, tmp_path)
    destination = tmp_path / "alpha.embr"
    rig.packager.export_project(project_id, destination)
    listed = read_manifest(destination).files

    _import(other, destination, Placement(None))
    once = _state(other)
    _import(other, destination, Placement(None))
    twice = _state(other)

    assert sorted(s for s, _ in once.archive_files) == sorted(f.sha256 for f in listed)
    assert (twice.archive_files, twice.archive) == (once.archive_files, once.archive)
    for file in listed:
        assert len(other.context.archive.names(file.sha256)) == 2


def test_failed_import_removes_what_it_created(
    rig: Rig, other: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, _ = _alpha(rig, tmp_path)
    destination = tmp_path / "alpha.embr"
    rig.packager.export_project(project_id, destination)
    package = other.packager.open(destination)
    before = _state(other)
    original_save = FirmwareStore.save
    saved: list[str] = []

    def failing_save(store: FirmwareStore, firmware: Firmware) -> None:
        if firmware.name not in saved:
            saved.append(firmware.name)
        if len(saved) > 1:  # the first firmware is written, the second fails
            raise OSError("disk full")
        original_save(store, firmware)

    monkeypatch.setattr(FirmwareStore, "save", failing_save)
    try:
        with pytest.raises(OSError, match="disk full"):
            other.packager.import_package(package, destination.name, Placement(None))
    finally:
        monkeypatch.undo()
        package.discard()

    assert len(saved) == 2
    after = _state(other)
    assert (after.projects, after.firmwares) == (before.projects, before.firmwares)
    assert other.context.projects.all() == []
    assert other.context.firmware.all() == []


@pytest.mark.parametrize("into_project", [True, False], ids=["project", "standalone"])
def test_failed_import_removes_the_loose_firmware_it_created(
    rig: Rig, other: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, into_project: bool
) -> None:
    source, _ = _two_loose(rig, tmp_path)
    q = other.library.create_project("Q")
    other.library.add_group(q.id, "h")
    other.library.create_firmware("kept in q", q.id, "h")
    other.library.create_firmware("kept")
    placement = Placement(q.id, "h") if into_project else Placement(None)
    package = other.packager.open(source)
    before = _state(other)
    firmware_before = {f.id for f in other.context.firmware.all()}
    original_save = FirmwareStore.save
    saved: list[str] = []

    def failing_save(store: FirmwareStore, firmware: Firmware) -> None:
        if firmware.name not in saved:
            saved.append(firmware.name)
        if len(saved) > 1:  # the first loose firmware is written, the second fails
            raise OSError("disk full")
        original_save(store, firmware)

    monkeypatch.setattr(FirmwareStore, "save", failing_save)
    try:
        with pytest.raises(OSError, match="disk full"):
            other.packager.import_package(package, source.name, placement)
    finally:
        monkeypatch.undo()
        package.discard()

    assert len(saved) == 2
    after = _state(other)
    assert (after.projects, after.firmwares) == (before.projects, before.firmwares)
    assert {f.id for f in other.context.firmware.all()} == firmware_before
    assert other.context.projects.get(q.id).devices == ["h"]
    assert [p.id for p in other.context.projects.all()] == [q.id]


# ---- rejected packages ------------------------------------------------------------------


def _members(path: Path) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        return [(i.filename, archive.read(i)) for i in archive.infolist()]


def _rezip(path: Path, members: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)


def _flip_a_file(members: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    name = next(n for n, _ in members if n.startswith("files/"))
    return [(n, bytes([*d[:-1], d[-1] ^ 0xFF]) if n == name else d) for n, d in members]


def _drop_a_file(members: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    name = next(n for n, _ in members if n.startswith("files/"))
    return [(n, d) for n, d in members if n != name]


def _break_the_manifest(members: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    return [(n, b"[[broken" if n == MANIFEST_NAME else d) for n, d in members]


def _newer_firmware(members: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    name = next(n for n, _ in members if n.startswith("firmwares/"))

    def newer(data: bytes) -> bytes:
        doc: dict[str, Any] = tomllib.loads(data.decode("utf-8"))
        doc["schema_version"] = FIRMWARE_SCHEMA_VERSION + 1
        return tomli_w.dumps(doc).encode("utf-8")

    return [(n, newer(d) if n == name else d) for n, d in members]


Change = Callable[[list[tuple[str, bytes]]], list[tuple[str, bytes]]]
REJECTED: dict[str, tuple[Change, type[PackageError]]] = {
    "file_content_changed": (_flip_a_file, PackageError),
    "file_missing": (_drop_a_file, PackageError),
    "manifest_broken": (_break_the_manifest, PackageError),
    "newer_firmware": (_newer_firmware, NewerPackageError),
}


@pytest.mark.parametrize("case", REJECTED)
def test_rejected_package_changes_nothing(rig: Rig, other: Rig, tmp_path: Path, case: str) -> None:
    project_id, _ = _alpha(rig, tmp_path)
    destination = tmp_path / "alpha.embr"
    rig.packager.export_project(project_id, destination)
    change, error = REJECTED[case]
    _rezip(destination, change(_members(destination)))
    before = _state(other)

    with pytest.raises(error):
        other.packager.open(destination)

    assert _state(other) == before  # temp/ included: the open() folder is gone


def test_open_reads_into_temp_and_discard_cleans_up(rig: Rig, other: Rig, tmp_path: Path) -> None:
    source, _ = _two_loose(rig, tmp_path)
    temp = _files_under(other.context.paths.temp)

    package = other.packager.open(source)
    assert package.folder.is_relative_to(other.context.paths.temp)
    assert package.folder.is_dir()
    package.discard()

    assert not package.folder.exists()
    assert _files_under(other.context.paths.temp) == temp


# ---- defaults for the dialogs -----------------------------------------------------------


def _loose(device: str, project_id: str | None = None) -> Firmware:
    return Firmware(
        id=new_id(), name=new_id(), project_id=project_id, device=device, source=Source.ARCHIVED
    )


def _package(firmwares: list[Firmware], tmp_path: Path) -> Package:
    manifest = Manifest(PACKAGE_FORMAT_VERSION, "0.1.0", "5.0.0", "2026-10-31T12:00:00", (), (), ())
    return Package(manifest, [], firmwares, {}, tmp_path)


@pytest.mark.parametrize(
    ("devices", "groups", "expected"),
    [
        (["g1", "g1"], ["g0", "g1"], "g1"),
        (["g1"], ["g1"], "g1"),
        (["g1", "g2"], ["g1", "g2"], ""),  # different groups
        (["g1", "g1"], ["g2"], ""),  # the project lacks it
        (["", ""], ["g1"], ""),
        (["g1", ""], ["g1"], ""),
        ([], ["g1"], ""),
    ],
)
def test_default_group(
    tmp_path: Path, devices: list[str], groups: list[str], expected: str
) -> None:
    package = _package([_loose(d) for d in devices], tmp_path)
    assert default_group(package, groups) == expected


def test_default_group_looks_only_at_loose_firmware(tmp_path: Path) -> None:
    project_id = new_id()
    package = _package([_loose("g1", project_id), _loose("g2"), _loose("g2")], tmp_path)
    assert default_group(package, ["g1", "g2"]) == "g2"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Alpha", "Alpha.embr"),
        ("Alpha (2)", "Alpha (2).embr"),
        ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j.embr"),
        ("tab\there", "tab_here.embr"),  # control characters are not allowed either
        ("release. . ", "release.embr"),
        ("v1.2", "v1.2.embr"),
        ("?", "_.embr"),
        ("", "package.embr"),
        ("   ", "package.embr"),
        ("...", "package.embr"),
        ("固件", "固件.embr"),
    ],
)
def test_default_package_name(name: str, expected: str) -> None:
    assert default_package_name(name) == expected
