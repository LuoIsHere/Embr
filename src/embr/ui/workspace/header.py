"""The firmware and project header (name, path, tags, notes) and the banner under it."""

import datetime

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import (
    QFontMetricsF,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from embr.app.chips import firmware_mcus
from embr.app.library import FirmwareFlag
from embr.app.locations import firmware_location
from embr.app.tracking import STATE_TEXT, TrackStatus
from embr.core.firmware.files import local_file
from embr.core.firmware.model import Firmware, Source
from embr.core.firmware.tracking import ResolveState
from embr.i18n import N_, tr
from embr.ui.owner import Owner
from embr.ui.widgets import (
    ElidedLabel,
    button,
    clear_layout,
    danger_action,
    elided,
    label,
    small,
    tag,
)

SOURCE_LABELS = {
    Source.MANUAL: (N_("fixed"), "gray"),
    Source.ARCHIVED: (N_("archived"), "gray"),
    Source.IDF_BUILD: (N_("tracks IDF build"), "info"),
    Source.PIO_BUILD: (N_("tracks PIO build"), "info"),
}
BACK_UP = N_("Back up this build…")
BACKING_UP = N_("Backing up…")
STATE_TAGS = {
    ResolveState.TARGET_SWITCHED: N_("target switched"),
    ResolveState.INCOMPLETE: N_("build incomplete"),
}


def crumb(owner: Owner, firmware: Firmware) -> str:
    if firmware.project_id is None:
        return tr("Standalone")
    try:
        project = owner.library.project(firmware.project_id).name
    except KeyError:
        project = "?"
    return f"{project} / {firmware.device or tr('Ungrouped')}"


class Header(QWidget):
    """Breadcrumb, name, tags (MCUs, source, the firmware's own tags, state), the ⋯ menu
    and the grey notes line."""

    def __init__(self, owner: Owner) -> None:
        super().__init__()
        self._owner = owner
        self.row = QHBoxLayout()
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(8)
        self._firmware: Firmware | None = None
        self.notes = elided("", tone="3")  # ends in "…" at its actual width
        small(self.notes)
        self._notes_tip = ""
        self.files_updated = label("", tone="3")
        small(self.files_updated)
        self.files_updated.hide()
        self.menu_button = button("⋯", "ibtn", tooltip=tr("Firmware actions"))
        self.menu = QMenu(self)
        self.menu.addAction(tr("Rename…\tF2"), lambda: owner.firmware_action("rename"))
        self.menu.addAction(tr("Duplicate"), lambda: owner.firmware_action("duplicate"))
        self.menu.addAction(tr("Move to project…"), lambda: owner.firmware_action("move"))
        self.archive_action = self.menu.addAction(
            tr("Archive this version…"), lambda: owner.firmware_action("archive")
        )
        self.backup_action = self.menu.addAction(
            tr(BACK_UP), lambda: owner.firmware_action("backup")
        )
        self.menu.setToolTipsVisible(True)  # explains a disabled Back up this build
        self.menu.addAction(tr("Export…"), lambda: owner.firmware_action("export"))
        self.reveal_action = self.menu.addAction(
            tr("Show in Explorer"), lambda: owner.firmware_action("reveal")
        )
        self.menu.aboutToShow.connect(self._update_reveal)
        self.menu.addSeparator()
        danger_action(self.menu, tr("Delete…"), lambda: owner.firmware_action("delete"))
        self.menu_button.clicked.connect(
            lambda: self.menu.popup(
                self.menu_button.mapToGlobal(QPoint(0, self.menu_button.height() + 4))
            )
        )
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addLayout(self.row)
        top.addStretch(1)
        top.addWidget(self.menu_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 0)
        layout.setSpacing(4)
        layout.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(NOTES_GAP)
        bottom.addWidget(self.files_updated)
        bottom.addWidget(self.notes, 1)
        layout.addLayout(bottom)

    def _update_reveal(self) -> None:
        """When the menu opens: the folder may have gone or appeared since the last refresh."""
        firmware = self._firmware
        self.reveal_action.setEnabled(firmware is not None and bool(firmware_location(firmware)))

    def show_firmware(self, status: TrackStatus, flag: FirmwareFlag) -> None:
        firmware = status.firmware
        self._firmware = firmware
        clear_layout(self.row)
        self.menu_button.show()
        # long names end in "…" (full text in the tooltip) when the row is too narrow
        path = elided(crumb(self._owner, firmware), tone="3")
        shrink_to(path, CRUMB_MIN)
        self.row.addWidget(path, 1)
        name = elided(firmware.name)
        name.setProperty("heading", "1")
        shrink_to(name, HEADING_MIN)
        lift_to_baseline(name, path)
        self.row.addWidget(name)
        for mcu in firmware_mcus(self._owner.context, firmware):
            self.row.addWidget(tag(mcu.label, "gray"))
        self.row.addWidget(tag(*SOURCE_LABELS[firmware.source]))
        for text in firmware.tags:  # the user's words: neither translated nor read as markup
            own = tag("", "gray")
            own.setTextFormat(Qt.TextFormat.PlainText)
            own.setText(text)
            self.row.addWidget(own)
        if flag == FirmwareFlag.NEEDS_CONFIRMATION:
            self.row.addWidget(tag(tr("needs confirmation"), "warn"))
        elif flag == FirmwareFlag.MISSING:
            missing = N_("build output missing") if firmware.source.tracking else N_("file missing")
            self.row.addWidget(tag(missing, "bad"))
        elif flag == FirmwareFlag.BLOCKED:
            self.row.addWidget(tag(STATE_TAGS.get(status.state, "build incomplete"), "bad"))
        if status.updated_at is not None:
            self.row.addWidget(tag(tr(f"Build updated {status.updated_at:%H:%M:%S}"), "info"))
        if firmware.read_only:
            self.row.addWidget(tag(tr("created by a newer Embr"), "warn"))
        self.notes.set_full_text(firmware.notes or tr("no note — edit on the Parameters tab"))
        self._notes_tip = firmware.notes
        self.show_files_updated(status)
        self.archive_action.setEnabled(
            status.state.flashable and any(local_file(e) for e in firmware.entries)
        )
        self._show_backup(status)

    def _show_backup(self, status: TrackStatus) -> None:
        """Back up this build: tracking firmware only; disabled (the state as the tooltip)
        unless the build can be flashed, while a backup runs, and while the firmware is
        locked or read-only."""
        firmware = status.firmware
        owner = self._owner
        tracking = firmware.source.tracking
        busy = tracking and owner.library.versions.busy(firmware.id)
        locked = owner.is_locked(firmware.id) or firmware.read_only
        flashable = status.state.flashable
        action = self.backup_action
        action.setVisible(tracking)
        action.setText(tr(BACKING_UP) if busy else tr(BACK_UP))
        action.setEnabled(tracking and flashable and not busy and not locked)
        action.setToolTip("" if flashable else tr(STATE_TEXT[status.state]))

    def show_files_updated(self, status: TrackStatus) -> None:
        """The grey time left of the notes, alone: a rebuild with the same content changes it
        without any reason to rebuild the workspace."""
        moment = status.files_updated
        self.files_updated.setText(tr(f"Files updated {time_text(moment)}") if moment else "")
        self.files_updated.setVisible(moment is not None)
        width = NOTES_WIDTH
        if moment is not None:
            width -= self.files_updated.sizeHint().width() + NOTES_GAP
        self.notes.setMaximumWidth(width)
        self.notes.setToolTip(self._notes_tip)

    def show_project(self, owner: Owner, project_id: str) -> None:
        self._firmware = None
        clear_layout(self.row)
        project = owner.library.project(project_id)
        count = len(owner.library.project_firmware(project_id))
        caption = label(tr("Project"), tone="3")
        self.row.addWidget(caption)
        name = elided(project.name)
        name.setProperty("heading", "1")
        shrink_to(name, HEADING_MIN)
        lift_to_baseline(name, caption)
        self.row.addWidget(name)
        self.row.addWidget(tag(tr(f"{count} firmware"), "gray"))
        groups = len(project.devices)
        self.row.addWidget(tag(tr("1 group") if groups == 1 else tr(f"{groups} groups"), "gray"))
        if project.read_only:
            self.row.addWidget(tag(tr("created by a newer Embr"), "warn"))
        self.menu_button.hide()
        self.files_updated.hide()
        self.notes.setMaximumWidth(NOTES_WIDTH)
        self.notes.set_full_text(project.notes or tr("no note — edit below"))
        self.notes.setToolTip(project.notes)


def shrink_to(widget: ElidedLabel, narrowest: int) -> None:
    """A header text may end in "…" down to `narrowest`, but a shorter text is no wider than
    itself, so that what follows it stays next to it. Measured with the font as styled."""
    widget.ensurePolished()
    widget.setMinimumWidth(min(narrowest, widget.sizeHint().width()))


def time_text(moment: datetime.datetime, today: datetime.date | None = None) -> str:
    """HH:MM:SS today, MM-DD HH:MM on any other day."""
    if moment.date() == (today or datetime.date.today()):
        return f"{moment:%H:%M:%S}"
    return f"{moment:%m-%d %H:%M}"


def lift_to_baseline(heading: QLabel, reference: QLabel) -> None:
    """Put the baseline of `heading` (a larger font) on that of `reference`, both centred
    in the same row: a bottom margin of twice the difference of their baselines' offsets
    from the text centre (a margin moves centred text by half of it). Computed from the
    fonts as styled, so it holds for any font, size and hinting."""
    heading.ensurePolished()
    reference.ensurePolished()
    big, little = QFontMetricsF(heading.font()), QFontMetricsF(reference.font())
    offset = (big.ascent() - big.height() / 2) - (little.ascent() - little.height() / 2)
    heading.setContentsMargins(0, 0, 0, max(0, round(2 * offset)))


class Banner(QFrame):
    """Warning strip above the tabs, with an optional action button."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("banner")
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.action = button("", "ibtn")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.action)
        self.mode = ""
        self.hide()

    def show_text(self, text: str, action: str = "", mode: str = "") -> None:
        """`mode` tells the owner what the button does ("confirm", "directory",
        "follow_env")."""
        self.mode = mode
        self.text.setText(text)
        self.action.setText(action)
        self.action.setVisible(bool(action))
        self.show()


CRUMB_MIN = 40
"""Narrowest the header's crumb gets, unless its text is shorter."""
HEADING_MIN = 80
"""The header's name never shrinks below this, unless its text is shorter."""
NOTES_WIDTH = 900
"""Widest the header's grey line gets, with the files' time; narrower when the window
leaves less room (the notes then end in "…" where they are cut)."""
NOTES_GAP = 12
"""Between the files' time and the notes."""
