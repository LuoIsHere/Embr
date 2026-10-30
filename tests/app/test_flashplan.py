"""app/flashplan: FlashPlan from a firmware and the connection panel (design doc §7.2)."""

import dataclasses
import hashlib
from pathlib import Path

import pytest
from app_testkit import BRIDGE, JTAG, app_revision_range, build_files, fixed_firmware

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
    firmware = fixed_firmware(library, [*HELLO, unknown])
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
    library.set_param(firmware.id, "chip", chip)
    library.add_attachment(firmware.id, data, address=0x10000)
    firmware = library.firmware(firmware.id)

    assert assemble_plan(context, firmware, BRIDGE, None).stub is stub
    command = preview_command(context, firmware, BRIDGE, None).split()
    assert command and ("--no-stub" in command) is not stub
