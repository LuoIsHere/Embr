"""Which MCU each entry is for (design doc §12 item 13). A fixed firmware may hold files
for several chips; a board only gets the entries for its own chip.

The MCU of an entry is, in this order: the chip in its image header (bootloader, app,
merged image; read-only); for tracking firmware the chip of the build; the chip chosen by
hand (Entry.chip); and when the firmware has exactly one chip, that chip. Otherwise it has
none, and the entry is not flashed to any board until one is chosen.

An MCU is a chip plus, for chips split into revision lines (ESP32-P4: v0-v1 and v3), the
line: from the image header's revision range, or chosen by hand with the chip
(Entry.chip_line). An image whose range spans lines has no line and fits both.
"""

import dataclasses
import hashlib
from dataclasses import dataclass

from embr.app.context import AppContext
from embr.core.firmware.files import read_entry
from embr.core.firmware.image_header import ImageHeader, read_image_header
from embr.core.firmware.image_sniff import sniff
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Entry, Firmware


@dataclass(frozen=True, order=True)
class Mcu:
    chip: str
    line: str = ""
    """chipinfo revision line name, "" for none (any revision)."""

    @property
    def key(self) -> str:
        """For messages: "esp32s3", "esp32p4 v3"."""
        raise NotImplementedError

    @property
    def label(self) -> str:
        """For the UI: "ESP32-S3", "ESP32-P4 v3" (the line label, with an en dash)."""
        raise NotImplementedError


def board_revision(major: int, minor: int) -> int | None:
    """major * 100 + minor of an identified chip; None when it could not be read
    (secure download mode reports a negative major)."""
    raise NotImplementedError


def image_mcu(context: AppContext, entry: Entry) -> Mcu | None:
    """The MCU named by the entry's image header (line from its revision range)."""
    raise NotImplementedError


def entry_mcu(context: AppContext, firmware: Firmware, entry: Entry) -> Mcu | None:
    """The MCU the entry is flashed to (see the module docstring), or None."""
    raise NotImplementedError


def firmware_mcus(context: AppContext, firmware: Firmware) -> tuple[Mcu, ...]:
    """The distinct MCUs of the entries, sorted (as firmware_chips(), with lines); more
    than one shows as "multi"."""
    raise NotImplementedError


def fits(mcu: Mcu | None, chip: str, revision: int | None) -> bool:
    """An entry with this MCU may be flashed to a board with `chip` and `revision`: same
    chip, and the line (if any) holds the revision. Without a revision only the chip
    counts. An entry without an MCU fits no board."""
    raise NotImplementedError


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
    """The chip of entry_mcu(), or None."""
    return _entry_chip(context, firmware, entry, firmware_chips(context, firmware))


def chip_choosable(context: AppContext, firmware: Firmware, entry: Entry) -> bool:
    """The MCU can be chosen by hand: a fixed firmware's entry whose file names no chip."""
    return not firmware.source.tracking and image_chip(context, entry) is None


def for_chip(
    context: AppContext, firmware: Firmware, chip: str, revision: int | None = None
) -> Firmware:
    """A shallow copy of `firmware` holding only the entries that fit a board with `chip`
    and `revision` (fits(); the Entry objects are shared). Plans, checks and partition
    placement work on this view, so the entries of other chips and lines never meet: no
    overlap, no second partition table."""
    chips = firmware_chips(context, firmware)
    entries = [e for e in firmware.entries if _entry_chip(context, firmware, e, chips) == chip]
    return dataclasses.replace(firmware, entries=entries)


def merge_problem(context: AppContext, firmware: Firmware) -> str | None:
    """Why the enabled entries cannot be merged into one image, or None: they are for more
    than one MCU ("Checked entries are for esp32p4 and esp32s3", "... for esp32p4 v0-v1 and
    esp32p4 v3": Mcu.key), or an enabled entry has no MCU ("storage.bin has no MCU")."""
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
