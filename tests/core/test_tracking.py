"""core/firmware/tracking: resolving a tracking firmware against its build (design doc
§3.4 "跟随", "切换目标芯片", "切换 ESP-IDF 版本"). Builds are assembled in tmp_path from
the IDF fixtures; DerivedBuild comes from import_idf as an importer would build it."""

import copy
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from embr_testkit import (
    IDF,
    app_file,
    edit_json,
    flip_last_byte,
    idf_project,
    move_file,
    rename_app,
    set_target,
    use_sample,
    write_newer,
)

from embr.core.firmware.filecache import FileCache
from embr.core.firmware.import_idf import configured_target, read_build
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import BuildRef, EntryOverride, Firmware, FlashParams, Source
from embr.core.firmware.tracking import (
    ChangeKind,
    DerivedBuild,
    DerivedFile,
    EntryMark,
    Resolution,
    ResolveState,
    accept,
    digest,
    entry_key,
    missing,
    resolve,
)

NOW = "2026-01-01T12:00:00"
BOOT = entry_key(EntryKind.BOOTLOADER, "bootloader.bin")
TABLE = entry_key(EntryKind.PARTITION_TABLE, "partition-table.bin")
APP = entry_key(EntryKind.APP, "hello_esp32s3.bin")


def _derive(build_dir: Path) -> DerivedBuild:
    build = read_build(build_dir)
    return DerivedBuild(
        files=tuple(DerivedFile(f.address, f.path) for f in build.files),
        declared_chips=tuple(c for c in (build.chip, build.target) if c),
        configured_target=configured_target(build.config_file),
        idf_version=build.idf_version,
        flash=FlashParams(build.flash_mode, build.flash_freq, build.flash_size),
        before=build.before,
        after=build.after,
        stub=build.stub,
        inputs=build.inputs,
    )


def _resolve(firmware: Firmware, build: Path) -> Resolution:
    return resolve(
        firmware, _derive(build), FileCache(), archive_dir=build.parent / "archive", now=NOW
    )


def _firmware(build: Path) -> Firmware:
    return Firmware(
        id="fw",
        name="hello",
        project_id=None,
        device="",
        source=Source.IDF_BUILD,
        build_ref=BuildRef(str(build)),
    )


def _follow(tmp_path: Path, sample: str = "esp32s3_hello") -> tuple[Firmware, Path]:
    """A tracking firmware whose first resolution was accepted."""
    build = idf_project(sample, tmp_path / "project")
    firmware = _firmware(build)
    first = _resolve(firmware, build)
    assert first.state is ResolveState.OK
    accept(firmware, first)
    return firmware, build


def _kinds(resolution: Resolution) -> set[ChangeKind]:
    return {c.kind for c in resolution.changes}


def _address(resolution: Resolution, entry_id: str) -> int | None:
    return next(e.address for e in resolution.firmware.entries if e.id == entry_id)


def test_first_resolve_takes_everything_from_the_build(tmp_path: Path) -> None:
    build = idf_project("esp32s3_hello", tmp_path / "project")
    firmware = _firmware(build)
    first = _resolve(firmware, build)

    assert (first.state, first.changes) == (ResolveState.OK, ())
    assert APP == "app:hello_esp32s3.bin"
    resolved = first.firmware
    assert sorted((e.address, e.id, e.file) for e in resolved.entries) == [
        (0x0, BOOT, f"path:{(build / 'bootloader/bootloader.bin').resolve()}"),
        (0x8000, TABLE, f"path:{(build / 'partition_table/partition-table.bin').resolve()}"),
        (0x10000, APP, f"path:{(build / 'hello_esp32s3.bin').resolve()}"),
    ]
    assert (resolved.chip, resolved.flash) == ("esp32s3", FlashParams("dio", "80m", "2MB"))
    assert (resolved.before, resolved.after, resolved.stub) == ("default-reset", "hard-reset", True)
    assert (first.idf_version, first.configured_target) == ("v5.5.5", "esp32s3")

    accept(firmware, first)
    assert firmware.last_resolved == first.snapshot
    again = _resolve(firmware, build)
    assert (again.state, again.changes) == (ResolveState.OK, ())
    assert digest(again.firmware) == digest(first.firmware)


