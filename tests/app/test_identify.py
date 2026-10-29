"""app/identify: clearing rules of the last identification (design doc §4.1)."""

import dataclasses
import datetime

from app_testkit import BRIDGE, info

from embr.app.identify import IdentifyMemory
from embr.core.serial import PortChange

TIME = datetime.datetime(2026, 1, 1, 12, 0)
INFO = info("esp32s3", 0, 2)


def remembered() -> IdentifyMemory:
    memory = IdentifyMemory()
    memory.record(BRIDGE, INFO, TIME)
    return memory


def test_select_port_keeps_the_same_device_and_clears_otherwise() -> None:
    memory = remembered()
    memory.select_port(BRIDGE)
    snapshot = memory.snapshot
    assert snapshot is not None
    assert (snapshot.port, snapshot.info, snapshot.time) == (BRIDGE, INFO, TIME)

    memory.select_port(dataclasses.replace(BRIDGE, device="COM4"))
    assert memory.snapshot is None

    memory = remembered()
    memory.select_port(None)
    assert memory.snapshot is None


def test_hotplug_keeps_a_reenumerated_device_and_clears_a_different_one() -> None:
    memory = remembered()
    memory.ports_changed(PortChange(added=(), removed=(BRIDGE,)))
    memory.ports_changed(PortChange(added=(BRIDGE,), removed=()))
    assert memory.snapshot is not None

    other_board = dataclasses.replace(BRIDGE, serial_number="0002")
    memory.ports_changed(PortChange(added=(other_board,), removed=(BRIDGE,)))
    assert memory.snapshot is None
