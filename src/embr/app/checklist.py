"""The checklist in the connection panel (design doc §6.1): what can be decided before
connecting, plus what the last identification says. The worker repeats the chip-side
checks in its own connection; this list is for the user, before pressing Flash.
"""

import copy
import re
from dataclasses import dataclass
from enum import StrEnum

from embr.app.chips import board_revision, firmware_mcus, fits, for_chip
from embr.app.context import AppContext
from embr.app.flashplan import PlanError, declared_flash_size, line_low, plan_entries
from embr.app.identify import IdentifySnapshot
from embr.app.tracking import TrackStatus
from embr.core.firmware.entries import (
    Problem,
    check_layout,
    enabled_partition_table,
    resolve_partitions,
)
from embr.core.firmware.files import FileState, entry_state, read_entry
from embr.core.firmware.image_sniff import PartitionTable
from embr.core.firmware.model import Firmware
from embr.core.firmware.tracking import ResolveState
from embr.core.flasher.checks import (
    CAPACITY,
    CHIP,
    FLASH_SIZE,
    REVISION,
    SECURITY,
    Check,
    CheckLevel,
    ChipState,
    check_capacity,
    check_chip,
    check_revisions,
    check_security,
)
from embr.core.flasher.plan import adjust_reset
from embr.core.serial import ConnectionType, PortInfo
from embr.i18n import N_


class CheckState(StrEnum):
    PASS = "pass"
    WARN = "warn"
    REFUSE = "refuse"
    LATER = "later"
    """Needs a connected chip; shown until something is identified."""


@dataclass(frozen=True)
class CheckItem:
    state: CheckState
    title: str
    detail: str = ""


def preflight(
    context: AppContext,
    firmware: Firmware,
    port: PortInfo | None,
    identified: IdentifySnapshot | None,
    status: TrackStatus | None = None,
) -> list[CheckItem]:
    """For a tracking firmware `status` is its Tracker status and `firmware` its resolved
    copy; the list then starts with a build item: PASS "Build resolved" (with the IDF
    version and chip, and the time it was followed when UPDATED), REFUSE for MISSING,
    INCOMPLETE, TARGET_SWITCHED, BUILDING and NEEDS_CONFIRMATION (with the problems or the
    first changes). Then, in this order:
    1. layout: overlapping enabled entries, entries without address, beyond the identified
       flash size, more than one enabled partition table, partition placement problems;
       one PASS item when there are none ("No address overlap")
    2. files: name, path and hash of every enabled entry; MISSING and CHANGED are REFUSE,
       PASS says how many files were checked; no enabled entries is REFUSE
    3. reset policy for the selected port (UART bridge without a port): PASS with the
       before / after used, WARN "adjusted to the connection" with the reasons
    4. without `identified`: one LATER item for chip, revision, flash size and security;
       with it: chip (REFUSE when the identified chip is not among the firmware's chips, or
       an entry's MCU differs), revision (REFUSE when an entry's range excludes the chip;
       only when the chip matches), flash size (WARN when the declared size differs from
       the detected one), security (REFUSE when secure download mode, flash encryption or
       secure boot is on)
    Entries are taken per chip (design doc §12 item 13): once identified, items 1, 2 and 4
    look only at the entries for the identified chip (app.chips.for_chip()), so "No entries
    enabled" also covers a board none of the entries are for; before that, item 1 checks
    each chip's entries separately and item 2 all enabled entries."""
    items = [_build(status)] if status is not None and firmware.source.tracking else []
    mcus = firmware_mcus(context, firmware)
    chip = identified.info.chip if identified is not None else None
    revision = board_revision(identified.info.major, identified.info.minor) if identified else None
    if chip is not None:
        views = [for_chip(context, firmware, chip, revision)]
    elif len(mcus) > 1:
        views = [for_chip(context, firmware, m.chip, line_low(m)) for m in mcus]
    else:
        views = [firmware]
    items += _layout(context, views, identified)
    items.append(_files(context, views[0] if chip else firmware, chip))
    items.append(_reset(firmware, port))
    if identified is None:
        items.append(
            CheckItem(
                CheckState.LATER,
                N_("Chip / revision / size / security"),
                N_("checked after connecting"),
            )
        )
        return items
    info = identified.info
    if info.security.secure_download_mode:  # nothing else could be read from the chip
        return items + [_item(c) for c in check_security(info.security)]
    state = ChipState(info.chip, info.major, info.minor, info.flash_size, info.security)
    view = views[0]
    try:
        entries = plan_entries(
            context, firmware, write_files=False, chip=info.chip, revision=revision
        )
    except PlanError:  # reported by the layout item
        entries = []
    if mcus and not any(fits(m, info.chip, revision) for m in mcus):
        names = ", ".join(m.label for m in mcus)
        items.append(
            CheckItem(
                CheckState.REFUSE,
                "Chip",
                N_(f"the firmware is for {names}, the board is {info.chip} {info.revision}"),
            )
        )
    else:
        chip = check_chip(info.chip, None, entries)
        items.append(_item(chip))
        if chip.level == CheckLevel.PASS:
            items.append(_item(check_revisions(state, entries)))
    table, _ = _table(context, view)
    declared = declared_flash_size(context, view)
    end = table.end if table else None
    items += [_item(c) for c in check_capacity(state, declared, entries, end)]
    items += [_item(c) for c in check_security(info.security)]
    return items


