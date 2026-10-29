"""Merge a firmware into one raw image (design doc §5 stage two "合成工具"): the enabled
entries (attachments included) and flash parameters of any firmware; tracking firmware use
their current resolution.
"""

from dataclasses import dataclass
from pathlib import Path

from embr.app.context import AppContext
from embr.app.tracking import Tracker
from embr.core.firmware.model import Firmware


@dataclass(frozen=True)
class MergeOptions:
    target_offset: int = 0
    pad_to_flash_size: bool = False
    """Pad to the firmware's flash size (or the size declared in its bootloader header)."""
    add_to_list: bool = True


class Merger:
    def __init__(self, context: AppContext, tracker: Tracker) -> None:
        raise NotImplementedError

    def command(self, firmware_id: str, output: Path, options: MergeOptions) -> str:
        """Equivalent `esptool ... merge-bin` command line for the dialog. PlanError when
        the firmware cannot be merged (see merge())."""
        raise NotImplementedError

    def merge(self, firmware_id: str, output: Path, options: MergeOptions) -> Firmware | None:
        """Write the merged image to `output` (via a temporary file in temp/, then moved).
        With add_to_list: a new MANUAL firmware named "<source> (merged)" (made unique) in
        the source's project and group, with the source's chip, flash parameters and reset
        modes, one enabled MERGED entry at target_offset referencing `output`, whose note
        lists the merged files, and notes giving the source and options; returned. Without
        it: None. The source firmware is unchanged. PlanError when no entry is enabled, the
        firmware is not flashable, it has no chip, or padding is asked without a known flash
        size; core.flasher.merge.MergeError propagates."""
        raise NotImplementedError
