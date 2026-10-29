"""app/library: use cases on projects, firmware and entries (design doc §3.1, §3.4, §5)."""

from pathlib import Path

import pytest
from app_testkit import build_files, copy_files, find_row, fixed_firmware

from embr.app.context import AppContext
from embr.app.library import UNTITLED, FirmwareFlag, Library
from embr.core.firmware.entries import AddressError
from embr.core.firmware.files import FileState
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, Source
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


def test_untitled_firmware_takes_name_and_chip_from_the_first_file(library: Library) -> None:
    firmware = library.create_firmware()
    assert (firmware.name, firmware.chip) == (UNTITLED, None)
    library.add_files(firmware.id, [APP])
    loaded = library.firmware(firmware.id)
    assert (loaded.name, loaded.chip) == (APP.stem, "esp32s3")


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

    (new,) = copy_files([APP], tmp_path / "new")
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


def test_entry_operations_refuse_tracking_firmware(library: Library, context: AppContext) -> None:
    tracked = context.firmware.create("tracked", source=Source.IDF_BUILD)
    with pytest.raises(ValueError):
        library.add_files(tracked.id, [APP])
    with pytest.raises(ValueError):
        library.add_attachment(tracked.id, APP, address=0x300000)
