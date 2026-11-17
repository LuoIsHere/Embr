"""The main window: wires the panels to the app layer."""

import contextlib
import html
from collections.abc import Generator
from datetime import date, datetime
from pathlib import Path

import esptool
from PySide6.QtCore import QEvent, QLocale, QObject, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QActionGroup,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from embr import __version__
from embr.app.archiving import Archiver
from embr.app.chips import board_revision
from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.identify import IdentifyMemory
from embr.app.library import AlreadyTrackedError, Library, TrackingEntriesError
from embr.app.locations import Location, firmware_location
from embr.app.merging import Merger
from embr.app.operations import (
    Operations,
)
from embr.app.packages import Packager, Placement
from embr.app.pio import PioConfigs
from embr.app.tracking import Tracker, TrackStatus
from embr.core.firmware.entries import FolderError
from embr.core.firmware.files import local_file
from embr.core.firmware.import_idf import IdfBuildError, build_dir_below
from embr.core.firmware.import_pio import (
    PLATFORMIO_INI,
    EnvDirError,
    EnvRef,
    PioBuildError,
    built_envs_below,
    env_ref,
)
from embr.core.firmware.store import ReadOnlyError, default_name_archived
from embr.core.firmware.tracking import ChangeKind, ResolveState
from embr.core.flasher.merge import MergeError
from embr.core.logs import HEADER_PREFIX
from embr.core.package import PACKAGE_SUFFIX, PackageError
from embr.core.storage.documents import DamagedDocument
from embr.i18n import N_, SETTINGS_KEY, Language, current, effective, tr
from embr.ui import dialogs
from embr.ui.adapters import explorer
from embr.ui.adapters import settings as ui_settings
from embr.ui.adapters.jobs import MainThreadDispatcher
from embr.ui.adapters.ports import PortMonitor
from embr.ui.adapters.scheme import SchemeWatcher
from embr.ui.adapters.settings import stored_language
from embr.ui.connection import ConnectionPanel
from embr.ui.frame import FrameWindow
from embr.ui.icons import app_icon
from embr.ui.log_panel import LOG_HEIGHT, LogPanel
from embr.ui.project_view import ProjectView
from embr.ui.sidebar import PLUS, Node, Sidebar
from embr.ui.theme import Palette, load_fonts, palette, style_sheet, ui_font
from embr.ui.widgets import label
from embr.ui.window.jobs import JobController
from embr.ui.window.recheck import SETTLE_RECHECK_MS, RecheckController
from embr.ui.window.right_panel import ConnectionPresenter
from embr.ui.workspace import FirmwareView, Header, kept_notes

CHANGES_FIRMWARE = ("rename", "move", "delete", "directory", "follow_env")
"""Firmware menu actions that would change a firmware's own file."""
PACKAGE_FOLDER = "package_folder"
"""settings.ini [ui]: the folder of the last .embr package exported or imported."""
RENAME_IN_PROJECT = N_(
    "Unique within the project; a duplicate gets a numeric suffix. Flash records keep the name "
    "used at the time."
)
RENAME_STANDALONE = N_(
    "Unique among standalone firmware; a duplicate gets a numeric suffix. Flash records keep "
    "the name used at the time."
)


