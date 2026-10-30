"""app/chips: which MCU each entry is for (design doc §12 item 13)."""

from pathlib import Path

from app_testkit import (
    Clock,
    build_files,
    copy_files,
    entry_ids_under,
    fixed_firmware,
    idf_project,
    multi_chip_firmware,
    seed_user_entry,
    write_newer,
)

from embr.app.chips import chip_choosable, entry_chip, firmware_chips, for_chip
from embr.app.context import AppContext
from embr.app.library import Library
from embr.app.tracking import Tracker
from embr.core.firmware.entries import attachment_entry
from embr.core.firmware.kinds import EntryKind


def _data(path: Path) -> Path:
    write_newer(path, b"\x5a" * 0x1000)
    return path


def test_header_chip_comes_before_the_hand_chosen_one(context: AppContext, tmp_path: Path) -> None:
    firmware = multi_chip_firmware(context, tmp_path)
    s3_app = next(e for e in firmware.entries if e.name == "hello_esp32s3.bin")
    s3_app.chip = "esp32p4"  # ignored: the image header names esp32s3
    data = attachment_entry(_data(tmp_path / "data.bin"), address=0x300000)
    firmware.entries.append(data)
    context.firmware.save(firmware)
    firmware = context.firmware.get(firmware.id)

    mcus = {e.id: entry_chip(context, firmware, e) for e in firmware.entries}
    assert mcus[s3_app.id] == "esp32s3"
    tables = [e for e in firmware.entries if e.kind == EntryKind.PARTITION_TABLE]
    assert {mcus[t.id] for t in tables} == {"esp32s3", "esp32p4"}  # chosen by hand
    assert mcus[data.id] is None  # two chips: nothing to fall back on


def test_the_only_chip_covers_entries_without_one(
    library: Library, context: AppContext, tmp_path: Path
) -> None:
    files = copy_files(build_files("esp32s3_hello"), tmp_path)
    firmware = fixed_firmware(library, [*files, _data(tmp_path / "data.bin")])
    assert firmware_chips(context, firmware) == ("esp32s3",)
    unassigned = [e for e in firmware.entries if e.kind != EntryKind.APP]
    assert {entry_chip(context, firmware, e) for e in unassigned} == {"esp32s3"}


def test_tracking_entries_take_the_build_chip(context: AppContext, tmp_path: Path) -> None:
    library = Library(context, Tracker(context, clock=Clock()))
    firmware = library.import_idf(idf_project("esp32s3_hello", tmp_path / "hello"))
    seed_user_entry(library, context, firmware.id, _data(tmp_path / "data.bin"), 0x300000)
    stored = context.firmware.get(firmware.id)
    stored.user_entries[0].chip = "esp32p4"  # a hand choice does not apply to tracking
    context.firmware.save(stored)
    library.tracker.invalidate(firmware.id)

    resolved = library.tracker.check(firmware.id).firmware
    assert firmware_chips(context, resolved) == ("esp32s3",)
    assert {entry_chip(context, resolved, e) for e in resolved.entries} == {"esp32s3"}


def test_firmware_chips_cover_disabled_and_hand_chosen_entries(
    library: Library, context: AppContext, tmp_path: Path
) -> None:
    firmware = fixed_firmware(library, copy_files([build_files("esp32s3_hello")[2]], tmp_path))
    stored = context.firmware.get(firmware.id)
    stored.entries.append(attachment_entry(_data(tmp_path / "data.bin"), address=0x300000))
    stored.entries[-1].chip = "esp32c3"
    stored.entries[-1].enabled = False
    context.firmware.save(stored)
    assert firmware_chips(context, context.firmware.get(firmware.id)) == ("esp32c3", "esp32s3")


def test_chip_is_choosable_only_for_fixed_entries_without_a_header(
    library: Library, context: AppContext, tmp_path: Path
) -> None:
    fixed = fixed_firmware(library, copy_files(build_files("esp32s3_hello"), tmp_path / "fw"))
    by_kind = {e.kind: e for e in fixed.entries}
    tracker = Tracker(context, clock=Clock())
    tracked = Library(context, tracker).import_idf(idf_project("esp32s3_hello", tmp_path / "idf"))
    resolved = tracker.check(tracked.id).firmware
    tracked_table = next(e for e in resolved.entries if e.kind == EntryKind.PARTITION_TABLE)

    assert chip_choosable(context, fixed, by_kind[EntryKind.PARTITION_TABLE])
    assert not chip_choosable(context, fixed, by_kind[EntryKind.APP])
    assert not chip_choosable(context, resolved, tracked_table)


def test_for_chip_shares_only_that_chips_entries(context: AppContext, tmp_path: Path) -> None:
    firmware = multi_chip_firmware(context, tmp_path)
    view = for_chip(context, firmware, "esp32p4")
    assert {e.id for e in view.entries} == entry_ids_under(firmware, tmp_path / "p4")
    assert all(any(v is e for e in firmware.entries) for v in view.entries)
    assert len(firmware.entries) == 6
