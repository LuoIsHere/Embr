"""Chip data derived from the bundled esptool.

This is the only module that reads esptool's chip definitions; everything else
asks here. ESP8266 is out of scope and never listed.
"""

from dataclasses import dataclass


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


def espressif_vid() -> int:
    """Espressif USB vendor ID."""
    raise NotImplementedError


def usb_serial_jtag_pid() -> int:
    """USB product ID shared by every chip's USB-Serial/JTAG peripheral."""
    raise NotImplementedError


def supported_chips() -> tuple[ChipInfo, ...]:
    """All chips the bundled esptool defines, in esptool's order, without ESP8266."""
    raise NotImplementedError


def get_chip(name: str) -> ChipInfo:
    """Look up a chip by esptool name. Raises UnknownChipError."""
    raise NotImplementedError


def chip_for_image_id(chip_id: int) -> ChipInfo | None:
    """Chip whose image header chip ID is `chip_id`, or None."""
    raise NotImplementedError


def chip_for_display_name(display_name: str) -> ChipInfo | None:
    """Chip whose esptool display name (e.g. "ESP32-P4") matches, or None."""
    raise NotImplementedError


def bootloader_offsets() -> tuple[int, ...]:
    """Distinct bootloader offsets of all supported chips, ascending."""
    raise NotImplementedError


def usb_otg_chip(vid: int, pid: int) -> ChipInfo | None:
    """Chip that enumerates as USB-OTG with this VID/PID (Espressif VID and PID equal
    to the image chip ID of a chip with USB-OTG), or None."""
    raise NotImplementedError


def flash_mode_name(code: int) -> str | None:
    """Header flash mode byte -> "qio" / "qout" / "dio" / "dout", or None."""
    raise NotImplementedError


def flash_size_name(chip: str, code: int) -> str | None:
    """High nibble of the header size/freq byte (already shifted, e.g. 0x40) -> "16MB"."""
    raise NotImplementedError


def flash_freq_name(chip: str, code: int) -> str | None:
    """Low nibble of the header size/freq byte -> "80m" etc. for that chip."""
    raise NotImplementedError
