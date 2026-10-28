"""FlashPlan: the complete description of one serial job.

The worker's esptool API arguments and the equivalent command line shown in the
UI are both derived from the same plan.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from embr.core.serial import ConnectionType

DEFAULT_UART_BAUD = 460800
"""Default baud rate for UART bridges, same as `idf.py flash`."""
ROM_BAUD = 115200
"""Baud rate of the ROM handshake."""

BEFORE_MODES: tuple[str, ...] = ("default-reset", "usb-reset", "no-reset")
AFTER_MODES: tuple[str, ...] = ("hard-reset", "watchdog-reset", "no-reset")


@dataclass(frozen=True)
class FlashSettings:
    mode: str = "keep"
    freq: str = "keep"
    size: str = "keep"


@dataclass(frozen=True)
class PlanEntry:
    address: int
    path: str
    """Absolute path of the file to write."""
    name: str
    sha256: str
    """Expected sha256 (hex) of the file; the worker refuses on mismatch."""
    size: int
    image_chip: str | None = None
    """Chip name from the image header, None if the entry is not an ESP image."""
    min_rev_full: int | None = None
    max_rev_full: int | None = None
    legacy_min_rev: int | None = None


@dataclass(frozen=True)
class FlashPlan:
    port: str
    connection: ConnectionType
    baud: int | None
    """None on native USB, where the baud rate does not apply."""
    chip: str
    """Expected chip (esptool name)."""
    before: str
    """Reset mode after adjustment to the connection type."""
    after: str
    stub: bool
    flash: FlashSettings
    entries: tuple[PlanEntry, ...]
    """Enabled entries, sorted by address."""
    erase_all: bool
    skip_flashed: bool
    """True unless erase_all is set."""
    declared_flash_size: str | None = None
    """Flash size declared by the firmware or the bootloader header, for the size warning."""
    partitions_end: int | None = None
    """End address of the last partition in the enabled partition table, if any."""
    adjustments: tuple[str, ...] = field(default=())
    """Human-readable reasons for reset modes changed by adjust_reset()."""


@dataclass(frozen=True)
class IdentifyPlan:
    port: str
    connection: ConnectionType
    before: str
    after: str
    """"no-reset" on native USB (chip stays in download mode), "hard-reset" otherwise."""
    stub: bool = True


def adjust_reset(
    connection: ConnectionType, before: str, after: str
) -> tuple[str, str, tuple[str, ...]]:
    """Apply the per-connection reset rules (design doc §4.4).

    UART bridge (and unknown): before default-reset | no-reset, after hard-reset | no-reset.
    USB-Serial/JTAG: before default-reset | usb-reset | no-reset; after hard-reset becomes
    watchdog-reset. USB-OTG: before is always no-reset. Returns (before, after, reasons),
    one reason per changed value.
    """
    raise NotImplementedError


def build_flash_plan(
    *,
    port: str,
    connection: ConnectionType,
    chip: str,
    entries: Sequence[PlanEntry],
    before: str,
    after: str,
    baud: int | None = None,
    stub: bool = True,
    flash: FlashSettings | None = None,
    erase_all: bool = False,
    declared_flash_size: str | None = None,
    partitions_end: int | None = None,
) -> FlashPlan:
    """Build a plan: sorts entries by address, adjusts reset modes, sets baud to None on
    native USB and to DEFAULT_UART_BAUD when not given otherwise, enables skip_flashed
    unless erase_all, and uses FlashSettings() (all "keep") when `flash` is None."""
    raise NotImplementedError


def build_identify_plan(
    port: str, connection: ConnectionType, before: str = "default-reset"
) -> IdentifyPlan:
    """Identify job plan with reset modes adjusted to the connection type."""
    raise NotImplementedError


def cli_args(plan: FlashPlan) -> list[str]:
    """esptool v5 arguments (without the program name) equivalent to the plan:
    --chip, -p, -b (UART only), --before, --after, --no-stub (if stub is off), write-flash,
    --erase-all / --skip-flashed, flash settings other than "keep", then address/path pairs.
    Never contains --force."""
    raise NotImplementedError


def command_line(plan: FlashPlan) -> str:
    """`esptool ...` command line for display and copying, quoted for Windows."""
    raise NotImplementedError


def write_flash_kwargs(plan: FlashPlan) -> dict[str, object]:
    """Keyword arguments the worker passes to esptool.cmds.write_flash for each entry
    (flash_mode, flash_freq, flash_size, skip_flashed). Never contains force or erase_all;
    the worker erases once before the first entry when plan.erase_all is set."""
    raise NotImplementedError
