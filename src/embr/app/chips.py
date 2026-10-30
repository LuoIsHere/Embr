"""Which MCU each entry is for (design doc §12 item 13). A fixed firmware may hold files
for several chips; a board only gets the entries for its own chip.

The MCU of an entry is, in this order: the chip in its image header (bootloader, app,
merged image; read-only); for tracking firmware the chip of the build; the chip chosen by
hand (Entry.chip); and when the firmware has exactly one chip, that chip. Otherwise it has
none, and the entry is not flashed to any board until one is chosen.
"""

from embr.app.context import AppContext
from embr.core.firmware.model import Entry, Firmware


def image_chip(context: AppContext, entry: Entry) -> str | None:
    """The chip named by the entry's image header (cached by content hash), else None."""
    raise NotImplementedError


def firmware_chips(context: AppContext, firmware: Firmware) -> tuple[str, ...]:
    """The chips of a firmware, sorted: tracking firmware the build's chip; fixed firmware
    the image chips and hand-chosen chips of all entries (enabled or not)."""
    raise NotImplementedError


def entry_chip(context: AppContext, firmware: Firmware, entry: Entry) -> str | None:
    """The MCU the entry is flashed to (see the module docstring), or None."""
    raise NotImplementedError


def chip_choosable(context: AppContext, firmware: Firmware, entry: Entry) -> bool:
    """The MCU can be chosen by hand: a fixed firmware's entry whose file names no chip."""
    raise NotImplementedError


def for_chip(context: AppContext, firmware: Firmware, chip: str) -> Firmware:
    """A shallow copy of `firmware` holding only the entries whose MCU is `chip` (the Entry
    objects are shared). Plans, checks and partition placement work on this view, so the
    entries of other chips never meet: no overlap, no second partition table."""
    raise NotImplementedError


def merge_problem(context: AppContext, firmware: Firmware) -> str | None:
    """Why the enabled entries cannot be merged into one image, or None: they are for more
    than one MCU ("Checked entries are for esp32p4 and esp32s3"), or an enabled entry has no
    MCU ("storage.bin has no MCU")."""
    raise NotImplementedError
