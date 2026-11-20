"""The main window: wires the panels to the app layer."""

import contextlib
import html
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import esptool
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from embr import __version__
from embr.app.chips import board_revision
from embr.app.context import AppContext
from embr.app.identify import IdentifyMemory
from embr.app.library import Library
from embr.app.locations import Location
from embr.app.operations import (
    Operations,
)
from embr.app.pio import PioConfigs
from embr.app.tracking import Tracker, TrackStatus
from embr.core.firmware.tracking import ChangeKind, ResolveState
from embr.core.logs import HEADER_PREFIX
from embr.core.storage.documents import DamagedDocument
from embr.i18n import tr
from embr.ui import dialogs
from embr.ui.adapters import explorer
from embr.ui.adapters import settings as ui_settings
from embr.ui.adapters.jobs import MainThreadDispatcher
from embr.ui.adapters.ports import PortMonitor
from embr.ui.connection import ConnectionPanel
from embr.ui.frame import FrameWindow
from embr.ui.icons import app_icon
from embr.ui.log_panel import LOG_HEIGHT, LogPanel
from embr.ui.project_view import ProjectView
from embr.ui.sidebar import PLUS, Sidebar
from embr.ui.theme import Palette
from embr.ui.widgets import label
from embr.ui.window.actions import LibraryActions
from embr.ui.window.appearance import Appearance
from embr.ui.window.jobs import JobController
from embr.ui.window.recheck import SETTLE_RECHECK_MS, RecheckController
from embr.ui.window.right_panel import ConnectionPresenter
from embr.ui.workspace import FirmwareView, Header


