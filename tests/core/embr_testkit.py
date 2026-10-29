"""Shared helpers for the core tests. Reference values come from esptool and the fixture
files directly, never from Embr's implementation."""

import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import esptool.targets
import serial.tools.list_ports
from esptool.bin_image import LoadFirmwareImage

from embr.core.flasher.plan import PlanEntry
from embr.core.flasher.protocol import Event, decode_event

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
IDF = FIXTURES / "idf"
PIO = FIXTURES / "pio"
WORKER_TIMEOUT = 90.0


def chip_defs() -> dict[str, Any]:
    """esptool chip classes by name in esptool's order, ESP8266 excluded."""
    defs = cast(dict[str, Any], esptool.targets.CHIP_DEFS)
    names: list[str] = esptool.targets.CHIP_LIST
    return {name: defs[name] for name in names if name != "esp8266"}


def ref_revisions(data: bytes) -> tuple[str, int, int, int] | None:
    """(chip, min_rev_full, max_rev_full, legacy min_rev) from esptool's parser, or None."""
    if len(data) < 24 or data[0] != 0xE9:
        return None
    chip_id = int.from_bytes(data[12:14], "little")
    chips = [n for n, c in chip_defs().items() if chip_id == c.IMAGE_CHIP_ID]
    if not chips:
        return None
    image: Any = LoadFirmwareImage(chips[0], data)
    return chips[0], int(image.min_rev_full), int(image.max_rev_full), int(image.min_rev)


def idf_expect(sample: str) -> dict[str, str]:
    source = tomllib.loads((IDF / sample / "SOURCE.toml").read_text(encoding="utf-8"))
    return cast(dict[str, str], source["expect"])


def parse_rev_range(text: str) -> tuple[int, int]:
    """'v0.0 - v1.99' -> (0, 199)."""
    revs: list[int] = []
    for part in text.split("-"):
        major, minor = part.strip().lstrip("v").split(".")
        revs.append(int(major) * 100 + int(minor))
    return revs[0], revs[1]


def idf_app_path(sample: str) -> Path:
    build = IDF / sample / "build"
    args = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    return (build / cast(str, args["app"]["file"])).resolve()


def make_entry(address: int, path: Path, name: str | None = None) -> PlanEntry:
    """PlanEntry for a file, with image fields taken from esptool's parser."""
    data = path.read_bytes()
    ref = ref_revisions(data)
    return PlanEntry(
        address=address,
        path=str(path.resolve()),
        name=name if name is not None else path.name,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        image_chip=ref[0] if ref else None,
        min_rev_full=ref[1] if ref else None,
        max_rev_full=ref[2] if ref else None,
        legacy_min_rev=ref[3] if ref else None,
    )


def idf_entries(sample: str) -> list[PlanEntry]:
    """Entries of the sample's flasher_args.json, sorted by address."""
    build = IDF / sample / "build"
    args = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    files = cast(dict[str, str], args["flash_files"])
    pairs = sorted((int(addr, 16), build / rel) for addr, rel in files.items())
    return [make_entry(addr, path) for addr, path in pairs]


def missing_port() -> str:
    """A serial port name that is not present on this machine."""
    if sys.platform != "win32":
        return "/dev/embr-test-port-that-does-not-exist"
    present = {p.device.upper() for p in serial.tools.list_ports.comports()}
    return next(f"COM{n}" for n in range(250, 100, -1) if f"COM{n}" not in present)


def decode_lines(stdout: str) -> list[Event]:
    """Decode every non-empty line; a non-protocol line raises ProtocolError."""
    return [decode_event(line) for line in stdout.splitlines() if line.strip()]


def run_worker(stdin_text: str) -> tuple[int, list[Event]]:
    """Run `python -m embr --worker`; a hang fails the test through TimeoutExpired."""
    proc = subprocess.run(
        [sys.executable, "-m", "embr", "--worker"],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WORKER_TIMEOUT,
        cwd=REPO,
        check=False,
    )
    return proc.returncode, decode_lines(proc.stdout)


# ---- IDF builds assembled in tmp_path ----------------------------------------------------


def write_newer(path: Path, data: bytes) -> None:
    """Write `data` with a modification time 1 s after the previous one (an hour in the
    past for a new file), so a size + mtime cache always sees the change and no file looks
    like a build still being written."""
    old = path.stat().st_mtime_ns if path.exists() else time.time_ns() - 3600 * 10**9
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, ns=(old + 10**9, old + 10**9))


def edit_json(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    change(data)
    write_newer(path, json.dumps(data, indent=4).encode())


def set_target(build: Path, target: str) -> None:
    """The project's sdkconfig (next to `build`) with one CONFIG_IDF_TARGET line."""
    write_newer(build.parent / "sdkconfig", f'CONFIG_IDF_TARGET="{target}"\n'.encode())


def use_sample(build: Path, sample: str) -> None:
    """Overwrite `build` with the build files of an IDF sample (files of a previous sample
    stay); project_description.json's config_file points at ../sdkconfig."""
    source = IDF / sample / "build"
    for file in source.rglob("*"):
        if file.is_file():
            write_newer(build / file.relative_to(source), file.read_bytes())
    config = str(build.parent / "sdkconfig")
    edit_json(build / "project_description.json", lambda d: d.update(config_file=config))


def idf_project(sample: str, folder: Path, target: str | None = None) -> Path:
    """folder/build with a copy of the sample's build and folder/sdkconfig naming `target`
    (default: the sample's chip). Returns the build directory."""
    build = folder / "build"
    use_sample(build, sample)
    set_target(build, target or idf_expect(sample)["chip"])
    return build


def app_file(build: Path) -> Path:
    args = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    return build / cast(str, args["app"]["file"])


def move_file(build: Path, old: int, new: int) -> None:
    """Move the flash file at address `old` to `new` in flasher_args.json."""

    def change(args: dict[str, Any]) -> None:
        files = cast(dict[str, str], args["flash_files"])
        key = next(k for k in files if int(k, 16) == old)
        files[hex(new)] = files.pop(key)
        for part in args.values():
            if isinstance(part, dict) and cast(dict[str, Any], part).get("offset") == key:
                cast(dict[str, Any], part)["offset"] = hex(new)

    edit_json(build / "flasher_args.json", change)


def rename_app(build: Path, name: str) -> None:
    """Rename the app file and its references in flasher_args.json."""
    old = app_file(build)
    data = old.read_bytes()
    old.unlink()
    write_newer(build / name, data)
    rel = old.relative_to(build).as_posix()

    def change(args: dict[str, Any]) -> None:
        files = cast(dict[str, str], args["flash_files"])
        for key, value in files.items():
            if value == rel:
                files[key] = name
        args["app"]["file"] = name

    edit_json(build / "flasher_args.json", change)


def flip_last_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    write_newer(path, bytes(data))
