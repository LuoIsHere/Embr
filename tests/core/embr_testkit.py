"""Shared helpers for the core tests. Reference values come from esptool and the fixture
files directly, never from Embr's implementation."""

import hashlib
import json
import subprocess
import sys
import tomllib
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
