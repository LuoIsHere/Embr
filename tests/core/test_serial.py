"""core/serial: connection type from USB VID/PID, values from chipinfo/esptool."""

from embr_testkit import chip_defs
from esp_pylib.constants import ESPRESSIF_VID, USB_JTAG_SERIAL_PID

from embr.core.serial import ConnectionType, classify_connection


def test_usb_serial_jtag() -> None:
    assert (ESPRESSIF_VID, USB_JTAG_SERIAL_PID) == (0x303A, 0x1001)
    assert classify_connection(0x303A, 0x1001) == (ConnectionType.USB_SERIAL_JTAG, None)


def test_usb_otg() -> None:
    for name in ("esp32s3", "esp32p4"):
        pid = chip_defs()[name].IMAGE_CHIP_ID
        assert classify_connection(ESPRESSIF_VID, pid) == (ConnectionType.USB_OTG, name)


def test_uart_bridge() -> None:
    assert classify_connection(0x1A86, 0x55D3) == (ConnectionType.UART_BRIDGE, None)
    assert classify_connection(0x10C4, 0xEA60) == (ConnectionType.UART_BRIDGE, None)


def test_no_usb_ids() -> None:
    assert classify_connection(None, None) == (ConnectionType.UNKNOWN, None)
