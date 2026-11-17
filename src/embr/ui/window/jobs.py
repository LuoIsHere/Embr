"""Board jobs from the window: identify, flash, erase and read flash, and their events."""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QUrl
from PySide6.QtGui import (
    QDesktopServices,
)

from embr.app.flashplan import PlanError
from embr.app.operations import (
    SAVE_FAILED,
    BusyError,
    JobKind,
    JobOutcome,
    JobProgress,
)
from embr.app.tracking import BuildChangedError
from embr.core.flasher.checks import CheckLevel
from embr.core.flasher.protocol import (
    CheckEvent,
    EntryEvent,
    EntryStatus,
    ErrorEvent,
    Event,
    InfoEvent,
    LogEvent,
    LogLevel,
    ResultEvent,
)
from embr.core.history import RecordResult
from embr.i18n import N_, tr
from embr.ui import dialogs
from embr.ui.adapters import settings as ui_settings
from embr.ui.adapters.jobs import JobSignals
from embr.ui.connection import port_text
from embr.ui.window.recheck import SETTLE_RECHECK_MS

if TYPE_CHECKING:
    from embr.ui.window.main import EmbrWindow

LOG_LEVELS = {
    LogLevel.INFO: "info",
    LogLevel.NOTE: "note",
    LogLevel.WARNING: "warning",
    LogLevel.ERROR: "error",
}
CHECK_LEVELS = {CheckLevel.PASS: "ok", CheckLevel.WARN: "warning", CheckLevel.REFUSE: "error"}
RESULT_TEXT = {
    RecordResult.OK: N_("Done"),
    RecordResult.REFUSED: N_("Refused"),
    RecordResult.FAILED: N_("Failed"),
    RecordResult.CANCELLED: N_("Cancelled"),
}
READ_FOLDER = "read_folder"
"""settings.ini [ui]: the folder the last read flash was saved to."""


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _port_unavailable(message: str) -> bool:
    """The job could not open its port: in use by another program, or gone."""
    text = message.lower()
    return any(k in text for k in ("could not open", "port is busy", "access is denied"))