def test_content_only_change_is_updated(tmp_path: Path) -> None:
    firmware, build = _follow(tmp_path)
    flip_last_byte(app_file(build))

    updated = _resolve(firmware, build)
    assert updated.state is ResolveState.UPDATED and updated.state.flashable
    assert _kinds(updated) == {ChangeKind.CONTENT}
    assert dict(updated.marks) == {APP: EntryMark.CHANGED}

    accept(firmware, updated)
    assert _resolve(firmware, build).state is ResolveState.OK


def _add_extra(build: Path) -> None:
    write_newer(build / "extra.bin", b"\x5a" * 0x1000)
    edit_json(
        build / "flasher_args.json", lambda a: a["flash_files"].update({"0x1a000": "extra.bin"})
    )


def _remove_otadata(args: dict[str, Any]) -> None:
    del args["flash_files"]["0x17000"]
    del args["otadata"]


CHANGES: dict[str, tuple[Callable[[Path], None], set[ChangeKind]]] = {
    "address": (lambda b: move_file(b, 0x20000, 0x1A0000), {ChangeKind.ADDRESS}),
    "entry_added": (_add_extra, {ChangeKind.ENTRY_ADDED}),
    "entry_removed": (
        lambda b: edit_json(b / "flasher_args.json", _remove_otadata),
        {ChangeKind.ENTRY_REMOVED},
    ),
    "app_renamed": (
        lambda b: rename_app(b, "renamed.bin"),
        {ChangeKind.ENTRY_ADDED, ChangeKind.ENTRY_REMOVED, ChangeKind.OVERRIDE_DROPPED},
    ),
}


@pytest.mark.parametrize("case", list(CHANGES))
def test_build_changes_need_confirmation(tmp_path: Path, case: str) -> None:
    change, expected = CHANGES[case]
    firmware, build = _follow(tmp_path, "esp32s3_ota_custom")
    app = entry_key(EntryKind.APP, "ota_custom.bin")
    firmware.entry_overrides[app] = EntryOverride(note="release")
    change(build)

    resolution = _resolve(firmware, build)
    assert resolution.state is ResolveState.NEEDS_CONFIRMATION
    assert not resolution.state.flashable
    assert expected <= _kinds(resolution)
    order = list(ChangeKind)
    kinds = [c.kind for c in resolution.changes]
    assert kinds == sorted(kinds, key=order.index)
    if case == "entry_added":
        assert EntryMark.NEW in resolution.marks.values()
    if case == "app_renamed":
        assert resolution.marks[entry_key(EntryKind.APP, "renamed.bin")] is EntryMark.NEW

    accept(firmware, resolution)
    assert (app in firmware.entry_overrides) is (ChangeKind.OVERRIDE_DROPPED not in expected)
    assert _resolve(firmware, build).state is ResolveState.OK


def test_manual_address_is_kept_when_the_build_moves_the_entry(tmp_path: Path) -> None:
    firmware, build = _follow(tmp_path)
    firmware.entry_overrides[APP] = EntryOverride(address=0x100000)
    manual = _resolve(firmware, build)
    assert manual.state is ResolveState.OK and _address(manual, APP) == 0x100000

    move_file(build, 0x10000, 0x20000)
    moved = _resolve(firmware, build)
    assert moved.state is ResolveState.NEEDS_CONFIRMATION
    assert ChangeKind.ADDRESS in _kinds(moved)
    assert _address(moved, APP) == 0x100000

    accept(firmware, moved)
    assert firmware.entry_overrides[APP].address == 0x100000
    assert _resolve(firmware, build).state is ResolveState.OK


def test_switched_target_without_rebuild(tmp_path: Path) -> None:
    firmware, build = _follow(tmp_path)
    set_target(build, "esp32p4")

    switched = _resolve(firmware, build)
    assert switched.state is ResolveState.TARGET_SWITCHED
    assert not switched.state.flashable
    assert switched.configured_target == "esp32p4"
    text = " ".join(switched.problems)
    assert "esp32p4" in text and "esp32s3" in text
    with pytest.raises(ValueError):
        accept(firmware, switched)


def _declare_p4(build: Path) -> None:
    edit_json(build / "flasher_args.json", lambda a: a["extra_esptool_args"].update(chip="esp32p4"))
    edit_json(build / "project_description.json", lambda d: d.update(target="esp32p4"))
    set_target(build, "esp32p4")


def _old_bootloader(build: Path) -> None:
    old = IDF / "esp32s3_hello/build/bootloader/bootloader.bin"
    write_newer(build / "bootloader/bootloader.bin", old.read_bytes())


