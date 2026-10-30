"""app/archiving: archive this version, archive an entry, clean the archive (design doc
§3.4 "归档此版本", "归档")."""

from pathlib import Path

import pytest
from app_testkit import (
    Clock,
    build_files,
    fixed_firmware,
    idf_project,
    move_app,
    seed_user_entry,
    write_newer,
)

from embr.app.archiving import Archiver
from embr.app.chips import firmware_chips
from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.library import Library
from embr.app.tracking import Tracker
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import FlashParams, Source

APP = "app:hello_esp32s3.bin"


def _rig(context: AppContext) -> tuple[Library, Archiver]:
    tracker = Tracker(context, clock=Clock())
    return Library(context, tracker), Archiver(context, tracker)


def test_archive_version_copies_the_resolved_firmware(context: AppContext, tmp_path: Path) -> None:
    library, archiver = _rig(context)
    project = library.create_project("p")
    build = idf_project("esp32s3_hello", tmp_path / "hello")
    source = library.import_idf(build, project_id=project.id, device="board")
    library.set_entry_address(source.id, APP, "0x20000")
    library.set_entry_note(source.id, APP, "main app")
    data = tmp_path / "data.bin"
    write_newer(data, b"\x5a" * 0x1000)
    seed_user_entry(library, context, source.id, data, 0x300000)  # legacy user entry
    before = context.firmware.get(source.id)
    files = {p: p.read_bytes() for p in [*build.rglob("*.bin"), data]}

    archived = archiver.archive_version(source.id, notes="release 1")

    assert (archived.source, archived.project_id, archived.device) == (
        Source.ARCHIVED,
        project.id,
        "board",
    )
    assert archived.name.startswith(f"{source.name} (archived ")
    assert archived.notes == "release 1" and source.name in archived.archived_from
    # a fixed firmware: its chips come from the entries (§12 item 13)
    assert (firmware_chips(context, archived), archived.flash) == (
        ("esp32s3",),
        FlashParams("dio", "80m", "2MB"),
    )
    entries = {e.name: e for e in archived.entries}
    assert set(entries) == {
        "bootloader.bin",
        "partition-table.bin",
        "hello_esp32s3.bin",
        "data.bin",
    }
    for entry in archived.entries:
        assert entry.file == f"archive:{entry.sha256}" and context.archive.has(entry.sha256)
    app = entries["hello_esp32s3.bin"]
    assert (app.address, app.note) == (0x20000, "main app")
    assert entries["data.bin"].address == 0x300000
    record = context.archive.names(app.sha256)[-1]
    assert (record.original_name, record.source_firmware) == ("hello_esp32s3.bin", source.name)

    after = context.firmware.get(source.id)
    assert (after.entry_overrides, after.user_entries, after.last_resolved) == (
        before.entry_overrides,
        before.user_entries,
        before.last_resolved,
    )
    assert {p: p.read_bytes() for p in files} == files


def test_archive_version_is_refused_until_confirmed(context: AppContext, tmp_path: Path) -> None:
    library, archiver = _rig(context)
    build = idf_project("esp32s3_hello", tmp_path / "hello")
    source = library.import_idf(build)
    move_app(build, 0x20000)

    with pytest.raises(PlanError):
        archiver.archive_version(source.id)
    assert [f.id for f in context.firmware.all()] == [source.id]


def test_archive_entry_points_a_fixed_entry_at_the_archive(context: AppContext) -> None:
    library, archiver = _rig(context)
    firmware = fixed_firmware(library, build_files("esp32s3_hello"))
    app = next(e for e in firmware.entries if e.kind == EntryKind.APP)
    library.set_entry_note(firmware.id, app.id, "v1")

    archived = archiver.archive_entry(firmware.id, app.id)

    expected = (app.id, app.name, "v1", f"archive:{app.sha256}")
    assert (archived.id, archived.name, archived.note, archived.file) == expected
    stored = next(e for e in library.firmware(firmware.id).entries if e.id == app.id)
    assert (stored.id, stored.name, stored.note, stored.file) == expected
    assert context.archive.has(app.sha256)
    with pytest.raises(ValueError):  # already archived
        archiver.archive_entry(firmware.id, app.id)


def test_clean_deletes_only_unreferenced_files(context: AppContext) -> None:
    library, archiver = _rig(context)
    firmware = fixed_firmware(library, build_files("esp32s3_hello"))
    app = next(e for e in firmware.entries if e.kind == EntryKind.APP)
    archiver.archive_entry(firmware.id, app.id)
    orphan = context.archive.add(b"old build", original_name="old.bin")

    assert [f.sha256 for f in archiver.unreferenced()] == [orphan.sha256]
    archiver.clean([orphan.sha256, app.sha256])
    assert not context.archive.has(orphan.sha256)
    assert context.archive.has(app.sha256)


def test_usage_lists_every_archive_file_with_its_firmware(context: AppContext) -> None:
    library, archiver = _rig(context)
    second = fixed_firmware(library, build_files("esp32s3_hello"), "b")
    app = next(e for e in second.entries if e.kind == EntryKind.APP)
    archiver.archive_entry(second.id, app.id)
    first = library.create_firmware("a")
    library.copy_entry_to(second.id, app.id, first.id)  # same archive file
    orphan = context.archive.add(b"old build", original_name="old.bin")

    usage = archiver.usage()

    assert [(u.file.sha256, u.firmware_ids) for u in usage] == [
        (app.sha256, (first.id, second.id)),  # by firmware name
        (orphan.sha256, ()),
    ]
    assert [u.file for u in usage if not u.firmware_ids] == archiver.unreferenced()
