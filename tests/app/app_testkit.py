"""Shared helpers for the app tests. Expected values come from the fixture files
(flasher_args.json, SOURCE.toml), never from Embr's implementation."""

import configparser
import datetime
import json
import os
import re
import shutil
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any, cast

from embr.app.context import AppContext
from embr.app.identify import IdentifySnapshot
from embr.app.library import FirmwareRow, Library, Tree
from embr.app.pio import PioConfigs
from embr.core.firmware.entries import entry_from_file
from embr.core.firmware.image_header import NO_MAX_REVISION
from embr.core.firmware.import_pio import PioUnavailableError
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Firmware
from embr.core.flasher.checks import SecurityState
from embr.core.flasher.protocol import InfoEvent
from embr.core.serial import ConnectionType, PortInfo

REPO = Path(__file__).resolve().parents[2]
IDF = REPO / "tests" / "fixtures" / "idf"
PIO = REPO / "tests" / "fixtures" / "pio"

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


def _stored_firmware(
    context: AppContext, folder: Path, name: str, builds: list[tuple[str, Path, str, str]]
) -> Firmware:
    """Fixed firmware stored directly, folder = `folder`: for each (sample, subfolder, chip,
    line) the sample's build copied to the subfolder, entries as sniffed, the partition
    table with chip and line chosen by hand."""
    firmware = context.firmware.create(name)
    firmware.folder = str(folder.resolve())
    for sample, subfolder, chip, line in builds:
        for path in copy_files(build_files(sample), subfolder):
            entry = entry_from_file(path)
            if entry.kind == EntryKind.PARTITION_TABLE:
                entry.chip, entry.chip_line = chip, line
            firmware.entries.append(entry)
    context.firmware.save(firmware)
    return context.firmware.get(firmware.id)


def multi_chip_firmware(context: AppContext, folder: Path, name: str = "multi") -> Firmware:
    """Fixed firmware holding the hello builds for esp32s3 (copied to `folder`) and esp32p4
    v0-v1 (copied to folder/p4), stored directly: entries as sniffed, each partition table
    with its MCU chosen by hand, folder = `folder`. The P4 bootloader (0x2000) overlaps the
    S3 one and both tables sit at 0x8000, so only per-chip handling keeps them apart."""
    builds = [
        ("esp32s3_hello", folder, "esp32s3", ""),
        ("esp32p4_rev1_hello", folder / "p4", "esp32p4", "v0-v1"),
    ]
    return _stored_firmware(context, folder, name, builds)


def p4_lines_firmware(context: AppContext, folder: Path, name: str = "p4") -> Firmware:
    """Fixed firmware holding the ESP32-P4 hello builds for v0-v1 (copied to `folder`) and
    v3 (copied to folder/rev3), stored as multi_chip_firmware(), each table with its line.
    Both builds use the same addresses, so only per-line handling keeps them apart."""
    builds = [
        ("esp32p4_rev1_hello", folder, "esp32p4", "v0-v1"),
        ("esp32p4_rev3_hello", folder / "rev3", "esp32p4", "v3"),
    ]
    return _stored_firmware(context, folder, name, builds)


def spanning_app(path: Path) -> Path:
    """The P4 v0-v1 hello app written to `path` with max_rev_full patched to "no upper
    limit" (v0.0 and later), so its range spans both P4 lines."""
    data = bytearray(build_files("esp32p4_rev1_hello")[2].read_bytes())
    data[17:19] = NO_MAX_REVISION.to_bytes(2, "little")  # extended header, after min_rev_full
    write_newer(path, bytes(data))
    return path.resolve()


def entry_ids_under(firmware: Firmware, folder: Path) -> set[str]:
    """Ids of the "path:" entries whose file lies in `folder` (no subfolders)."""
    return {
        e.id
        for e in firmware.entries
        if e.file.startswith("path:") and Path(e.file[5:]).parent == folder.resolve()
    }


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


# ---- PlatformIO projects (same helpers as tests/core/embr_testkit.py) --------------------

PIO_BOARDS = {"esp32dev": "esp32", "esp32-s3-devkitc-1": "esp32s3"}
"""Board definitions written into the fake PIO core (build.mcu)."""
PIO_LIST_OPTIONS = ("framework", "platform_packages", "build_flags", "lib_deps")
"""Options `pio project config --json-output` prints as JSON lists."""


