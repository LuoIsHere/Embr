"""Hotplug: Windows device notifications plus a slow timer drive core.serial.PortWatcher.

QSerialPortInfo is in QtSerialPort, which PySide6-Essentials does not ship. Serial devices
broadcast WM_DEVICECHANGE to top-level windows, so the main window forwards its native
events here; no extra registration is needed.
"""

import ctypes
import ctypes.wintypes
import sys

from PySide6.QtCore import QByteArray, QObject, QTimer, Signal, Slot

from embr.core.serial import PortChange, PortInfo, PortWatcher

WM_DEVICECHANGE = 0x0219
SETTLE_MS = 300
"""Delay after a notification: enumeration lags the notification."""
FALLBACK_MS = 2000


class PortMonitor(QObject):
    changed = Signal(object)
    """PortChange, only when something changed (the first poll reports every port)."""

    def __init__(self, watcher: PortWatcher | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._watcher = watcher or PortWatcher()
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self.poll)
        self._fallback = QTimer(self)
        self._fallback.setInterval(FALLBACK_MS)
        self._fallback.timeout.connect(self.poll)

    @property
    def ports(self) -> tuple[PortInfo, ...]:
        return self._watcher.ports

    def start(self) -> None:
        """Poll once now, then on notifications and every FALLBACK_MS."""
        self.poll()
        self._fallback.start()

    def poll_soon(self) -> None:
        """Poll after SETTLE_MS; repeated calls within that time poll once."""
        self._settle.start()

    def handle_native_event(self, event_type: QByteArray | bytes, message: int) -> None:
        """Call from QWidget.nativeEvent(); reacts to WM_DEVICECHANGE only."""
        kind = event_type.data() if isinstance(event_type, QByteArray) else event_type
        if sys.platform == "win32" and kind == b"windows_generic_MSG" and message:
            msg = ctypes.wintypes.MSG.from_address(message)
            if msg.message == WM_DEVICECHANGE:
                self.poll_soon()

    @Slot()
    def poll(self) -> None:
        change: PortChange = self._watcher.poll()
        if change.changed:
            self.changed.emit(change)
