"""Shared helpers for the app tests. Expected values come from the fixture files
(flasher_args.json, SOURCE.toml), never from Embr's implementation."""

import datetime
import json
import shutil
import tomllib
from pathlib import Path
from typing import cast

from embr.app.identify import IdentifySnapshot
from embr.app.library import FirmwareRow, Library, Tree
from embr.core.firmware.model import Firmware
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


def fixed_firmware(library: Library, files: list[Path], name: str = "fw") -> Firmware:
    """Standalone fixed firmware with `files` added."""
    firmware = library.create_firmware(name)
    library.add_files(firmware.id, files)
    return library.firmware(firmware.id)


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
