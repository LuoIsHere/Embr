"""Which MCU each entry is for (design doc §12 item 13). A fixed firmware may hold files
for several chips; a board only gets the entries for its own chip.

The MCU of an entry is, in this order: the chip in its image header (bootloader, app,
merged image; read-only); for tracking firmware the chip of the build; the chip chosen by
hand (Entry.chip); and when the firmware has exactly one chip, that chip. Otherwise it has
none, and the entry is not flashed to any board until one is chosen.
"""

import dataclasses
import hashlib

from embr.app.context import AppContext
from embr.core.firmware.files import read_entry
from embr.core.firmware.image_header import ImageHeader, read_image_header
from embr.core.firmware.image_sniff import sniff
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Entry, Firmware


def image_header(context: AppContext, entry: Entry) -> ImageHeader | None:
    """Header of a bootloader, app or merged entry (for merged images, its bootloader).
    Cached by content hash; None for other kinds and for unreadable files."""
    if entry.kind not in (EntryKind.BOOTLOADER, EntryKind.APP, EntryKind.MERGED):
        return None
    if entry.sha256 in _headers:
        return _headers[entry.sha256]
    try:
        data = read_entry(entry, context.paths.archive)
    except OSError:
        return None
    if entry.kind == EntryKind.MERGED:
        header = sniff(data, entry.name).header
    else:
        header = read_image_header(data)
    if hashlib.sha256(data).hexdigest() == entry.sha256:  # cache only the recorded content
        _headers[entry.sha256] = header
    return header


_headers: dict[str, ImageHeader | None] = {}


def image_chip(context: AppContext, entry: Entry) -> str | None:
    """The chip named by the entry's image header (cached by content hash), else None."""
    header = image_header(context, entry)
    return header.chip if header is not None else None


def firmware_chips(context: AppContext, firmware: Firmware) -> tuple[str, ...]:
    """The chips of a firmware, sorted: tracking firmware the build's chip; fixed firmware
    the image chips and hand-chosen chips of all entries (enabled or not)."""
    if firmware.source.tracking:
        return (firmware.chip,) if firmware.chip else ()
    found = {image_chip(context, e) or e.chip for e in firmware.entries}
    return tuple(sorted(c for c in found if c))


def entry_chip(context: AppContext, firmware: Firmware, entry: Entry) -> str | None:
    """The MCU the entry is flashed to (see the module docstring), or None."""
    return _entry_chip(context, firmware, entry, firmware_chips(context, firmware))


def chip_choosable(context: AppContext, firmware: Firmware, entry: Entry) -> bool:
    """The MCU can be chosen by hand: a fixed firmware's entry whose file names no chip."""
    return not firmware.source.tracking and image_chip(context, entry) is None


def for_chip(context: AppContext, firmware: Firmware, chip: str) -> Firmware:
    """A shallow copy of `firmware` holding only the entries whose MCU is `chip` (the Entry
    objects are shared). Plans, checks and partition placement work on this view, so the
    entries of other chips never meet: no overlap, no second partition table."""
    chips = firmware_chips(context, firmware)
    entries = [e for e in firmware.entries if _entry_chip(context, firmware, e, chips) == chip]
    return dataclasses.replace(firmware, entries=entries)


def merge_problem(context: AppContext, firmware: Firmware) -> str | None:
    """Why the enabled entries cannot be merged into one image, or None: they are for more
    than one MCU ("Checked entries are for esp32p4 and esp32s3"), or an enabled entry has no
    MCU ("storage.bin has no MCU")."""
    chips = firmware_chips(context, firmware)
    found: set[str] = set()
    for entry in firmware.entries:
        if not entry.enabled:
            continue
        chip = _entry_chip(context, firmware, entry, chips)
        if chip is None:
            return f"{entry.name} has no MCU"
        found.add(chip)
    if len(found) > 1:
        return f"Checked entries are for {' and '.join(sorted(found))}"
    return None


def _entry_chip(
    context: AppContext, firmware: Firmware, entry: Entry, chips: tuple[str, ...]
) -> str | None:
    if found := image_chip(context, entry):
        return found
    if firmware.source.tracking:
        return firmware.chip
    if entry.chip:
        return entry.chip
    return chips[0] if len(chips) == 1 else None
