"""core/serial: connection type from USB VID/PID (values from chipinfo/esptool), hotplug
comparison and remembered baud rates."""

import dataclasses
from pathlib import Path

import pytest
from embr_testkit import chip_defs
from esptool.loader import ESPLoader

from embr.core.serial import (
    BAUD_SECTION,
    DEFAULT_BAUD,
    ConnectionType,
    PortInfo,
    PortWatcher,
    baud_adjustable,
    classify_connection,
    default_baud,
    remember_baud,
)
from embr.core.storage.settings import Settings

ESPRESSIF_VID = ESPLoader.ESPRESSIF_VID
USB_JTAG_SERIAL_PID = ESPLoader.USB_JTAG_SERIAL_PID


def test_usb_serial_jtag() -> None:
    assert (ESPRESSIF_VID, USB_JTAG_SERIAL_PID) == (0x303A, 0x1001)
    assert classify_connection(0x303A, 0x1001) == (ConnectionType.USB_SERIAL_JTAG, None)


def test_usb_otg() -> None:
    for name in ("esp32s3", "esp32p4"):
        pid = chip_defs()[name].IMAGE_CHIP_ID
        assert classify_connection(ESPRESSIF_VID, pid) == (ConnectionType.USB_OTG, name)


def test_uart_bridge() -> None:
    assert classify_connection(0x10C4, 0xEA60) == (ConnectionType.UART_BRIDGE, None)
    assert classify_connection(0x10C4, 0xEA60) == (ConnectionType.UART_BRIDGE, None)


def test_no_usb_ids() -> None:
    assert classify_connection(None, None) == (ConnectionType.UNKNOWN, None)


# --- hotplug -----------------------------------------------------------------------------


def _port(device: str, vid: int | None, pid: int | None, serial_number: str | None) -> PortInfo:
    connection, otg_chip = classify_connection(vid, pid)
    return PortInfo(device, vid, pid, serial_number, "", connection, otg_chip)


BRIDGE = _port("COM3", 0x10C4, 0xEA60, "0001")
JTAG = _port("COM4", 0x303A, 0x1001, "AA:BB:CC:DD:EE:FF")
OTG = _port("COM5", ESPRESSIF_VID, chip_defs()["esp32s3"].IMAGE_CHIP_ID, None)
NO_USB = _port("COM6", None, None, None)


def _watcher(*polls: list[PortInfo]) -> PortWatcher:
    """A watcher whose enumeration returns `polls` in turn."""
    script = iter(polls)
    return PortWatcher(lambda: next(script))


def test_first_poll_reports_present_ports_as_added() -> None:
    watcher = _watcher([BRIDGE, JTAG])
    assert watcher.ports == ()
    change = watcher.poll()
    assert (change.added, change.removed) == ((BRIDGE, JTAG), ())
    assert watcher.ports == (BRIDGE, JTAG)


def test_plug_in_and_unplug() -> None:
    watcher = _watcher([BRIDGE], [BRIDGE, JTAG], [BRIDGE])
    watcher.poll()
    plugged = watcher.poll()
    assert (plugged.added, plugged.removed) == ((JTAG,), ())
    assert watcher.ports == (BRIDGE, JTAG)
    unplugged = watcher.poll()
    assert (unplugged.added, unplugged.removed) == ((), (JTAG,))
    assert watcher.ports == (BRIDGE,)


def test_quick_replug_of_same_device_is_no_change() -> None:
    # unplugged and back between two polls: same identity, only the description differs
    replugged = dataclasses.replace(JTAG, description="USB Serial Device")
    watcher = _watcher([BRIDGE, JTAG], [BRIDGE, replugged])
    watcher.poll()
    change = watcher.poll()
    assert not change.changed
    assert (change.added, change.removed) == ((), ())


@pytest.mark.parametrize(
    "new",
    [
        dataclasses.replace(JTAG, serial_number="11:22:33:44:55:66"),
        _port("COM4", 0x10C4, 0xEA60, "AA:BB:CC:DD:EE:FF"),  # other VID/PID, same name
    ],
    ids=["serial_number", "vid_pid"],
)
def test_same_name_with_other_identity_is_removed_and_added(new: PortInfo) -> None:
    watcher = _watcher([JTAG], [new])
    watcher.poll()
    change = watcher.poll()
    assert (change.added, change.removed) == ((new,), (JTAG,))
    assert watcher.ports == (new,)


# --- baud --------------------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(tmp_path / "settings.ini", tmp_path / "backup")


@pytest.mark.parametrize("port", [JTAG, OTG], ids=["usb_serial_jtag", "usb_otg"])
def test_native_usb_has_no_baud(tmp_path: Path, port: PortInfo) -> None:
    settings = _settings(tmp_path)
    assert not baud_adjustable(port)
    assert default_baud(settings, port) is None
    assert not remember_baud(settings, port, 921600)
    assert settings.keys(BAUD_SECTION) == []


def test_port_without_usb_ids_is_not_remembered(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert baud_adjustable(NO_USB)
    assert not remember_baud(settings, NO_USB, 921600)
    assert settings.keys(BAUD_SECTION) == []
    assert default_baud(settings, NO_USB) == DEFAULT_BAUD


def test_bridge_baud_is_remembered_by_vid_pid(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert baud_adjustable(BRIDGE)
    assert default_baud(settings, BRIDGE) == DEFAULT_BAUD
    assert remember_baud(settings, BRIDGE, 921600)
    assert settings.keys(BAUD_SECTION) == ["10c4:ea60"]
    assert default_baud(settings, BRIDGE) == 921600
    same_kind = _port("COM7", 0x10C4, 0xEA60, "0002")
    assert default_baud(settings, same_kind) == 921600


def test_remembered_baud_survives_reload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert remember_baud(settings, BRIDGE, 230400)
    settings.save()
    assert default_baud(_settings(tmp_path), BRIDGE) == 230400
