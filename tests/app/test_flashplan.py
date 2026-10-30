"""app/flashplan: FlashPlan from a firmware and the connection panel (design doc §7.2)."""

import dataclasses
import hashlib
from pathlib import Path

import pytest
from app_testkit import (
    BRIDGE,
    JTAG,
    app_revision_range,
    build_files,
    copy_files,
    fixed_firmware,
    multi_chip_firmware,
    p4_lines_firmware,
)

from embr.app.context import AppContext
from embr.app.flashplan import (
    PREVIEW_PORT,
    PlanError,
    assemble_plan,
    plan_entries,
    preview_command,
)
from embr.app.library import Library
from embr.core.firmware import entries
from embr.core.serial import PortInfo

HELLO = build_files("esp32s3_hello")


def test_plan_entries_resolve_files_by_address(context: AppContext) -> None:
    boot, table, app = HELLO
    data = boot.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    (context.paths.archive / f"{sha}.bin").write_bytes(data)
    archived = dataclasses.replace(entries.entry_from_file(boot), file=f"archive:{sha}")
    disabled = dataclasses.replace(entries.entry_from_file(table), enabled=False)
    blank = entries.blank_otadata_entry(0xD000, 0x2000)
    firmware = context.firmware.create("plan")
    firmware.chip = "esp32s3"
    firmware.entries = [entries.entry_from_file(app), blank, disabled, archived]

    planned = plan_entries(context, firmware, write_files=False)

    assert [e.address for e in planned] == [0x0, 0xD000, 0x10000]
    assert Path(planned[0].path).resolve() == (context.paths.archive / f"{sha}.bin").resolve()
    assert Path(planned[2].path).resolve() == app
    assert planned[0].image_chip == "esp32s3"
    assert planned[1].image_chip is None
    revisions = (planned[2].min_rev_full, planned[2].max_rev_full)
    assert revisions == app_revision_range("esp32s3_hello")


def test_blank_file_is_written_only_on_request(context: AppContext) -> None:
    firmware = context.firmware.create("blank")
    firmware.chip = "esp32s3"
    firmware.entries = [entries.blank_otadata_entry(0xD000, 0x2000)]

    (preview,) = plan_entries(context, firmware, write_files=False)
    path = Path(preview.path)
    assert path.resolve() == (context.paths.temp / "blank-8192.bin").resolve()
    assert not path.exists()

    (written,) = plan_entries(context, firmware, write_files=True)
    assert Path(written.path) == path
    assert path.read_bytes() == b"\xff" * 0x2000
    assert (written.sha256, written.size) == (hashlib.sha256(b"\xff" * 0x2000).hexdigest(), 0x2000)


@pytest.mark.parametrize(
    ("port", "baud", "after", "adjusted"),
    [(JTAG, None, "watchdog-reset", True), (BRIDGE, 921600, "hard-reset", False)],
    ids=["usb-serial-jtag", "uart-bridge"],
)
def test_plan_follows_the_connection(
    library: Library,
    context: AppContext,
    port: PortInfo,
    baud: int | None,
    after: str,
    adjusted: bool,
) -> None:
    firmware = fixed_firmware(library, HELLO)
    plan = assemble_plan(context, firmware, port, 921600)
    assert (plan.baud, plan.after, plan.chip) == (baud, after, "esp32s3")
    assert bool(plan.adjustments) == adjusted
    assert [e.address for e in plan.entries] == [0x0, 0x8000, 0x10000]


def test_skip_flashed_unless_erase_all(library: Library, context: AppContext) -> None:
    firmware = fixed_firmware(library, HELLO)
    assert assemble_plan(context, firmware, BRIDGE, None).skip_flashed
    firmware = library.set_param(firmware.id, "erase_all", True)
    plan = assemble_plan(context, firmware, BRIDGE, None)
    assert plan.erase_all
    assert not plan.skip_flashed


@pytest.mark.parametrize(("flash_size", "declared"), [("keep", "2MB"), ("4MB", "4MB")])
def test_declared_flash_size(
    library: Library, context: AppContext, flash_size: str, declared: str
) -> None:
    # "keep" falls back to the bootloader header (2MB in this sample)
    firmware = fixed_firmware(library, HELLO)
    firmware = library.set_param(firmware.id, "flash_size", flash_size)
    assert assemble_plan(context, firmware, BRIDGE, None).declared_flash_size == declared


def test_preview_without_port_writes_nothing(library: Library, context: AppContext) -> None:
    firmware = fixed_firmware(library, build_files("esp32s3_ota_custom"))
    command = preview_command(context, firmware, None, None)
    assert f"-p {PREVIEW_PORT}" in command
    assert "-b" in command.split()
    assert list(context.paths.temp.glob("blank-*")) == []


