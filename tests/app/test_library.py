"""app/library: use cases on projects, firmware and entries (design doc §3.1, §3.4, §5)."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from app_testkit import (
    BRIDGE,
    Clock,
    build_files,
    copy_files,
    entry_ids_under,
    find_row,
    fixed_firmware,
    idf_project,
    info,
    multi_chip_firmware,
    seed_user_entry,
    set_target,
    write_newer,
)

from embr.app.archiving import Archiver
from embr.app.chips import entry_chip
from embr.app.context import AppContext
from embr.app.flashplan import PlanError, assemble_plan
from embr.app.library import (
    UNTITLED,
    AlreadyTrackedError,
    FirmwareFlag,
    Library,
    TrackingEntriesError,
)
from embr.app.tracking import Tracker
from embr.core.firmware.entries import (
    AddressError,
    FolderError,
    attachment_entry,
    entries_outside,
)
from embr.core.firmware.files import FileState
from embr.core.firmware.import_idf import BuildMissingError
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import (
    AddressSource,
    BuildRef,
    Entry,
    EntryOverride,
    Firmware,
    Source,
)
from embr.core.firmware.tracking import ResolveState, entry_key
from embr.core.project import ProjectDefaults

APP = build_files("esp32s3_hello")[2]


def test_new_firmware_copies_project_defaults(library: Library) -> None:
    project = library.create_project("p")
    library.set_project_defaults(
        project.id, ProjectDefaults(before="no-reset", after="no-reset", erase_all=True)
    )
    inside = library.create_firmware("inside", project.id)
    alone = library.create_firmware("alone")
    assert (inside.before, inside.after, inside.erase_all) == ("no-reset", "no-reset", True)
    assert (alone.before, alone.after, alone.erase_all) == ("default-reset", "hard-reset", False)


def test_untitled_firmware_takes_name_from_the_first_file(library: Library) -> None:
    firmware = library.create_firmware()
    assert firmware.name == UNTITLED
    library.add_files(firmware.id, [APP])
    loaded = library.firmware(firmware.id)
    # a fixed firmware stores no chip; its chips come from the entries (§12 item 13)
    assert (loaded.name, loaded.chip) == (APP.stem, None)


def test_idf_build_files_get_addresses_and_one_blank_otadata(library: Library) -> None:
    boot, table, app = build_files("esp32s3_ota_custom")
    firmware = library.create_firmware("ota")
    # the table comes last but is added first, so the app is placed in ota_0
    library.add_files(firmware.id, [app, boot, table])
    entries = library.firmware(firmware.id).entries
    addresses = {e.kind: e.address for e in entries}
    assert addresses[EntryKind.BOOTLOADER] == 0x0
    assert addresses[EntryKind.PARTITION_TABLE] == 0x10000
    assert addresses[EntryKind.APP] == 0x20000
    (otadata,) = [e for e in entries if e.kind == EntryKind.OTADATA]
    assert (otadata.address, otadata.size, otadata.file) == (0x17000, 0x2000, "blank:")
    assert otadata.auto_added

    library.add_files(firmware.id, [table])
    kinds = [e.kind for e in library.firmware(firmware.id).entries]
    assert kinds.count(EntryKind.OTADATA) == 1


def test_rename_and_move_make_names_unique(library: Library) -> None:
    project = library.create_project("p")
    library.create_firmware("a", project.id)
    b = library.create_firmware("b", project.id)
    assert library.rename_firmware(b.id, "a").name == "a (2)"
    solo = library.create_firmware("a")
    assert library.move_firmware(solo.id, project.id).name == "a (3)"
    other = library.create_project("q")
    assert library.rename_project(other.id, "p").name == "p (2)"


def test_duplicate_firmware(library: Library) -> None:
    project = library.create_project("p")
    firmware = library.create_firmware("fw", project.id, "main")
    library.add_files(firmware.id, [APP])
    original = library.firmware(firmware.id)

    copy = library.duplicate_firmware(firmware.id)
    assert (copy.name, copy.project_id, copy.device) == ("fw copy", project.id, "main")
    assert copy.source == Source.MANUAL
    assert [e.file for e in copy.entries] == [e.file for e in original.entries]
    assert not {e.id for e in copy.entries} & {e.id for e in original.entries}


def test_entry_address_edit(library: Library, context: AppContext) -> None:
    firmware = fixed_firmware(library, [APP])
    entry_id = firmware.entries[0].id
    library.set_entry_address(firmware.id, entry_id, "0x30000")

    with pytest.raises(AddressError):
        library.set_entry_address(firmware.id, entry_id, "0xZZ")
    stored = context.firmware.get(firmware.id).entries[0]
    assert (stored.address, stored.address_source) == (0x30000, AddressSource.MANUAL)

    library.set_entry_address(firmware.id, entry_id, "")
    stored = context.firmware.get(firmware.id).entries[0]
    assert (stored.address, stored.address_source) == (0x10000, AddressSource.AUTO)


def test_copied_entry_is_independent_of_the_source(library: Library) -> None:
    source = fixed_firmware(library, [APP], "source")
    target = library.create_firmware("target")
    entry = source.entries[0]
    library.set_entry_note(source.id, entry.id, "release build")

    copied = library.copy_entry_to(source.id, entry.id, target.id)
    library.remove_entry(source.id, entry.id)

    assert library.firmware(source.id).entries == []
    (kept,) = library.firmware(target.id).entries
    assert kept.id == copied.id != entry.id
    assert (kept.file, kept.address, kept.note) == (entry.file, entry.address, "release build")


def test_missing_file_is_flagged_and_can_be_relocated(
    library: Library, context: AppContext, tmp_path: Path
) -> None:
    (old,) = copy_files([APP], tmp_path / "old")
    firmware = fixed_firmware(library, [old])
    entry_id = firmware.entries[0].id
    old.unlink()
    # a library that has not checked the files yet still sees the file is gone
    assert find_row(Library(context).tree(), firmware.id).flag == FirmwareFlag.MISSING

    (new,) = copy_files([APP], tmp_path / "old" / "new")  # inside the folder (§12 item 8)
    library.relocate_entry(firmware.id, entry_id, new)
    assert library.check_files(firmware.id) == {entry_id: FileState.OK}
    assert library.flag(firmware.id) == FirmwareFlag.NONE


def test_changed_file_needs_confirmation(library: Library, tmp_path: Path) -> None:
    (path,) = copy_files([APP], tmp_path)
    firmware = fixed_firmware(library, [path])
    entry_id = firmware.entries[0].id
    with path.open("ab") as file:
        file.write(b"\0" * 16)

    assert library.check_files(firmware.id) == {entry_id: FileState.CHANGED}
    assert find_row(library.tree(), firmware.id).flag == FirmwareFlag.NEEDS_CONFIRMATION

    library.confirm_entries(firmware.id)
    assert library.flag(firmware.id) == FirmwareFlag.NONE
    assert library.check_files(firmware.id) == {entry_id: FileState.OK}


def test_removed_group_leaves_its_firmware_ungrouped(library: Library) -> None:
    project = library.create_project("p")
    library.add_group(project.id, "main")
    firmware = library.create_firmware("fw", project.id, "main")

    assert library.remove_group(project.id, "main").devices == []
    kept = library.firmware(firmware.id)
    assert (kept.project_id, kept.device) == (project.id, "")


@pytest.mark.parametrize("delete_firmware", [False, True])
def test_delete_project(library: Library, delete_firmware: bool) -> None:
    project = library.create_project("p")
    firmware = library.create_firmware("fw", project.id)

    library.delete_project(project.id, delete_firmware=delete_firmware)

    with pytest.raises(KeyError):
        library.project(project.id)
    if delete_firmware:
        with pytest.raises(KeyError):
            library.firmware(firmware.id)
    else:
        assert library.firmware(firmware.id).project_id is None


# ---- IDF tracking firmware ---------------------------------------------------------------

HELLO_APP = "app:hello_esp32s3.bin"


def _tracked(context: AppContext) -> tuple[Library, Tracker]:
    tracker = Tracker(context, clock=Clock())
    return Library(context, tracker), tracker


def test_import_idf_creates_a_tracking_firmware(context: AppContext, tmp_path: Path) -> None:
    library, _ = _tracked(context)
    build = idf_project("esp32s3_hello", tmp_path / "hello")
    project = library.create_project("p")
    library.set_project_defaults(
        project.id, ProjectDefaults(before="no-reset", after="no-reset", erase_all=True)
    )
    assert library.preview_idf(build).project_name == "hello_esp32s3"
    assert context.firmware.all() == []

    firmware = library.import_idf(build, project_id=project.id, device="board")
    stored = context.firmware.get(firmware.id)
    assert (stored.name, stored.source, stored.project_id, stored.device) == (
        "hello_esp32s3",
        Source.IDF_BUILD,
        project.id,
        "board",
    )
    # only erase_all comes from the project; reset modes come from the build
    assert (stored.before, stored.after, stored.erase_all) == ("default-reset", "hard-reset", True)
    assert stored.build_ref is not None
    assert Path(stored.build_ref.path).resolve() == build.resolve()
    assert stored.last_resolved is not None
    other = idf_project("esp32s3_hello", tmp_path / "other")  # one build is tracked once
    assert library.tracking_firmware_for(other) is None
    assert library.import_idf(other, project_id=project.id).name == "hello_esp32s3 (2)"


UNUSABLE: dict[str, tuple[Callable[[Path], None], type[Exception]]] = {
    "target_switched": (lambda b: set_target(b, "esp32p4"), PlanError),
    "build_missing": (lambda b: (b / "flasher_args.json").unlink(), BuildMissingError),
}


@pytest.mark.parametrize("case", list(UNUSABLE))
def test_import_idf_creates_nothing_for_an_unusable_build(
    context: AppContext, tmp_path: Path, case: str
) -> None:
    change, error = UNUSABLE[case]
    library, _ = _tracked(context)
    build = idf_project("esp32s3_hello", tmp_path / "hello")
    change(build)
    with pytest.raises(error):
        library.import_idf(build)
    assert context.firmware.all() == []


def test_entry_changes_on_tracking_firmware(context: AppContext, tmp_path: Path) -> None:
    library, tracker = _tracked(context)
    firmware = library.import_idf(idf_project("esp32s3_hello", tmp_path / "hello"))
    tracker.check(firmware.id)

    library.set_entry_address(firmware.id, HELLO_APP, "0x20000")
    library.set_entry_enabled(firmware.id, HELLO_APP, False)
    library.set_entry_note(firmware.id, HELLO_APP, "main app")
    stored = context.firmware.get(firmware.id)
    assert stored.entry_overrides[HELLO_APP] == EntryOverride(0x20000, False, "main app")
    # the change is seen at once, although no build file changed
    app = next(e for e in tracker.check(firmware.id).firmware.entries if e.id == HELLO_APP)
    assert (app.address, app.enabled, app.note) == (0x20000, False, "main app")

    # a user entry added before §12 item 16 still flashes and can be removed
    data = tmp_path / "data.bin"
    write_newer(data, b"Z" * 0x1000)
    user = seed_user_entry(library, context, firmware.id, data, 0x300000)
    plan = assemble_plan(context, tracker.check(firmware.id).firmware, BRIDGE, None)
    assert [e.address for e in plan.entries if e.name == "data.bin"] == [0x300000]
    with pytest.raises(ValueError):
        library.remove_entry(firmware.id, HELLO_APP)
    library.remove_entry(firmware.id, user.id)
    assert context.firmware.get(firmware.id).user_entries == []


def test_switched_target_is_flagged_blocked(context: AppContext, tmp_path: Path) -> None:
    library, tracker = _tracked(context)
    build = idf_project("esp32s3_hello", tmp_path / "hello")
    firmware = library.import_idf(build)
    set_target(build, "esp32p4")

    assert tracker.check(firmware.id).state is ResolveState.TARGET_SWITCHED
    assert find_row(library.tree(), firmware.id).flag == FirmwareFlag.BLOCKED


SAME_DIR: dict[str, Callable[[Path], Path]] = {
    "case": lambda b: Path(str(b).upper()),
    "dotdot": lambda b: b / ".." / "build",
    "relative": lambda b: b.relative_to(Path.cwd()),
}


@pytest.mark.parametrize("variant", list(SAME_DIR))
def test_a_build_dir_is_tracked_once(
    context: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    library, _ = _tracked(context)
    build = idf_project("esp32s3_hello", tmp_path / "hello")
    first = library.import_idf(build)
    monkeypatch.chdir(tmp_path)
    same = SAME_DIR[variant](build)

    found = library.tracking_firmware_for(same)
    assert found is not None and found.id == first.id
    with pytest.raises(AlreadyTrackedError) as raised:
        library.import_idf(same)
    assert raised.value.firmware_id == first.id
    assert [f.id for f in context.firmware.all()] == [first.id]


def test_stored_build_dir_with_trailing_separator_is_found(
    context: AppContext, tmp_path: Path
) -> None:
    library, _ = _tracked(context)
    build = idf_project("esp32s3_hello", tmp_path / "hello")
    firmware = context.firmware.get(library.import_idf(build).id)
    firmware.build_ref = BuildRef(str(build) + os.sep)
    context.firmware.save(firmware)
    found = library.tracking_firmware_for(build)
    assert found is not None and found.id == firmware.id


ADDITIONS: dict[str, Callable[[Library, str, str, Path], object]] = {
    "add_files": lambda lib, fw, _, data: lib.add_files(fw, [data]),
    "add_attachment": lambda lib, fw, _, data: lib.add_attachment(fw, data, address=0x300000),
    "copy_entry_to": lambda lib, fw, src, _: lib.copy_entry_to(
        src, lib.firmware(src).entries[0].id, fw
    ),
}


@pytest.mark.parametrize("case", list(ADDITIONS))
def test_tracking_firmware_takes_no_added_entries(
    context: AppContext, tmp_path: Path, case: str
) -> None:
    # entries of tracking firmware come from the build only (§12 item 16)
    library, _ = _tracked(context)
    firmware = library.import_idf(idf_project("esp32s3_hello", tmp_path / "hello"))
    fixed = fixed_firmware(library, [APP], "fixed")
    data = tmp_path / "data.bin"
    write_newer(data, b"Z" * 0x1000)
    before = context.firmware.get(firmware.id)

    with pytest.raises(TrackingEntriesError):
        ADDITIONS[case](library, firmware.id, fixed.id, data)
    assert context.firmware.get(firmware.id) == before


# ---- mismatches with the identified chip (§12 item 1) -------------------------------------

REV3 = build_files("esp32p4_rev3_hello")  # images require v3.1 - v3.99


@pytest.mark.parametrize(
    ("chip", "major", "minor", "reason"),
    [("esp32s3", 0, 2, ""), ("esp32p4", 1, 0, "v1.0"), ("esp32p4", 3, 1, None)],
    ids=["other_chip", "revision_out_of_range", "fits"],
)
def test_mismatches_judge_chip_and_revision(
    library: Library, chip: str, major: int, minor: int, reason: str | None
) -> None:
    firmware = fixed_firmware(library, REV3)
    found = library.mismatches(info(chip, major, minor))
    if reason is None:
        assert firmware.id not in found
    else:
        assert reason in found[firmware.id]


def test_disabled_entries_and_chipless_firmware_fit(library: Library, tmp_path: Path) -> None:
    rev3 = fixed_firmware(library, REV3, "rev3")
    for entry in rev3.entries:
        if entry.kind != EntryKind.PARTITION_TABLE:
            library.set_entry_enabled(rev3.id, entry.id, False)
    data = tmp_path / "data.bin"
    data.write_bytes(b"" * 64)
    chipless = library.create_firmware("data")
    library.add_attachment(chipless.id, data, address=0x100000)
    assert library.mismatches(info("esp32p4", 1, 0)) == {}


def test_mismatches_use_only_the_cached_resolution(context: AppContext, tmp_path: Path) -> None:
    importer, _ = _tracked(context)
    firmware = importer.import_idf(idf_project("esp32s3_hello", tmp_path / "hello"))
    library, tracker = _tracked(context)  # this tracker never resolved the firmware
    assert firmware.id not in library.mismatches(info("esp32p4", 1, 0))
    assert tracker.cached(firmware.id) is None
    tracker.check(firmware.id)
    assert firmware.id in library.mismatches(info("esp32p4", 1, 0))


def test_mismatches_dim_firmware_without_the_identified_chip(
    library: Library, context: AppContext, tmp_path: Path
) -> None:
    multi = multi_chip_firmware(context, tmp_path)
    s3 = fixed_firmware(library, build_files("esp32s3_hello"), "s3")
    found = library.mismatches(info("esp32p4", 1, 0))
    assert multi.id not in found
    assert s3.id in found


# ---- MCU of entries (§12 item 13) ---------------------------------------------------------


def _with_data(context: AppContext, firmware: Firmware, folder: Path) -> Entry:
    """Store an attachment without MCU (a file in `folder`) in `firmware`."""
    data = folder / "data.bin"
    write_newer(data, b"\x5a" * 0x1000)
    entry = attachment_entry(data, address=0x300000)
    firmware.entries.append(entry)
    context.firmware.save(firmware)
    return entry


def _stored_entry(context: AppContext, firmware_id: str, entry_id: str) -> Entry:
    return next(e for e in context.firmware.get(firmware_id).entries if e.id == entry_id)


def test_set_entry_chip_saves_and_clears(library: Library, context: AppContext) -> None:
    firmware = fixed_firmware(library, build_files("esp32s3_hello"))
    table = next(e for e in firmware.entries if e.kind == EntryKind.PARTITION_TABLE)

    library.set_entry_chip(firmware.id, table.id, "esp32p4")
    assert _stored_entry(context, firmware.id, table.id).chip == "esp32p4"
    library.set_entry_chip(firmware.id, table.id, None)
    assert _stored_entry(context, firmware.id, table.id).chip is None


def _table_of(library: Library) -> tuple[str, str]:
    firmware = fixed_firmware(library, build_files("esp32s3_hello"))
    return firmware.id, next(e.id for e in firmware.entries if e.kind == EntryKind.PARTITION_TABLE)


def _app_of(library: Library) -> tuple[str, str]:
    firmware = fixed_firmware(library, [APP])
    return firmware.id, firmware.entries[0].id


def _tracked_table(library: Library, tmp_path: Path) -> tuple[str, str]:
    firmware = library.import_idf(idf_project("esp32s3_hello", tmp_path / "hello"))
    return firmware.id, entry_key(EntryKind.PARTITION_TABLE, "partition-table.bin")


CHIP_REFUSALS: dict[str, tuple[Callable[[Library, Path], tuple[str, str]], str]] = {
    "unknown_chip": (lambda lib, _: _table_of(lib), "esp9999"),
    "header_names_a_chip": (lambda lib, _: _app_of(lib), "esp32p4"),
    "tracking": (_tracked_table, "esp32p4"),
}


@pytest.mark.parametrize("case", list(CHIP_REFUSALS))
def test_set_entry_chip_refuses(context: AppContext, tmp_path: Path, case: str) -> None:
    library, _ = _tracked(context)
    setup, chip = CHIP_REFUSALS[case]
    firmware_id, entry_id = setup(library, tmp_path)
    before = context.firmware.get(firmware_id)
    with pytest.raises(ValueError):
        library.set_entry_chip(firmware_id, entry_id, chip)
    assert context.firmware.get(firmware_id) == before


def test_fit_entries_unticks_entries_for_other_chips(
    library: Library, context: AppContext, tmp_path: Path
) -> None:
    firmware = multi_chip_firmware(context, tmp_path)
    data = _with_data(context, firmware, tmp_path)
    s3_ids = entry_ids_under(firmware, tmp_path) - {data.id}
    before = context.firmware.get(firmware.id)

    # esp32c3 is none of the firmware's chips: the whole firmware is for other boards
    assert library.fit_entries(firmware.id, "esp32c3") == []
    assert context.firmware.get(firmware.id) == before

    unticked = library.fit_entries(firmware.id, "esp32s3")
    assert {e.id for e in unticked} == entry_ids_under(firmware, tmp_path / "p4") | {data.id}
    assert {e.id for e in context.firmware.get(firmware.id).entries if e.enabled} == s3_ids


def test_choosing_the_board_chip_makes_an_entry_eligible_again(
    library: Library, context: AppContext, tmp_path: Path
) -> None:
    firmware = multi_chip_firmware(context, tmp_path)
    data = _with_data(context, firmware, tmp_path)
    library.fit_entries(firmware.id, "esp32s3")

    library.set_entry_chip(firmware.id, data.id, "esp32s3")
    assert not _stored_entry(context, firmware.id, data.id).enabled  # not ticked again
    library.set_entry_enabled(firmware.id, data.id, True)
    assert library.fit_entries(firmware.id, "esp32s3") == []
    stored = context.firmware.get(firmware.id)
    entry = next(e for e in stored.entries if e.id == data.id)
    assert entry.enabled and entry_chip(context, stored, entry) == "esp32s3"


# ---- one folder per fixed firmware (§12 item 8) -------------------------------------------


def _folders(tmp_path: Path) -> tuple[Path, Path]:
    """root/hello_esp32s3.bin and other/data.bin."""
    (app,) = copy_files([APP], tmp_path / "root")
    data = tmp_path / "other" / "data.bin"
    write_newer(data, b"\x5a" * 0x1000)
    return app, data


def test_first_file_sets_the_folder_and_subfolders_fit(library: Library, tmp_path: Path) -> None:
    app, _ = _folders(tmp_path)
    (boot,) = copy_files([build_files("esp32s3_hello")[0]], tmp_path / "root" / "bootloader")
    firmware = library.create_firmware("fw")

    library.add_files(firmware.id, [app])
    assert Path(library.firmware(firmware.id).folder) == app.parent
    library.add_files(firmware.id, [boot])
    assert len(library.firmware(firmware.id).entries) == 2


def test_create_from_files_names_the_firmware_after_the_first_file(
    library: Library, tmp_path: Path
) -> None:
    app, _ = _folders(tmp_path)
    (table,) = copy_files([build_files("esp32s3_hello")[1]], tmp_path / "root")
    project = library.create_project("p")
    firmware = library.create_from_files([app, table], project.id, "board")
    stored = library.firmware(firmware.id)
    assert (stored.name, stored.project_id, stored.device, stored.source) == (
        app.stem,
        project.id,
        "board",
        Source.MANUAL,
    )
    assert (Path(stored.folder), len(stored.entries)) == (app.parent, 2)


def _file(entry: Entry) -> Path:
    return Path(entry.file.removeprefix("path:"))


OUTSIDE: dict[str, Callable[[Library, Firmware, Firmware, Path], object]] = {
    "add_files": lambda lib, fw, _, data: lib.add_files(fw.id, [data]),
    "add_attachment": lambda lib, fw, _, data: lib.add_attachment(fw.id, data, address=0x300000),
    "copy_entry_to": lambda lib, fw, src, _: lib.copy_entry_to(src.id, src.entries[0].id, fw.id),
    "relocate_entry": lambda lib, fw, _, data: lib.relocate_entry(fw.id, fw.entries[0].id, data),
    "create_from_files": lambda lib, fw, _, data: lib.create_from_files(
        [_file(fw.entries[0]), data]
    ),
}


@pytest.mark.parametrize("case", list(OUTSIDE))
def test_file_outside_the_folder_is_refused(
    library: Library, context: AppContext, tmp_path: Path, case: str
) -> None:
    app, data = _folders(tmp_path)
    firmware = fixed_firmware(library, [app])
    source = fixed_firmware(library, [data], "src")
    before = sorted(context.firmware.all(), key=lambda f: f.id)

    with pytest.raises(FolderError) as raised:
        OUTSIDE[case](library, firmware, source, data)
    assert [p.name for p in raised.value.outside] == ["data.bin"]
    assert sorted(context.firmware.all(), key=lambda f: f.id) == before


def test_archived_entries_fit_any_folder(context: AppContext, tmp_path: Path) -> None:
    library, _ = _tracked(context)
    app, data = _folders(tmp_path)
    firmware = fixed_firmware(library, [app])
    source = fixed_firmware(library, [data], "src")
    archived = Archiver(context, library.tracker).archive_entry(source.id, source.entries[0].id)

    library.copy_entry_to(source.id, archived.id, firmware.id)
    stored = library.firmware(firmware.id)
    assert stored.entries[-1].file == archived.file
    assert entries_outside(stored) == []


def test_relocate_folder_keeps_relative_paths(library: Library, tmp_path: Path) -> None:
    boot, table, app = build_files("esp32s3_hello")
    old, new = tmp_path / "old", tmp_path / "new"
    files = [*copy_files([app, table], old), *copy_files([boot], old / "bootloader")]
    firmware = fixed_firmware(library, files)
    copy_files([app], new)
    copy_files([boot], new / "bootloader")  # the table is not there

    assert library.relocate_folder(firmware.id, new) == [table.name]
    stored = library.firmware(firmware.id)
    assert Path(stored.folder) == new.resolve()
    assert {e.name: _file(e) for e in stored.entries} == {
        app.name: (new / app.name).resolve(),
        boot.name: (new / "bootloader" / boot.name).resolve(),
        table.name: (old / table.name).resolve(),
    }
    with pytest.raises(ValueError):  # no folder yet
        library.relocate_folder(library.create_firmware("empty").id, new)


def test_folder_outlives_its_files(library: Library, tmp_path: Path) -> None:
    app, data = _folders(tmp_path)
    (table,) = copy_files([build_files("esp32s3_hello")[1]], tmp_path / "root")
    firmware = fixed_firmware(library, [app, table])

    for entry in firmware.entries:  # the first file's entry included
        library.remove_entry(firmware.id, entry.id)
    assert Path(library.firmware(firmware.id).folder) == app.parent
    with pytest.raises(FolderError):
        library.add_files(firmware.id, [data])
