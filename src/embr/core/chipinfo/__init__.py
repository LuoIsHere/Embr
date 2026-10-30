"""Chip data derived from the bundled esptool.

This is the only module that reads esptool's chip definitions; everything else
asks here. ESP8266 is out of scope and never listed.
"""

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from esptool.loader import ESPLoader, StubFlasher
from esptool.targets import CHIP_DEFS, CHIP_LIST

ESPRESSIF_VID: int = ESPLoader.ESPRESSIF_VID
USB_JTAG_SERIAL_PID: int = ESPLoader.USB_JTAG_SERIAL_PID


class UnknownChipError(KeyError):
    """Raised for a chip name that the bundled esptool does not define."""


@dataclass(frozen=True)
class ChipInfo:
    name: str
    """esptool `--chip` argument, e.g. "esp32s3"."""
    display_name: str
    """Name esptool reports after connecting, e.g. "ESP32-S3"."""
    image_chip_id: int
    """Chip ID stored in the extended image header."""
    bootloader_offset: int
    flash_sizes: tuple[str, ...]
    """Flash sizes accepted by esptool for this chip, e.g. ("1MB", "2MB", ...)."""
    flash_freqs: tuple[str, ...]
    """Flash frequencies accepted by esptool for this chip, e.g. ("80m", "40m", ...)."""
    usb_otg: bool
    usb_serial_jtag: bool


@dataclass(frozen=True)
class RevisionLine:
    """A range of chip revisions that needs its own build (design doc §12 item 13)."""

    name: str
    """Stored name, e.g. "v0-v1"."""
    label: str
    """Shown after the chip name: "v3", or v0 and v1 joined by an en dash."""
    low: int
    """Lowest revision, major * 100 + minor, inclusive."""
    high: int
    """Highest revision, inclusive."""


REVISION_LINES: dict[str, tuple[RevisionLine, ...]] = {
    # ESP-IDF builds ESP32-P4 either for v3.0 and later or, with
    # CONFIG_ESP32P4_SELECTS_REV_LESS_V3, for the revisions before it
    "esp32p4": (
        RevisionLine("v0-v1", "v0\u2013v1", 0, 299),
        RevisionLine("v3", "v3", 300, 0xFFFF),
    ),
}
"""Chips whose revisions are split into lines; other chips have none."""


FLASH_MODES: tuple[str, ...] = ("qio", "qout", "dio", "dout")
"""Flash modes in header encoding order (0..3)."""

_EXCLUDED = frozenset({"esp8266"})

# esptool 5.3.1 has no per-chip USB flags; these are the USB_SERIAL_JTAG_SUPPORTED and
# USB_OTG_SUPPORTED values of esptool 5.6.0 (design doc §12.1)
_USB_SERIAL_JTAG = frozenset(
    {"esp32c3", "esp32c5", "esp32c6", "esp32c61", "esp32h2", "esp32h21", "esp32h4"}
    | {"esp32s3", "esp32p4", "esp32s31"}
)
_USB_OTG = frozenset({"esp32s2", "esp32s3", "esp32p4", "esp32s31", "esp32e22"})


def espressif_vid() -> int:
    """Espressif USB vendor ID."""
    return ESPRESSIF_VID


def usb_serial_jtag_pid() -> int:
    """USB product ID shared by every chip's USB-Serial/JTAG peripheral."""
    return USB_JTAG_SERIAL_PID


@cache
def supported_chips() -> tuple[ChipInfo, ...]:
    """All chips the bundled esptool defines, in esptool's order, without ESP8266."""
    chips: list[ChipInfo] = []
    for name in CHIP_LIST:
        if name in _EXCLUDED:
            continue
        rom = CHIP_DEFS[name]
        if rom.IMAGE_CHIP_ID is None:  # only ESP8266, already excluded
            continue
        chips.append(
            ChipInfo(
                name=name,
                display_name=rom.CHIP_NAME,
                image_chip_id=rom.IMAGE_CHIP_ID,
                bootloader_offset=rom.BOOTLOADER_FLASH_OFFSET,
                flash_sizes=tuple(rom.FLASH_SIZES),
                flash_freqs=tuple(rom.FLASH_FREQUENCY),
                usb_otg=name in _USB_OTG,
                usb_serial_jtag=name in _USB_SERIAL_JTAG,
            )
        )
    return tuple(chips)


