"""Entry kinds, shared by image_sniff and the firmware model."""

from enum import StrEnum


class EntryKind(StrEnum):
    BOOTLOADER = "bootloader"
    APP = "app"
    PARTITION_TABLE = "partition_table"
    OTADATA = "otadata"
    FS = "fs"
    """File system image (spiffs / littlefs / fatfs)."""
    MERGED = "merged"
    ATTACHMENT = "attachment"
    """Data the user's firmware reads; never parsed by Embr."""
    UNKNOWN = "unknown"
