"""Serial port enumeration and connection type detection. Never opens a port."""

from dataclasses import dataclass
from enum import StrEnum

from serial.tools import list_ports as _list_ports

from embr.core import chipinfo


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
    """e.g. "COM3"."""
    vid: int | None
    pid: int | None
    serial_number: str | None
    description: str
    connection: ConnectionType
    otg_chip: str | None
    """For USB-OTG: the chip that PID belongs to (esptool name), else None."""


def classify_connection(vid: int | None, pid: int | None) -> tuple[ConnectionType, str | None]:
    """Connection type for a USB VID/PID, plus the chip name for USB-OTG."""
    if vid is None or pid is None:
        return ConnectionType.UNKNOWN, None
    if vid != chipinfo.espressif_vid():
        return ConnectionType.UART_BRIDGE, None
    if pid == chipinfo.usb_serial_jtag_pid():
        return ConnectionType.USB_SERIAL_JTAG, None
    chip = chipinfo.usb_otg_chip(vid, pid)
    if chip is not None:
        return ConnectionType.USB_OTG, chip.name
    # Espressif VID with an unknown PID: most likely a custom USB stack in the running
    # firmware; treat it like a bridge so the normal reset sequence is tried.
    return ConnectionType.UART_BRIDGE, None


def list_ports() -> list[PortInfo]:
    """Ports currently present, sorted by device name. Classified, never opened."""
    ports: list[PortInfo] = []
    for p in _list_ports.comports():
        connection, otg_chip = classify_connection(p.vid, p.pid)
        ports.append(
            PortInfo(
                device=p.device,
                vid=p.vid,
                pid=p.pid,
                serial_number=p.serial_number,
                description=p.description or "",
                connection=connection,
                otg_chip=otg_chip,
            )
        )
    return sorted(ports, key=lambda p: _natural_key(p.device))


def _natural_key(device: str) -> tuple[str, int]:
    # "COM10" sorts after "COM2"
    head = device.rstrip("0123456789")
    tail = device[len(head) :]
    return head, int(tail) if tail else -1
