"""Merge entries into one raw image with esptool's merge_bin (design doc §5 stage two
"合成工具"). Offline, in the main process. Output is identical to the esptool command line
from merge_args() with the same esptool version.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from embr.core.flasher.plan import FlashSettings


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
    raise NotImplementedError


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
    raise NotImplementedError
