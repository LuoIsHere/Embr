"""Read ESP image headers through esptool's offline image parser."""

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from esptool.bin_image import ESP32FirmwareImage, ImageSegment, LoadFirmwareImage

from embr.core import chipinfo

NO_MAX_REVISION = 0xFFFF
"""max_rev_full value meaning "no upper limit"."""

_IMAGE_MAGIC = 0xE9
_CHIP_ID_OFFSET = 12  # u16 in the extended header
_APP_DESC_MAGIC = 0xABCD5432
_BOOTLOADER_DESC_MAGIC = 0x50
# idf_ver[32] offsets in esp_app_desc_t and esp_bootloader_desc_t (ESP-IDF esp_app_format)
_APP_IDF_VER = 0x70
_BOOTLOADER_IDF_VER = 0x08
_IDF_VER_SIZE = 32
_DESC_READ = _APP_IDF_VER + _IDF_VER_SIZE


@dataclass(frozen=True)
class ImageHeader:
    chip: str | None
    """esptool chip name from the header chip ID, or None if the ID is unknown."""
    chip_id: int
    min_rev_full: int
    """Minimum chip revision, major * 100 + minor."""
    max_rev_full: int
    """Maximum chip revision, major * 100 + minor; NO_MAX_REVISION means no limit."""
    legacy_min_rev: int
    """Old single-number min_rev field (major revision; minor for ESP32-C3)."""
    flash_mode: str | None
    flash_freq: str | None
    flash_size: str | None
    segment_count: int
    has_app_desc: bool
    """First segment starts with the esp_app_desc_t magic 0xABCD5432."""
    has_bootloader_desc: bool
    """First segment starts with the bootloader descriptor magic byte 0x50."""
    segment_addresses: tuple[int, ...] = ()
    """Load address of every segment, in image order."""
    idf_ver: str | None = None
    """ESP-IDF version string from the descriptor (esp_app_desc_t.idf_ver at 0x70 of the
    first segment's data, or the bootloader descriptor's idf_ver at 0x08), NUL-stripped,
    at most 31 characters; None without a descriptor."""


def read_image_header(data: bytes) -> ImageHeader | None:
    """Parse an ESP image. Returns None if `data` is not one (no 0xE9 magic, unknown
    chip ID, or esptool cannot parse it). Never raises for bad input."""
    if len(data) < 24 or data[0] != _IMAGE_MAGIC:
        return None
    (chip_id,) = struct.unpack_from("<H", data, _CHIP_ID_OFFSET)
    chip = chipinfo.chip_for_image_id(chip_id)
    if chip is None:
        return None
    try:
        image = LoadFirmwareImage(chip.name, data)
    except Exception:  # esptool raises FatalError, struct.error and others on bad input
        return None
    if not isinstance(image, ESP32FirmwareImage):  # ESP8266 image classes
        return None
    # esptool leaves segment attributes unannotated
    segments = cast(list[ImageSegment], image.segments)  # pyright: ignore[reportUnknownMemberType]
    desc = bytes(cast(bytes, segments[0].data)[:_DESC_READ]) if segments else b""  # pyright: ignore[reportUnknownMemberType]
    first = desc[:4]
    has_app_desc = len(first) == 4 and struct.unpack("<I", first)[0] == _APP_DESC_MAGIC
    has_bootloader_desc = first[:1] == bytes([_BOOTLOADER_DESC_MAGIC])
    idf_ver: str | None = None
    if has_app_desc != has_bootloader_desc:  # exactly one descriptor
        at = _APP_IDF_VER if has_app_desc else _BOOTLOADER_IDF_VER
        idf_ver = _c_string(desc[at : at + _IDF_VER_SIZE])
    size_code = image.flash_size_freq & 0xF0
    freq_code = image.flash_size_freq & 0x0F
    return ImageHeader(
        chip=chip.name,
        chip_id=chip_id,
        min_rev_full=int(cast(int, image.min_rev_full)),
        max_rev_full=int(cast(int, image.max_rev_full)),
        legacy_min_rev=int(cast(int, image.min_rev)),
        flash_mode=chipinfo.flash_mode_name(image.flash_mode),
        flash_freq=chipinfo.flash_freq_name(chip.name, freq_code),
        flash_size=chipinfo.flash_size_name(chip.name, size_code),
        segment_count=len(segments),
        segment_addresses=tuple(int(cast(int, s.addr)) for s in segments),  # pyright: ignore[reportUnknownMemberType]
        has_app_desc=has_app_desc,
        has_bootloader_desc=has_bootloader_desc,
        idf_ver=idf_ver,
    )


def read_image_header_file(path: Path) -> ImageHeader | None:
    """read_image_header() on a file's contents."""
    return read_image_header(path.read_bytes())


def _c_string(field: bytes) -> str | None:
    """NUL-terminated ASCII field; None when empty or not text."""
    text = field.split(b"\0", 1)[0].decode("ascii", "replace")
    return text or None


def format_revision(rev_full: int) -> str:
    """301 -> "v3.1"; NO_MAX_REVISION -> "any"."""
    if rev_full == NO_MAX_REVISION:
        return "any"
    return f"v{rev_full // 100}.{rev_full % 100}"
