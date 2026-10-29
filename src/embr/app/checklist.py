"""The checklist in the connection panel (design doc §6.1): what can be decided before
connecting, plus what the last identification says. The worker repeats the chip-side
checks in its own connection; this list is for the user, before pressing Flash.
"""

import copy
import re
from dataclasses import dataclass
from enum import StrEnum

from embr.app.context import AppContext
from embr.app.flashplan import PlanError, declared_flash_size, plan_entries
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
       with it: chip (REFUSE on mismatch with the firmware or an entry's image chip),
       revision (REFUSE when an entry's range excludes the chip; only when the chip
       matches), flash size (WARN when the declared size differs from the detected one),
       security (REFUSE when secure download mode, flash encryption or secure boot is on)
    A firmware without a chip (no ESP image among its entries) gets a WARN chip item once
    identified ("taken from the identified chip", design doc §4.3) and the LATER item
    before."""
    items = _layout(context, firmware, identified)
    items.append(_files(context, firmware))
    items.append(_reset(firmware, port))
    if identified is None:
        items.append(
            CheckItem(
                CheckState.LATER, "Chip / revision / size / security", "checked after connecting"
            )
        )
        return items
    info = identified.info
    if info.security.secure_download_mode:  # nothing else could be read from the chip
        return items + [_item(c) for c in check_security(info.security)]
    state = ChipState(info.chip, info.major, info.minor, info.flash_size, info.security)
    try:
        entries = plan_entries(context, firmware, write_files=False)
    except PlanError:  # reported by the layout item
        entries = []
    if firmware.chip is None:
        items.append(CheckItem(CheckState.WARN, "Chip taken from the identified board", info.chip))
    else:
        chip = check_chip(info.chip, firmware.chip, entries)
        items.append(_item(chip))
        if chip.level == CheckLevel.PASS:
            items.append(_item(check_revisions(state, entries)))
    table, _ = _table(context, firmware)
    declared = declared_flash_size(context, firmware)
    end = table.end if table else None
    items += [_item(c) for c in check_capacity(state, declared, entries, end)]
    items += [_item(c) for c in check_security(info.security)]
    return items


def can_flash(items: list[CheckItem]) -> bool:
    """No REFUSE item."""
    return all(item.state != CheckState.REFUSE for item in items)


_TITLES = {
    CHIP: "Chip",
    REVISION: "Chip revision",
    FLASH_SIZE: "Header flash size",
    CAPACITY: "Flash capacity",
    SECURITY: "Security",
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
    context: AppContext, firmware: Firmware, identified: IdentifySnapshot | None
) -> list[CheckItem]:
    probe = copy.deepcopy(firmware)  # resolving partitions must not touch the shown firmware
    table, problems = _table(context, probe)
    if table is not None:
        problems += resolve_partitions(probe, table)
    size = identified.info.flash_size if identified else None
    problems += check_layout(probe, _size_bytes(size) if size else None)
    if not problems:
        return [CheckItem(CheckState.PASS, "No address overlap")]
    return [CheckItem(CheckState(p.level.value), "Entry layout", p.message) for p in problems]


def _files(context: AppContext, firmware: Firmware) -> CheckItem:
    enabled = [e for e in firmware.entries if e.enabled]
    if not enabled:
        return CheckItem(CheckState.REFUSE, "No entries enabled", "enable at least one entry")
    states = {e.name: entry_state(e, context.paths.archive) for e in enabled}
    missing = [n for n, s in states.items() if s == FileState.MISSING]
    changed = [n for n, s in states.items() if s == FileState.CHANGED]
    if missing:
        return CheckItem(
            CheckState.REFUSE,
            "File missing",
            f"{', '.join(missing)}: not found at the recorded path. Re-locate or remove the entry.",
        )
    if changed:
        return CheckItem(
            CheckState.REFUSE,
            "Needs confirmation",
            f"{', '.join(changed)}: content on disk differs from the recorded hash. "
            "Review the entries, then confirm.",
        )
    return CheckItem(
        CheckState.PASS,
        "Files match",
        f"{len(enabled)} files checked: name, path and hash agree with the recorded values",
    )


def _reset(firmware: Firmware, port: PortInfo | None) -> CheckItem:
    connection = port.connection if port else ConnectionType.UART_BRIDGE
    before, after, reasons = adjust_reset(connection, firmware.before, firmware.after)
    used = f"before={before} · after={after}"
    if reasons:
        detail = f"{used} ({'; '.join(reasons)})"
        return CheckItem(CheckState.WARN, "Reset policy adjusted to connection", detail)
    return CheckItem(CheckState.PASS, "Reset policy", used)


def _size_bytes(size: str) -> int | None:
    match = re.fullmatch(r"(\d+)(KB|MB)", size)
    if match is None:
        return None
    return int(match[1]) * (1024 if match[2] == "KB" else 1024 * 1024)
