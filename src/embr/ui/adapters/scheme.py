"""Follow the system light / dark setting."""

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication


class SchemeWatcher(QObject):
    changed = Signal(bool)
    """True for dark."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_change)

    @property
    def dark(self) -> bool:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark

    def _on_change(self, scheme: Qt.ColorScheme) -> None:
        self.changed.emit(scheme == Qt.ColorScheme.Dark)