def pio_project(sample: str, root: Path, core: Path) -> None:
    """A copy of a PIO sample: root/platformio.ini from SOURCE.toml (an hour old), the
    sample's .pio/build/ 10 s younger with <PROJECT_DIR>, <PIO_CORE> and <HOME> in
    idedata.json replaced (<HOME> by root/../home), and `core` holding the sample's
    pio_core/ plus board definitions for PIO_BOARDS in platforms/espressif32/boards/."""
    source = PIO / sample
    t0 = time.time_ns() - 3600 * 10**9
    ini = root / "platformio.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    text = tomllib.loads((source / "SOURCE.toml").read_text(encoding="utf-8"))["platformio_ini"]
    ini.write_text(cast(str, text), encoding="utf-8")
    os.utime(ini, ns=(t0, t0))
    places = {"<PROJECT_DIR>": root, "<PIO_CORE>": core, "<HOME>": root.parent / "home"}
    for file in (source / ".pio").rglob("*"):
        if not file.is_file():
            continue
        target = root / ".pio" / file.relative_to(source / ".pio")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = file.read_bytes()
        if file.name == "idedata.json":
            content = data.decode("utf-8")
            for key, value in places.items():
                content = content.replace(key, json.dumps(str(value))[1:-1])  # JSON-escaped
            data = content.encode("utf-8")
        target.write_bytes(data)
        os.utime(target, ns=(t0 + 10**10, t0 + 10**10))
    if (source / "pio_core").is_dir():
        shutil.copytree(source / "pio_core", core, dirs_exist_ok=True)
    boards = core / "platforms" / "espressif32" / "boards"
    boards.mkdir(parents=True, exist_ok=True)
    for board, mcu in PIO_BOARDS.items():
        (boards / f"{board}.json").write_text(json.dumps({"build": {"mcu": mcu}}))


def pio_build(root: Path, env: str) -> Path:
    return root / ".pio" / "build" / env


def idedata_images(build: Path) -> list[tuple[int, str]]:
    """(address, file name) of the images idedata.json lists plus firmware.bin, by address."""

    def offset(value: str | int) -> int:
        return value if isinstance(value, int) else int(value, 16)

    extra = json.loads((build / "idedata.json").read_text(encoding="utf-8"))["extra"]
    images = [(offset(i["offset"]), PureWindowsPath(i["path"]).name) for i in extra["flash_images"]]
    return sorted([*images, (offset(extra["application_offset"]), "firmware.bin")])


def write_ini(root: Path, text: str) -> None:
    """Rewrite platformio.ini as a user edit would: newer than every build file (so
    idedata.json counts as outdated), still half an hour older than the tracker clock."""
    ini = root / "platformio.ini"
    mtime = max(time.time_ns() - 1800 * 10**9, ini.stat().st_mtime_ns + 10**9)
    ini.write_text(text, encoding="utf-8")
    os.utime(ini, ns=(mtime, mtime))


def make_package(folder: Path, version: str, boot_app0: bytes = b"\xff" * 0x2000) -> Path:
    """An Arduino framework package: package.json naming `version` and
    tools/partitions/boot_app0.bin. Returns `folder`."""
    (folder / "tools" / "partitions").mkdir(parents=True, exist_ok=True)
    (folder / "tools" / "partitions" / "boot_app0.bin").write_bytes(boot_app0)
    package = {"name": "framework-arduinoespressif32", "version": version}
    (folder / "package.json").write_text(json.dumps(package), encoding="utf-8")
    return folder


def pio_config_json(root: Path) -> str:
    """What `pio project config --json-output` prints for root/platformio.ini: a list of
    [section, [[option, value], ...]] with [env] merged into every [env:*] and list options
    as JSON lists."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(root / "platformio.ini", encoding="utf-8")
    base = dict(parser["env"]) if parser.has_section("env") else {}

    def value(option: str, text: str) -> str | list[str]:
        if option not in PIO_LIST_OPTIONS:
            return text
        return [part.strip() for part in re.split(r"[,\n]", text) if part.strip()]

    sections: list[list[Any]] = []
    for name in parser.sections():
        if name == "env" or name.startswith("env:"):
            options = base if name == "env" else {**base, **dict(parser[name])}
            sections.append([name, [[k, value(k, v)] for k, v in options.items()]])
    return json.dumps(sections)


class FakePio:
    """Runner for PioConfigs: answers from the platformio.ini in its working directory
    (pio_config_json()) and records every argv. Raises PioUnavailableError while `fail`."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False

    def __call__(self, argv: Sequence[str], cwd: Path, timeout: float) -> str:
        self.calls.append(list(argv))
        if self.fail:
            raise PioUnavailableError("pio exited with status 1")
        return pio_config_json(cwd)


def pio_configs(
    core: Path,
    runner: FakePio,
    *,
    start: Callable[[Callable[[], None]], None] | None = None,
    on_ready: Callable[[Path], None] | None = None,
) -> PioConfigs:
    """PioConfigs with PLATFORMIO_CORE_DIR = `core`, pio found on PATH (a dummy file next to
    `core`), `runner` and a synchronous `start` unless one is given."""
    pio = core.parent / "bin" / "pio.exe"
    pio.parent.mkdir(parents=True, exist_ok=True)
    pio.write_bytes(b"")
    environ = {"PLATFORMIO_CORE_DIR": str(core), "USERPROFILE": str(core.parent / "home")}

    def run_now(job: Callable[[], None]) -> None:
        job()

    return PioConfigs(
        environ=environ,
        run=runner,
        which=lambda _: str(pio),
        start=start or run_now,
        on_ready=on_ready,
    )
