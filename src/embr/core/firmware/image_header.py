"""Read ESP image headers through esptool's offline image parser."""

from dataclasses import dataclass
from pathlib import Path

NO_MAX_REVISION = 0xFFFF
"""max_rev_full value meaning "no upper limit"."""


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


def read_image_header(data: bytes) -> ImageHeader | None:
    """Parse an ESP image. Returns None if `data` is not one (no 0xE9 magic, unknown
    chip ID, or esptool cannot parse it). Never raises for bad input."""
    raise NotImplementedError


def read_image_header_file(path: Path) -> ImageHeader | None:
    """read_image_header() on a file's contents."""
    raise NotImplementedError


def format_revision(rev_full: int) -> str:
    """301 -> "v3.1"; NO_MAX_REVISION -> "any"."""
    raise NotImplementedError
