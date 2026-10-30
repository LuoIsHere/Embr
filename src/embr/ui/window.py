"""The main window: wires the panels to the app layer."""

from datetime import date, datetime
from pathlib import Path

import esptool
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMenu,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from embr import __version__
from embr.app.archiving import Archiver
from embr.app.checklist import flash_ready, preflight
from embr.app.context import AppContext
from embr.app.flashplan import PlanError, preview_command
from embr.app.identify import IdentifyMemory, IdentifySnapshot
from embr.app.library import AlreadyTrackedError, Library
from embr.app.merging import Merger
from embr.app.operations import BusyError, JobKind, JobOutcome, JobProgress, Operations
from embr.app.refresh import REFRESH_SECONDS, RefreshSchedule
from embr.app.tracking import BuildChangedError, TrackStatus
from embr.core.firmware.import_idf import IdfBuildError
from embr.core.firmware.model import editable_params
from embr.core.firmware.store import default_name_archived
from embr.core.firmware.tracking import ResolveState
from embr.core.flasher.checks import CheckLevel
from embr.core.flasher.merge import MergeError
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
from embr.core.logs import HEADER_PREFIX
from embr.core.serial import ConnectionType, PortChange, PortInfo, default_baud
from embr.ui import dialogs
from embr.ui.adapters import settings as ui_settings
from embr.ui.adapters.jobs import JobSignals, MainThreadDispatcher
from embr.ui.adapters.ports import PortMonitor
from embr.ui.adapters.scheme import SchemeWatcher
from embr.ui.connection import ConnectionPanel, port_text
from embr.ui.frame import FrameWindow
from embr.ui.icons import app_icon
from embr.ui.log_panel import LOG_HEIGHT, LogPanel
from embr.ui.project_view import ProjectView
from embr.ui.sidebar import PLUS, Sidebar
from embr.ui.theme import Palette, palette, style_sheet, ui_font
from embr.ui.widgets import label
from embr.ui.workspace import FirmwareView, Header

LOG_LEVELS = {
    LogLevel.INFO: "info",
    LogLevel.NOTE: "note",
    LogLevel.WARNING: "warning",
    LogLevel.ERROR: "error",
}
CHECK_LEVELS = {CheckLevel.PASS: "ok", CheckLevel.WARN: "warning", CheckLevel.REFUSE: "error"}
RESULT_TEXT = {
    RecordResult.OK: "Done",
    RecordResult.REFUSED: "Refused",
    RecordResult.FAILED: "Failed",
    RecordResult.CANCELLED: "Cancelled",
}
HINT_MANUAL_RESET = (
    "Flashed and verified. If the device did not restart on its own (usually when download "
    "mode was entered by hand), reset it manually."
)
HINT_DOWNLOAD_MODE = (
    "Identified. The chip stays in download mode so the next flash can start right away; "
    "reset the board to return to its application."
)
HINT_IDENTIFY = (
    "Connect & identify this board first; Flash becomes available when no check refuses."
)
HINT_OTG = (
    "Connect & identify this board first. USB-OTG: put the board into download mode as "
    "described by its vendor before connecting; Cancel is available while waiting."
)
HINT_JTAG = (
    "Connect & identify this board first. USB-Serial/JTAG: automatic reset is tried; if the "
    "running firmware does not keep this peripheral, the connect step times out, then enter "
    "download mode as described by the board's vendor."
)


def apply_theme(app: QApplication, p: Palette) -> None:
    app.setStyle("Fusion")
    app.setFont(ui_font())
    qp = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, p.panel),
        (QPalette.ColorRole.Base, p.panel),
        (QPalette.ColorRole.AlternateBase, p.panel_2),
        (QPalette.ColorRole.Button, p.panel),
        (QPalette.ColorRole.Text, p.fg),
        (QPalette.ColorRole.WindowText, p.fg),
        (QPalette.ColorRole.ButtonText, p.fg),
        (QPalette.ColorRole.PlaceholderText, p.fg_3),
        (QPalette.ColorRole.Highlight, p.accent),
        (QPalette.ColorRole.HighlightedText, p.accent_fg),
        (QPalette.ColorRole.ToolTipBase, p.panel),
        (QPalette.ColorRole.ToolTipText, p.fg),
        (QPalette.ColorRole.Mid, p.line),
        (QPalette.ColorRole.Midlight, p.line_2),
        (QPalette.ColorRole.Link, p.accent),
    ):
        qp.setColor(role, QColor(color))
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.fg_3))
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.fg_3))
    app.setPalette(qp)
    app.setStyleSheet(style_sheet(p))


