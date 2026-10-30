"""Merge entries into one raw image with esptool's merge_bin (design doc §5 stage two
"合成工具"). Offline, in the main process. Output is identical to the esptool command line
from merge_args() with the same esptool version.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from esptool.cmds import merge_bin  # pyright: ignore[reportUnknownVariableType]  # untyped **kwargs

from embr.core.flasher.plan import FlashSettings
from embr.i18n import N_


class MergeError(ValueError):
    """Nothing to merge, overlapping entries, an entry below target_offset, or padding
    requested without a known flash size."""


@dataclass(frozen=True)
class MergeInput:
    address: int
    path: Path


def merge_args(
    chip: str,
    entries: Sequence[MergeInput],
    flash: FlashSettings,
    output: Path,
    *,
    target_offset: int = 0,
    pad_to_size: str | None = None,
) -> list[str]:
    """esptool v5 arguments (without the program name): --chip, merge-bin, -o, --format raw,
    flash settings other than "keep", --target-offset when not 0, --pad-to-size when given,
    then address/path pairs sorted by address."""
    args = ["--chip", chip, "merge-bin", "-o", str(output), "--format", "raw"]
    for option, value in (
        ("--flash-mode", flash.mode),
        ("--flash-freq", flash.freq),
        ("--flash-size", flash.size),
    ):
        if value != "keep":
            args += [option, value]
    if target_offset:
        args += ["--target-offset", f"{target_offset:#x}"]
    if pad_to_size is not None:
        args += ["--pad-to-size", pad_to_size]
    for entry in sorted(entries, key=lambda e: e.address):
        args += [f"{entry.address:#x}", str(entry.path)]
    return args


def merge_image(
    chip: str,
    entries: Sequence[MergeInput],
    flash: FlashSettings,
    *,
    target_offset: int = 0,
    pad_to_size: str | None = None,
) -> bytes:
    """The merged image: gaps filled with 0xFF, the bootloader header patched with the
    flash settings as esptool does. `pad_to_size` is a flash size such as "4MB". Raises
    MergeError."""
    ordered = sorted(entries, key=lambda e: e.address)
    if not ordered:
        raise MergeError(N_("nothing to merge"))
    if ordered[0].address < target_offset:
        raise MergeError(
            N_(
                f"{ordered[0].path.name} at {ordered[0].address:#x} is below the start offset "
                f"{target_offset:#x}"
            )
        )
    for a, b in itertools.pairwise(ordered):
        if a.address + a.path.stat().st_size > b.address:
            raise MergeError(N_(f"{a.path.name} and {b.path.name} overlap"))
    merged = merge_bin(
        [(e.address, str(e.path)) for e in ordered],
        chip,
        None,
        flash_freq=flash.freq,
        flash_mode=flash.mode,
        flash_size=flash.size,
        format="raw",
        target_offset=target_offset,
        pad_to_size=pad_to_size,
    )
    assert merged is not None  # no output file: the bytes are returned
    return merged
