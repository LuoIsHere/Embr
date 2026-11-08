"""app/checklist.flash_ready() with Quick flash (design D7): quick_chip_known drops only the
identification; a port and no refusing item are still needed. The default keeps the rule
of design doc §12 item 5."""

import dataclasses

import pytest
from app_testkit import BRIDGE, JTAG, info, snapshot

from embr.app.checklist import CheckItem, CheckState, flash_ready
from embr.app.identify import IdentifySnapshot
from embr.core.serial import PortInfo

PASS, WARN, REFUSE, LATER = CheckState.PASS, CheckState.WARN, CheckState.REFUSE, CheckState.LATER
FINE = [CheckItem(PASS, "a"), CheckItem(WARN, "b"), CheckItem(LATER, "c")]
REFUSED = [*FINE, CheckItem(REFUSE, "d")]
OTHER = dataclasses.replace(BRIDGE, device="COM4")
SAME_PORT = snapshot(BRIDGE, info("esp32s3", 0, 2))
OTHER_PORT = snapshot(OTHER, info("esp32s3", 0, 2))

# (items, snapshot, port, ready with quick off, ready with quick on)
CASES = [
    (FINE, SAME_PORT, BRIDGE, True, True),
    (FINE, None, BRIDGE, False, True),
    (FINE, OTHER_PORT, BRIDGE, False, True),
    (FINE, SAME_PORT, None, False, False),
    (FINE, None, None, False, False),
    (REFUSED, SAME_PORT, BRIDGE, False, False),
    (REFUSED, None, BRIDGE, False, False),
    (REFUSED, OTHER_PORT, BRIDGE, False, False),
    ([CheckItem(REFUSE, "only")], None, BRIDGE, False, False),
]
IDS = [
    "identified",
    "no_snapshot",
    "snapshot_of_other_port",
    "no_port",
    "no_port_no_snapshot",
    "refused_identified",
    "refused_no_snapshot",
    "refused_other_port",
    "only_refusal",
]


@pytest.mark.parametrize(("items", "identified", "port", "off", "on"), CASES, ids=IDS)
def test_quick_drops_only_the_identification(
    items: list[CheckItem],
    identified: IdentifySnapshot | None,
    port: PortInfo | None,
    off: bool,
    on: bool,
) -> None:
    assert flash_ready(items, identified, port, quick_chip_known=False) is off
    assert flash_ready(items, identified, port, quick_chip_known=True) is on


@pytest.mark.parametrize(("items", "identified", "port", "off", "on"), CASES, ids=IDS)
def test_default_is_quick_off(
    items: list[CheckItem],
    identified: IdentifySnapshot | None,
    port: PortInfo | None,
    off: bool,
    on: bool,
) -> None:
    assert flash_ready(items, identified, port) is off


@pytest.mark.parametrize("quick", [False, True])
def test_same_device_name_counts_as_identified(quick: bool) -> None:
    # the snapshot is matched by device name only, whatever the link type
    assert flash_ready(FINE, snapshot(JTAG, info("esp32s3", 0, 2)), BRIDGE, quick_chip_known=quick)


@pytest.mark.parametrize("quick", [False, True])
def test_no_items_refuse_nothing(quick: bool) -> None:
    assert flash_ready([], SAME_PORT, BRIDGE, quick_chip_known=quick)
    assert flash_ready([], None, BRIDGE, quick_chip_known=quick) is quick


@pytest.mark.parametrize("state", [PASS, WARN, LATER])
def test_quick_accepts_every_state_but_refuse(state: CheckState) -> None:
    assert flash_ready([CheckItem(state, "x")], None, BRIDGE, quick_chip_known=True)