class EmbrWindow(FrameWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__(f"{__version__} · esptool {esptool.__version__}")
        self.setWindowTitle("Embr")
        self.setWindowIcon(app_icon())
        self._context = context
        self._library = Library(context)
        tracker = self._library.tracker
        self._memory = IdentifyMemory()
        self._dispatcher = MainThreadDispatcher(self)
        self._ops = Operations(context, self._memory, self._dispatcher.dispatch, tracker=tracker)
        self._archiver = Archiver(context, tracker)
        self._merger = Merger(context, tracker)
        self._schedule = RefreshSchedule()
        self._schedule.seed(context.history)
        self._shown: tuple[str, ResolveState, str] | None = None
        """Firmware id, state and digest the workspace shows (flash compares the digest)."""
        self._round: list[str] = []
        self._round_changed = False
        self._job_kind: JobKind | None = None
        self._signals = JobSignals(self)
        self._selection: tuple[str, str] | None = None
        self._query = ""
        self._last_outcome: JobOutcome | None = None
        self._dismissed_hint: tuple[int, str | None, str] | None = None
        """(id of the job outcome, port, text) of the hint the user clicked away."""
        self._job_firmware: str | None = None
        self._ports_pending = False
        self._scheme = SchemeWatcher(self)
        self._palette = palette(self._scheme.dark)

        self.sidebar_panel = Sidebar()
        self.header = Header(self)
        self.firmware_view = FirmwareView(self)
        self.project_view = ProjectView(self)
        self.empty_view = QWidget()
        empty = QVBoxLayout(self.empty_view)
        empty.setContentsMargins(14, 14, 14, 14)
        empty.addWidget(
            label(
                f"Create a firmware or a project with {PLUS} New in the sidebar.",
                tone="3",
            )
        )
        empty.addStretch(1)
        self.pages = QStackedWidget()
        for page in (self.firmware_view, self.project_view, self.empty_view):
            self.pages.addWidget(page)
        center = QWidget()
        center.setObjectName("center")
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        center_layout.addWidget(self.header)
        center_layout.addWidget(self.pages, 1)
        self.workspace = center
        self.log_panel = LogPanel()
        self.center_split = QSplitter(Qt.Orientation.Vertical)
        self.center_split.setHandleWidth(7)
        self.center_split.setChildrenCollapsible(False)
        self.center_split.addWidget(center)
        self.center_split.addWidget(self.log_panel)
        self.center_split.setStretchFactor(0, 1)
        self.center_split.setSizes([500, LOG_HEIGHT[1]])
        self.connection = ConnectionPanel()
        self.set_panels(self.sidebar_panel, self.center_split, self.connection)
        self.ports = PortMonitor(parent=self)
        self.native_event_hook = self.ports.handle_native_event

        self.sidebar_panel.firmware_selected.connect(self.select_firmware)
        self.sidebar_panel.project_selected.connect(self.select_project)
        self.sidebar_panel.query_changed.connect(self._search)
        self.sidebar_panel.new_firmware.connect(self._new_firmware)
        self.sidebar_panel.new_project.connect(self._new_project)
        self.sidebar_panel.import_idf.connect(self._import_idf)
        self.firmware_view.entries.archive_requested.connect(self._archive_entry)
        self.firmware_view.entries.merge_requested.connect(self._merge)
        self.connection.erase_clicked.connect(self._erase)
        self.connection.read_clicked.connect(self._read)
        self.connection.board_log_clicked.connect(self._open_board_log)
        self.sidebar_panel.tree.move_requested.connect(self._move)
        self.connection.port_changed.connect(self._port_changed)
        self.connection.baud_changed.connect(self._refresh_right)
        self.connection.identify_clicked.connect(self._identify)
        self.connection.flash_clicked.connect(self._flash_or_cancel)
        self.connection.erase_all_toggled.connect(self._set_erase_all)
        self.connection.hint_dismissed.connect(self._dismiss_hint)
        self.log_panel.expand_toggled.connect(self._expand_log)
        self.log_panel.log_folder_clicked.connect(self._open_log_folder)
        self.ports.changed.connect(self._ports_changed)
        self._signals.job_event.connect(self._job_event)
        self._signals.log_batch.connect(self._job_logs)
        self._signals.progress.connect(self._job_progress)
        self._signals.finished.connect(self._job_finished)
        self._scheme.changed.connect(self._scheme_changed)
        self.topbar.menu_button.clicked.connect(self._window_menu)
        for keys, slot in (
            ("Ctrl+B", self.toggle_sidebar),
            ("Ctrl+Return", self._flash_shortcut),
            ("Ctrl+Enter", self._flash_shortcut),
            ("Esc", self._escape),
            ("F2", lambda: self.firmware_action("rename")),
        ):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(slot)

        self._apply_palette()
        self._restore_layout()
        self.log_panel.log_lines(
            [("dim", "[idle] Pick a firmware and a port, then Connect & identify or Flash.")]
        )
        first = self._first_firmware()
        if first is not None:
            self._selection = ("firmware", first)
        self.ports.start()
        if not self.ports.ports:
            self.connection.set_ports(())
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_SECONDS * 1000)
        self._refresh_timer.timeout.connect(self._start_round)
        self._refresh_timer.start()
        self.changed()

    # ---- Owner ------------------------------------------------------------------------

    @property
    def context(self) -> AppContext:
        return self._context

    @property
    def library(self) -> Library:
        return self._library

    @property
    def palette(self) -> Palette:  # type: ignore[override]
        return self._palette

    def changed(self) -> None:
        self._refresh_workspace()
        self._refresh_tree()
        self._refresh_right()

    def note(self, level: str, text: str) -> None:
        self.log_panel.log_lines([(level, text)])

    def select_firmware(self, firmware_id: str) -> None:
        self._leave_selection()
        self._schedule.touch(firmware_id)
        self._selection = ("firmware", firmware_id)
        self._last_outcome = None
        self.changed()

    def select_project(self, project_id: str) -> None:
        self._leave_selection()
        self._selection = ("project", project_id)
        self._last_outcome = None
        self.changed()

    def is_locked(self, firmware_id: str) -> bool:
        return self._ops.running_firmware_id == firmware_id

    def show_log_segment(self, file_name: str, segment_id: str) -> None:
        line = self._context.logs.find_segment(file_name, segment_id)
        if line is None:
            self.note("error", f"[log] section {segment_id} not found in logs/{file_name}")
            return
        lines = (self._context.paths.logs / file_name).read_text(encoding="utf-8").splitlines()
        section = [lines[line]]
        for text in lines[line + 1 :]:
            if text.startswith(HEADER_PREFIX):
                break
            section.append(text)
        self.log_panel.clear_log()
        self.log_panel.log_lines(
            [("dim", f"[log] logs/{file_name}, line {line + 1}")] + [("info", t) for t in section]
        )

    def firmware_action(self, name: str) -> None:
        firmware_id = self._selected_firmware_id()
        if firmware_id is None or self.is_locked(firmware_id):
            return
        firmware = self._library.firmware(firmware_id)
        if name == "rename":
            scope = "the project" if firmware.project_id else "standalone firmware"
            text = dialogs.ask_text(
                self,
                "Rename firmware",
                "Name",
                firmware.name,
                f"Unique within {scope}; a duplicate gets a numeric suffix. Flash records keep "
                "the name used at the time.",
                "Rename",
            )
            if text:
                renamed = self._library.rename_firmware(firmware_id, text)
                self.note("dim", f"[firmware] renamed to '{renamed.name}'")
        elif name == "duplicate":
            copy = self._library.duplicate_firmware(firmware_id)
            self._selection = ("firmware", copy.id)
            self.note("dim", f"[firmware] duplicated as '{copy.name}'")
        elif name == "move":
            placement = dialogs.move_firmware(
                self, self._library, firmware.project_id, firmware.device
            )
            if placement is not None:
                self._move(firmware_id, placement.project_id, placement.device)
                return
        elif name == "archive":
            self._archive_version(firmware_id)
            return
        elif name == "directory":
            self._change_build_dir(firmware_id)
            return
        elif name == "delete":
            accepted, _ = dialogs.confirm(
                self,
                "Delete firmware",
                [
                    f"Delete <b>{firmware.name}</b>? Archived files are kept (use Clean archive to "
                    "remove unreferenced ones) and flash records are not affected."
                ],
                "Delete",
            )
            if not accepted:
                return
            self._library.delete_firmware(firmware_id)
            self.note("warning", f"[firmware] '{firmware.name}' deleted")
            first = self._first_firmware()
            self._selection = ("firmware", first) if first else None
        self.changed()

    # ---- refresh ----------------------------------------------------------------------

    def _selected_firmware_id(self) -> str | None:
        return self._selection[1] if self._selection and self._selection[0] == "firmware" else None

    def _first_firmware(self) -> str | None:
        tree = self._library.tree()
        for project in tree.projects:
            for group in project.groups:
                if group.firmware:
                    return group.firmware[0].id
        return tree.standalone[0].id if tree.standalone else None

    def _refresh_tree(self) -> None:
        snapshot = self._memory.snapshot
        self.sidebar_panel.show_tree(
            self._library.tree(self._query),
            self._selection,
            self._library.mismatches(snapshot.info) if snapshot else {},
            self._ops.running_firmware_id,
        )

    def _refresh_workspace(self) -> None:
        selection = self._selection
        try:
            if selection is not None and selection[0] == "firmware":
                status = self._library.tracker.check(selection[1])
                self._show_status(status)
                self.pages.setCurrentWidget(self.firmware_view)
                return
            if selection is not None and selection[0] == "project":
                self.header.show_project(self, selection[1])
                self.project_view.show_project(selection[1])
                self.pages.setCurrentWidget(self.project_view)
                return
        except KeyError:  # deleted meanwhile
            self._selection = None
        self._shown = None
        self.header.hide()
        self.pages.setCurrentWidget(self.empty_view)
        return

    def _refresh_right(self) -> None:
        self.header.setVisible(self._selection is not None)
        port = self.connection.selected_port()
        snapshot = self._memory.snapshot
        self.connection.show_identity(snapshot)
        firmware_id = self._selected_firmware_id()
        busy = self._ops.busy
        if firmware_id is None:
            self.connection.show_checks(None)
            self.connection.set_flash_enabled(False)
            self.cmdbar.set_command("(select a firmware to see its command)")
        else:
            tracker = self._library.tracker
            status = tracker.cached(firmware_id) or tracker.check(firmware_id)
            firmware = status.firmware
            items = preflight(self._context, firmware, port, snapshot, status)
            self.connection.show_checks(items)
            ready = flash_ready(items, snapshot, port)
            self.connection.set_flash_enabled(ready and not busy)
            command = preview_command(
                self._context, firmware, port, self.connection.selected_baud()
            )
            self.cmdbar.set_command(command or "(add entries with addresses to see the command)")
        self.connection.identify.setEnabled(port is not None and not busy)
        self.connection.set_board_actions(
            port is not None
            and snapshot is not None
            and snapshot.port.device == port.device
            and not busy
        )
        self._show_erase_all(firmware_id, busy)
        text, kind = self._hint(port, snapshot)
        key = (id(self._last_outcome), port.device if port else None, text)
        self.connection.set_hint("" if key == self._dismissed_hint else text, kind)

    def _hint(self, port: PortInfo | None, snapshot: IdentifySnapshot | None) -> tuple[str, str]:
        """The hint above Flash: what the last job left behind, else what to do first."""
        outcome = self._last_outcome
        if outcome is not None and outcome.result == RecordResult.OK:
            if outcome.kind == JobKind.FLASH and outcome.native_usb:
                return HINT_MANUAL_RESET, "warn"
            if (
                outcome.kind == JobKind.IDENTIFY
                and snapshot is not None
                and snapshot.info.in_download_mode
            ):
                return HINT_DOWNLOAD_MODE, "ok"
        if port is None or (snapshot is not None and snapshot.port.device == port.device):
            return "", "ok"
        if port.connection == ConnectionType.USB_OTG:
            return HINT_OTG, "warn"
        if port.connection == ConnectionType.USB_SERIAL_JTAG:
            return HINT_JTAG, "ok"
        return HINT_IDENTIFY, "ok"

    def _dismiss_hint(self) -> None:
        """Keep a clicked-away hint hidden until the job outcome, port or text changes."""
        port = self.connection.selected_port()
        text, _ = self._hint(port, self._memory.snapshot)
        self._dismissed_hint = (id(self._last_outcome), port.device if port else None, text)

    def _show_erase_all(self, firmware_id: str | None, busy: bool) -> None:
        if firmware_id is None:
            self.connection.set_erase_all(False, False, visible=False)
            return
        firmware = self._library.firmware(firmware_id)
        editable = "erase_all" in editable_params(firmware) and not firmware.read_only
        enabled = editable and not busy and not self.is_locked(firmware_id)
        self.connection.set_erase_all(firmware.erase_all, enabled, visible=True)

    def _set_erase_all(self, checked: bool) -> None:
        firmware_id = self._selected_firmware_id()
        if firmware_id is None or self._ops.busy:
            return
        try:
            self._library.set_param(firmware_id, "erase_all", checked)
        except ValueError as exc:
            self.note("error", f"[params] {exc}")
        self.changed()

    # ---- sidebar actions ----------------------------------------------------------------

    def _search(self, text: str) -> None:
        self._query = text
        self._refresh_tree()

    def _move(self, firmware_id: str, project_id: object, device: str) -> None:
        if self.is_locked(firmware_id):
            return
        target = project_id if isinstance(project_id, str) else None
        before = self._library.firmware(firmware_id)
        if before.project_id == target and before.device == device:
            return
        moved = self._library.move_firmware(firmware_id, target, device)
        if target is None:
            where = "standalone"
        else:
            where = self._library.project(target).name + (
                f" / {device}" if device else " (ungrouped)"
            )
        self.note("dim", f"[move] '{moved.name}' → {where}")
        self.changed()

    def _current_placement(self) -> tuple[str | None, str]:
        selection = self._selection
        if selection is None:
            return None, ""
        if selection[0] == "project":
            return selection[1], ""
        try:
            firmware = self._library.firmware(selection[1])
        except KeyError:
            return None, ""
        return firmware.project_id, firmware.device

    def _new_firmware(self) -> None:
        project_id, device = self._current_placement()
        result = dialogs.new_firmware(self, self._library, project_id, device)
        if result is None:
            return
        name, placement = result
        firmware = self._library.create_firmware(name, placement.project_id, placement.device)
        where = (
            f" in {self._library.project(placement.project_id).name}"
            if placement.project_id
            else " as standalone"
        )
        self.note("dim", f"[new] fixed firmware '{firmware.name}' created{where}")
        self.select_firmware(firmware.id)

    def _new_project(self) -> None:
        result = dialogs.new_project(self)
        if result is None:
            return
        project = self._library.create_project(*result)
        self.note("dim", f"[project] created '{project.name}'")
        self.select_project(project.id)

    # ---- ports ------------------------------------------------------------------------

    def _ports_changed(self, change: object) -> None:
        if isinstance(change, PortChange):
            self._memory.ports_changed(change)
        if self._ops.busy:
            # the job keeps its port; the list (and selection) follow once it ends
            self._ports_pending = True
            return
        self._apply_ports()

    def _apply_ports(self) -> None:
        self._ports_pending = False
        had = self.connection.selected_port()
        self.connection.set_ports(self.ports.ports)
        now = self.connection.selected_port()
        if had is None or now is None or had.device != now.device:
            self._port_changed()
        else:
            self._refresh_tree()
            self._refresh_right()

    def _port_changed(self) -> None:
        port = self.connection.selected_port()
        self._memory.select_port(port)
        self._last_outcome = None
        self.connection.set_baud(
            port, default_baud(self._context.settings, port) if port else 460800
        )
        self._refresh_tree()
        self._refresh_right()

    # ---- jobs -------------------------------------------------------------------------

    def _identify(self) -> None:
        port = self.connection.selected_port()
        if port is None or self._ops.busy:
            return
        self._last_outcome = None
        self.log_panel.clear_log()
        self.log_panel.log_lines([("dim", f"# job identify · port {port.device} · {_now()}")])
        try:
            self._ops.identify(port, self._signals)
        except (BusyError, OSError) as exc:
            self.note("error", f"[identify] {exc}")
            return
        self._job_firmware = None
        self._job_kind = JobKind.IDENTIFY
        self.connection.set_running(True)
        self.connection.show_progress(0, "Connecting…")

    def _flash_or_cancel(self) -> None:
        if self._ops.busy:
            self.note("error", "Cancelling: worker process killed, serial port released.")
            self._ops.cancel()
            return
        firmware_id = self._selected_firmware_id()
        port = self.connection.selected_port()
        if firmware_id is None or port is None:
            return
        firmware = self._library.firmware(firmware_id)
        self._last_outcome = None
        self.log_panel.clear_log()
        self.log_panel.log_lines(
            [("dim", f"# job flash · {firmware.name} · {port_text(port)} · {_now()}")]
        )
        shown = self._shown[2] if self._shown and self._shown[0] == firmware_id else None
        try:
            self._ops.flash(
                firmware_id,
                port,
                self.connection.selected_baud(),
                self._signals,
                shown_digest=shown,
            )
        except BuildChangedError as exc:
            self.note("warning", f"[flash] not started: {exc}")
            self.changed()
            if exc.building:
                QTimer.singleShot(SETTLE_RECHECK_MS, self._recheck_selected)
            return
        except (PlanError, BusyError, OSError, ValueError) as exc:
            self.note("error", f"[flash] {exc}")
            self._refresh_right()
            return
        self._job_firmware = firmware_id
        self._job_kind = JobKind.FLASH
        self._schedule.touch(firmware_id)
        self.connection.set_running(True)
        self.connection.show_progress(0, "Connecting…")
        self._refresh_tree()
        self._refresh_workspace()

    def _flash_shortcut(self) -> None:
        if not self._ops.busy and self.connection.flash.isEnabled():
            self._flash_or_cancel()

    def _escape(self) -> None:
        if self._ops.busy:
            self._flash_or_cancel()

    def _job_logs(self, batch: list[object]) -> None:
        lines: list[tuple[str, str]] = []
        for event in batch:
            if isinstance(event, LogEvent) and event.level != LogLevel.STAGE:
                lines.append((LOG_LEVELS.get(event.level, "info"), event.text))
        if lines:
            self.log_panel.log_lines(lines)

    def _job_event(self, event: Event) -> None:
        if isinstance(event, CheckEvent):
            level = CHECK_LEVELS[event.level]
            self.note(level, f"[check] {event.level.value.upper()} {event.name}: {event.message}")
        elif isinstance(event, InfoEvent):
            self.note(
                "ok",
                f"Chip {event.chip} {event.revision} · MAC {event.mac or '-'} · flash "
                f"{event.flash_size or '-'}",
            )
            self._refresh_tree()
            self.connection.show_identity(self._memory.snapshot)
        elif isinstance(event, EntryEvent):
            if event.status == EntryStatus.SKIPPED:
                self.note("note", f"0x{event.address:X}: already in flash, skipped")
                self.connection.progress_text.setText(
                    f"0x{event.address:X} already in flash, skipped"
                )
        elif isinstance(event, ResultEvent):
            self.note(
                "ok" if event.status.value == "ok" else "warning",
                f"[result] {event.status.value}: {event.message}",
            )
        elif isinstance(event, ErrorEvent):
            self.note("error", f"[error] {event.message}")

    def _job_progress(self, progress: object) -> None:
        if isinstance(progress, JobProgress):
            if self._job_kind == JobKind.READ_FLASH:
                text = f"Reading from 0x{progress.address:X}"
            else:
                text = (
                    f"Writing 0x{progress.address:X} {progress.name} "
                    f"({progress.entry_index + 1}/{progress.entry_count})"
                )
            self.connection.show_progress(progress.percent, text)

    def _job_finished(self, outcome: object) -> None:
        if not isinstance(outcome, JobOutcome):
            return
        self._last_outcome = outcome
        self._job_firmware = None
        self._job_kind = None
        self.connection.set_running(False)
        if self._ports_pending:
            self._apply_ports()
        if outcome.result == RecordResult.FAILED and _port_unavailable(outcome.message):
            self._memory.select_port(None)
            self.note("warning", "The port could not be opened (in use or disconnected).")
        if outcome.kind in (JobKind.ERASE, JobKind.READ_FLASH):
            done = outcome.result == RecordResult.OK
            self.connection.show_progress(
                100 if done else self.connection.progress.value(), RESULT_TEXT[outcome.result]
            )
            self.note(
                "ok" if done else "warning",
                f"{RESULT_TEXT[outcome.result]}. Record saved. Port released.",
            )
            if done and outcome.firmware_id is not None:
                self.note("note", "[read] saved as a new firmware; it may contain secrets (NVS)")
                self.select_firmware(outcome.firmware_id)
                return
        elif outcome.kind == JobKind.IDENTIFY:
            done = outcome.result == RecordResult.OK
            self.connection.show_progress(0, "Identified" if done else RESULT_TEXT[outcome.result])
            self.note("dim", "Port released.")
        else:
            percent = 100 if outcome.result == RecordResult.OK else self.connection.progress.value()
            self.connection.show_progress(percent, RESULT_TEXT[outcome.result])
            self.note(
                "ok" if outcome.result == RecordResult.OK else "warning",
                f"{RESULT_TEXT[outcome.result]}. Record saved: "
                f"result={outcome.result.value}. Port released.",
            )
            if outcome.result == RecordResult.CANCELLED:
                self.note("warning", "Flash content may be incomplete.")
        self.changed()

    # ---- tracking: selection, activation, periodic re-check ---------------------------

    def _show_status(self, status: TrackStatus) -> None:
        self.header.show_firmware(status, self._library.flag(status.firmware_id))
        self.firmware_view.show_firmware(status)
        self._shown = (status.firmware_id, status.state, status.digest)
        if status.state == ResolveState.BUILDING:
            QTimer.singleShot(SETTLE_RECHECK_MS, self._recheck_selected)

    def _leave_selection(self) -> None:
        firmware_id = self._selected_firmware_id()
        if firmware_id is not None:
            self._library.tracker.mark_seen(firmware_id)

    def _recheck_selected(self) -> None:
        """Window activated, a build settling, a flash refused: check the selection and
        refresh when it differs from what is shown."""
        firmware_id = self._selected_firmware_id()
        if firmware_id is None or self.is_locked(firmware_id):
            return
        try:
            status = self._library.tracker.check(firmware_id)
        except KeyError:
            return
        if self._shown != (firmware_id, status.state, status.digest):
            self.changed()

    def _start_round(self) -> None:
        if self._ops.busy or self.isMinimized() or self._round:
            return
        existing = [f.id for f in self._context.firmware.all()]
        self._round = self._schedule.round(self._selected_firmware_id(), existing)
        self._round_changed = False
        QTimer.singleShot(0, self._next_in_round)

    def _next_in_round(self) -> None:
        """One firmware per event loop pass, so a round never blocks the window."""
        if not self._round or self._ops.busy:
            self._round = []
            return
        firmware_id = self._round.pop(0)
        tracker = self._library.tracker
        before = tracker.cached(firmware_id)
        try:
            status = tracker.check(firmware_id)
        except KeyError:
            self._schedule.forget(firmware_id)
        else:
            if before is None or (before.state, before.digest) != (status.state, status.digest):
                self._round_changed = True
        if self._round:
            QTimer.singleShot(0, self._next_in_round)
        elif self._round_changed:
            selected = self._selected_firmware_id()
            if selected is not None and not self.is_locked(selected):
                self.changed()
            else:
                self._refresh_tree()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            QTimer.singleShot(0, self._recheck_selected)

    # ---- stage two actions --------------------------------------------------------------

    def _import_idf(self) -> None:
        project_id, device = self._current_placement()
        request = dialogs.import_idf(self, self._library, project_id, device, self.select_firmware)
        if request is None:
            return
        try:
            firmware = self._library.import_idf(
                request.build_dir,
                name=request.name,
                project_id=request.placement.project_id,
                device=request.placement.device,
            )
        except AlreadyTrackedError as exc:  # imported meanwhile by another way
            self.note("error", f"[import] {exc}")
            self.select_firmware(exc.firmware_id)
            return
        except (PlanError, IdfBuildError, OSError) as exc:
            self.note("error", f"[import] {exc}")
            return
        self.note("ok", f"[import] '{firmware.name}' follows {request.build_dir}")
        self.select_firmware(firmware.id)

    def _archive_version(self, firmware_id: str) -> None:
        try:
            entries = self._archiver.preview(firmware_id)
        except PlanError as exc:
            self.note("error", f"[archive] {exc}")
            return
        firmware = self._library.firmware(firmware_id)
        answer = dialogs.archive_version(
            self, firmware.name, default_name_archived(firmware.name, date.today()), entries
        )
        if answer is None:
            return
        try:
            archived = self._archiver.archive_version(firmware_id, name=answer[0], notes=answer[1])
        except (PlanError, OSError) as exc:
            self.note("error", f"[archive] {exc}")
            return
        self.note("ok", f"[archive] '{archived.name}' created; '{firmware.name}' is unchanged")
        self.select_firmware(archived.id)

    def _archive_entry(self, entry_id: str) -> None:
        firmware_id = self._selected_firmware_id()
        if firmware_id is None:
            return
        try:
            entry = self._archiver.archive_entry(firmware_id, entry_id)
        except (PlanError, ValueError, OSError) as exc:
            self.note("error", f"[archive] {exc}")
            return
        self.note("ok", f"[archive] {entry.name} copied into the archive")
        self.changed()

    def _change_build_dir(self, firmware_id: str) -> None:
        firmware = self._library.firmware(firmware_id)
        start = firmware.build_ref.path if firmware.build_ref else ""
        chosen = QFileDialog.getExistingDirectory(self, "Build directory", start)
        if not chosen:
            return
        status = self._library.tracker.set_build_dir(firmware_id, Path(chosen))
        self.note("dim", f"[build] {firmware.name} now follows {chosen} ({status.state.value})")
        self.changed()

    def _clean_archive(self) -> None:
        names = {f.id: f.name for f in self._context.firmware.all()}
        doomed = dialogs.clean_archive(self, self._archiver.usage(), names, self.select_firmware)
        if doomed:
            self._archiver.clean(doomed)
            self.note("warning", f"[archive] {len(doomed)} unreferenced files deleted")

    def _merge(self) -> None:
        firmware_id = self._selected_firmware_id()
        if firmware_id is None:
            return
        status = self._library.tracker.check(firmware_id)
        firmware = status.firmware
        paths = [Path(e.file[5:]) for e in firmware.entries if e.file.startswith("path:")]
        folder = paths[0].parent if paths else self._context.paths.root
        request = dialogs.merge(
            self, self._merger, firmware, folder / f"{firmware.name}_merged.bin"
        )
        if request is None:
            return
        try:
            created = self._merger.merge(firmware_id, *request)
        except (PlanError, MergeError, OSError) as exc:
            self.note("error", f"[merge] {exc}")
            return
        self.note("ok", f"[merge] written to {request[0]}")
        if created is not None:
            self.select_firmware(created.id)

    def _board_job(self, kind: JobKind) -> None:
        port = self.connection.selected_port()
        snapshot = self._memory.snapshot
        if port is None or snapshot is None or self._ops.busy:
            return
        request: dialogs.ReadRequest | None = None
        if kind == JobKind.ERASE:
            if not dialogs.erase_flash(self, port.device, snapshot.info):
                return
        else:
            project_id, device = self._current_placement()
            request = dialogs.read_flash(self, self._library, snapshot.info, project_id, device)
            if request is None:
                return
        self._last_outcome = None
        self.log_panel.clear_log()
        self.log_panel.log_lines([("dim", f"# job {kind.value} · {port_text(port)} · {_now()}")])
        try:
            if request is None:
                self._ops.erase(port, self._signals)
            else:
                self._ops.read_flash(
                    port,
                    self.connection.selected_baud(),
                    self._signals,
                    address=request.address,
                    size=request.size,
                    project_id=request.placement.project_id,
                    device=request.placement.device,
                )
        except (BusyError, OSError, ValueError) as exc:
            self.note("error", f"[{kind.value}] {exc}")
            return
        self._job_firmware = None
        self._job_kind = kind
        self.connection.set_running(True)
        self.connection.show_progress(0, "Connecting…")

    def _erase(self) -> None:
        self._board_job(JobKind.ERASE)

    def _read(self) -> None:
        self._board_job(JobKind.READ_FLASH)

    def _open_board_log(self) -> None:
        snapshot = self._memory.snapshot
        if snapshot is None or not snapshot.info.mac:
            return
        path = self._context.logs.board_log_path(snapshot.info.mac)
        if not path.is_file():
            self.note("note", f"[board] no flash, erase or read on {snapshot.info.mac} yet")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ---- window -----------------------------------------------------------------------

    def _window_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("Settings…", lambda: dialogs.settings(self, self._context.paths))
        menu.addAction("Clean archive…", self._clean_archive)
        menu.addSeparator()
        menu.addAction("About Embr", lambda: dialogs.about(self, esptool.__version__))
        button = self.topbar.menu_button
        menu.exec(button.mapToGlobal(QPoint(0, button.height() + 4)))

    def _open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._context.paths.logs)))

    def _expand_log(self, on: bool) -> None:
        """The log takes the whole centre column (the workspace above it is hidden)."""
        self.workspace.setVisible(not on)

    def _scheme_changed(self, dark: bool) -> None:
        self._palette = palette(dark)
        self._apply_palette()
        self.changed()

    def _apply_palette(self) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, self._palette)
        self.topbar.apply_palette(self._palette)
        self.sidebar_panel.apply_palette(self._palette)
        self.connection.apply_palette(self._palette)
        self.log_panel.apply_palette(self._palette)

    def _restore_layout(self) -> None:
        settings = self._context.settings
        geometry = ui_settings.load_bytes(settings, "window")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1280, 800)
        columns = ui_settings.load_ints(settings, "columns")
        if columns is not None and len(columns) == 3:
            self.columns.setSizes(columns)
        log = ui_settings.load_ints(settings, "log")
        if log is not None and len(log) == 2:
            self.center_split.setSizes(log)
        if not ui_settings.load_flag(settings, "sidebar", True):
            self.toggle_sidebar()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._ops.busy:
            self.note("warning", "A job is running; cancel it (Esc) before closing Embr.")
            event.ignore()
            return
        settings = self._context.settings
        ui_settings.save_bytes(settings, "window", self.saveGeometry())
        ui_settings.save_ints(settings, "columns", self.columns.sizes())
        if self.workspace.isVisible():
            ui_settings.save_ints(settings, "log", self.center_split.sizes())
        ui_settings.save_flag(settings, "sidebar", self.sidebar_visible or self._auto_hidden)
        self.ports.blockSignals(True)
        event.accept()


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


SETTLE_RECHECK_MS = 2500
"""Look again after a build was seen settling (app.tracking.SETTLE_SECONDS plus margin)."""


def _port_unavailable(message: str) -> bool:
    """The job could not open its port: in use by another program, or gone."""
    text = message.lower()
    return any(k in text for k in ("could not open", "port is busy", "access is denied"))
