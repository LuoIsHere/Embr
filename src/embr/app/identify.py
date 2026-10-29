"""The last identification result (design doc §4.1).

The port is released when a job ends, so this is only a snapshot: it dims firmware for
other chips and lets checks run before connecting, but a flash job always identifies the
chip again in its own connection.
"""

import datetime
from dataclasses import dataclass

from embr.core.flasher.protocol import InfoEvent
from embr.core.serial import PortChange, PortInfo


@dataclass(frozen=True)
class IdentifySnapshot:
    port: PortInfo
    info: InfoEvent
    time: datetime.datetime


class IdentifyMemory:
    """Holds the snapshot and applies the clearing rules: it is bound to the port's device
    name, VID, PID and serial number. Selecting another port, or the port coming back with a
    different VID/PID/serial number, clears it; the same device re-enumerating (a
    USB-Serial/JTAG reset makes the port vanish briefly) keeps it."""

    def __init__(self) -> None:
        self._snapshot: IdentifySnapshot | None = None

    @property
    def snapshot(self) -> IdentifySnapshot | None:
        return self._snapshot

    def record(self, port: PortInfo, info: InfoEvent, time: datetime.datetime) -> None:
        """Store the result of an identify (or of the identification inside a flash job)."""
        self._snapshot = IdentifySnapshot(port, info, time)

    def select_port(self, port: PortInfo | None) -> None:
        """The user picked a port (None: no port). Clears the snapshot unless it is for the
        same device."""
        if self._snapshot is not None and (
            port is None or _identity(port) != _identity(self._snapshot.port)
        ):
            self._snapshot = None

    def ports_changed(self, change: PortChange) -> None:
        """Hotplug result from serial.PortWatcher. Clears the snapshot when a port with the
        snapshot's device name was added with a different identity."""
        if self._snapshot is None:
            return
        held = self._snapshot.port
        for port in change.added:
            if port.device == held.device and _identity(port) != _identity(held):
                self._snapshot = None
                return


def _identity(port: PortInfo) -> tuple[str, int | None, int | None, str | None]:
    return port.device, port.vid, port.pid, port.serial_number
