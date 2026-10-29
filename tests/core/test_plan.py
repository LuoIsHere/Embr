"""core/flasher/plan: reset rules, plan building, and API kwargs vs CLI argv agreement."""

import shlex
from pathlib import Path
from typing import Any

import pytest
from embr_testkit import idf_entries
from esptool import cli as esptool_cli

from embr.core.flasher.plan import (
    DEFAULT_UART_BAUD,
    ErasePlan,
    FlashPlan,
    FlashSettings,
    ReadPlan,
    adjust_reset,
    build_erase_plan,
    build_flash_plan,
    build_read_plan,
    cli_args,
    command_line,
    erase_cli_args,
    read_cli_args,
    write_flash_kwargs,
)
from embr.core.serial import ConnectionType

UART = ConnectionType.UART_BRIDGE
JTAG = ConnectionType.USB_SERIAL_JTAG
OTG = ConnectionType.USB_OTG


def _plan(connection: ConnectionType = UART, **kwargs: Any) -> FlashPlan:
    params: dict[str, Any] = {
        "port": "COM3",
        "connection": connection,
        "chip": "esp32s3",
        "entries": list(reversed(idf_entries("esp32s3_hello"))),
        "before": "default-reset",
        "after": "hard-reset",
    }
    return build_flash_plan(**(params | kwargs))


# --- reset rules (design doc §4.4) ------------------------------------------------------


def test_uart_keeps_default_and_hard_reset() -> None:
    assert adjust_reset(UART, "default-reset", "hard-reset") == ("default-reset", "hard-reset", ())


def test_usb_serial_jtag_hard_reset_becomes_watchdog() -> None:
    before, after, reasons = adjust_reset(JTAG, "default-reset", "hard-reset")
    assert (before, after) == ("default-reset", "watchdog-reset")
    assert len(reasons) == 1 and reasons[0].strip()
    plan = _plan(JTAG)
    assert plan.after == "watchdog-reset"  # the plan records the value actually used
    assert len(plan.adjustments) == 1


def test_usb_otg_before_is_no_reset() -> None:
    for before in ("default-reset", "usb-reset"):
        new_before, _, reasons = adjust_reset(OTG, before, "hard-reset")
        assert new_before == "no-reset"
        assert reasons
    assert _plan(OTG).before == "no-reset"


# --- build_flash_plan -------------------------------------------------------------------


def test_plan_defaults() -> None:
    plan = _plan()
    assert [e.address for e in plan.entries] == [0x0, 0x8000, 0x10000]
    assert plan.baud == DEFAULT_UART_BAUD == 460800
    assert plan.flash == FlashSettings()
    assert (plan.erase_all, plan.skip_flashed) == (False, True)
    assert _plan(erase_all=True).skip_flashed is False


def test_native_usb_has_no_baud() -> None:
    assert _plan(JTAG, baud=921600).baud is None
    assert _plan(OTG).baud is None
    assert _plan(baud=921600).baud == 921600


# --- the two derivations ----------------------------------------------------------------


