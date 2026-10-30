"""FlashPlan from a firmware plus the port and baud chosen in the connection panel
(design doc §7.2). The worker call and the equivalent command line come from the same plan.
"""

import hashlib
from pathlib import Path

from embr.app.context import AppContext
from embr.core import chipinfo
from embr.core.firmware.entries import enabled_partition_table, sorted_entries
from embr.core.firmware.files import read_entry
from embr.core.firmware.image_header import ImageHeader, read_image_header
from embr.core.firmware.image_sniff import sniff
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Entry, Firmware
from embr.core.flasher.plan import (
    FlashPlan,
    FlashSettings,
    PlanEntry,
    build_flash_plan,
    command_line,
)
from embr.core.serial import ConnectionType, PortInfo

PREVIEW_PORT = "PORT"
"""Port shown in the equivalent command while no port is selected."""


class PlanError(ValueError):
    """The firmware cannot be turned into a plan (no chip, an enabled entry without an
    address or whose file is missing)."""


def plan_entries(context: AppContext, firmware: Firmware, *, write_files: bool) -> list[PlanEntry]:
    """Enabled entries by address with file paths the worker can read: "path:" as is,
    "archive:" -> data/archive/<sha256>.bin, "blank:" -> temp/blank-<size>.bin (0xFF bytes;
    written only when `write_files`, the path is the same either way). Image header
    fields (chip, revision range) are read from the files. Raises PlanError."""
    planned: list[PlanEntry] = []
    for entry in sorted_entries([e for e in firmware.entries if e.enabled]):
        if entry.address is None:
            raise PlanError(f"{entry.name} has no address")
        path = entry_path(context, entry)
        if write_files and entry.file.startswith("blank:"):
            path.write_bytes(b"\xff" * entry.size)
        header = image_header(context, entry)
        planned.append(
            PlanEntry(
                address=entry.address,
                path=str(path),
                name=entry.name,
                sha256=entry.sha256,
                size=entry.size,
                image_chip=header.chip if header else None,
                min_rev_full=header.min_rev_full if header else None,
                max_rev_full=header.max_rev_full if header else None,
                legacy_min_rev=header.legacy_min_rev if header else None,
            )
        )
    return planned


def assemble_plan(
    context: AppContext,
    firmware: Firmware,
    port: PortInfo,
    baud: int | None,
    *,
    write_files: bool = True,
) -> FlashPlan:
    """Plan for `firmware` on `port`: reset modes adjusted to the connection type, baud
    ignored on native USB, flash settings from the firmware, declared flash size from the
    firmware ("keep"/"detect": the bootloader image header), partitions_end from the
    enabled partition table. The firmware must have a chip (Operations.flash fills it from
    the identified chip first). Raises PlanError."""
    if firmware.chip is None:
        raise PlanError(f"{firmware.name} has no chip; identify the board first")
    entries = plan_entries(context, firmware, write_files=write_files)
    try:
        table, _ = enabled_partition_table(firmware, lambda e: read_entry(e, context.paths.archive))
    except OSError:  # the files check reports the missing table
        table = None
    flash = firmware.flash
    return build_flash_plan(
        port=port.device,
        connection=port.connection,
        chip=firmware.chip,
        entries=entries,
        before=firmware.before,
        after=firmware.after,
        baud=baud,
        stub=firmware.stub and chipinfo.has_stub(firmware.chip),
        flash=FlashSettings(mode=flash.mode, freq=flash.freq, size=flash.size),
        erase_all=firmware.erase_all,
        declared_flash_size=declared_flash_size(context, firmware),
        partitions_end=table.end if table else None,
    )


def preview_command(
    context: AppContext, firmware: Firmware, port: PortInfo | None, baud: int | None
) -> str:
    """Equivalent esptool command for the command bar; writes nothing. Without a port it
    uses PREVIEW_PORT on a UART bridge. A firmware that cannot be planned gives ""."""
    if port is None:
        port = PortInfo(PREVIEW_PORT, None, None, None, "", ConnectionType.UART_BRIDGE, None)
    try:
        return command_line(assemble_plan(context, firmware, port, baud, write_files=False))
    except (PlanError, ValueError):
        return ""


def entry_path(context: AppContext, entry: Entry) -> Path:
    """The file the worker reads for an entry (see plan_entries())."""
    scheme, _, rest = entry.file.partition(":")
    if scheme == "archive":
        return context.paths.archive / f"{rest}.bin"
    if scheme == "blank":
        return context.paths.temp / f"blank-{entry.size}.bin"
    return Path(rest)


def declared_flash_size(context: AppContext, firmware: Firmware) -> str | None:
    """The firmware's flash size, or for "keep"/"detect" the size in the header of its
    enabled bootloader (or merged) image; None when unknown."""
    if firmware.flash.size not in ("keep", "detect"):
        return firmware.flash.size
    for entry in sorted_entries([e for e in firmware.entries if e.enabled]):
        if entry.kind in (EntryKind.BOOTLOADER, EntryKind.MERGED):
            header = image_header(context, entry)
            if header is not None:
                return header.flash_size
    return None


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
