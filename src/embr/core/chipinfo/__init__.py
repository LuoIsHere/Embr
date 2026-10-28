"""Chip data derived from the bundled esptool.

This is the only module that reads esptool's chip definitions; everything else
asks here. ESP8266 is out of scope and never listed.
"""

from dataclasses import dataclass
from functools import cache

from esp_pylib.constants import ESPRESSIF_VID, USB_JTAG_SERIAL_PID
from esptool.targets import CHIP_DEFS, CHIP_LIST


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


FLASH_MODES: tuple[str, ...] = ("qio", "qout", "dio", "dout")
"""Flash modes in header encoding order (0..3)."""

_EXCLUDED = frozenset({"esp8266"})


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
                usb_otg=bool(rom.USB_OTG_SUPPORTED),
                usb_serial_jtag=bool(rom.USB_SERIAL_JTAG_SUPPORTED),
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