def _parse_with_esptool(args: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse argv with esptool's own click parser (nothing runs); returns the global
    options and the write-flash options, with addr_filename as (address, path) pairs."""
    group: Any = esptool_cli
    group._esp = None  # normally set by EsptoolGroup.__call__
    ctx: Any = group.make_context("esptool", list(args))
    with ctx:
        sub: Any = group.commands["write-flash"].make_context(
            "write-flash", args[args.index("write-flash") + 1 :], parent=ctx
        )
    write: dict[str, Any] = dict(sub.params)
    pairs: list[tuple[int, str]] = []
    for address, handle in write.pop("addr_filename"):
        pairs.append((int(address), str(Path(handle.name).resolve())))
        handle.close()
    write["pairs"] = pairs
    return dict(ctx.params), write


VARIANTS: dict[str, dict[str, Any]] = {
    "uart": {},
    "jtag-no-stub": {"connection": JTAG, "stub": False},
    "erase-all-settings": {"erase_all": True, "flash": FlashSettings("dio", "80m", "4MB")},
}


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_cli_args_and_write_flash_kwargs_agree(variant: str) -> None:
    plan = _plan(**VARIANTS[variant])
    args = cli_args(plan)
    kwargs = write_flash_kwargs(plan)
    options, write = _parse_with_esptool(args)
    assert (options["chip"], options["port"]) == (plan.chip, plan.port)
    assert (options["before"], options["after"]) == (plan.before, plan.after)
    assert options["no_stub"] is (not plan.stub)
    assert ("-b" in args or "--baud" in args) is (plan.baud is not None)
    if plan.baud is not None:
        assert options["baud"] == plan.baud
    assert write["pairs"] == [(e.address, str(Path(e.path).resolve())) for e in plan.entries]
    for key, value in (
        ("flash_mode", plan.flash.mode),
        ("flash_freq", plan.flash.freq),
        ("flash_size", plan.flash.size),
    ):
        assert write[key] == kwargs.get(key, "keep") == value
    assert write["skip_flashed"] is bool(kwargs.get("skip_flashed")) is plan.skip_flashed
    assert write["erase_all"] is plan.erase_all
    # force is never passed; erase_all is done by the worker, not per write_flash call.
    assert write["force"] is False
    assert "force" not in kwargs and "erase_all" not in kwargs
    assert not any(arg.startswith("--force") for arg in args)


def test_command_line() -> None:
    plan = _plan()
    line = command_line(plan)
    assert line.startswith("esptool ")
    assert "--force" not in line
    lexer = shlex.shlex(line, posix=True)  # Windows-style quoting: quotes, no escapes
    lexer.whitespace_split, lexer.escape = True, ""
    assert list(lexer)[1:] == cli_args(plan)
    assert "--skip-flashed" in line
    assert "--skip-flashed" not in command_line(_plan(erase_all=True))


# --- erase and read plans ---------------------------------------------------------------


def test_erase_and_read_plans_adjust_reset_and_baud() -> None:
    for connection in (UART, JTAG, OTG):
        before, after, _ = adjust_reset(connection, "default-reset", "hard-reset")
        erase = build_erase_plan("COM3", connection)
        read = build_read_plan("COM3", connection, "dump.bin")
        assert (erase.before, erase.after) == (read.before, read.after) == (before, after)
        assert (read.address, read.size) == (0, None)
        uart = connection is UART
        assert read.baud == (DEFAULT_UART_BAUD if uart else None)
        assert build_read_plan("COM3", connection, "d", baud=921600).baud == (
            921600 if uart else None
        )
    with pytest.raises(ValueError):
        build_read_plan("COM3", UART, "d", address=-1)
    with pytest.raises(ValueError):
        build_read_plan("COM3", UART, "d", size=0)


DUMP = str(Path("dump.bin").resolve())
SERIAL_PLANS: dict[str, ErasePlan | ReadPlan] = {
    "erase": ErasePlan("COM3", UART, "default-reset", "hard-reset"),
    "read_all": ReadPlan("COM3", UART, 460800, "default-reset", "hard-reset", 0x0, None, DUMP),
    "read_jtag_no_stub": ReadPlan(
        "COM3", JTAG, None, "usb-reset", "watchdog-reset", 0x10000, 0x2000, DUMP, stub=False
    ),
}


@pytest.mark.parametrize("variant", list(SERIAL_PLANS))
def test_erase_and_read_cli_args(variant: str) -> None:
    plan = SERIAL_PLANS[variant]
    if isinstance(plan, ErasePlan):
        args, command, baud = erase_cli_args(plan), "erase-flash", None
    else:
        args, command, baud = read_cli_args(plan), "read-flash", plan.baud
    group: Any = esptool_cli
    group._esp = None
    ctx: Any = group.make_context("esptool", list(args))
    with ctx:
        sub: Any = group.commands[command].make_context(
            command, args[args.index(command) + 1 :], parent=ctx
        )
    options: dict[str, Any] = dict(ctx.params)
    assert (options["chip"], options["port"]) == ("auto", plan.port)
    assert (options["before"], options["after"]) == (plan.before, plan.after)
    assert options["no_stub"] is (not plan.stub)
    assert ("-b" in args or "--baud" in args) is (baud is not None)
    if baud is not None:
        assert options["baud"] == baud
    if isinstance(plan, ReadPlan):
        params: dict[str, Any] = dict(sub.params)
        size = "ALL" if plan.size is None else plan.size
        assert (params["address"], params["size"], params["output"]) == (
            plan.address,
            size,
            plan.output,
        )
    lexer = shlex.shlex(command_line(plan), posix=True)
    lexer.whitespace_split, lexer.escape = True, ""
    assert list(lexer) == ["esptool", *args]
