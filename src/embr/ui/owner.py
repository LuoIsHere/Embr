"""What the panels need from the main window, so they do not import it."""

from typing import Protocol

from embr.app.context import AppContext
from embr.app.library import Library
from embr.ui.theme import Palette


class Owner(Protocol):
    @property
    def context(self) -> AppContext: ...

    @property
    def library(self) -> Library: ...

    @property
    def palette(self) -> Palette: ...

    def changed(self) -> None:
        """Data changed: refresh the tree, the workspace and the connection panel."""
        ...

    def note(self, level: str, text: str) -> None:
        """A line in the log panel for an action of the user; level: info, note, warning,
        error, ok, dim."""
        ...

    def select_firmware(self, firmware_id: str) -> None: ...

    def select_project(self, project_id: str) -> None: ...

    def is_locked(self, firmware_id: str) -> bool:
        """True while the firmware is being flashed (editing is locked)."""
        ...

    def show_log_segment(self, file_name: str, segment_id: str) -> None:
        """Show a record's section of its day's log in the log panel."""
        ...

    def firmware_action(self, name: str) -> None:
        """rename, duplicate, move, delete for the selected firmware."""
        ...
