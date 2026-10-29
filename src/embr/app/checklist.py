"""The checklist in the connection panel (design doc §6.1): what can be decided before
connecting, plus what the last identification says. The worker repeats the chip-side
checks in its own connection; this list is for the user, before pressing Flash.
"""

from dataclasses import dataclass
from enum import StrEnum

from embr.app.context import AppContext
from embr.app.identify import IdentifySnapshot
from embr.core.firmware.model import Firmware
from embr.core.serial import PortInfo


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
) -> list[CheckItem]:
    """In this order:
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
    raise NotImplementedError


def can_flash(items: list[CheckItem]) -> bool:
    """No REFUSE item."""
    raise NotImplementedError