class EmbrWindow(FrameWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__(f"{__version__} · esptool {esptool.__version__}")
        self.setWindowTitle("Embr")
        self.setWindowIcon(app_icon())
        self._context = context
        self._dispatcher = MainThreadDispatcher(self)
        pio = PioConfigs(on_ready=self._pio_queried)
        self._library = Library(context, Tracker(context, pio=pio))
        tracker = self._library.tracker
        self.memory = IdentifyMemory()
        self.ops = Operations(context, self.memory, self._dispatcher.dispatch, tracker=tracker)
        self.recheck = RecheckController(self)
        self._shown: tuple[str, ResolveState, str] | None = None
        """Firmware id, state and digest the workspace shows (flash compares the digest)."""
        self._built_for: tuple[str, str] | None = None
        """The selection the workspace page was last built for."""
        self._workspace_stale = False
        """A refresh waited for a text field being edited."""
        self._held = 0
        """Depth of refreshes_held() blocks."""
        self._held_parts: set[str] = set()
        """Parts asked to refresh inside them: "workspace", "tree", "panel"."""
        self._closed = False
        """Set by closeEvent: queued refresh rounds stop."""
        self._error_shown = False
        """An unexpected error was shown in a dialog this run (later ones: log panel only)."""
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.focusChanged.connect(self._focus_moved)
        self._reported: set[tuple[Path, str]] = set()
        """Damaged files already shown this session."""
        self._renames_logged: set[tuple[str, datetime]] = set()
        """(firmware id, checked_at) of resolutions whose app rename was logged."""
        self._selection: tuple[str, str] | None = None
        self._query = ""
        self.jobs = JobController(self)
        self.right_panel = ConnectionPresenter(self)
        self.library_actions = LibraryActions(self)
        self.restart_requested = False
        """Set by the language dialog; main.run_gui() starts Embr again after closing."""
        self.appearance = Appearance(self)

        self.sidebar_panel = Sidebar()
        self.header = Header(self)
        self.firmware_view = FirmwareView(self)
        self.project_view = ProjectView(self)
        self.empty_view = QWidget()
        empty = QVBoxLayout(self.empty_view)
        empty.setContentsMargins(14, 14, 14, 14)
        empty.addWidget(
            label(
                tr(f"Create a firmware or a project with {PLUS} New in the sidebar."),
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
        self.sidebar_panel.new_firmware.connect(self.library_actions.new_firmware)
        self.sidebar_panel.new_project.connect(self.library_actions.new_project)
        self.sidebar_panel.import_idf.connect(self.library_actions.import_idf)
        self.sidebar_panel.import_pio.connect(self.library_actions.import_pio)
        self.sidebar_panel.import_package.connect(self.library_actions.import_package)
        self.sidebar_panel.files_dropped.connect(self.library_actions.files_dropped)
        self.sidebar_panel.tree.menu_requested.connect(self._firmware_menu)
        self.firmware_view.entries.archive_requested.connect(self.library_actions.archive_entry)
        self.firmware_view.entries.merge_requested.connect(self.library_actions.merge)
        self.connection.erase_clicked.connect(self.jobs.erase)
        self.connection.read_clicked.connect(self.jobs.read)
        self.connection.board_log_clicked.connect(self.jobs.open_board_log)
        self.sidebar_panel.tree.move_requested.connect(self.library_actions.move)
        self.sidebar_panel.projects_reordered.connect(self.library_actions.reorder_projects)
        self.sidebar_panel.groups_reordered.connect(self.library_actions.reorder_groups)
        self.connection.port_changed.connect(self.right_panel.port_changed)
        self.connection.baud_changed.connect(self.right_panel.refresh)
        self.connection.identify_clicked.connect(self.jobs.identify)
        self.connection.flash_clicked.connect(self.jobs.flash_or_cancel)
        self.connection.erase_all_toggled.connect(self.right_panel.set_erase_all)
        self.connection.set_quick(self.right_panel.quick)
        self.connection.quick_toggled.connect(self.right_panel.set_quick)
        self.connection.hint_dismissed.connect(self.right_panel.dismiss_hint)
        self.log_panel.expand_toggled.connect(self.appearance.expand_log)
        self.log_panel.log_folder_clicked.connect(self._open_log_folder)
        self.ports.changed.connect(self.right_panel.ports_changed)
        self.topbar.menu_button.clicked.connect(self.appearance.window_menu)
        for keys, slot in (
            ("Ctrl+B", self.toggle_sidebar),
            ("Ctrl+Return", self.jobs.flash_shortcut),
            ("Ctrl+Enter", self.jobs.flash_shortcut),
            ("Esc", self.jobs.escape),
            ("F2", lambda: self.firmware_action("rename")),
        ):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(slot)

        self.appearance.apply_palette()
        self.appearance.restore_layout()
        self.log_panel.log_lines(
            [("dim", "[idle] Pick a firmware and a port, then Connect & identify or Flash.")]
        )
        first = self.first_firmware()
        if first is not None:
            self._selection = ("firmware", first)
        with self.refreshes_held():  # the first port list refreshes too
            self.ports.start()
            if not self.ports.ports:
                self.connection.set_ports(())
            self.recheck.start()
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
        return self.appearance.palette

    @property
    def shown(self) -> tuple[str, ResolveState, str] | None:
        """Firmware id, state and digest the workspace shows (flash compares the digest)."""
        return self._shown

    @property
    def closed(self) -> bool:
        """closeEvent ran: nothing of this window runs any more."""
        return self._closed

    @property
    def selection(self) -> tuple[str, str] | None:
        """("firmware" or "project", id) shown in the middle; None when nothing is."""
        return self._selection

    def clear_selection(self) -> None:
        """Nothing is selected any more (the last firmware was deleted)."""
        self._leave_selection()
        self._selection = None
        self.jobs.last_outcome = None
        self.changed()

    def firmware_action(self, name: str) -> None:
        """An action of the firmware menu on the selected firmware (LibraryActions)."""
        self.library_actions.firmware_action(name)

    def export_project(self, project_id: str) -> None:
        self.library_actions.export_project(project_id)

    def changed(self) -> None:
        self.refresh_workspace()
        self.refresh_tree()
        self.right_panel.refresh()

    @contextlib.contextmanager
    def refreshes_held(self) -> Generator[None]:
        """Refreshes asked for inside are done once when the outermost block ends, in the
        order of changed(): one event handled in several steps (startup, the end of a job)
        builds each part once. Nothing inside may rely on a refresh having happened."""
        self._held += 1
        try:
            yield
        finally:
            self._held -= 1
            if not self._held:
                parts, self._held_parts = self._held_parts, set()
                if "workspace" in parts:
                    self.refresh_workspace()
                if "tree" in parts:
                    self.refresh_tree()
                if "panel" in parts:
                    self.right_panel.refresh()

    def refresh_held(self, part: str) -> bool:
        """Inside refreshes_held(): note `part` for the end of the block and return True."""
        if self._held:
            self._held_parts.add(part)
        return bool(self._held)

    def note(self, level: str, text: str) -> None:
        self.log_panel.log_lines([(level, text)])

    def select_firmware(self, firmware_id: str) -> None:
        """Show `firmware_id`. Leaving another firmware ends its "Build updated" state and
        "content changed" marks and the hint of the last job; selecting the
        shown one again (a click on its sidebar row) keeps them."""
        if self.selected_firmware_id() != firmware_id:
            self._leave_selection()
            self.jobs.last_outcome = None
        self.recheck.touch(firmware_id)
        self._selection = ("firmware", firmware_id)
        self.changed()

    def _firmware_menu(self, firmware_id: str, at: QPoint) -> None:
        """Right-click on a firmware in the sidebar: select it, then show the same menu as the
        workspace's ⋯ button (its actions work on the selected firmware)."""
        if self.selected_firmware_id() != firmware_id:
            self.select_firmware(firmware_id)
        self.header.menu.popup(at)

    def select_project(self, project_id: str) -> None:
        """Show the project page. Selecting the shown project again (a click on its sidebar
        row) keeps the hint of the last job, as select_firmware() does."""
        if self._selection != ("project", project_id):
            self._leave_selection()
            self.jobs.last_outcome = None
        self._selection = ("project", project_id)
        self.changed()

    def identified_chip(self) -> str | None:
        port = self.connection.selected_port()
        snapshot = self.memory.snapshot
        if port is None or snapshot is None or snapshot.port.device != port.device:
            return None
        return snapshot.info.chip

    def identified_revision(self) -> int | None:
        snapshot = self.memory.snapshot
        if self.identified_chip() is None or snapshot is None:
            return None
        return board_revision(snapshot.info.major, snapshot.info.minor)

    def is_locked(self, firmware_id: str) -> bool:
        return self.ops.running_firmware_id == firmware_id

    def busy(self) -> bool:
        return self.ops.busy

    def report_internal_error(self, summary: str, crash_log: Path) -> None:
        """An exception no code handled (core.logs.crash): one line in the log panel (the
        traceback is in the crash log), and the first one of a run also in a dialog."""
        if self._closed:
            return
        self.note(
            "error",
            f"[internal] unexpected error: {summary}; details in logs/{crash_log.name}",
        )
        if not self._error_shown:
            self._error_shown = True
            # not from inside the exception hook: the dialog runs its own event loop
            QTimer.singleShot(0, lambda: self._show_internal_error(crash_log.name))

    def _show_internal_error(self, crash_log: str) -> None:
        if not self._closed and dialogs.internal_error(self, crash_log):
            self._open_log_folder()

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

    def show_in_explorer(self, location: Location | None) -> None:
        if location is None:
            self.note("warning", "[explorer] the folder no longer exists")
            return
        try:
            explorer.reveal(location)
        except OSError as exc:
            self.note("error", f"[explorer] {exc}")

    # ---- refresh ----------------------------------------------------------------------

    def selected_firmware_id(self) -> str | None:
        return self._selection[1] if self._selection and self._selection[0] == "firmware" else None

    def first_firmware(self) -> str | None:
        tree = self._library.tree()
        for project in tree.projects:
            for group in project.groups:
                if group.firmware:
                    return group.firmware[0].id
        return tree.standalone[0].id if tree.standalone else None

    def refresh_tree(self) -> None:
        if self.refresh_held("tree"):
            return
        snapshot = self.memory.snapshot
        self.sidebar_panel.show_tree(
            self._library.tree(self._query),
            self._selection,
            self._library.mismatches(snapshot.info) if snapshot else {},
            self.ops.running_firmware_id,
        )
        fresh = [d for d in self._library.damaged() if (d.path, d.reason) not in self._reported]
        if fresh:
            self._reported.update((d.path, d.reason) for d in fresh)
            QTimer.singleShot(0, lambda: self._show_damaged(fresh))

    def _show_damaged(self, damaged: list[DamagedDocument]) -> None:
        """Project or firmware files that cannot be read: left out, a
        copy kept in data/backup/, the originals unchanged. Waits for an open dialog."""
        if QApplication.activeModalWidget() is not None:
            QTimer.singleShot(500, lambda: self._show_damaged(damaged))
            return
        folder = self._library.keep_damaged(damaged)
        lines = "".join(
            f"<br>•&nbsp;<b>{html.escape(d.path.name)}</b> ({html.escape(str(d.path.parent))})"
            f" — {html.escape(tr(d.reason))}"
            for d in damaged
        )
        text = [
            tr(
                "These project or firmware files cannot be read and are left out; everything "
                f"else works as usual. The files themselves are not changed:{lines}"
            )
        ]
        if folder is not None:
            text.append(tr(f"A copy of each is kept in {html.escape(str(folder))}."))
        for d in damaged:
            self.note("warning", f"[data] {d.path} cannot be read: {d.reason}")
        dialogs.notice(self, tr("Files that cannot be read"), text)

    def _editing_field(self) -> QWidget | None:
        """The text field of the firmware or project page being edited (it has the focus).
        Such a field saves what was typed only when it loses the focus."""
        return self._page_field(QApplication.focusWidget())

    def _page_field(self, widget: QWidget | None) -> QWidget | None:
        """`widget` when it is an editable text field of the firmware or project page."""
        pages = (self.firmware_view, self.project_view)
        if (
            isinstance(widget, (QLineEdit, QPlainTextEdit))
            and not widget.isReadOnly()
            and any(page.isAncestorOf(widget) for page in pages)
        ):
            return widget
        return None

    def _focus_moved(self, old: QWidget | None, new: QWidget | None) -> None:
        field = self._page_field(new)
        if field is not None and _handed_on(old):
            # Qt passed on the focus of a button disabled or hidden (Flash at the end of a
            # job): nobody is typing there, so the page must not wait for that field
            field.clearFocus()  # moves the focus again; that call refreshes a waiting page
            return
        if self._workspace_stale:
            QTimer.singleShot(0, self._refresh_if_stale)  # after the field's own save

    def _refresh_if_stale(self) -> None:
        """Rebuild a page that waited for its field, once no field is edited and no button
        is held (a rebuild between press and release would take the click away), and the
        right panel with it, which checks the page as it is now."""
        if (
            self._workspace_stale
            and not self._closed
            and self._editing_field() is None
            and QApplication.mouseButtons() == Qt.MouseButton.NoButton
        ):
            self.refresh_workspace()
            self.right_panel.refresh()

    def refresh_workspace(self) -> None:
        """Build the page of the selection. While a text field of it is being edited, a
        refresh of the same page waits until the field is left (job ended, port changed, a
        check), so nothing typed is lost; showing another page first lets the field save what
        was typed, as leaving it does. The header above the page has no field: it is shown
        as it is now also while the page waits (a name saved with Enter, a firmware dragged
        to another group)."""
        if self.refresh_held("workspace"):
            return
        field = self._editing_field()
        if field is not None:
            if self._selection == self._built_for:
                self._workspace_stale = True
                self._refresh_header()
                return
            field.clearFocus()  # its save may refresh (and build the new page) already
        self._workspace_stale = False
        self._built_for = self._selection
        selection = self._selection
        try:
            if selection is not None and selection[0] == "firmware":
                # the tracker's cached status outlives a deleted or damaged firmware file
                self._library.firmware(selection[1])
                self._fit_entries(selection[1])
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
            self._selection = self._built_for = None
        self._shown = None
        self.header.hide()
        self.pages.setCurrentWidget(self.empty_view)
        return

    def _refresh_header(self) -> None:
        """The header of the shown page alone; the page keeps what it shows (and _shown, the
        state Flash is checked against). A selection deleted meanwhile is left to the
        refresh after the field."""
        selection = self._selection
        try:
            if selection is not None and selection[0] == "firmware":
                self._library.firmware(selection[1])  # the cached status outlives a deletion
                status = self._library.tracker.check(selection[1])
                self.header.show_firmware(status, self._library.flag(status.firmware_id))
            elif selection is not None and selection[0] == "project":
                self.header.show_project(self, selection[1])
        except KeyError:
            return

    def _fit_entries(self, firmware_id: str) -> None:
        """Untick the entries that are not for the identified board; the entries table then shows
        them greyed out."""
        chip = self.identified_chip()
        if chip is None or self.is_locked(firmware_id):
            return
        unticked = self._library.fit_entries(firmware_id, chip, self.identified_revision())
        if unticked:
            names = ", ".join(e.name for e in unticked)
            self.note("note", f"[entries] not for this {chip}, no longer enabled: {names}")

    # ---- search and placement --------------------------------------------------------

    def _search(self, text: str) -> None:
        self._query = text
        self.refresh_tree()

    def current_placement(self) -> tuple[str | None, str]:
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

    # ---- tracking: selection, activation, periodic re-check ---------------------------

    def _show_status(self, status: TrackStatus) -> None:
        self.header.show_firmware(status, self._library.flag(status.firmware_id))
        self.firmware_view.show_firmware(status)
        self._shown = (status.firmware_id, status.state, status.digest)
        self._log_renames(status)
        if status.state == ResolveState.BUILDING:
            self.recheck.request(SETTLE_RECHECK_MS, settle=True)

    def _log_renames(self, status: TrackStatus) -> None:
        """One log line per renamed app file, once per resolution (the
        firmware keeps its name, the entries show the new file name)."""
        renames = [c for c in status.changes if c.kind == ChangeKind.ENTRY_RENAMED]
        seen = (status.firmware_id, status.checked_at)
        if not renames or seen in self._renames_logged:
            return
        self._renames_logged.add(seen)
        for change in renames:
            self.note(
                "note",
                f"[track] {status.firmware.name}: app file renamed "
                f"{change.before} -> {change.after}",
            )

    def _leave_selection(self) -> None:
        firmware_id = self.selected_firmware_id()
        if firmware_id is not None:
            self._library.tracker.mark_seen(firmware_id)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.recheck.request()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """FrameWindow's filter on the application, plus: a mouse button released anywhere
        in this window queues a check of the selection (on release, so a refresh does not
        take the click away). So do files dragged into it: a drop from Explorer neither
        activates the window nor releases a button in it, and files overwritten just before
        should show as changed while they are dragged (internal drags carry no URLs and
        never refresh the sidebar under them)."""
        if super().eventFilter(watched, event):
            return True
        if event.type() not in (QEvent.Type.MouseButtonRelease, QEvent.Type.DragEnter):
            return False  # the type first: every event of the application passes here
        if not isinstance(watched, QWidget) or watched.window() is not self:
            return False
        if event.type() == QEvent.Type.MouseButtonRelease or (
            event.type() == QEvent.Type.DragEnter
            and isinstance(event, QDragEnterEvent)
            and event.mimeData().hasUrls()
        ):
            self.recheck.request()
        if event.type() == QEvent.Type.MouseButtonRelease and self._workspace_stale:
            QTimer.singleShot(0, self._refresh_if_stale)  # after the click is handled
        return False

    # ---- PlatformIO configuration ----------------------------------------------------

    def _pio_queried(self, project_root: Path) -> None:
        """PioConfigs on_ready, called from its worker thread; the query may end after the
        window is gone (then the dispatcher is deleted and there is nothing to refresh)."""
        with contextlib.suppress(RuntimeError):
            self._dispatcher.dispatch(lambda: self._pio_ready(project_root))

    def _pio_ready(self, project_root: Path) -> None:
        """A `pio project config` result arrived: re-check the PIO firmware of that project
        (the board may have changed) and refresh what is shown."""
        changed = self._library.tracker.invalidate_project(project_root)
        if self.selected_firmware_id() in changed:
            self.recheck.check_selected()

    # ---- window -----------------------------------------------------------------------

    def _open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._context.paths.logs)))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.ops.busy:
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
        # nothing of this window runs after it closed (its data may be closed next)
        self._closed = True
        self.recheck.stop()
        event.accept()


def _handed_on(old: QWidget | None) -> bool:
    """The focus left `old` because it was disabled, hidden or deleted: Qt moved it to the
    next widget, the user did not (a click or Tab leaves an enabled, visible widget)."""
    if old is None:  # the window became active again: back to where the user was
        return False
    try:
        return not old.isEnabled() or not old.isVisible()
    except RuntimeError:  # its C++ object is already deleted
        return True