def apply_theme(app: QApplication, p: Palette) -> None:
    app.setStyle("Fusion")
    load_fonts()
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
        self._dispatcher = MainThreadDispatcher(self)
        pio = PioConfigs(on_ready=self._pio_queried)
        self._library = Library(context, Tracker(context, pio=pio))
        tracker = self._library.tracker
        self.memory = IdentifyMemory()
        self.ops = Operations(context, self.memory, self._dispatcher.dispatch, tracker=tracker)
        self._archiver = Archiver(context, tracker)
        self._merger = Merger(context, tracker)
        self._packager = Packager(context, tracker)
        self.recheck = RecheckController(self)
        self._shown: tuple[str, ResolveState, str] | None = None
        """Firmware id, state and digest the workspace shows (flash compares the digest)."""
        self._built_for: tuple[str, str] | None = None
        """The selection the workspace page was last built for."""
        self._workspace_stale = False
        """A refresh waited for a text field being edited."""
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
        self._scheme = SchemeWatcher(self)
        self._theme = ui_settings.load_choice(context.settings, "theme", THEMES, "system")
        self._language = stored_language(context.settings)
        self._language_dialog: dialogs.Dialog | None = None
        self.restart_requested = False
        """Set by the language dialog; main.run_gui() starts Embr again after closing."""
        self._palette = palette(self._dark())

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
        self.sidebar_panel.new_firmware.connect(self._new_firmware)
        self.sidebar_panel.new_project.connect(self._new_project)
        self.sidebar_panel.import_idf.connect(self._import_idf)
        self.sidebar_panel.import_pio.connect(self._import_pio)
        self.sidebar_panel.import_package.connect(self._import_package)
        self.sidebar_panel.files_dropped.connect(self._files_dropped)
        self.sidebar_panel.tree.menu_requested.connect(self._firmware_menu)
        self.firmware_view.entries.archive_requested.connect(self._archive_entry)
        self.firmware_view.entries.merge_requested.connect(self._merge)
        self.connection.erase_clicked.connect(self.jobs.erase)
        self.connection.read_clicked.connect(self.jobs.read)
        self.connection.board_log_clicked.connect(self.jobs.open_board_log)
        self.sidebar_panel.tree.move_requested.connect(self._move)
        self.sidebar_panel.projects_reordered.connect(self._reorder_projects)
        self.sidebar_panel.groups_reordered.connect(self._reorder_groups)
        self.connection.port_changed.connect(self.right_panel.port_changed)
        self.connection.baud_changed.connect(self.right_panel.refresh)
        self.connection.identify_clicked.connect(self.jobs.identify)
        self.connection.flash_clicked.connect(self.jobs.flash_or_cancel)
        self.connection.erase_all_toggled.connect(self.right_panel.set_erase_all)
        self.connection.set_quick(self.right_panel.quick)
        self.connection.quick_toggled.connect(self.right_panel.set_quick)
        self.connection.hint_dismissed.connect(self.right_panel.dismiss_hint)
        self.log_panel.expand_toggled.connect(self._expand_log)
        self.log_panel.log_folder_clicked.connect(self._open_log_folder)
        self.ports.changed.connect(self.right_panel.ports_changed)
        self._scheme.changed.connect(self._scheme_changed)
        self.topbar.menu_button.clicked.connect(self._window_menu)
        for keys, slot in (
            ("Ctrl+B", self.toggle_sidebar),
            ("Ctrl+Return", self.jobs.flash_shortcut),
            ("Ctrl+Enter", self.jobs.flash_shortcut),
            ("Esc", self.jobs.escape),
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
        return self._palette

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

    def allow_restart(self) -> None:
        """A job ended: an open language dialog may offer Restart now."""
        if self._language_dialog is not None:
            self._language_dialog.ok.setEnabled(True)

    def changed(self) -> None:
        self.refresh_workspace()
        self.refresh_tree()
        self.right_panel.refresh()

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

    def firmware_action(self, name: str) -> None:
        firmware_id = self.selected_firmware_id()
        if firmware_id is None or self.is_locked(firmware_id):
            return
        firmware = self._library.firmware(firmware_id)
        if firmware.read_only and name in CHANGES_FIRMWARE:
            self._read_only_notice(firmware.name)
            return
        if name == "reveal":
            self.show_in_explorer(firmware_location(firmware))
            return
        if name == "rename":
            text = dialogs.ask_text(
                self,
                tr("Rename firmware"),
                tr("Name"),
                firmware.name,
                tr(RENAME_IN_PROJECT if firmware.project_id else RENAME_STANDALONE),
                tr("Rename"),
            )
            if text:
                renamed = self._library.rename_firmware(firmware_id, text)
                self.note("dim", f"[firmware] renamed to '{renamed.name}'")
        elif name == "duplicate":
            copy = self._library.duplicate_firmware(firmware_id)
            self.note("dim", f"[firmware] duplicated as '{copy.name}'")
            self.select_firmware(copy.id)  # the usual rules of leaving the selection
            return
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
        elif name == "export":
            self._export_firmware(firmware_id)
            return
        elif name == "directory":
            self._change_build_dir(firmware_id)
            return
        elif name == "follow_env":
            self._follow_env(firmware_id)
            return
        elif name == "delete":
            accepted, _ = dialogs.confirm(
                self,
                tr("Delete firmware"),
                [
                    tr(f"Delete <b>{html.escape(firmware.name)}</b>?"),
                    tr(dialogs.ARCHIVE_KEPT),
                ],
                tr("Delete"),
            )
            if not accepted:
                return
            self._library.delete_firmware(firmware_id)
            self.note("warning", f"[firmware] '{firmware.name}' deleted")
            first = self._first_firmware()
            if first is not None:
                self.select_firmware(first)
                return
            self._leave_selection()
            self._selection = None
            self.jobs.last_outcome = None
        self.changed()

    # ---- refresh ----------------------------------------------------------------------

    def selected_firmware_id(self) -> str | None:
        return self._selection[1] if self._selection and self._selection[0] == "firmware" else None

    def _first_firmware(self) -> str | None:
        tree = self._library.tree()
        for project in tree.projects:
            for group in project.groups:
                if group.firmware:
                    return group.firmware[0].id
        return tree.standalone[0].id if tree.standalone else None

    def refresh_tree(self) -> None:
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
        focused = QApplication.focusWidget()
        pages = (self.firmware_view, self.project_view)
        if (
            isinstance(focused, (QLineEdit, QPlainTextEdit))
            and not focused.isReadOnly()
            and any(page.isAncestorOf(focused) for page in pages)
        ):
            return focused
        return None

    def _focus_moved(self, _old: QWidget | None, _new: QWidget | None) -> None:
        if self._workspace_stale:
            QTimer.singleShot(0, self._refresh_if_stale)  # after the field's own save

    def _refresh_if_stale(self) -> None:
        """Rebuild a page that waited for its field, once no field is edited and no button
        is held (a rebuild between press and release would take the click away)."""
        if (
            self._workspace_stale
            and not self._closed
            and self._editing_field() is None
            and QApplication.mouseButtons() == Qt.MouseButton.NoButton
        ):
            self.refresh_workspace()

    def refresh_workspace(self) -> None:
        """Build the page of the selection. While a text field of it is being edited, a
        refresh of the same page waits until the field is left (job ended, port changed, a
        check), so nothing typed is lost; showing another page first lets the field save what
        was typed, as leaving it does."""
        field = self._editing_field()
        if field is not None:
            if self._selection == self._built_for:
                self._workspace_stale = True
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

    def _fit_entries(self, firmware_id: str) -> None:
        """Untick the entries that are not for the identified board; the entries table then shows
        them greyed out."""
        chip = self.identified_chip()
        if chip is None or self.is_locked(firmware_id):
            return
        unticked = self._library.fit_entries(firmware_id, chip, self.identified_revision())
        if unticked:
            names = ", ".join(e.name for e in unticked)
            self.note("note", f"[entries] unticked, not for this {chip}: {names}")

    # ---- sidebar actions ----------------------------------------------------------------

    def _search(self, text: str) -> None:
        self._query = text
        self.refresh_tree()

    def _move(self, firmware_id: str, project_id: object, device: str) -> None:
        if self.is_locked(firmware_id):
            return
        target = project_id if isinstance(project_id, str) else None
        before = self._library.firmware(firmware_id)
        if before.project_id == target and before.device == device:
            return
        if before.read_only:
            self._read_only_notice(before.name)
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

    def _reorder_projects(self, project_ids: list[str]) -> None:
        """Projects dragged into a new order in the sidebar (settings.ini)."""
        self._library.set_project_order(project_ids)
        self.refresh_tree()

    def _reorder_groups(self, project_id: str, names: list[str]) -> None:
        """Groups dragged into a new order within their project (its `devices`)."""
        try:
            self._library.reorder_groups(project_id, names)
        except ReadOnlyError:
            self._read_only_notice(self._library.project(project_id).name)
        except (KeyError, ValueError):  # the project changed under the drag
            pass
        self.refresh_tree()

    def _read_only_notice(self, name: str) -> None:
        """Firmware or project written by a newer Embr: shown, never changed."""
        dialogs.notice(
            self,
            tr("Read only"),
            [
                tr(
                    f"<b>{html.escape(name)}</b> was created by a newer version of Embr and is "
                    "read-only here."
                )
            ],
        )

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

    def _new_firmware(self) -> None:
        project_id, device = self.current_placement()
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

    # ---- jobs -------------------------------------------------------------------------

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

    # ---- import, export, archive, merge, erase and read flash actions -------------------

    def _import_idf(
        self, build_dir: Path | None = None, placement: tuple[str | None, str] | None = None
    ) -> None:
        project_id, device = placement or self.current_placement()
        request = dialogs.import_idf(
            self, self._library, project_id, device, self.select_firmware, build_dir
        )
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

    def _package_folder(self) -> Path:
        stored = self._context.settings.get(ui_settings.UI_SECTION, PACKAGE_FOLDER)
        folder = Path(stored) if stored else Path.home()
        return folder if folder.is_dir() else Path.home()

    def _remember_package_folder(self, path: Path) -> None:
        settings = self._context.settings
        settings.set(ui_settings.UI_SECTION, PACKAGE_FOLDER, str(path.parent))
        settings.save()

    def _export_firmware(self, firmware_id: str) -> None:
        """Export…: the firmware of the same scope, the chosen one checked."""
        firmware = self._library.firmware(firmware_id)
        if firmware.project_id is not None:
            scope = self._library.project_firmware(firmware.project_id)
        else:
            scope = [self._library.firmware(r.id) for r in self._library.tree().standalone]
        check = self._packager.check_export([f.id for f in scope])
        request = dialogs.export_firmware(self, scope, firmware_id, check, self._package_folder())
        if request is None:
            return
        ids, path = request
        try:
            with _busy():
                self._packager.export_firmware(ids, path)
        except (PlanError, OSError, ValueError) as exc:
            self.note("error", f"[export] {_text(exc)}")
            return
        self._remember_package_folder(path)
        self.note("ok", f"[export] {len(ids)} firmware written to {path}")

    def export_project(self, project_id: str) -> None:
        project = self._library.project(project_id)
        ids = [f.id for f in self._library.project_firmware(project_id)]
        check = self._packager.check_export(ids)
        path = dialogs.export_project(self, project.name, len(ids), check, self._package_folder())
        if path is None:
            return
        try:
            with _busy():
                self._packager.export_project(project_id, path)
        except (PlanError, OSError, ValueError) as exc:
            self.note("error", f"[export] {_text(exc)}")
            return
        self._remember_package_folder(path)
        self.note("ok", f"[export] '{project.name}' with {len(ids)} firmware written to {path}")

    def _import_package(
        self,
        path: Path | None = None,
        project_id: str | None = None,
        device: str | None = None,
    ) -> None:
        """Import .embr package… (New menu, or a package dropped on the sidebar, where the
        drop point gives the default project and group): the package is checked in full
        first; nothing is written unless all of it is valid."""
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(
                self,
                tr("Import .embr package"),
                str(self._package_folder()),
                tr(dialogs.PACKAGE_FILTER),
            )
            if not chosen:
                return
            path = Path(chosen)
            project_id = self.current_placement()[0]
        try:
            with _busy():
                package = self._packager.open(path)
        except (PackageError, OSError) as exc:
            dialogs.notice(
                self,
                tr("Not imported"),
                [
                    tr(f"<b>{html.escape(path.name)}</b> cannot be imported."),
                    html.escape(tr(_text(exc))),
                ],
            )
            return
        try:
            placement = dialogs.import_package(
                self, self._library, package, path.name, project_id, device
            )
            if placement is None:
                return
            with _busy():
                result = self._packager.import_package(
                    package, path.name, Placement(placement.project_id, placement.device)
                )
        except (KeyError, OSError, ValueError) as exc:
            self.note("error", f"[import] {path.name}: {_text(exc)}")
            return
        finally:
            package.discard()
        self._remember_package_folder(path)
        self.note(
            "ok",
            f"[import] {path.name}: {len(result.project_ids)} project(s), "
            f"{len(result.firmware_ids)} firmware",
        )
        if result.firmware_ids:
            self.select_firmware(result.firmware_ids[0])
        elif result.project_ids:
            self.select_project(result.project_ids[0])
        else:
            self.changed()

    def _archive_entry(self, entry_id: str) -> None:
        firmware_id = self.selected_firmware_id()
        if firmware_id is None:
            return
        try:
            entry = self._archiver.archive_entry(firmware_id, entry_id)
        except (PlanError, ValueError, OSError) as exc:
            self.note("error", f"[archive] {exc}")
            return
        self.note("ok", f"[archive] {entry.name} copied into the archive")
        self.changed()

    def _import_pio(
        self, build_dir: Path | None = None, placement: tuple[str | None, str] | None = None
    ) -> None:
        project_id, device = placement or self.current_placement()
        request = dialogs.import_pio(
            self, self._library, project_id, device, self.select_firmware, build_dir
        )
        if request is None:
            return
        try:
            created = self._library.import_pio(
                request.project_root,
                request.envs,
                project_id=request.placement.project_id,
                device=request.placement.device,
            )
        except AlreadyTrackedError as exc:  # imported meanwhile by another way
            self.note("error", f"[import] {exc}")
            self.select_firmware(exc.firmware_id)
            return
        except (PlanError, PioBuildError, OSError, ValueError) as exc:
            self.note("error", f"[import] {exc}")
            return
        for firmware in created:
            ref = firmware.build_ref
            env = ref.env if ref is not None else "?"
            self.note("ok", f"[import] '{firmware.name}' follows {request.project_root} env {env}")
        self.select_firmware(created[0].id)

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

    def _change_build_dir(self, firmware_id: str) -> None:
        firmware = self._library.firmware(firmware_id)
        start = firmware.build_ref.path if firmware.build_ref else ""
        title = (
            tr("Env build folder (.pio\\build\\<env>)")
            if firmware.build_ref and firmware.build_ref.env
            else tr("Build directory")
        )
        chosen = QFileDialog.getExistingDirectory(self, title, start)
        if not chosen:
            return
        try:
            status = self._library.tracker.set_build_dir(firmware_id, Path(chosen))
        except ValueError as exc:  # not an env build folder of a PlatformIO project
            self.note("error", f"[build] {exc}")
            return
        self.note("dim", f"[build] {firmware.name} now follows {chosen} ({status.state.value})")
        self.changed()

    def _follow_env(self, firmware_id: str) -> None:
        """The banner's "Follow env": follow the one built env platformio.ini
        still declares and no other firmware follows. The firmware keeps its name."""
        firmware = self._library.firmware(firmware_id)
        candidates = self._library.tracker.env_candidates(firmware_id)
        if len(candidates) != 1 or firmware.build_ref is None:
            self.changed()  # the project changed since the banner was shown
            return
        (env,) = candidates
        build_dir = EnvRef(Path(firmware.build_ref.path), env).build_dir
        try:
            status = self._library.tracker.set_build_dir(firmware_id, build_dir)
        except ValueError as exc:
            self.note("error", f"[build] {exc}")
            return
        self.note("dim", f"[build] {firmware.name} now follows env {env} ({status.state.value})")
        self.changed()

    def _clean_archive(self) -> None:
        names = {f.id: f.name for f in self._context.firmware.all()}
        doomed = dialogs.clean_archive(self, self._archiver.usage(), names, self.select_firmware)
        if doomed:
            cleaned = self._archiver.clean(doomed)
            self.note("warning", f"[archive] {cleaned.deleted} unreferenced files deleted")
            if cleaned.in_use:
                self.note(
                    "warning",
                    f"[archive] {cleaned.in_use} files in use were kept;"
                    " Clean archive tries them again next time",
                )

    def _merge(self) -> None:
        firmware_id = self.selected_firmware_id()
        if firmware_id is None:
            return
        status = self._library.tracker.check(firmware_id)
        firmware = status.firmware
        paths = [p for e in firmware.entries if (p := local_file(e)) is not None]
        folder = paths[0].parent if paths else self._context.paths.root
        request = dialogs.merge(
            self, self._merger, firmware, self._merger.default_output(firmware, folder)
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

    def _files_dropped(self, names: list[str], node: object) -> None:
        """Files or a folder dragged from Explorer onto the sidebar:
        on a firmware they are added; on a project, group or the standalone header a fixed
        firmware is created after its name is confirmed, or an ESP-IDF build folder opens the
        import dialog, and a PlatformIO env build folder or project folder opens the
        PlatformIO import dialog. Anything else is refused as a whole, with a dialog saying
        why."""
        if not isinstance(node, Node):
            return
        if node.firmware_id is not None and self.is_locked(node.firmware_id):
            dialogs.notice(self, tr("Nothing added"), [tr("This firmware is being flashed.")])
            return
        paths = [Path(n) for n in names]
        packages = [p for p in paths if p.suffix.lower() == PACKAGE_SUFFIX]
        if packages:
            if len(paths) > 1:
                dialogs.notice(self, tr("Nothing added"), [tr("Drop a .embr package on its own.")])
                return
            # the drop point, as for dragged firmware: a group, a project (ungrouped), the
            # standalone header, or a firmware's own project and group
            project_id, device = node.project_id, node.device
            if node.firmware_id is not None:
                dropped_on = self._library.firmware(node.firmware_id)
                project_id, device = dropped_on.project_id, dropped_on.device
            self._import_package(packages[0], project_id, device)
            return
        files, build, problems = sort_drop(paths)
        if node.firmware_id is not None:
            self._drop_on_firmware(node.firmware_id, files, build, problems)
            return
        if build is not None and not problems:
            if is_pio_folder(build):
                self._import_pio(build, (node.project_id, node.device))
            else:
                self._import_idf(build, (node.project_id, node.device))
            return
        if problems or not files:
            dialogs.notice(self, tr("Nothing added"), problems or [tr("No files were dropped.")])
            return
        name = dialogs.ask_text(
            self,
            tr("New firmware"),
            tr("Name shown in the sidebar"),
            files[0].stem,
            tr(f"{len(files)} file(s) from {files[0].parent}"),
            tr("Create"),
        )
        if name is None:
            return
        try:
            firmware = self._library.create_from_files(files, node.project_id, node.device, name)
        except (FolderError, OSError, ValueError) as exc:
            dialogs.notice(self, tr("Nothing added"), [html.escape(tr(_text(exc)), quote=False)])
            return
        self.note("dim", f"[new] fixed firmware '{firmware.name}' from {len(files)} file(s)")
        self.select_firmware(firmware.id)

    def _drop_on_firmware(
        self, firmware_id: str, files: list[Path], build: Path | None, problems: list[str]
    ) -> None:
        if build is not None:
            problems = [
                *problems,
                tr(f"{html.escape(build.name)}: drop a build folder on a project or group"),
            ]
        if problems or not files:
            dialogs.notice(self, tr("Nothing added"), problems or [tr("No files were dropped.")])
            return
        try:
            result = self._library.add_files(firmware_id, files)
        except (TrackingEntriesError, FolderError, OSError, ValueError) as exc:
            dialogs.notice(self, tr("Nothing added"), [html.escape(tr(_text(exc)), quote=False)])
            return
        name = self._library.firmware(firmware_id).name
        if result.added:
            self.note("dim", f"[entry] {len(result.added)} entries added to '{name}'")
        for line in kept_notes(result):
            self.note("dim", line)
        self.select_firmware(firmware_id)
        if not result.added and not result.updated:
            names = html.escape(", ".join(e.name for e in result.unchanged))
            dialogs.notice(
                self,
                tr("Nothing added"),
                [tr(f"Already in this firmware, content unchanged: {names}")],
            )

    # ---- window -----------------------------------------------------------------------

    def _window_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction(
            tr("Settings…"), lambda: dialogs.settings(self, self._context.paths, self._library)
        )
        menu.addAction(tr("Clean archive…"), self._clean_archive)
        themes = menu.addMenu(tr("Theme"))
        group = QActionGroup(themes)
        for mode in THEMES:
            action = themes.addAction(tr(THEME_LABELS[mode]), lambda m=mode: self._set_theme(m))
            action.setCheckable(True)
            action.setChecked(mode == self._theme)
            group.addAction(action)
        languages = menu.addMenu(tr("Language"))
        language_group = QActionGroup(languages)
        for choice, text in LANGUAGE_LABELS:
            action = languages.addAction(tr(text), lambda c=choice: self._set_language(c))
            action.setCheckable(True)
            action.setChecked(choice == self._language)
            language_group.addAction(action)
        menu.addSeparator()
        menu.addAction(tr("About Embr"), lambda: dialogs.about(self, esptool.__version__))
        button = self.topbar.menu_button
        menu.exec(button.mapToGlobal(QPoint(0, button.height() + 4)))

    def _open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._context.paths.logs)))

    def _expand_log(self, on: bool) -> None:
        """The log takes the whole centre column (the workspace above it is hidden)."""
        self.workspace.setVisible(not on)

    def _scheme_changed(self, dark: bool) -> None:
        if self._theme == "system":
            self._palette = palette(dark)
            self._apply_palette()
            self.changed()

    def _dark(self) -> bool:
        return self._scheme.dark if self._theme == "system" else self._theme == "dark"

    def _set_theme(self, mode: str) -> None:
        """System follows the Windows light / dark setting; the choice is kept in
        settings.ini."""
        self._theme = mode
        ui_settings.save_choice(self._context.settings, "theme", mode)
        self._palette = palette(self._dark())
        self._apply_palette()
        self.changed()

    def _set_language(self, choice: Language) -> None:
        """Store the choice; the interface changes after a restart. The
        dialog offers Restart, disabled while a job runs and enabled when it ends."""
        if choice == self._language:
            return
        self._language = choice
        settings = self._context.settings
        ui_settings.save_choice(settings, SETTINGS_KEY, choice.value)
        settings.save()
        if effective(choice, QLocale.system().uiLanguages()) == current():
            return
        dialog = dialogs.Dialog(self, tr("Language"), tr("Restart"), danger=False)
        dialog.cancel.setText(tr("OK"))
        dialog.text(tr("The new language is used after Embr restarts."))
        busy = dialog.text(
            tr("A job is running: the choice is saved; Restart becomes available when it ends.")
        )
        busy.setVisible(self.ops.busy)
        dialog.ok.setEnabled(not self.ops.busy)
        self._language_dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._language_dialog = None
        if accepted and not self.ops.busy:
            self.restart_requested = True
            self.close()

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


