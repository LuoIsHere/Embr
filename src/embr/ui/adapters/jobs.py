"""Worker events to the main thread, and job callbacks to Qt signals."""

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from embr.app.operations import JobOutcome, JobProgress
from embr.core.flasher.protocol import Event, LogEvent

LOG_BATCH_MS = 50


class MainThreadDispatcher(QObject):
    """app.operations.Dispatch for Qt: dispatch(fn) runs fn on the thread this object lives
    in (the GUI thread), in call order, whatever thread calls it."""

    _queued = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # emitted from a worker reader thread -> queued connection to this object's thread
        self._queued.connect(self._run)

    def dispatch(self, fn: Callable[[], None]) -> None:
        self._queued.emit(fn)

    @Slot(object)
    def _run(self, fn: object) -> None:
        if callable(fn):
            fn()


class JobSignals(QObject):
    """app.operations.Listener that re-emits as signals (call it on the GUI thread, which
    Operations does through MainThreadDispatcher). Log events are collected and emitted
    in batches every LOG_BATCH_MS, so a flood of lines does not stall the log view; the
    batch is flushed before `finished`, so the log is complete when a job ends."""

    job_event = Signal(object)
    """Every event except LogEvent, as it arrives."""
    log_batch = Signal(list)
    """list[LogEvent], in order."""
    progress = Signal(object)
    """JobProgress."""
    finished = Signal(object)
    """JobOutcome."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pending: list[LogEvent] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(LOG_BATCH_MS)
        self._timer.timeout.connect(self.flush)

    def on_event(self, event: Event) -> None:
        if isinstance(event, LogEvent):
            self._pending.append(event)
            if not self._timer.isActive():
                self._timer.start()
            return
        self.flush()  # keep log lines and other events in their original order
        self.job_event.emit(event)

    def on_progress(self, progress: JobProgress) -> None:
        self.progress.emit(progress)

    def on_finished(self, outcome: JobOutcome) -> None:
        self.flush()
        self.finished.emit(outcome)

    @Slot()
    def flush(self) -> None:
        self._timer.stop()
        if self._pending:
            batch, self._pending = self._pending, []
            self.log_batch.emit(batch)
