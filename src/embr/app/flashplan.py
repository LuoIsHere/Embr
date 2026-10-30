"""FlashPlan from a firmware plus the port and baud chosen in the connection panel
(design doc §7.2). The worker call and the equivalent command line come from the same plan.
"""

from pathlib import Path

from embr.app.chips import Mcu, entry_mcu, firmware_mcus, fits, for_chip, image_header
from embr.app.context import AppContext
from embr.core import chipinfo
from embr.core.firmware.entries import enabled_partition_table, sorted_entries
from embr.core.firmware.files import read_entry
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


def plan_entries(
    context: AppContext,
    firmware: Firmware,
    *,
    write_files: bool,
    chip: str | None = None,
    revision: int | None = None,
) -> list[PlanEntry]:
    """Enabled entries by address with file paths the worker can read: "path:" as is,
    "archive:" -> data/archive/<sha256>.bin, "blank:" -> temp/blank-<size>.bin (0xFF bytes;
    written only when `write_files`, the path is the same either way). Image header
    fields (revision range) are read from the files. With `chip`, only the entries that fit
    a board with `chip` and `revision` (app.chips.fits()), and PlanEntry.image_chip is the
    entry's chip, so the worker refuses a hand-chosen chip that differs from the board.
    Raises PlanError."""
    planned: list[PlanEntry] = []
    enabled = [e for e in firmware.entries if e.enabled]
    if chip is not None:
        enabled = [e for e in enabled if fits(entry_mcu(context, firmware, e), chip, revision)]
    for entry in sorted_entries(enabled):
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
                image_chip=chip or (header.chip if header else None),
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
    chip: str | None = None,
    revision: int | None = None,
    write_files: bool = True,
) -> FlashPlan:
    """Plan for `firmware` on `port`, for `chip` and `revision` (the identified board) or,
    without a chip, the firmware's only chip: the entries that fit (app.chips.for_chip();
    without a revision, the firmware's only MCU's line), reset modes
    adjusted to the connection type, baud ignored on native USB, flash settings from the
    firmware, declared flash size from the firmware ("keep"/"detect": the bootloader image
    header of that chip), partitions_end from that chip's enabled partition table. PlanError
    when there is no chip to plan for (several chips, or none, and nothing identified)."""
    if chip is None and (only := plan_mcu(context, firmware)) is not None:
        chip, revision = only.chip, line_low(only)
    chip = plan_chip(context, firmware, chip)
    if chip is None:
        raise PlanError(f"{firmware.name}: identify the board to choose which entries to flash")
    entries = plan_entries(context, firmware, write_files=write_files, chip=chip, revision=revision)
    view = for_chip(context, firmware, chip, revision)
    try:
        table, _ = enabled_partition_table(view, lambda e: read_entry(e, context.paths.archive))
    except OSError:  # the files check reports the missing table
        table = None
    flash = firmware.flash
    return build_flash_plan(
        port=port.device,
        connection=port.connection,
        chip=chip,
        entries=entries,
        before=firmware.before,
        after=firmware.after,
        baud=baud,
        stub=firmware.stub and chipinfo.has_stub(chip),
        flash=FlashSettings(mode=flash.mode, freq=flash.freq, size=flash.size),
        erase_all=firmware.erase_all,
        declared_flash_size=declared_flash_size(context, view),
        partitions_end=table.end if table else None,
    )


def preview_command(
    context: AppContext,
    firmware: Firmware,
    port: PortInfo | None,
    baud: int | None,
    chip: str | None = None,
    revision: int | None = None,
) -> str:
    """Equivalent esptool command for the command bar; writes nothing. Without a port it
    uses PREVIEW_PORT on a UART bridge. `chip` and `revision` are the identified board's,
    as for assemble_plan(). A firmware that cannot be planned gives "" (so a multi-chip firmware
    shows no command until a board is identified, design doc §12 item 13)."""
    if port is None:
        port = PortInfo(PREVIEW_PORT, None, None, None, "", ConnectionType.UART_BRIDGE, None)
    try:
        plan = assemble_plan(
            context, firmware, port, baud, chip=chip, revision=revision, write_files=False
        )
        return command_line(plan)
    except (PlanError, ValueError):
        return ""


def plan_chip(context: AppContext, firmware: Firmware, identified: str | None) -> str | None:
    """The chip a plan is made for: the identified chip, else the chip of the firmware's
    only MCU (several MCUs, ESP32-P4 lines included, need a board first)."""
    if identified is not None:
        return identified
    only = plan_mcu(context, firmware)
    return only.chip if only is not None else None


def plan_mcu(context: AppContext, firmware: Firmware) -> Mcu | None:
    """The firmware's only MCU, else None."""
    mcus = firmware_mcus(context, firmware)
    return mcus[0] if len(mcus) == 1 else None


def line_low(mcu: Mcu) -> int | None:
    """A revision inside the MCU's line (its lowest), None for no line."""
    return next((ln.low for ln in chipinfo.revision_lines(mcu.chip) if ln.name == mcu.line), None)


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