class JobController:
    """Starts board jobs from the right panel and shows their events, progress and end."""

    def __init__(self, window: "EmbrWindow") -> None:
        self._window = window
        self.signals = JobSignals(window)
        self._kind: JobKind | None = None
        self.last_outcome: JobOutcome | None = None
        """The last job's outcome; the hint above Flash describes it until the selection or the
        port changes."""
        self.signals.job_event.connect(self._event)
        self.signals.log_batch.connect(self._logs)
        self.signals.progress.connect(self._progress)
        self.signals.finished.connect(self._finished)

    def identify(self) -> None:
        port = self._window.connection.selected_port()
        if port is None or self._window.ops.busy:
            return
        self.last_outcome = None
        self._window.log_panel.clear_log()
        self._window.log_panel.log_lines(
            [("dim", f"# job identify · port {port.device} · {_now()}")]
        )
        try:
            self._window.ops.identify(port, self.signals)
        except (BusyError, OSError) as exc:
            self._window.note("error", f"[identify] {exc}")
            return
        self._kind = JobKind.IDENTIFY
        self._window.connection.set_running(True)
        self._window.connection.show_progress(0, N_("Connecting…"))

    def flash_or_cancel(self) -> None:
        if self._window.ops.busy:
            self._window.ops.cancel()
            self._window.note("error", "Cancelled: worker process killed, serial port released.")
            return
        firmware_id = self._window.selected_firmware_id()
        port = self._window.connection.selected_port()
        if firmware_id is None or port is None:
            return
        firmware = self._window.library.firmware(firmware_id)
        self.last_outcome = None
        self._window.log_panel.clear_log()
        self._window.log_panel.log_lines(
            [("dim", f"# job flash · {firmware.name} · {port_text(port)} · {_now()}")]
        )
        shown = (
            self._window.shown[2]
            if self._window.shown and self._window.shown[0] == firmware_id
            else None
        )
        try:
            self._window.ops.flash(
                firmware_id,
                port,
                self._window.connection.selected_baud(),
                self.signals,
                shown_digest=shown,
            )
        except BuildChangedError as exc:
            self._window.note("warning", f"[flash] not started: {exc}")
            self._window.changed()
            if exc.building:
                self._window.recheck.request(SETTLE_RECHECK_MS, settle=True)
            return
        except (PlanError, BusyError, OSError, ValueError) as exc:
            self._window.note("error", f"[flash] {exc}")
            self._window.right_panel.refresh()
            return
        self._kind = JobKind.FLASH
        self._window.recheck.touch(firmware_id)
        self._window.connection.set_running(True)
        self._window.connection.show_progress(0, N_("Connecting…"))
        self._window.refresh_tree()
        self._window.refresh_workspace()

    def flash_shortcut(self) -> None:
        if not self._window.ops.busy and self._window.connection.flash.isEnabled():
            self.flash_or_cancel()

    def escape(self) -> None:
        if self._window.ops.busy:
            self.flash_or_cancel()

    def _logs(self, batch: list[object]) -> None:
        lines: list[tuple[str, str]] = []
        for event in batch:
            if isinstance(event, LogEvent) and event.level != LogLevel.STAGE:
                lines.append((LOG_LEVELS.get(event.level, "info"), event.text))
        if lines:
            self._window.log_panel.log_lines(lines)

    def _event(self, event: Event) -> None:
        if isinstance(event, CheckEvent):
            level = CHECK_LEVELS[event.level]
            self._window.note(
                level, f"[check] {event.level.value.upper()} {event.name}: {event.message}"
            )
        elif isinstance(event, InfoEvent):
            self._window.note(
                "ok",
                f"Chip {event.chip} {event.revision} · MAC {event.mac or '-'} · flash "
                f"{event.flash_size or '-'}",
            )
            self._window.refresh_tree()
            self._window.connection.show_identity(self._window.memory.snapshot)
        elif isinstance(event, EntryEvent):
            if event.status == EntryStatus.SKIPPED:
                self._window.note("note", f"0x{event.address:X}: already in flash, skipped")
                self._window.connection.progress_text.setText(
                    tr(f"0x{event.address:X} already in flash, skipped")
                )
        elif isinstance(event, ResultEvent):
            self._window.note(
                "ok" if event.status.value == "ok" else "warning",
                f"[result] {event.status.value}: {event.message}",
            )
        elif isinstance(event, ErrorEvent):
            self._window.note("error", f"[error] {event.message}")

    def _progress(self, progress: object) -> None:
        if isinstance(progress, JobProgress):
            if self._kind == JobKind.READ_FLASH:
                text = N_(f"Reading from 0x{progress.address:X}")
            else:
                text = N_(
                    f"Writing 0x{progress.address:X} {progress.name} "
                    f"({progress.entry_index + 1}/{progress.entry_count})"
                )
            self._window.connection.show_progress(progress.percent, text)

    def _finished(self, outcome: object) -> None:
        if not isinstance(outcome, JobOutcome):
            return
        self.last_outcome = outcome
        self._kind = None
        self._window.connection.set_running(False)
        self._window.appearance.allow_restart()  # the language dialog's Restart waited for the job
        if self._window.right_panel.ports_pending:
            self._window.right_panel.apply_ports()
        if outcome.result == RecordResult.FAILED and _port_unavailable(outcome.message):
            self._window.memory.select_port(None)
            self._window.note("warning", "The port could not be opened (in use or disconnected).")
        for problem in outcome.problems:
            self._window.note("error", f"[finish] could not save {problem}")
        recorded = "Record saved" if outcome.record_id is not None else "Record NOT saved"
        if outcome.kind in (JobKind.ERASE, JobKind.READ_FLASH):
            done = outcome.result == RecordResult.OK
            self._window.connection.show_progress(
                100 if done else self._window.connection.progress.value(),
                RESULT_TEXT[outcome.result],
            )
            self._window.note(
                "ok" if done else "warning",
                f"{RESULT_TEXT[outcome.result]}. {recorded}. Port released.",
            )
            if outcome.message.startswith(SAVE_FAILED):
                self._window.note("error", f"[read] {outcome.message}")
            if done and outcome.saved_to:
                self._window.note("ok", f"[read] saved to {outcome.saved_to}")
            if done and outcome.firmware_id is not None:
                self._window.note(
                    "note", "[read] saved as a new firmware; it may contain secrets (NVS)"
                )
                self._window.select_firmware(outcome.firmware_id)
                return
        elif outcome.kind == JobKind.IDENTIFY:
            done = outcome.result == RecordResult.OK
            self._window.connection.show_progress(
                0, N_("Identified") if done else RESULT_TEXT[outcome.result]
            )
            self._window.note("dim", "Port released.")
        else:
            percent = (
                100
                if outcome.result == RecordResult.OK
                else self._window.connection.progress.value()
            )
            self._window.connection.show_progress(percent, RESULT_TEXT[outcome.result])
            self._window.note(
                "ok" if outcome.result == RecordResult.OK else "warning",
                f"{RESULT_TEXT[outcome.result]}. {recorded}: "
                f"result={outcome.result.value}. Port released.",
            )
            if outcome.result == RecordResult.CANCELLED:
                self._window.note("warning", "Flash content may be incomplete.")
        self._window.changed()

    def board_job(self, kind: JobKind) -> None:
        port = self._window.connection.selected_port()
        snapshot = self._window.memory.snapshot
        if port is None or snapshot is None or self._window.ops.busy:
            return
        request: dialogs.ReadRequest | None = None
        if kind == JobKind.ERASE:
            if not dialogs.erase_flash(self._window, port.device, snapshot.info):
                return
        else:
            project_id, device = self._window.current_placement()
            settings = self._window.context.settings
            folder = Path(settings.get(ui_settings.UI_SECTION, READ_FOLDER) or Path.home())
            request = dialogs.read_flash(
                self._window, self._window.library, snapshot.info, project_id, device, folder
            )
            if request is None:
                return
            settings.set(ui_settings.UI_SECTION, READ_FOLDER, str(request.output.parent))
            settings.save()
        self.last_outcome = None
        self._window.log_panel.clear_log()
        self._window.log_panel.log_lines(
            [("dim", f"# job {kind.value} · {port_text(port)} · {_now()}")]
        )
        try:
            if request is None:
                self._window.ops.erase(port, self.signals)
            else:
                self._window.ops.read_flash(
                    port,
                    self._window.connection.selected_baud(),
                    self.signals,
                    address=request.address,
                    size=request.size,
                    project_id=request.placement.project_id,
                    device=request.placement.device,
                    output=request.output,
                    import_firmware=request.import_firmware,
                )
        except (BusyError, OSError, ValueError) as exc:
            self._window.note("error", f"[{kind.value}] {exc}")
            return
        self._kind = kind
        self._window.connection.set_running(True)
        self._window.connection.show_progress(0, N_("Connecting…"))

    def erase(self) -> None:
        self.board_job(JobKind.ERASE)

    def read(self) -> None:
        self.board_job(JobKind.READ_FLASH)

    def open_board_log(self) -> None:
        snapshot = self._window.memory.snapshot
        if snapshot is None or not snapshot.info.mac:
            return
        path = self._window.context.logs.board_log_path(snapshot.info.mac)
        if not path.is_file():
            self._window.note("note", f"[board] no flash, erase or read on {snapshot.info.mac} yet")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
