"""Checks of the firmware files: of the selection on a click, on activation and while a build
settles, and the periodic round over every firmware."""

import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from embr.app.refresh import REFRESH_SECONDS, RefreshSchedule
from embr.core.firmware.tracking import ResolveState

if TYPE_CHECKING:
    from embr.ui.window.main import EmbrWindow

SETTLE_RECHECK_MS = 2500
"""Look again after a build was seen settling (app.tracking.SETTLE_SECONDS plus margin)."""
RECHECK_GAP_S = 1.0
"""At most one check of the selection per this many seconds (clicks, activation)."""


def modal_open() -> bool:
    """A dialog or a menu is open: a click there is not a click in the window."""
    return (
        QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None
    )


class RecheckController:
    """Decides when the tracker looks at the files again and refreshes the window when what
    it finds differs from what is shown."""

    def __init__(self, window: "EmbrWindow") -> None:
        self._window = window
        self.schedule = RefreshSchedule()
        self.schedule.seed(window.context.history)
        self.clock: Callable[[], float] = time.monotonic
        """Seconds for the re-check rules; tests replace it to count checks without waiting."""
        self._timer = QTimer(window)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run)
        self._due: float | None = None
        """When the queued re-check runs (clock time); None when nothing is queued."""
        self._settle = False
        """The queued re-check looks at a settling build."""
        self._last: float | None = None
        self._checking = False
        """A check of the selection, or the refresh it caused, is running."""
        self._round: list[str] = []
        self._round_changed = False
        self._round_timer = QTimer(window)
        self._round_timer.setInterval(REFRESH_SECONDS * 1000)
        self._round_timer.timeout.connect(self.start_round)

    def start(self) -> None:
        """Start the periodic rounds."""
        self._round_timer.start()

    def stop(self) -> None:
        """The window closed: nothing is checked any more."""
        self._round_timer.stop()
        self._timer.stop()
        self._round = []

    def touch(self, firmware_id: str) -> None:
        """The firmware was used: it comes early in the next rounds."""
        self.schedule.touch(firmware_id)

    def request(self, delay_ms: int = 0, *, settle: bool = False) -> None:
        """The one way to queue a check of the selection: a click, the window activated, a
        build settling, a flash refused. One single-shot timer: an earlier queued
        check stands, a later one is moved up to the new time. At most one check per
        RECHECK_GAP_S; a request inside the gap waits for it, neither dropped nor repeated.
        `settle` (a build seen settling) looks again after delay_ms whatever the gap. While the
        shown build settles, other requests only make sure that look is queued. Nothing else
        is queued during a refresh round (it checks the selection first) or a job, behind a
        dialog or menu, or while the window is minimized."""
        w = self._window
        if w.closed:
            return
        if not settle:
            if self._round or w.busy() or w.isMinimized() or modal_open():
                return
            if self._shown_building():
                if self._timer.isActive():
                    return
                delay_ms, settle = SETTLE_RECHECK_MS, True
        now = self.clock()
        due = now + delay_ms / 1000
        if not settle and self._last is not None:
            due = max(due, self._last + RECHECK_GAP_S)
        queued = self._timer.isActive() and self._due is not None
        if queued and self._due is not None and self._due <= due:
            return
        self._settle = settle or (queued and self._settle)
        self._due = due
        self._timer.start(max(0, math.ceil((due - now) * 1000)))

    def _run(self) -> None:
        """The re-check timer fired: check the selection unless that is not the time now."""
        w = self._window
        settle = self._settle
        self._due = None
        self._settle = False
        if w.closed or w.busy() or w.isMinimized() or modal_open():
            return  # a job's end, closing the dialog or restoring the window asks again
        if self._round and not settle:
            return
        if self._checking:
            self.request(SETTLE_RECHECK_MS if settle else 0, settle=settle)
            return
        self.check_selected()

    def _shown_building(self) -> bool:
        shown = self._window.shown
        return (
            shown is not None
            and shown[1] == ResolveState.BUILDING
            and shown[0] == self._window.selected_firmware_id()
        )

    def check_selected(self) -> None:
        """Check the selection and refresh when it differs from what is shown (from the
        re-check timer, and when a PIO query finished). No refresh while a parameter field is
        being edited: what is shown stays, so the next check finds the difference again. A
        build still settling queues the next look."""
        w = self._window
        firmware_id = w.selected_firmware_id()
        if firmware_id is None or w.is_locked(firmware_id):
            return
        if self._checking:
            self.request()
            return
        self._checking = True
        try:
            self._last = self.clock()
            try:
                status = w.library.tracker.check(firmware_id)
            except KeyError:
                return
            if w.shown == (firmware_id, status.state, status.digest):
                w.header.show_files_updated(status)
            elif not w.firmware_view.params.editing():
                w.changed()
            if status.state == ResolveState.BUILDING:
                self.request(SETTLE_RECHECK_MS, settle=True)
        finally:
            self._checking = False

    def start_round(self) -> None:
        w = self._window
        if w.closed or w.busy() or w.isMinimized() or self._round:
            return
        existing = [f.id for f in w.context.firmware.all()]
        self._round = self.schedule.round(w.selected_firmware_id(), existing)
        self._round_changed = False
        QTimer.singleShot(0, self._next_in_round)

    def _next_in_round(self) -> None:
        """One firmware per event loop pass, so a round never blocks the window."""
        if self._window.closed or not self._round or self._window.busy():
            self._round = []
            return
        try:
            self._check_in_round(self._round.pop(0))
        finally:
            # an unexpected error goes on to the crash log; the round still goes on and
            # ends, or every later check would wait for it until a restart
            self._continue_round()

    def _check_in_round(self, firmware_id: str) -> None:
        w = self._window
        tracker = w.library.tracker
        before = tracker.cached(firmware_id)
        try:
            status = tracker.check(firmware_id)
        except KeyError:
            self.schedule.forget(firmware_id)
            return
        selected = firmware_id == w.selected_firmware_id()
        if selected:
            self._last = self.clock()
        if before is None or (before.state, before.digest) != (status.state, status.digest):
            self._round_changed = True
        elif selected:
            w.header.show_files_updated(status)
        if selected and status.state == ResolveState.BUILDING:
            self.request(SETTLE_RECHECK_MS, settle=True)

    def _continue_round(self) -> None:
        """The next firmware of the round, or its end: refresh what changed."""
        w = self._window
        if self._round:
            QTimer.singleShot(0, self._next_in_round)
        elif self._round_changed:
            selected = w.selected_firmware_id()
            if (
                selected is not None
                and not w.is_locked(selected)
                and not w.firmware_view.params.editing()
            ):
                w.changed()
            else:
                w.refresh_tree()
