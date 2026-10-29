"""Serial port enumeration, connection type detection, hotplug comparison and remembered
baud rates. Never opens a port."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from serial.tools import list_ports as _list_ports

from embr.core import chipinfo
from embr.core.storage.settings import Settings

DEFAULT_BAUD = 460800
"""Default for adjustable ports; the same as `idf.py flash -b`."""

BAUD_SECTION = "baud"
"""settings.ini section; keys are "vvvv:pppp" (lower-case hex VID:PID)."""


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


@dataclass(frozen=True)
class PortChange:
    added: tuple[PortInfo, ...]
    removed: tuple[PortInfo, ...]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


class PortWatcher:
    """Compares successive port lists. It has no thread or timer: the caller decides when
    to poll (the UI adapter polls on WM_DEVICECHANGE plus a slow timer, design doc §7.3).

    Ports are compared by identity: device name, VID, PID and serial number. A port that
    vanished and came back with the same identity between two polls is no change; one that
    came back with a different identity is reported as removed (old) and added (new)."""

    def __init__(self, enumerate_ports: Callable[[], list[PortInfo]] = list_ports) -> None:
        """`enumerate_ports` is replaceable for tests. The first poll() reports every port
        present as added."""
        self._enumerate = enumerate_ports
        self._ports: tuple[PortInfo, ...] = ()

    @property
    def ports(self) -> tuple[PortInfo, ...]:
        """The port list seen by the last poll(), sorted like list_ports(); empty before the
        first poll."""
        return self._ports

    def poll(self) -> PortChange:
        """Enumerate once and report what changed since the previous poll. Added and removed
        ports are sorted like list_ports()."""
        current = tuple(sorted(self._enumerate(), key=lambda p: _natural_key(p.device)))
        before = {_identity(p) for p in self._ports}
        after = {_identity(p) for p in current}
        added = tuple(p for p in current if _identity(p) not in before)
        removed = tuple(p for p in self._ports if _identity(p) not in after)
        self._ports = current
        return PortChange(added, removed)


def baud_adjustable(port: PortInfo) -> bool:
    """False for native USB (USB-Serial/JTAG, USB-OTG), where baud rate does not apply."""
    return port.connection not in (ConnectionType.USB_SERIAL_JTAG, ConnectionType.USB_OTG)


def default_baud(settings: Settings, port: PortInfo) -> int | None:
    """Baud to preselect for a port: None when not adjustable; else the rate remembered for
    its VID/PID, else DEFAULT_BAUD."""
    if not baud_adjustable(port):
        return None
    key = _baud_key(port)
    stored = settings.get(BAUD_SECTION, key) if key else None
    if stored is not None and stored.isdecimal() and int(stored) > 0:
        return int(stored)
    return DEFAULT_BAUD


def remember_baud(settings: Settings, port: PortInfo, baud: int) -> bool:
    """After a successful flash: store `baud` for the port's VID/PID. Not stored (False) for
    native USB and for ports without VID/PID. The caller saves the settings."""
    key = _baud_key(port)
    if key is None or not baud_adjustable(port):
        return False
    settings.set(BAUD_SECTION, key, str(baud))
    return True


def _identity(port: PortInfo) -> tuple[str, int | None, int | None, str | None]:
    return port.device, port.vid, port.pid, port.serial_number


def _baud_key(port: PortInfo) -> str | None:
    if port.vid is None or port.pid is None:
        return None
    return f"{port.vid:04x}:{port.pid:04x}"


def _natural_key(device: str) -> tuple[str, int]:
    # "COM10" sorts after "COM2"
    head = device.rstrip("0123456789")
    tail = device[len(head) :]
    return head, int(tail) if tail else -1
