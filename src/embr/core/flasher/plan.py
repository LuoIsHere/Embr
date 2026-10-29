"""FlashPlan: the complete description of one serial job.

The worker's esptool API arguments and the equivalent command line shown in the
UI are both derived from the same plan.
"""

import subprocess
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


@dataclass(frozen=True)
class ErasePlan:
    """Erase the whole flash (no firmware involved)."""

    port: str
    connection: ConnectionType
    before: str
    after: str
    stub: bool = True


@dataclass(frozen=True)
class ReadPlan:
    """Read flash into a file in temp/ (dump)."""

    port: str
    connection: ConnectionType
    baud: int | None
    """None on native USB."""
    before: str
    after: str
    address: int
    size: int | None
    """Bytes to read; None reads to the end of the detected flash."""
    output: str
    """Absolute path the worker writes; the app moves the content into the archive."""
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
    if before not in BEFORE_MODES:
        raise ValueError(f"unknown before mode: {before}")
    if after not in AFTER_MODES:
        raise ValueError(f"unknown after mode: {after}")
    new_before, new_after = before, after
    reasons: list[str] = []
    if connection == ConnectionType.USB_OTG:
        new_before = "no-reset"
        if before != new_before:
            reasons.append(
                f"before={before} -> no-reset: on USB-OTG the chip only appears in ROM "
                "download mode, it cannot be reset into it"
            )
    elif connection == ConnectionType.USB_SERIAL_JTAG:
        if after == "hard-reset":
            new_after = "watchdog-reset"
            reasons.append(
                "after=hard-reset -> watchdog-reset: on USB-Serial/JTAG an RTS reset may "
                "leave the chip in download mode"
            )
    else:
        if before == "usb-reset":
            new_before = "default-reset"
            reasons.append("before=usb-reset -> default-reset: not a USB-Serial/JTAG port")
        if after == "watchdog-reset":
            new_after = "hard-reset"
            reasons.append("after=watchdog-reset -> hard-reset: not a native USB port")
    return new_before, new_after, tuple(reasons)


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
    new_before, new_after, reasons = adjust_reset(connection, before, after)
    if connection in _NATIVE_USB:
        baud = None
    elif baud is None:
        baud = DEFAULT_UART_BAUD
    return FlashPlan(
        port=port,
        connection=connection,
        baud=baud,
        chip=chip,
        before=new_before,
        after=new_after,
        stub=stub,
        flash=flash or FlashSettings(),
        entries=tuple(sorted(entries, key=lambda e: e.address)),
        erase_all=erase_all,
        skip_flashed=not erase_all,
        declared_flash_size=declared_flash_size,
        partitions_end=partitions_end,
        adjustments=reasons,
    )


def build_identify_plan(
    port: str, connection: ConnectionType, before: str = "default-reset"
) -> IdentifyPlan:
    """Identify job plan with reset modes adjusted to the connection type."""
    after = "no-reset" if connection in _NATIVE_USB else "hard-reset"
    new_before, new_after, _ = adjust_reset(connection, before, after)
    return IdentifyPlan(port=port, connection=connection, before=new_before, after=new_after)


def build_erase_plan(port: str, connection: ConnectionType) -> ErasePlan:
    """Erase plan with default reset modes adjusted to the connection type."""
    before, after, _ = adjust_reset(connection, "default-reset", "hard-reset")
    return ErasePlan(port=port, connection=connection, before=before, after=after)


def build_read_plan(
    port: str,
    connection: ConnectionType,
    output: str,
    *,
    address: int = 0,
    size: int | None = None,
    baud: int | None = None,
) -> ReadPlan:
    """Read plan with default reset modes adjusted to the connection type; baud as in
    build_flash_plan(). ValueError for a negative address or a size below 1."""
    if address < 0:
        raise ValueError(f"negative address {address:#x}")
    if size is not None and size < 1:
        raise ValueError(f"size must be at least 1 byte, got {size}")
    before, after, _ = adjust_reset(connection, "default-reset", "hard-reset")
    if connection in _NATIVE_USB:
        baud = None
    elif baud is None:
        baud = DEFAULT_UART_BAUD
    return ReadPlan(
        port=port,
        connection=connection,
        baud=baud,
        before=before,
        after=after,
        address=address,
        size=size,
        output=output,
    )


def erase_cli_args(plan: ErasePlan) -> list[str]:
    """--chip auto, -p, --before, --after, --no-stub (if off), erase-flash."""
    return [*_connect_args(plan.port, None, plan.before, plan.after, plan.stub), "erase-flash"]


def read_cli_args(plan: ReadPlan) -> list[str]:
    """--chip auto, -p, -b (UART only), --before, --after, --no-stub (if off),
    read-flash <address> <size, or ALL when None> <output>."""
    size = "ALL" if plan.size is None else f"{plan.size:#x}"
    return [
        *_connect_args(plan.port, plan.baud, plan.before, plan.after, plan.stub),
        "read-flash",
        f"{plan.address:#x}",
        size,
        plan.output,
    ]


def _connect_args(port: str, baud: int | None, before: str, after: str, stub: bool) -> list[str]:
    args = ["--chip", "auto", "-p", port]
    if baud is not None:
        args += ["-b", str(baud)]
    args += ["--before", before, "--after", after]
    if not stub:
        args.append("--no-stub")
    return args


def cli_args(plan: FlashPlan) -> list[str]:
    """esptool v5 arguments (without the program name) equivalent to the plan:
    --chip, -p, -b (UART only), --before, --after, --no-stub (if stub is off), write-flash,
    --erase-all / --skip-flashed, flash settings other than "keep", then address/path pairs.
    Never contains --force."""
    args = ["--chip", plan.chip, "-p", plan.port]
    if plan.baud is not None:
        args += ["-b", str(plan.baud)]
    args += ["--before", plan.before, "--after", plan.after]
    if not plan.stub:
        args.append("--no-stub")
    args.append("write-flash")
    if plan.erase_all:
        args.append("--erase-all")
    for key, value in _flash_settings(plan).items():
        args += ["--" + key.replace("_", "-"), value]
    if plan.skip_flashed:
        args.append("--skip-flashed")
    for entry in plan.entries:
        args += [f"{entry.address:#x}", entry.path]
    return args


def command_line(plan: FlashPlan | ErasePlan | ReadPlan) -> str:
    """`esptool ...` command line for display and copying, quoted for Windows."""
    if isinstance(plan, ErasePlan):
        args = erase_cli_args(plan)
    elif isinstance(plan, ReadPlan):
        args = read_cli_args(plan)
    else:
        args = cli_args(plan)
    return subprocess.list2cmdline(["esptool", *args])


def write_flash_kwargs(plan: FlashPlan) -> dict[str, object]:
    """Keyword arguments the worker passes to esptool.cmds.write_flash for each entry
    (flash_mode, flash_freq, flash_size, skip_flashed). Never contains force or erase_all;
    the worker erases once before the first entry when plan.erase_all is set."""
    kwargs: dict[str, object] = {
        "flash_mode": plan.flash.mode,
        "flash_freq": plan.flash.freq,
        "flash_size": plan.flash.size,
    }
    kwargs["skip_flashed"] = plan.skip_flashed
    return kwargs


_NATIVE_USB = (ConnectionType.USB_SERIAL_JTAG, ConnectionType.USB_OTG)


def _flash_settings(plan: FlashPlan) -> dict[str, str]:
    values = {
        "flash_mode": plan.flash.mode,
        "flash_freq": plan.flash.freq,
        "flash_size": plan.flash.size,
    }
    return {k: v for k, v in values.items() if v != "keep"}
