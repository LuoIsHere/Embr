"""Generate the synthetic fixtures in tests/fixtures/synthetic.

Run from the repository root:

    uv run python tests/fixtures/make_synthetic.py          # write the files
    uv run python tests/fixtures/make_synthetic.py --check  # verify the files on disk

Output is deterministic: merged images come from the IDF fixtures through the
pinned esptool, everything else from a fixed random seed.
"""

import argparse
import hashlib
import json
import random
import struct
import sys
from pathlib import Path

import tomli_w
from esptool.cmds import merge_bin  # pyright: ignore[reportUnknownVariableType]  # untyped **kwargs
from esptool.util import ImageSource

FIXTURES = Path(__file__).resolve().parent
IDF = FIXTURES / "idf"
OUT = FIXTURES / "synthetic"
SEED = 20261028

# IDF sample, chip, bootloader offset
MERGE_SOURCES = {
    "esp32": ("esp32_hello", 0x1000),
    "esp32s3": ("esp32s3_hello", 0x0),
    "esp32p4": ("esp32p4_rev1_hello", 0x2000),
}


def idf_images(sample: str) -> list[tuple[int, bytes]]:
    build = IDF / sample / "build"
    args = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    files: dict[str, str] = args["flash_files"]
    return sorted((int(addr, 16), (build / rel).read_bytes()) for addr, rel in files.items())


def build_merged(chip: str, target_offset: int, pad_to_size: str | None = None) -> bytes:
    """Merge the IDF hello_world images of `chip`; also used by tests for padded images."""
    sample, _ = MERGE_SOURCES[chip]
    images: list[tuple[int, ImageSource]] = list(idf_images(sample))
    data = merge_bin(images, chip, None, target_offset=target_offset, pad_to_size=pad_to_size)
    assert isinstance(data, bytes)
    return data


def minimal_elf() -> bytes:
    """ELF32 little-endian header for Xtensa (e_machine 94), padded to 4 KiB."""
    ident = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    header = ident + struct.pack(
        "<HHIIIIIHHHHHH", 2, 94, 1, 0x40080000, 52, 0, 0x300, 52, 32, 0, 40, 0, 0
    )
    return header + bytes(4096 - len(header))


def generate() -> dict[str, tuple[bytes, str]]:
    """Map of relative path -> (content, purpose)."""
    rng = random.Random(SEED)
    out: dict[str, tuple[bytes, str]] = {}
    for chip, (sample, bl) in MERGE_SOURCES.items():
        offsets = [0x0] if bl == 0 else [0x0, bl]
        for off in offsets:
            out[f"merged/{chip}_offset_{off:#x}.bin"] = (
                build_merged(chip, off),
                f"merged image of idf/{sample}, target offset {off:#x}, bootloader at {bl:#x}",
            )
    out["attachments/assets_fits.bin"] = (
        rng.randbytes(0x4000),
        "attachment placed by partition 'assets' (0x6000 at 0x1a000 in idf/esp32s3_ota_custom)",
    )
    out["attachments/assets_too_big.bin"] = (
        rng.randbytes(0x7000),
        "attachment larger than partition 'assets'; placing it by partition must be refused",
    )
    out["attachments/calib_at_address.bin"] = (
        rng.randbytes(0x1000),
        "attachment placed by address 0x3ff000",
    )
    out["negative/random_64k.bin"] = (
        rng.randbytes(0x10000),
        "random data, must classify as unknown",
    )
    out["negative/program.elf"] = (minimal_elf(), "ELF header, must classify as unknown")
    out["negative/notes.txt"] = (
        b"Embr negative sample.\nThis is a text file, not a firmware image.\n" * 32,
        "text file, must classify as unknown",
    )
    out["negative/ff_4k.bin"] = (
        b"\xff" * 0x1000,
        "all 0xFF but not 8 KiB, must not classify as otadata",
    )
    out["variants/app_no_desc.bin"] = (
        app_without_descriptor(),
        "idf/esp32s3_hello app with the esp_app_desc_t magic cleared; classify by segments",
    )
    out["variants/partition_table_no_md5.bin"] = (
        partition_table_without_md5(),
        "idf/esp32s3_hello partition table with its MD5 entry removed",
    )
    return out


def app_without_descriptor() -> bytes:
    """App image whose first segment no longer starts with the app descriptor magic."""
    data = bytearray((IDF / "esp32s3_hello" / "build" / "hello_esp32s3.bin").read_bytes())
    # the first segment's data starts after the 24-byte image header and 8-byte segment header
    data[0x20:0x24] = bytes(4)
    return bytes(data)


def partition_table_without_md5() -> bytes:
    """Partition table with the 0xEBEB MD5 entry blanked to 0xFF."""
    path = IDF / "esp32s3_hello" / "build" / "partition_table" / "partition-table.bin"
    data = bytearray(path.read_bytes())
    for i in range(0, len(data), 32):
        if data.startswith(b"\xeb\xeb", i):
            data[i : i + 32] = b"\xff" * 32
    return bytes(data)


def manifest(files: dict[str, tuple[bytes, str]]) -> bytes:
    doc = {
        "schema": 1,
        "kind": "synthetic",
        "generator": "tests/fixtures/make_synthetic.py",
        "seed": SEED,
        "padded_images": "not stored; tests call build_merged(chip, offset, pad_to_size) instead",
        "files": {
            path: {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "purpose": purpose,
            }
            for path, (data, purpose) in sorted(files.items())
        },
    }
    return tomli_w.dumps(doc).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the synthetic fixtures.")
    parser.add_argument(
        "--check", action="store_true", help="compare with the files on disk instead of writing"
    )
    check = parser.parse_args().check
    files = generate()
    files["SOURCE.toml"] = (manifest(files), "")
    stale = [
        p
        for p, (data, _) in files.items()
        if not (OUT / p).is_file() or (OUT / p).read_bytes() != data
    ]
    if check:
        for p in stale:
            print(f"differs: {p}")
        return 1 if stale else 0
    for p, (data, _) in files.items():
        (OUT / p).parent.mkdir(parents=True, exist_ok=True)
        (OUT / p).write_bytes(data)
    print(f"wrote {len(files)} files to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