def test_entry_without_address_cannot_be_planned(
    library: Library, context: AppContext, tmp_path: Path
) -> None:
    unknown = tmp_path / "data.bin"
    unknown.write_bytes(bytes(range(1, 200)))
    firmware = fixed_firmware(library, [*copy_files(HELLO, tmp_path), unknown])
    assert [e.address for e in firmware.entries if e.name == "data.bin"] == [None]

    with pytest.raises(PlanError):
        assemble_plan(context, firmware, BRIDGE, None)
    assert preview_command(context, firmware, BRIDGE, None) == ""


@pytest.mark.parametrize(("chip", "stub"), [("esp32h21", False), ("esp32s3", True)])
def test_chip_without_stub_is_planned_without_it(
    library: Library, context: AppContext, tmp_path: Path, chip: str, stub: bool
) -> None:
    # esptool 5.3.1 has no flasher stub for ESP32-H21 (design doc §12.1)
    data = tmp_path / "data.bin"
    data.write_bytes(b"" * 64)
    firmware = library.create_firmware("fw")
    entry = library.add_attachment(firmware.id, data, address=0x10000)
    library.set_entry_chip(firmware.id, entry.id, chip)  # the firmware's only chip
    firmware = library.firmware(firmware.id)

    assert assemble_plan(context, firmware, BRIDGE, None).stub is stub
    command = preview_command(context, firmware, BRIDGE, None).split()
    assert command and ("--no-stub" in command) is not stub


# ---- several chips in one firmware (§12 item 13) ------------------------------------------


@pytest.mark.parametrize(
    ("chip", "addresses", "app"),
    [
        ("esp32s3", [0x0, 0x8000, 0x10000], "hello_esp32s3.bin"),
        ("esp32p4", [0x2000, 0x8000, 0x10000], "hello_esp32p4_rev1.bin"),
    ],
)
def test_plan_for_a_chip_takes_only_its_entries(
    context: AppContext, tmp_path: Path, chip: str, addresses: list[int], app: str
) -> None:
    firmware = multi_chip_firmware(context, tmp_path)
    plan = assemble_plan(context, firmware, BRIDGE, None, chip=chip)
    assert (plan.chip, [e.address for e in plan.entries]) == (chip, addresses)
    assert plan.entries[2].name == app
    assert plan.entries[1].image_chip == chip  # the table's hand-chosen MCU
    assert (plan.declared_flash_size, plan.partitions_end is not None) == ("2MB", True)


def test_multi_chip_firmware_has_no_plan_before_identification(
    context: AppContext, tmp_path: Path
) -> None:
    firmware = multi_chip_firmware(context, tmp_path)
    with pytest.raises(PlanError):
        assemble_plan(context, firmware, BRIDGE, None)
    assert preview_command(context, firmware, None, None) == ""
    command = preview_command(context, firmware, None, None, chip="esp32p4")
    assert "hello_esp32p4_rev1.bin" in command and "hello_esp32s3.bin" not in command


APPS = {100: "hello_esp32p4_rev1.bin", 301: "hello_esp32p4_rev3.bin"}


@pytest.mark.parametrize(("revision", "subfolder"), [(100, "."), (301, "rev3")])
def test_plan_for_a_p4_revision_takes_only_its_line(
    context: AppContext, tmp_path: Path, revision: int, subfolder: str
) -> None:
    # both lines' builds sit at 0x2000 / 0x8000 / 0x10000; the tables' lines are chosen by hand
    firmware = p4_lines_firmware(context, tmp_path)
    plan = assemble_plan(context, firmware, BRIDGE, None, chip="esp32p4", revision=revision)
    assert [e.address for e in plan.entries] == [0x2000, 0x8000, 0x10000]
    folders = {Path(e.path).parent for e in plan.entries}
    assert folders == {(tmp_path / subfolder).resolve()}
    command = preview_command(context, firmware, None, None, chip="esp32p4", revision=revision)
    other = next(name for rev, name in APPS.items() if rev != revision)
    assert APPS[revision] in command and other not in command


def test_firmware_with_files_outside_its_folder_still_plans(
    context: AppContext, tmp_path: Path
) -> None:
    # made before the folder rule (§12 item 8): kept as is, still flashable
    boot, table, app = HELLO
    (boot_copy,) = copy_files([boot], tmp_path / "a")
    others = copy_files([table, app], tmp_path / "b")
    firmware = context.firmware.create("legacy")
    firmware.entries = [entries.entry_from_file(p) for p in [boot_copy, *others]]
    firmware.folder = str((tmp_path / "b").resolve())
    context.firmware.save(firmware)

    assert [e.name for e in entries.entries_outside(firmware)] == [boot.name]
    plan = assemble_plan(context, firmware, BRIDGE, None)
    assert [e.address for e in plan.entries] == [0x0, 0x8000, 0x10000]