INCOMPLETE: dict[str, tuple[str, Callable[[Path], None], tuple[str, ...]]] = {
    "declared_chip_vs_images": ("esp32s3_hello", _declare_p4, ("esp32p4", "esp32s3")),
    "mixed_idf_versions": ("esp32s3_hello_v61", _old_bootloader, ("v5.5.5", "v6.1")),
    "file_missing": ("esp32s3_hello", lambda b: app_file(b).unlink(), ("hello_esp32s3.bin",)),
}


@pytest.mark.parametrize("case", list(INCOMPLETE))
def test_inconsistent_build_is_incomplete(tmp_path: Path, case: str) -> None:
    sample, change, shown = INCOMPLETE[case]
    firmware, build = _follow(tmp_path, sample)
    change(build)

    resolution = _resolve(firmware, build)
    assert resolution.state is ResolveState.INCOMPLETE
    assert not resolution.state.flashable
    text = " ".join(resolution.problems)
    assert all(value in text for value in shown), text


def test_rebuild_with_another_idf_needs_confirmation(tmp_path: Path) -> None:
    firmware, build = _follow(tmp_path)  # v5.5.5: "default_reset" spelling
    firmware.entry_overrides[APP] = EntryOverride(address=0x20000)
    use_sample(build, "esp32s3_hello_v61")  # v6.1: "default-reset" spelling
    rename_app(build, "hello_esp32s3.bin")

    rebuilt = _resolve(firmware, build)
    assert rebuilt.state is ResolveState.NEEDS_CONFIRMATION
    (idf,) = [c for c in rebuilt.changes if c.kind is ChangeKind.IDF_VERSION]
    assert (idf.before, idf.after) == ("v5.5.5", "v6.1")
    assert ChangeKind.MANUAL_ADDRESS in _kinds(rebuilt)
    assert not _kinds(rebuilt) & {ChangeKind.CHIP, ChangeKind.RESET}

    accept(firmware, rebuilt)
    assert firmware.entry_overrides[APP].address == 0x20000  # kept on an IDF change
    assert _resolve(firmware, build).state is ResolveState.OK


def test_chip_change_is_listed_first_and_drops_manual_addresses(tmp_path: Path) -> None:
    firmware, build = _follow(tmp_path)
    firmware.entry_overrides[TABLE] = EntryOverride(address=0x9000, note="table")
    use_sample(build, "esp32p4_rev1_hello")
    set_target(build, "esp32p4")

    changed = _resolve(firmware, build)
    assert changed.state is ResolveState.NEEDS_CONFIRMATION
    chip = changed.changes[0]
    assert (chip.kind, chip.before, chip.after) == (ChangeKind.CHIP, "esp32s3", "esp32p4")
    assert ChangeKind.MANUAL_ADDRESS in _kinds(changed)

    accept(firmware, changed)
    assert (firmware.chip, firmware.flash) == ("esp32p4", FlashParams("dio", "40m", "2MB"))
    override = firmware.entry_overrides.get(TABLE)
    assert override is None or override.address is None
    assert _resolve(firmware, build).state is ResolveState.OK


def test_missing_build_shows_the_last_snapshot(tmp_path: Path) -> None:
    never = missing(_firmware(tmp_path / "nowhere"), "build directory not found")
    assert never.state is ResolveState.MISSING and never.firmware.entries == []
    assert any("build directory not found" in p for p in never.problems)

    firmware, build = _follow(tmp_path)
    shutil.rmtree(build)
    gone = missing(firmware, "build directory not found")
    assert gone.state is ResolveState.MISSING and not gone.state.flashable
    assert {e.name for e in gone.firmware.entries} == {
        "bootloader.bin",
        "partition-table.bin",
        "hello_esp32s3.bin",
    }
    with pytest.raises(ValueError):
        accept(firmware, gone)


def test_digest_follows_what_would_be_flashed(tmp_path: Path) -> None:
    firmware, build = _follow(tmp_path)
    resolved = _resolve(firmware, build).firmware
    base = digest(resolved)

    noted = copy.deepcopy(resolved)
    noted.entries[0].note = "only a note"
    noted.notes = "notes"
    assert digest(noted) == base

    changes: list[Callable[[Firmware], None]] = [
        lambda f: setattr(f.entries[0], "address", 0x1000),
        lambda f: setattr(f.entries[0], "sha256", "0" * 64),
        lambda f: setattr(f.entries[0], "enabled", False),
        lambda f: setattr(f, "erase_all", True),
    ]
    for change in changes:
        other = copy.deepcopy(resolved)
        change(other)
        assert digest(other) != base
