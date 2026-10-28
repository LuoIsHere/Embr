"""Serial port enumeration and connection type detection. Never opens a port."""

from dataclasses import dataclass
from enum import StrEnum


class ConnectionType(StrEnum):
    UART_BRIDGE = "uart_bridge"
    """USB-to-UART bridge (any non-Espressif USB VID)."""
    USB_SERIAL_JTAG = "usb_serial_jtag"
    """Espressif VID, USB-Serial/JTAG PID."""
    USB_OTG = "usb_otg"
    """Espressif VID, PID equal to the chip's image chip ID."""
    UNKNOWN = "unknown"
    """No USB VID/PID (e.g. a Bluetooth serial port)."""


@dataclass(frozen=True)
class PortInfo:
    device: str
    """e.g. "COM9"."""
    vid: int | None
    pid: int | None
    serial_number: str | None
    description: str
    connection: ConnectionType
    otg_chip: str | None
    """For USB-OTG: the chip that PID belongs to (esptool name), else None."""


def classify_connection(vid: int | None, pid: int | None) -> tuple[ConnectionType, str | None]:
    """Connection type for a USB VID/PID, plus the chip name for USB-OTG."""
    raise NotImplementedError


def list_ports() -> list[PortInfo]:
    """Ports currently present, sorted by device name. Classified, never opened."""
    raise NotImplementedError
