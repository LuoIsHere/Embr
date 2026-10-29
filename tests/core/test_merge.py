"""core/flasher/merge: merged raw images, byte for byte against `idf.py merge-bin`, the PIO
reference image and esptool's own command line (design doc §5 stage two "合成工具")."""

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from embr_testkit import IDF, PIO

from embr.core.flasher.merge import MergeError, MergeInput, merge_args, merge_image
from embr.core.flasher.plan import FlashSettings

ALIASES = {"-c": "--chip", "-o": "--output", "-f": "--format", "-t": "--target-offset"}
REFERENCES: dict[str, tuple[Path, Path]] = {
    # command file, folder its paths are relative to
    "idf_v61": (IDF / "esp32s3_hello_v61/idf_merge_command.txt", IDF / "esp32s3_hello_v61/build"),
    "pio_arduino": (
        PIO / "arduino_esp32_16mb/reference_merge_command.txt",
        PIO / "arduino_esp32_16mb",
    ),
}


def _parse(args: list[str]) -> tuple[dict[str, str], list[tuple[int, str]]]:
    """Options (long names) and address/path pairs of `... merge-bin ...` arguments."""
    options: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i].startswith("-"):
            options[ALIASES.get(args[i], args[i])] = args[i + 1]
            i += 2
        else:
            positional.append(args[i])
            i += 1
    assert positional[0] == "merge-bin"
    rest = positional[1:]
    return options, [(int(rest[i], 0), rest[i + 1]) for i in range(0, len(rest), 2)]


@pytest.mark.parametrize("name", list(REFERENCES))
def test_merge_matches_the_reference_image(name: str, tmp_path: Path) -> None:
    command_file, base = REFERENCES[name]
    tokens = shlex.split(command_file.read_text(encoding="utf-8"))
    assert tokens[0] == "esptool"
    reference, pairs = _parse(tokens[1:])
    chip = reference["--chip"]
    entries = [MergeInput(address, base / rel) for address, rel in pairs]
    flash = FlashSettings(
        reference["--flash-mode"], reference["--flash-freq"], reference["--flash-size"]
    )

    image = merge_image(chip, list(reversed(entries)), flash)
    assert image == (base / reference["--output"]).read_bytes()

    output = tmp_path / "merged.bin"
    args = merge_args(chip, list(reversed(entries)), flash, output)
    options, merged_pairs = _parse(args)
    assert args[:3] == ["--chip", chip, "merge-bin"]
    assert Path(options.pop("--output")) == output
    del reference["--output"]
    assert options == reference
    assert [(a, Path(p).resolve()) for a, p in merged_pairs] == [
        (e.address, e.path.resolve()) for e in entries
    ]


def _v61_entries(tmp_path: Path) -> list[MergeInput]:
    """The v6.1 sample's flash files plus a data attachment."""
    build = IDF / "esp32s3_hello_v61/build"
    args = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    files = cast(dict[str, str], args["flash_files"])
    data = tmp_path / "data.bin"
    data.write_bytes(b"\x5a" * 1000)
    entries = [MergeInput(int(address, 16), build / rel) for address, rel in files.items()]
    return [*entries, MergeInput(0x200000, data)]


CLI_VARIANTS: dict[str, dict[str, Any]] = {
    "keep_settings": {"flash": FlashSettings()},
    "target_offset": {"flash": FlashSettings("dio", "80m", "4MB"), "target_offset": 0x8000},
    "pad_to_size": {"flash": FlashSettings("qio", "40m", "4MB"), "pad_to_size": "4MB"},
}


@pytest.mark.parametrize("variant", list(CLI_VARIANTS))
def test_merge_image_equals_the_esptool_command_line(variant: str, tmp_path: Path) -> None:
    options = dict(CLI_VARIANTS[variant])
    flash = cast(FlashSettings, options.pop("flash"))
    offset = cast(int, options.get("target_offset", 0))
    entries = [e for e in _v61_entries(tmp_path) if e.address >= offset]
    output = tmp_path / "cli.bin"

    args = merge_args("esp32s3", entries, flash, output, **options)
    subprocess.run(
        [sys.executable, "-m", "esptool", *args],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=120,
    )
    image = merge_image("esp32s3", entries, flash, **options)

    assert image == output.read_bytes()
    if offset:  # the image starts with the entry at the offset (the partition table)
        first = min(entries, key=lambda e: e.address)
        assert first.address == offset and image.startswith(first.path.read_bytes())
    if "pad_to_size" in options:
        assert len(image) == 0x400000
    if flash == FlashSettings():
        assert "--flash-mode" not in args and "--flash-size" not in args


def test_invalid_layouts_raise_merge_error(tmp_path: Path) -> None:
    block = tmp_path / "block.bin"
    block.write_bytes(b"\x01" * 0x1000)
    flash = FlashSettings()
    with pytest.raises(MergeError):  # overlap
        merge_image("esp32", [MergeInput(0x0, block), MergeInput(0x800, block)], flash)
    with pytest.raises(MergeError):  # below the target offset
        merge_image("esp32", [MergeInput(0x0, block)], flash, target_offset=0x1000)
    with pytest.raises(MergeError):  # nothing to merge
        merge_image("esp32", [], flash)