def can_flash(items: list[CheckItem]) -> bool:
    """No REFUSE item."""
    return all(item.state != CheckState.REFUSE for item in items)


def flash_ready(
    items: list[CheckItem], snapshot: IdentifySnapshot | None, port: PortInfo | None
) -> bool:
    """Whether the Flash button may be pressed (design doc §12 item 5): a port is selected,
    the last identification is a snapshot of that port (same device name), and no item
    refuses. The flash job still identifies the chip again in its own connection."""
    return (
        port is not None
        and snapshot is not None
        and snapshot.port.device == port.device
        and can_flash(items)
    )


_TITLES = {
    CHIP: N_("Chip"),
    REVISION: N_("Chip revision"),
    FLASH_SIZE: N_("Header flash size"),
    CAPACITY: N_("Flash capacity"),
    SECURITY: N_("Security"),
}


def _item(check: Check) -> CheckItem:
    return CheckItem(
        CheckState(check.level.value), _TITLES.get(check.name, check.name), check.message
    )


def _table(context: AppContext, firmware: Firmware) -> tuple[PartitionTable | None, list[Problem]]:
    try:
        return enabled_partition_table(firmware, lambda e: read_entry(e, context.paths.archive))
    except OSError:  # the files item reports the missing file
        return None, []


def _layout(
    context: AppContext, views: list[Firmware], identified: IdentifySnapshot | None
) -> list[CheckItem]:
    """Layout problems of each view (one per chip), each problem listed once."""
    size = identified.info.flash_size if identified else None
    problems: list[Problem] = []
    for view in views:
        probe = copy.deepcopy(view)  # resolving partitions must not touch the shown firmware
        table, found = _table(context, probe)
        if table is not None:
            found += resolve_partitions(probe, table)
        found += check_layout(probe, _size_bytes(size) if size else None)
        problems += [p for p in found if p not in problems]
    if not problems:
        return [CheckItem(CheckState.PASS, N_("No address overlap"))]
    return [CheckItem(CheckState(p.level.value), N_("Entry layout"), p.message) for p in problems]


def _files(context: AppContext, firmware: Firmware, chip: str | None) -> CheckItem:
    enabled = [e for e in firmware.entries if e.enabled]
    if not enabled:
        detail = (
            N_(f"enable at least one entry for {chip}") if chip else N_("enable at least one entry")
        )
        return CheckItem(CheckState.REFUSE, N_("No entries enabled"), detail)
    states = {e.name: entry_state(e, context.paths.archive) for e in enabled}
    missing = [n for n, s in states.items() if s == FileState.MISSING]
    changed = [n for n, s in states.items() if s == FileState.CHANGED]
    if missing:
        return CheckItem(
            CheckState.REFUSE,
            N_("File missing"),
            N_(
                f"{', '.join(missing)}: not found at the recorded path. Re-locate or remove "
                "the entry."
            ),
        )
    if changed:
        return CheckItem(
            CheckState.REFUSE,
            N_("Needs confirmation"),
            N_(
                f"{', '.join(changed)}: content on disk differs from the recorded hash. "
                "Review the entries, then confirm."
            ),
        )
    return CheckItem(
        CheckState.PASS,
        N_("Files match"),
        N_(f"{len(enabled)} files checked: name, path and hash agree with the recorded values"),
    )


def _build(status: TrackStatus) -> CheckItem:
    if status.state.flashable:
        parts = [
            p
            for p in (status.idf_version and f"ESP-IDF {status.idf_version}", status.firmware.chip)
            if p
        ]
        detail = " · ".join(parts)
        if status.updated_at is not None:  # whole messages, so that they translate
            time = f"{status.updated_at:%H:%M:%S}"
            detail = N_(f"{detail} · updated {time}") if detail else N_(f"updated {time}")
        return CheckItem(CheckState.PASS, N_("Build resolved"), detail)
    reasons = status.problems or tuple(c.message for c in status.changes[:3])
    return CheckItem(CheckState.REFUSE, _BUILD_TITLES[status.state], "; ".join(reasons))


_BUILD_TITLES = {
    ResolveState.NEEDS_CONFIRMATION: N_("Build changed: confirm entries"),
    ResolveState.TARGET_SWITCHED: N_("Target switched: rebuild"),
    ResolveState.INCOMPLETE: N_("Build incomplete"),
    ResolveState.MISSING: N_("Build output missing"),
    ResolveState.BUILDING: N_("Build in progress"),
}


def _reset(firmware: Firmware, port: PortInfo | None) -> CheckItem:
    connection = port.connection if port else ConnectionType.UART_BRIDGE
    before, after, reasons = adjust_reset(connection, firmware.before, firmware.after)
    used = f"before={before} · after={after}"
    if reasons:
        detail = f"{used} ({'; '.join(reasons)})"
        return CheckItem(CheckState.WARN, N_("Reset policy adjusted to connection"), detail)
    return CheckItem(CheckState.PASS, N_("Reset policy"), used)


def _size_bytes(size: str) -> int | None:
    match = re.fullmatch(r"(\d+)(KB|MB)", size)
    if match is None:
        return None
    return int(match[1]) * (1024 if match[2] == "KB" else 1024 * 1024)
