"""Shared helpers for the app tests. Expected values come from the fixture files
(flasher_args.json, SOURCE.toml), never from Embr's implementation."""

import datetime
import json
import os
import shutil
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from embr.app.context import AppContext
from embr.app.identify import IdentifySnapshot
from embr.app.library import FirmwareRow, Library, Tree
from embr.core.firmware.entries import attachment_entry, entry_from_file
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Entry, Firmware
from embr.core.flasher.checks import SecurityState
from embr.core.flasher.protocol import InfoEvent
from embr.core.serial import ConnectionType, PortInfo

REPO = Path(__file__).resolve().parents[2]
IDF = REPO / "tests" / "fixtures" / "idf"

BRIDGE = PortInfo(
    device="COM3",
    vid=0x10C4,
    pid=0xEA60,
    serial_number="0001",
    description="USB to UART bridge",
    connection=ConnectionType.UART_BRIDGE,
    otg_chip=None,
)
JTAG = PortInfo(
    device="COM3",
    vid=0x303A,
    pid=0x1001,
    serial_number="SN0001",
    description="USB JTAG/serial debug unit",
    connection=ConnectionType.USB_SERIAL_JTAG,
    otg_chip=None,
)
NO_SECURITY = SecurityState(secure_download_mode=False, flash_encryption=False, secure_boot=False)
MAC = "aa:bb:cc:00:00:01"


def build_files(sample: str) -> list[Path]:
    """[bootloader, partition table, app] of an IDF sample, from its flasher_args.json."""
    build = IDF / sample / "build"
    args = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    parts = cast(dict[str, dict[str, str]], args)
    return [
        (build / parts[key]["file"]).resolve() for key in ("bootloader", "partition-table", "app")
    ]


def copy_files(files: list[Path], folder: Path) -> list[Path]:
    """Copies of `files` in `folder` (created), same names."""
    folder.mkdir(parents=True, exist_ok=True)
    return [Path(shutil.copy(f, folder / f.name)).resolve() for f in files]


def app_revision_range(sample: str) -> tuple[int, int]:
    """(min_rev_full, max_rev_full) of the app from SOURCE.toml, e.g. 'v3.1 - v3.99'."""
    source = tomllib.loads((IDF / sample / "SOURCE.toml").read_text(encoding="utf-8"))
    text = cast(dict[str, str], source["expect"])["app_chip_revision"]
    low, high = (part.strip().lstrip("v").split(".") for part in text.split("-"))
    return int(low[0]) * 100 + int(low[1]), int(high[0]) * 100 + int(high[1])


def root_first(files: list[Path]) -> list[Path]:
    """`files` with the shallowest first, so the firmware's folder (the folder of the first
    file, design doc §12 item 8) holds all of them, e.g. an IDF build's bootloader/ and
    partition_table/ files below the app."""
    return sorted(files, key=lambda p: len(p.parts))


def fixed_firmware(library: Library, files: list[Path], name: str = "fw") -> Firmware:
    """Standalone fixed firmware with `files` added (root_first())."""
    firmware = library.create_firmware(name)
    library.add_files(firmware.id, root_first(files))
    return library.firmware(firmware.id)


def multi_chip_firmware(context: AppContext, folder: Path, name: str = "multi") -> Firmware:
    """Fixed firmware holding the hello builds for esp32s3 (copied to `folder`) and esp32p4
    (copied to folder/p4), stored directly: entries as sniffed, each partition table with
    its chip chosen by hand, folder = `folder`. The P4 bootloader (0x2000) overlaps the S3
    one and both tables sit at 0x8000, so only per-chip handling keeps them apart."""
    firmware = context.firmware.create(name)
    firmware.folder = str(folder.resolve())
    for chip, files in (
        ("esp32s3", copy_files(build_files("esp32s3_hello"), folder)),
        ("esp32p4", copy_files(build_files("esp32p4_rev1_hello"), folder / "p4")),
    ):
        for path in files:
            entry = entry_from_file(path)
            if entry.kind == EntryKind.PARTITION_TABLE:
                entry.chip = chip
            firmware.entries.append(entry)
    context.firmware.save(firmware)
    return context.firmware.get(firmware.id)


