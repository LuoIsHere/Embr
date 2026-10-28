"""Pre-write checks (design doc §4.2, §4.3). Pure functions; the worker feeds them the
state it reads from the chip, tests feed them injected state."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

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
    raise NotImplementedError


def check_chip(detected: str, firmware_chip: str | None, entries: Sequence[PlanEntry]) -> Check:
    """REFUSE if the firmware chip or any entry's image chip differs from the detected chip;
    the message names every party's value. Entries without an image chip are ignored."""
    raise NotImplementedError


def revision_incompatibility(state: ChipState, entry: PlanEntry) -> str | None:
    """Why `entry` cannot run on this chip revision, or None. Same rule as esptool:
    max_rev_full == 0 means the image predates the *_full fields; 0xFFFF means no upper
    limit; with both *_full fields unusable, compare legacy_min_rev against the major
    revision (minor on ESP32-C3). Entries without revision fields are compatible."""
    raise NotImplementedError


def check_revisions(state: ChipState, entries: Sequence[PlanEntry]) -> Check:
    """REFUSE naming the first incompatible entry; PASS otherwise. Checks every entry,
    not only the one at the bootloader offset."""
    raise NotImplementedError


def check_capacity(
    state: ChipState,
    declared_size: str | None,
    entries: Sequence[PlanEntry],
    partitions_end: int | None,
) -> list[Check]:
    """WARN if declared_size (not None / "keep" / "detect") differs from the detected size;
    REFUSE if partitions_end or any entry's end exceeds the detected size; WARN if the size
    could not be detected. PASS entries for checks that pass."""
    raise NotImplementedError


def check_files(entries: Sequence[PlanEntry]) -> Check:
    """Read every entry's file and compare size and sha256 with the plan. REFUSE naming the
    first missing or changed file."""
    raise NotImplementedError


def refuses(checks: Sequence[Check]) -> bool:
    """True if any check is REFUSE."""
    raise NotImplementedError