THEMES = ("system", "light", "dark")
"""Theme menu choices; "system" follows Windows."""
THEME_LABELS = {"system": N_("System"), "light": N_("Light"), "dark": N_("Dark")}
LANGUAGE_LABELS = (
    (Language.SYSTEM, N_("System")),
    (Language.ENGLISH, "English"),
    (Language.ZH_CN, "简体中文"),
)
"""Language menu; each language is named in itself."""


def sort_drop(paths: list[Path]) -> tuple[list[Path], Path | None, list[str]]:
    """(.bin files to add, a build folder, problems) of what was dropped. A build folder is
    an ESP-IDF build folder or a PlatformIO env build folder (checked first: an espidf env
    folder also has flasher_args.json); it counts only when dropped on its own. A project
    folder stands for its build folder (PlatformIO: the first built env, else the project
    folder itself, which the import dialog explains). is_pio_folder() tells the kinds
    apart."""
    files: list[Path] = []
    builds: list[Path] = []
    problems: list[str] = []
    for path in paths:
        name = html.escape(path.name or str(path))
        if path.is_dir():
            build = _pio_folder(path)
            if build is None:
                idf = (path / "flasher_args.json").is_file()
                build = path if idf else build_dir_below(path)
            if build is None:
                problems.append(
                    tr(f"<b>{name}</b>: a folder without an ESP-IDF or PlatformIO build in it")
                )
            else:
                builds.append(build)
        elif not path.exists():
            problems.append(tr(f"<b>{name}</b>: not found"))
        elif path.suffix.lower() == ".lnk":
            problems.append(tr(f"<b>{name}</b>: a shortcut; drop the file it points to"))
        elif path.suffix.lower() != ".bin":
            problems.append(
                tr(
                    f"<b>{name}</b>: not a .bin file. To flash another kind of file, open a "
                    "firmware and use Add entry, then Attachment, on its Entries tab."
                )
            )
        else:
            try:
                with path.open("rb") as handle:
                    empty = not handle.read(1)
            except OSError as exc:
                problems.append(tr(f"<b>{name}</b>: cannot be read ({html.escape(_text(exc))})"))
                continue
            if empty:
                problems.append(tr(f"<b>{name}</b>: empty file (0 bytes)"))
            else:
                files.append(path)
    if builds and (files or len(builds) > 1 or problems):
        problems.append(tr("Drop a build folder on its own."))
    build = builds[0] if len(builds) == 1 else None
    return files, build, problems


def is_pio_folder(path: Path) -> bool:
    """A PlatformIO env build folder (`.pio/build/<env>`) or a project folder with
    platformio.ini; sort_drop() gives one of these or an ESP-IDF build folder."""
    parent = path.parent
    in_build = parent.name.lower() == "build" and parent.parent.name.lower() == ".pio"
    return in_build or (path / PLATFORMIO_INI).is_file()


def _pio_folder(path: Path) -> Path | None:
    try:
        return env_ref(path).build_dir
    except EnvDirError:
        pass
    if not (path / PLATFORMIO_INI).is_file():
        return None
    built = built_envs_below(path)
    return built[0].build_dir if built else path


@contextlib.contextmanager
def _busy() -> Generator[None]:
    """Wait cursor while packing or checking a package."""
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


def _text(exc: Exception) -> str:
    return exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
