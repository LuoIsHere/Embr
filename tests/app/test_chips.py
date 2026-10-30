"""app/chips: which MCU each entry is for (design doc §12 item 13)."""

from pathlib import Path

import pytest
from app_testkit import (
    Clock,
    build_files,
    copy_files,
    entry_ids_under,
    fixed_firmware,
    idf_project,
    multi_chip_firmware,
    p4_lines_firmware,
    spanning_app,
    write_newer,
)

from embr.app.chips import (
    Mcu,
    board_revision,
    chip_choosable,
    entry_chip,
    entry_mcu,
    firmware_chips,
    firmware_mcus,
    fits,
    for_chip,
    image_mcu,
)
from embr.app.context import AppContext
from embr.app.library import Library
from embr.app.tracking import Tracker
from embr.core.firmware.entries import attachment_entry, entry_from_file
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
    resolved = library.tracker.check(firmware.id).firmware
    table = next(e for e in resolved.entries if e.kind == EntryKind.PARTITION_TABLE)
    table.chip = "esp32p4"  # a hand choice does not apply to tracking
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


# ---- ESP32-P4 revision lines (§12 item 13, "P4 按芯片版本分档") -----------------------------

P4_V0_V1, P4_V3, P4_ANY = Mcu("esp32p4", "v0-v1"), Mcu("esp32p4", "v3"), Mcu("esp32p4")


@pytest.mark.parametrize(
    ("mcu", "key", "label"),
    [
        (Mcu("esp32s3"), "esp32s3", "ESP32-S3"),
        (P4_V3, "esp32p4 v3", "ESP32-P4 v3"),
        (P4_V0_V1, "esp32p4 v0-v1", "ESP32-P4 v0\u2013v1"),
    ],
)
def test_mcu_key_and_label(mcu: Mcu, key: str, label: str) -> None:
    assert (mcu.key, mcu.label) == (key, label)


@pytest.mark.parametrize(
    ("major", "minor", "revision"),
    [(1, 0, 100), (3, 1, 301), (-1, 0, None)],  # secure download mode reports a negative major
)
def test_board_revision(major: int, minor: int, revision: int | None) -> None:
    assert board_revision(major, minor) == revision


@pytest.mark.parametrize(
    ("mcu", "chip", "revision", "fit"),
    [
        (P4_V3, "esp32p4", 301, True),
        (P4_V3, "esp32p4", 100, False),
        (P4_V0_V1, "esp32p4", 100, True),
        (P4_V3, "esp32p4", None, True),  # revision unknown: only the chip counts
        (P4_ANY, "esp32p4", 100, True),
        (Mcu("esp32s3"), "esp32p4", None, False),
        (None, "esp32p4", 100, False),
    ],
)
def test_fits(mcu: Mcu | None, chip: str, revision: int | None, fit: bool) -> None:
    assert fits(mcu, chip, revision) is fit


def test_image_line_comes_from_the_header_range(context: AppContext, tmp_path: Path) -> None:
    rev1, rev3 = build_files("esp32p4_rev1_hello"), build_files("esp32p4_rev3_hello")
    images = {
        rev1[2]: P4_V0_V1,  # v0.0 - v1.99
        rev3[0]: P4_V3,  # v3.1 - v3.99
        spanning_app(tmp_path / "spanning.bin"): P4_ANY,
        build_files("esp32s3_hello")[2]: Mcu("esp32s3"),
        rev3[1]: None,  # a partition table has no image header
    }
    assert {p: image_mcu(context, entry_from_file(p)) for p in images} == images


def test_two_p4_lines_are_two_mcus(context: AppContext, tmp_path: Path) -> None:
    firmware = p4_lines_firmware(context, tmp_path)
    data = attachment_entry(_data(tmp_path / "data.bin"), address=0x300000)
    firmware.entries.append(data)
    context.firmware.save(firmware)
    firmware = context.firmware.get(firmware.id)

    assert firmware_mcus(context, firmware) == (P4_V0_V1, P4_V3)
    tables = [e for e in firmware.entries if e.kind == EntryKind.PARTITION_TABLE]
    assert {entry_mcu(context, firmware, t) for t in tables} == {P4_V0_V1, P4_V3}  # by hand
    assert entry_mcu(context, firmware, data) is None  # two MCUs: nothing to fall back on


def test_the_only_mcu_lends_its_line(library: Library, context: AppContext, tmp_path: Path) -> None:
    files = copy_files(build_files("esp32p4_rev3_hello"), tmp_path)
    firmware = fixed_firmware(library, [*files, _data(tmp_path / "data.bin")])
    assert firmware_mcus(context, firmware) == (P4_V3,)
    unassigned = [e for e in firmware.entries if e.kind != EntryKind.APP]
    assert {entry_mcu(context, firmware, e) for e in unassigned} == {P4_V3}


@pytest.mark.parametrize("revision", [100, 301, None])
def test_for_chip_takes_the_lines_holding_the_revision(
    context: AppContext, tmp_path: Path, revision: int | None
) -> None:
    firmware = p4_lines_firmware(context, tmp_path)
    spanning = entry_from_file(spanning_app(tmp_path / "span" / "spanning.bin"))
    spanning.address = 0x200000
    firmware.entries.append(spanning)
    context.firmware.save(firmware)
    firmware = context.firmware.get(firmware.id)

    expected = {
        100: entry_ids_under(firmware, tmp_path) | {spanning.id},
        301: entry_ids_under(firmware, tmp_path / "rev3") | {spanning.id},
        None: {e.id for e in firmware.entries},  # secure download mode: only the chip counts
    }[revision]
    assert {e.id for e in for_chip(context, firmware, "esp32p4", revision).entries} == expected
