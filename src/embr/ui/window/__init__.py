"""The main window. EmbrWindow (main) assembles the panels and hands the work to one
controller per part: re-checking the files (recheck), board jobs (jobs), the right panel
(right_panel), firmware and project operations (actions) and theme, language and layout
(appearance)."""

from embr.ui.window.actions import is_pio_folder, sort_drop
from embr.ui.window.appearance import apply_theme
from embr.ui.window.main import EmbrWindow
from embr.ui.window.recheck import RECHECK_GAP_S, SETTLE_RECHECK_MS
from embr.ui.window.right_panel import (
    HINT_DOWNLOAD_MODE,
    HINT_QUICK_JTAG,
    HINT_QUICK_NEEDS_ID,
    HINT_QUICK_OTG,
    QUICK_KEY,
)

__all__ = [
    "HINT_DOWNLOAD_MODE",
    "HINT_QUICK_JTAG",
    "HINT_QUICK_NEEDS_ID",
    "HINT_QUICK_OTG",
    "QUICK_KEY",
    "RECHECK_GAP_S",
    "SETTLE_RECHECK_MS",
    "EmbrWindow",
    "apply_theme",
    "is_pio_folder",
    "sort_drop",
]
