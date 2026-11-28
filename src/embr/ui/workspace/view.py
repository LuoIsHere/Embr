"""One firmware's page: header, banner and the tabs (Versions only for tracking firmware)."""

import html

from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from embr.app.tracking import TrackStatus
from embr.core import chipinfo
from embr.core.firmware.model import Firmware, Source
from embr.core.firmware.tracking import ChangeKind, EntryMark, ResolveState
from embr.i18n import N_, tr
from embr.ui.owner import Owner
from embr.ui.widgets import (
    button,
)
from embr.ui.workspace.entries import EntriesTab
from embr.ui.workspace.header import Banner
from embr.ui.workspace.params import ParamsTab
from embr.ui.workspace.records import RecordsTab
from embr.ui.workspace.versions import VersionsTab

VERSIONS_TAB = 3


class FirmwareView(QWidget):
    """Header, banner, tabs and the tab pages for one firmware."""

    def __init__(self, owner: Owner) -> None:
        super().__init__()
        self._owner = owner
        self.banner = Banner()
        self.banner.action.clicked.connect(self._banner_action)
        self.tabs = QHBoxLayout()
        self.tabs.setContentsMargins(14, 8, 14, 0)
        self.tabs.setSpacing(2)
        self.tab_group = QButtonGroup(self)
        self.pages = QStackedWidget()
        self.entries = EntriesTab(owner)
        self.params = ParamsTab(owner)
        self.records = RecordsTab(owner)
        self.versions = VersionsTab(owner)
        for index, (name, page) in enumerate(
            (
                (N_("Entries"), self.entries),
                (N_("Parameters"), self.params),
                (N_("Records"), self.records),
                (N_("Versions"), self.versions),
            )
        ):
            tab_button = button(tr(name), "tab")
            tab_button.setCheckable(True)
            tab_button.setChecked(index == 0)
            self.tab_group.addButton(tab_button, index)
            self.tabs.addWidget(tab_button)
            self.pages.addWidget(_scrolling(page))
        self.tabs.addStretch(1)
        self.tab_group.idClicked.connect(self.pages.setCurrentIndex)
        tab_line = QFrame()
        tab_line.setObjectName("tabline")
        tab_line.setLayout(self.tabs)
        body = QVBoxLayout()
        body.setContentsMargins(14, 10, 14, 0)
        body.addWidget(self.banner)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(tab_line)
        layout.addLayout(body)
        layout.addWidget(self.pages, 1)
        self._firmware: Firmware | None = None

    def show_firmware(self, status: TrackStatus) -> None:
        firmware = status.firmware
        self._firmware = firmware
        if firmware.source.tracking:
            self._tracking_banner(status)
        else:
            self._fixed_banner(status)
        self.banner.action.setEnabled(not self._owner.is_locked(firmware.id))
        self.entries.show_firmware(firmware, status.marks, status.entry_notes, status.removed)
        self.params.show_firmware(firmware)
        self.records.show_firmware(firmware)
        self._show_versions(status)

    def _show_versions(self, status: TrackStatus) -> None:
        """Only tracking firmware have versions: for others the tab is hidden, and when it
        was the open one the page goes back to Entries."""
        tab = self.tab_group.button(VERSIONS_TAB)
        tracking = status.firmware.source.tracking
        tab.setVisible(tracking)
        if tracking:
            self.versions.show_firmware(status)
        elif self.pages.currentIndex() == VERSIONS_TAB:
            self.tab_group.button(0).setChecked(True)
            self.pages.setCurrentIndex(0)

    def _fixed_banner(self, status: TrackStatus) -> None:
        entries = status.firmware.entries
        missing = [
            html.escape(e.name) for e in entries if status.marks.get(e.id) == EntryMark.MISSING
        ]
        changed = [
            html.escape(e.name) for e in entries if status.marks.get(e.id) == EntryMark.CHANGED
        ]
        if missing:
            self.banner.show_text(
                tr(
                    f"Files missing: {', '.join(missing)}. Re-locate them (right-click an entry) "
                    "or remove the entries; flashing is blocked until then."
                )
            )
        elif changed:
            self.banner.show_text(
                tr(
                    f"File contents changed on disk since the last check: {', '.join(changed)}. "
                    "Flashing is blocked until you confirm the list below."
                ),
                tr("Confirm entries"),
                "confirm",
            )
        else:
            self.banner.hide()

    def _tracking_banner(self, status: TrackStatus) -> None:
        firmware = status.firmware
        problems = html.escape("; ".join(tr(p) for p in status.problems))
        rebuild = (
            tr("Rebuild the env in PlatformIO")
            if firmware.source == Source.PIO_BUILD
            else tr("Run <code>idf.py build</code>")
        )
        if status.env_gone:  # offer the one env it may have been renamed to
            env = html.escape((firmware.build_ref.env if firmware.build_ref else None) or "")
            text = tr(f"platformio.ini no longer declares [env:{env}]. It may have been renamed.")
            candidates = self._owner.library.tracker.env_candidates(firmware.id)
            if len(candidates) == 1:
                self.banner.show_text(text, tr(f"Follow env {candidates[0]}"), "follow_env")
            else:
                self.banner.show_text(text, tr("Change directory…"), "directory")
        elif status.state == ResolveState.TARGET_SWITCHED:
            self.banner.show_text(
                tr(
                    f"Target switched, rebuild needed: the configuration targets "
                    f"<b>{chipinfo.display_name(status.configured_target or '')}</b>, the build "
                    f"output is still <b>{chipinfo.display_name(firmware.chip or '')}</b>. "
                    f"{rebuild}; flashing, archiving and merging are blocked until then."
                )
            )
        elif status.state == ResolveState.INCOMPLETE:
            self.banner.show_text(
                tr(
                    f"Build incomplete: {problems}. Wait for the build to finish or rebuild; "
                    "flashing is blocked."
                )
            )
        elif status.state == ResolveState.BUILDING:
            self.banner.show_text(tr("The build output is being written; checking again shortly."))
        elif status.state == ResolveState.MISSING:
            resolved = (firmware.last_resolved or {}).get("resolved_at")
            when = str(resolved).replace("T", " ")
            self.banner.show_text(
                tr(
                    f"{problems}. Last resolved {when}; the entries below are that snapshot and "
                    "cannot be flashed until the directory exists again."
                )
                if resolved
                else tr(
                    f"{problems}. Never resolved; flashing is blocked until the directory exists "
                    "again."
                ),
                tr("Change directory…"),
                "directory",
            )
        elif status.state == ResolveState.NEEDS_CONFIRMATION:
            first = [c for c in status.changes if c.kind != ChangeKind.GROUP_HINT]
            lines = "".join(f"<br>•&nbsp;{html.escape(tr(c.message))}" for c in first[:8])
            more = tr(f"<br>…and {len(first) - 8} more") if len(first) > 8 else ""
            hint = (
                tr("<br>The group may need adjusting.")
                if any(c.kind == ChangeKind.GROUP_HINT for c in status.changes)
                else ""
            )
            self.banner.show_text(
                tr(
                    f"The build output changed since it was last confirmed:{lines}{more}{hint}"
                    "<br>Flashing is blocked until you confirm."
                ),
                tr("Confirm entries"),
                "confirm",
            )
        else:
            self.banner.hide()

    def _banner_action(self) -> None:
        firmware = self._firmware
        if firmware is None:
            return
        if self.banner.mode in ("directory", "follow_env"):
            self._owner.firmware_action(self.banner.mode)
            return
        try:
            self._owner.library.confirm_entries(firmware.id)
        except ValueError:  # the build changed again since the banner was shown
            self._owner.note("note", f"[entries] {firmware.name}: nothing left to confirm")
        else:
            self._owner.note("ok", f"[entries] {firmware.name}: entries confirmed")
        self._owner.changed()


def _scrolling(page: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setObjectName("pane")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(14, 10, 14, 10)
    layout.addWidget(page)
    area.setWidget(holder)
    return area
