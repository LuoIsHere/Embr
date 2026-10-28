"""identify and flash jobs. Runs inside the worker process only."""

import hashlib
from collections.abc import Callable
from pathlib import Path

from esptool.cmds import (  # pyright: ignore[reportUnknownVariableType]  # untyped **kwargs
    attach_flash,
    detect_chip,
    detect_flash_size,
    erase_flash,
    reset_chip,
    run_stub,
    write_flash,  # pyright: ignore[reportUnknownVariableType]  # untyped **kwargs
)
from esptool.loader import ESPLoader

from embr.core import chipinfo
from embr.core.flasher import checks
from embr.core.flasher.checks import Check, CheckLevel, ChipState, SecurityState
from embr.core.flasher.plan import ROM_BAUD, FlashPlan, IdentifyPlan, write_flash_kwargs
from embr.core.flasher.protocol import (
    CheckEvent,
    EntryEvent,
    EntryStatus,
    Event,
    InfoEvent,
    ResultEvent,
    ResultStatus,
)
from embr.core.flasher.worker.logger import EntryCursor
from embr.core.serial import ConnectionType

Send = Callable[[Event], None]


def identify(plan: IdentifyPlan, send: Send) -> None:
    esp = _connect(plan.port, plan.before)
    try:
        esp, info, _ = _read_chip(esp, plan.stub, plan.after == "no-reset")
        send(info)
        if not info.security.secure_download_mode:
            reset_chip(esp, plan.after)
    finally:
        _close(esp)
    send(ResultEvent(ResultStatus.OK, f"{info.chip} {info.revision} identified"))


def flash(plan: FlashPlan, send: Send, cursor: EntryCursor) -> None:
    for reason in plan.adjustments:
        send(CheckEvent(checks.RESET, CheckLevel.WARN, reason))
    if not plan.adjustments:
        send(CheckEvent(checks.RESET, CheckLevel.PASS, f"before={plan.before}, after={plan.after}"))
    if _send_checks(send, [checks.check_files(plan.entries)]):
        send(ResultEvent(ResultStatus.REFUSED, "entry files do not match the plan"))
        return
    # write exactly the bytes whose hash was checked
    payloads = [Path(e.path).read_bytes() for e in plan.entries]
    for entry, data in zip(plan.entries, payloads, strict=True):
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            send(
                CheckEvent(
                    checks.FILES, CheckLevel.REFUSE, f"{entry.name}: changed while preparing"
                )
            )
            send(ResultEvent(ResultStatus.REFUSED, "entry files do not match the plan"))
            return

    esp = _connect(plan.port, plan.before)
    try:
        esp, info, state = _read_chip(esp, plan.stub, plan.after == "no-reset")
        send(info)
        if _send_checks(send, checks.check_security(state.security)):
            send(ResultEvent(ResultStatus.REFUSED, "security features enabled; nothing written"))
            return
        found = [
            checks.check_chip(state.chip, plan.chip, plan.entries),
            checks.check_revisions(state, plan.entries),
            *checks.check_capacity(
                state, plan.declared_flash_size, plan.entries, plan.partitions_end
            ),
        ]
        if _send_checks(send, found):
            reset_chip(esp, plan.after)
            send(ResultEvent(ResultStatus.REFUSED, "checks refused the firmware; nothing written"))
            return
        if plan.baud is not None and plan.connection in (
            ConnectionType.UART_BRIDGE,
            ConnectionType.UNKNOWN,
        ):
            esp.change_baud(plan.baud)  # pyright: ignore[reportUnknownMemberType]  # decorated
        if plan.erase_all:
            erase_flash(esp)
        kwargs = write_flash_kwargs(plan)
        written = 0
        for index, (entry, data) in enumerate(zip(plan.entries, payloads, strict=True)):
            cursor.index, cursor.address, cursor.progressed = index, entry.address, False
            write_flash(esp, [(entry.address, data)], **kwargs)  # pyright: ignore[reportArgumentType]
            # skip_flashed makes esptool return without writing (and without progress callbacks)
            status = EntryStatus.WRITTEN if cursor.progressed else EntryStatus.SKIPPED
            written += status == EntryStatus.WRITTEN
            send(EntryEvent(index, entry.address, status))
        reset_chip(esp, plan.after)
    finally:
        _close(esp)
    skipped = len(plan.entries) - written
    send(ResultEvent(ResultStatus.OK, f"{written} written, {skipped} already in flash"))


def _connect(port: str, before: str) -> ESPLoader:
    return detect_chip(port=port, baud=ROM_BAUD, connect_mode=before)


def _read_chip(
    esp: ESPLoader, stub: bool, in_download_mode: bool
) -> tuple[ESPLoader, InfoEvent, ChipState]:
    """Read security state first; with secure download mode nothing else is read."""
    chip = chipinfo.chip_for_display_name(esp.CHIP_NAME)
    if chip is None:
        raise RuntimeError(f"connected chip {esp.CHIP_NAME} is not supported")
    sdm = bool(esp.secure_download_mode)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    if sdm:
        security = SecurityState(
            secure_download_mode=True, flash_encryption=False, secure_boot=False
        )
        info = InfoEvent(chip.name, "unknown", -1, -1, "", None, security, in_download_mode)
        return esp, info, ChipState(chip.name, -1, -1, None, security)
    security = SecurityState(
        secure_download_mode=False,
        flash_encryption=bool(esp.get_flash_encryption_enabled()),
        secure_boot=bool(esp.get_secure_boot_enabled()),
    )
    major, minor = int(esp.get_major_chip_version()), int(esp.get_minor_chip_version())
    mac = ":".join(f"{b:02x}" for b in esp.read_mac("BASE_MAC"))
    if stub:
        esp = run_stub(esp)
    attach_flash(esp)
    size = detect_flash_size(esp)
    info = InfoEvent(
        chip.name, f"v{major}.{minor}", major, minor, mac, size, security, in_download_mode
    )
    return esp, info, ChipState(chip.name, major, minor, size, security)


def _send_checks(send: Send, found: list[Check]) -> bool:
    """Send every check; True if any refuses."""
    for c in found:
        send(CheckEvent(c.name, c.level, c.message))
    return checks.refuses(found)


def _close(esp: ESPLoader) -> None:
    esp._port.close()  # pyright: ignore[reportPrivateUsage]  # releases the serial port
