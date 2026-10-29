"""Pre-write checks (design doc §4.2, §4.3). Pure functions; the worker feeds them the
state it reads from the chip, tests feed them injected state."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.core.firmware.image_header import NO_MAX_REVISION
from embr.core.flasher.plan import PlanEntry


class CheckLevel(StrEnum):
    PASS = "pass"
    WARN = "warn"
    REFUSE = "refuse"


# check names
RESET = "reset"
SECURITY = "security"
CHIP = "chip"
REVISION = "revision"
FLASH_SIZE = "flash_size"
CAPACITY = "capacity"
FILES = "files"


@dataclass(frozen=True)
class Check:
    name: str
    level: CheckLevel
    message: str


@dataclass(frozen=True)
class SecurityState:
    secure_download_mode: bool
    flash_encryption: bool
    secure_boot: bool


@dataclass(frozen=True)
class ChipState:
    chip: str
    """esptool chip name of the connected chip."""
    major: int
    minor: int
    flash_size: str | None
    """Detected flash size, e.g. "16MB"; None if detection failed."""
    security: SecurityState

    @property
    def rev_full(self) -> int:
        return self.major * 100 + self.minor


def check_security(state: SecurityState) -> list[Check]:
    """One REFUSE per enabled feature, secure download mode first; a single PASS if none."""
    enabled = [
        (state.secure_download_mode, "secure download mode"),
        (state.flash_encryption, "flash encryption"),
        (state.secure_boot, "secure boot"),
    ]
    refusals = [
        Check(
            SECURITY,
            CheckLevel.REFUSE,
            f"{feature} is enabled; Embr does not flash such chips, use esptool / espsecure",
        )
        for on, feature in enabled
        if on
    ]
    return refusals or [
        Check(
            SECURITY,
            CheckLevel.PASS,
            "secure download mode, flash encryption and secure boot are off",
        )
    ]


def check_chip(detected: str, firmware_chip: str | None, entries: Sequence[PlanEntry]) -> Check:
    """REFUSE if the firmware chip or any entry's image chip differs from the detected chip;
    the message names every party's value. Entries without an image chip are ignored."""
    parties = [("board", detected)]
    if firmware_chip is not None:
        parties.append(("firmware", firmware_chip))
    parties += [(e.name, e.image_chip) for e in entries if e.image_chip is not None]
    mismatched = [(who, chip) for who, chip in parties if chip != detected]
    if mismatched:
        detail = ", ".join(f"{who}: {chip}" for who, chip in parties)
        return Check(CHIP, CheckLevel.REFUSE, f"chip mismatch ({detail})")
    return Check(CHIP, CheckLevel.PASS, f"{detected}: board, firmware and images agree")


def revision_incompatibility(state: ChipState, entry: PlanEntry) -> str | None:
    """Why `entry` cannot run on this chip revision, or None. Same rule as esptool:
    max_rev_full == 0 means the image predates the *_full fields; 0xFFFF means no upper
    limit; with both *_full fields unusable, compare legacy_min_rev against the major
    revision (minor on ESP32-C3). Entries without revision fields are compatible."""
    if entry.min_rev_full is None or entry.max_rev_full is None:
        return None
    min_full, max_full = entry.min_rev_full, entry.max_rev_full
    legacy = entry.legacy_min_rev or 0
    if max_full == 0:
        use_full = False
    elif max_full == NO_MAX_REVISION:
        use_full = not (min_full == 0 and legacy != 0)
    else:
        use_full = True
    if use_full:
        if min_full <= state.rev_full <= max_full:
            return None
        if max_full == NO_MAX_REVISION:
            need = f"{_rev(min_full)} or higher"
        elif min_full == 0:
            need = f"{_rev(max_full)} or lower"
        else:
            need = f"{_rev(min_full)} - {_rev(max_full)}"
        return f"{entry.name} requires chip revision {need}, chip is {_rev(state.rev_full)}"
    rev = state.minor if state.chip == "esp32c3" else state.major
    if rev >= legacy:
        return None
    return f"{entry.name} requires chip revision {legacy} or higher, chip is {rev}"


def check_revisions(state: ChipState, entries: Sequence[PlanEntry]) -> Check:
    """REFUSE naming the first incompatible entry; PASS otherwise. Checks every entry,
    not only the one at the bootloader offset."""
    for entry in entries:
        reason = revision_incompatibility(state, entry)
        if reason is not None:
            return Check(REVISION, CheckLevel.REFUSE, reason)
    return Check(REVISION, CheckLevel.PASS, f"chip {_rev(state.rev_full)}, all entries in range")


def check_capacity(
    state: ChipState,
    declared_size: str | None,
    entries: Sequence[PlanEntry],
    partitions_end: int | None,
) -> list[Check]:
    """WARN if declared_size (not None / "keep" / "detect") differs from the detected size;
    REFUSE if partitions_end or any entry's end exceeds the detected size; WARN if the size
    could not be detected. PASS entries for checks that pass."""
    checks: list[Check] = []
    detected = _size_bytes(state.flash_size) if state.flash_size else None
    if detected is None:
        checks.append(Check(FLASH_SIZE, CheckLevel.WARN, "flash size could not be detected"))
        return checks
    if declared_size not in (None, "keep", "detect") and declared_size != state.flash_size:
        checks.append(
            Check(
                FLASH_SIZE,
                CheckLevel.WARN,
                f"firmware declares {declared_size}, board has {state.flash_size}",
            )
        )
    else:
        checks.append(Check(FLASH_SIZE, CheckLevel.PASS, f"board has {state.flash_size}"))
    over = [e for e in entries if e.address + e.size > detected]
    if partitions_end is not None and partitions_end > detected:
        checks.append(
            Check(
                CAPACITY,
                CheckLevel.REFUSE,
                f"partition table ends at {partitions_end:#x}, beyond {state.flash_size}",
            )
        )
    elif over:
        e = over[0]
        checks.append(
            Check(
                CAPACITY,
                CheckLevel.REFUSE,
                f"{e.name} ends at {e.address + e.size:#x}, beyond {state.flash_size}",
            )
        )
    else:
        checks.append(Check(CAPACITY, CheckLevel.PASS, f"all entries fit in {state.flash_size}"))
    return checks


def check_files(entries: Sequence[PlanEntry]) -> Check:
    """Read every entry's file and compare size and sha256 with the plan. REFUSE naming the
    first missing or changed file."""
    for e in entries:
        path = Path(e.path)
        if not path.is_file():
            return Check(FILES, CheckLevel.REFUSE, f"{e.name}: file missing ({e.path})")
        data = path.read_bytes()
        if len(data) != e.size or hashlib.sha256(data).hexdigest() != e.sha256:
            return Check(
                FILES, CheckLevel.REFUSE, f"{e.name}: content changed since it was checked"
            )
    return Check(FILES, CheckLevel.PASS, f"{len(entries)} files: name, path and hash match")


def flash_size_bytes(size: str) -> int | None:
    """ "4MB" -> 4194304; None for anything else."""
    return _size_bytes(size)


def refuses(checks: Sequence[Check]) -> bool:
    """True if any check is REFUSE."""
    return any(c.level == CheckLevel.REFUSE for c in checks)


_SIZE_UNITS = {"KB": 1024, "MB": 1024 * 1024}


def _size_bytes(size: str) -> int | None:
    for unit, factor in _SIZE_UNITS.items():
        if size.upper().endswith(unit) and size[: -len(unit)].isdigit():
            return int(size[: -len(unit)]) * factor
    return None


def _rev(rev_full: int) -> str:
    return f"v{rev_full // 100}.{rev_full % 100}"