def get_chip(name: str) -> ChipInfo:
    """Look up a chip by esptool name. Raises UnknownChipError."""
    for chip in supported_chips():
        if chip.name == name:
            return chip
    raise UnknownChipError(name)


def chip_for_image_id(chip_id: int) -> ChipInfo | None:
    """Chip whose image header chip ID is `chip_id`, or None."""
    return next((c for c in supported_chips() if c.image_chip_id == chip_id), None)


def revision_lines(chip: str) -> tuple[RevisionLine, ...]:
    """The chip's revision lines; () for chips that have none."""
    return REVISION_LINES.get(chip, ())


def line_for_revision(chip: str, revision: int) -> str:
    """Name of the line holding `revision` (major * 100 + minor), "" for chips without
    lines."""
    return next((ln.name for ln in revision_lines(chip) if ln.low <= revision <= ln.high), "")


def line_for_range(chip: str, low: int, high: int) -> str:
    """Name of the line holding every revision from `low` to `high` (an image header's
    range), "" when the range spans lines or the chip has none."""
    return next((ln.name for ln in revision_lines(chip) if ln.low <= low and high <= ln.high), "")


def chip_for_display_name(display_name: str) -> ChipInfo | None:
    """Chip whose esptool display name (e.g. "ESP32-P4") matches, or None."""
    return next((c for c in supported_chips() if c.display_name == display_name), None)


def bootloader_offsets() -> tuple[int, ...]:
    """Distinct bootloader offsets of all supported chips, ascending."""
    return tuple(sorted({c.bootloader_offset for c in supported_chips()}))


def usb_otg_chip(vid: int, pid: int) -> ChipInfo | None:
    """Chip that enumerates as USB-OTG with this VID/PID (Espressif VID and PID equal
    to the image chip ID of a chip with USB-OTG), or None."""
    if vid != ESPRESSIF_VID or pid == USB_JTAG_SERIAL_PID:
        return None
    return next((c for c in supported_chips() if c.usb_otg and c.image_chip_id == pid), None)


def has_stub(chip: str) -> bool:
    """Whether the bundled esptool ships a flasher stub for the chip (esptool 5.3.1 has
    none for ESP32-H21 and ESP32-E22). Raises UnknownChipError."""
    name = get_chip(chip).name
    root = Path(StubFlasher.STUB_DIR)
    subdirs: list[str] = list(StubFlasher.STUB_SUBDIRS)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # class attribute assigned at runtime
    # ESP32-P4 picks esp32p4-rev1.json below v3.0; esp32p4.json exists for both lines
    return any((root / sub / f"{name}.json").is_file() for sub in subdirs)


def flash_map_ranges(chip: str) -> tuple[tuple[int, int], ...]:
    """Address ranges [start, end) that the chip maps to flash through its cache (IROM and
    DROM). An image segment loaded inside one of them is executed or read from flash."""
    rom = CHIP_DEFS[get_chip(chip).name]
    names = (("IROM_MAP_START", "IROM_MAP_END"), ("DROM_MAP_START", "DROM_MAP_END"))
    ranges = {(int(getattr(rom, lo)), int(getattr(rom, hi))) for lo, hi in names}
    return tuple(sorted(ranges))


def flash_mode_name(code: int) -> str | None:
    """Header flash mode byte -> "qio" / "qout" / "dio" / "dout", or None."""
    return FLASH_MODES[code] if 0 <= code < len(FLASH_MODES) else None


def flash_size_name(chip: str, code: int) -> str | None:
    """High nibble of the header size/freq byte (already shifted, e.g. 0x40) -> "16MB"."""
    sizes: dict[str, int] = CHIP_DEFS[get_chip(chip).name].FLASH_SIZES
    return next((name for name, value in sizes.items() if value == code), None)


def flash_freq_name(chip: str, code: int) -> str | None:
    """Low nibble of the header size/freq byte -> "80m" etc. for that chip."""
    freqs: dict[str, int] = CHIP_DEFS[get_chip(chip).name].FLASH_FREQUENCY
    return next((name for name, value in freqs.items() if value == code), None)