def entry_ids_under(firmware: Firmware, folder: Path) -> set[str]:
    """Ids of the "path:" entries whose file lies in `folder` (no subfolders)."""
    return {
        e.id
        for e in firmware.entries
        if e.file.startswith("path:") and Path(e.file[5:]).parent == folder.resolve()
    }


def seed_user_entry(
    library: Library, context: AppContext, firmware_id: str, path: Path, address: int
) -> Entry:
    """An attachment stored as a user entry of a tracking firmware, as data from before
    design doc §12 item 16 has it (Library no longer adds them)."""
    firmware = context.firmware.get(firmware_id)
    entry = attachment_entry(path, address=address)
    firmware.user_entries.append(entry)
    context.firmware.save(firmware)
    library.tracker.invalidate(firmware_id)
    return entry


def info(
    chip: str,
    major: int,
    minor: int,
    flash_size: str | None = "2MB",
    security: SecurityState = NO_SECURITY,
) -> InfoEvent:
    return InfoEvent(
        chip=chip,
        revision=f"v{major}.{minor}",
        major=major,
        minor=minor,
        mac=MAC,
        flash_size=flash_size,
        security=security,
        in_download_mode=False,
    )


def snapshot(port: PortInfo, event: InfoEvent) -> IdentifySnapshot:
    return IdentifySnapshot(port=port, info=event, time=datetime.datetime(2026, 1, 1, 12, 0))


def find_row(tree: Tree, firmware_id: str) -> FirmwareRow:
    rows = [r for p in tree.projects for g in p.groups for r in g.firmware]
    rows += tree.standalone
    return next(r for r in rows if r.id == firmware_id)


# ---- IDF builds assembled in tmp_path (same helpers as tests/core/embr_testkit.py) -------


class Clock:
    """Tracker clock: an hour ahead of the files' modification times, moved by hand."""

    def __init__(self) -> None:
        self.now = datetime.datetime.now().replace(microsecond=0) + datetime.timedelta(hours=1)

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)


def write_newer(path: Path, data: bytes) -> None:
    """Write `data` with a modification time 1 s after the previous one (an hour in the
    past for a new file), so the change is always seen and never looks like a build."""
    old = path.stat().st_mtime_ns if path.exists() else time.time_ns() - 3600 * 10**9
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, ns=(old + 10**9, old + 10**9))


def edit_json(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    change(data)
    write_newer(path, json.dumps(data, indent=4).encode())


def set_target(build: Path, target: str) -> None:
    write_newer(build.parent / "sdkconfig", f'CONFIG_IDF_TARGET="{target}"\n'.encode())


def idf_project(sample: str, folder: Path) -> Path:
    """folder/build: copy of the sample's build, config_file pointing at folder/sdkconfig
    (the sample's target). Returns the build directory."""
    source = IDF / sample / "build"
    build = folder / "build"
    for file in source.rglob("*"):
        if file.is_file():
            write_newer(build / file.relative_to(source), file.read_bytes())
    config = str(folder / "sdkconfig")
    edit_json(build / "project_description.json", lambda d: d.update(config_file=config))
    expect = tomllib.loads((IDF / sample / "SOURCE.toml").read_text(encoding="utf-8"))
    set_target(build, cast(dict[str, str], expect["expect"])["chip"])
    return build


def move_app(build: Path, address: int) -> None:
    """Give the app another address in flasher_args.json."""

    def change(args: dict[str, Any]) -> None:
        files = cast(dict[str, str], args["flash_files"])
        key = next(k for k, v in files.items() if v == args["app"]["file"])
        files[hex(address)] = files.pop(key)
        args["app"]["offset"] = hex(address)

    edit_json(build / "flasher_args.json", change)


def flip_last_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    write_newer(path, bytes(data))
