"""app/checklist: the checks shown before pressing Flash (design doc §4, §6.1)."""

import dataclasses
from pathlib import Path

import pytest
from app_testkit import BRIDGE, JTAG, build_files, copy_files, fixed_firmware, info, snapshot

from embr.app.checklist import CheckItem, CheckState, can_flash, flash_ready, preflight
from embr.app.context import AppContext
from embr.app.identify import IdentifySnapshot
from embr.app.library import Library
from embr.core.flasher.checks import SecurityState
from embr.core.flasher.protocol import InfoEvent
from embr.core.serial import PortInfo

PASS, WARN, REFUSE, LATER = CheckState.PASS, CheckState.WARN, CheckState.REFUSE, CheckState.LATER
HELLO = build_files("esp32s3_hello")


def states(items: list[CheckItem]) -> list[CheckState]:
    return [i.state for i in items]


def refusals(items: list[CheckItem]) -> list[CheckItem]:
    return [i for i in items if i.state == REFUSE]


def test_clean_firmware_before_identification(library: Library, context: AppContext) -> None:
    # layout, files, reset (UART bridge keeps hard-reset), one LATER for the chip side
    items = preflight(context, fixed_firmware(library, HELLO), BRIDGE, None)
    assert states(items) == [PASS, PASS, PASS, LATER]
    assert can_flash(items)


def test_overlap_refuses_layout(library: Library, context: AppContext) -> None:
    firmware = fixed_firmware(library, HELLO)
    app = next(e for e in firmware.entries if e.name == HELLO[2].name)
    library.set_entry_address(firmware.id, app.id, "0x8000")
    items = preflight(context, library.firmware(firmware.id), BRIDGE, None)
    assert items[0].state == REFUSE
    assert not can_flash(items)


def test_changed_file_refuses_files(library: Library, context: AppContext, tmp_path: Path) -> None:
    files = copy_files(HELLO, tmp_path)
    firmware = fixed_firmware(library, files)
    with files[2].open("ab") as file:
        file.write(b"\0" * 16)
    items = preflight(context, firmware, BRIDGE, None)
    assert states(items) == [PASS, REFUSE, PASS, LATER]
    assert not can_flash(items)


def test_reset_adjusted_on_usb_serial_jtag_warns(library: Library, context: AppContext) -> None:
    items = preflight(context, fixed_firmware(library, HELLO), JTAG, None)
    assert states(items) == [PASS, PASS, WARN, LATER]
    assert can_flash(items)


@pytest.mark.parametrize(
    ("sample", "event"),
    [("esp32s3_hello", info("esp32s3", 0, 2)), ("esp32p4_rev3_hello", info("esp32p4", 3, 1))],
    ids=["esp32s3", "esp32p4-v3"],
)
def test_matching_chip_passes_everything(
    library: Library, context: AppContext, sample: str, event: InfoEvent
) -> None:
    firmware = fixed_firmware(library, build_files(sample))
    items = preflight(context, firmware, BRIDGE, snapshot(BRIDGE, event))
    assert len(items) > 4
    assert set(states(items)) == {PASS}


@pytest.mark.parametrize(
    ("sample", "event", "mentions"),
    [
        ("esp32s3_hello", info("esp32c3", 0, 4), "esp32c3"),
        # the chip matches, so the only possible refusal is the v3.x-only image range
        ("esp32p4_rev3_hello", info("esp32p4", 1, 0), ""),
        (
            "esp32s3_hello",
            info("esp32s3", 0, 2, security=SecurityState(False, False, secure_boot=True)),
            "secure boot",
        ),
    ],
    ids=["chip-mismatch", "p4-revision", "secure-boot"],
)
def test_identification_refuses(
    library: Library, context: AppContext, sample: str, event: InfoEvent, mentions: str
) -> None:
    firmware = fixed_firmware(library, build_files(sample))
    items = preflight(context, firmware, BRIDGE, snapshot(BRIDGE, event))
    (refused,) = refusals(items)
    assert mentions in f"{refused.title} {refused.detail}".lower()
    assert states(items)[:3] == [PASS, PASS, PASS]
    assert not can_flash(items)


def test_declared_size_differing_from_detected_warns(library: Library, context: AppContext) -> None:
    # the bootloader header declares 2MB
    firmware = fixed_firmware(library, HELLO)
    items = preflight(context, firmware, BRIDGE, snapshot(BRIDGE, info("esp32s3", 0, 2, "4MB")))
    assert states(items).count(WARN) == 1
    assert REFUSE not in states(items)
    assert can_flash(items)


IDENTIFIED = snapshot(BRIDGE, info("esp32s3", 0, 2))
FINE = [CheckItem(PASS, "a"), CheckItem(WARN, "b"), CheckItem(LATER, "c")]


@pytest.mark.parametrize(
    ("items", "identified", "port", "ready"),
    [
        (FINE, IDENTIFIED, BRIDGE, True),
        (FINE, IDENTIFIED, None, False),
        (FINE, None, BRIDGE, False),
        (FINE, IDENTIFIED, dataclasses.replace(BRIDGE, device="COM4"), False),
        ([*FINE, CheckItem(REFUSE, "d")], IDENTIFIED, BRIDGE, False),
    ],
    ids=["ready", "no_port", "not_identified", "other_port", "refused"],
)
def test_flash_ready(
    items: list[CheckItem], identified: IdentifySnapshot | None, port: PortInfo | None, ready: bool
) -> None:
    # Flash needs an identification of the selected port and no refusal (§12 item 5)
    assert flash_ready(items, identified, port) is ready
