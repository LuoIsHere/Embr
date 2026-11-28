"""The Versions tab of a tracking firmware: its build versions, newest first, with editable
names, notes and pins, and Back up this build."""

import dataclasses
from collections.abc import Mapping, Sequence

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from embr.app.tracking import STATE_TEXT, TrackStatus
from embr.core.firmware.model import Firmware
from embr.core.versions import Origin, Version, VersionParams, minute
from embr.i18n import N_, tr
from embr.ui.adapters import settings as ui_settings
from embr.ui.dialogs.device import size_text
from embr.ui.owner import Owner
from embr.ui.widgets import button, label, plain_table, small
from embr.ui.workspace.entries import NOTE_MIN_WIDTH, ROW_PX
from embr.ui.workspace.header import BACK_UP, BACKING_UP

COLUMN_WIDTHS = "version_columns"
"""settings.ini [ui]: widths the user dragged the version columns to, 0 = fit."""
NAME, NOTE, PINNED = 1, 7, 8
_DRAGGABLE = (1, 2, 3, 4, 5, 6)
"""Name to Flashed; Note takes what is left, # and Pinned keep their width."""
_MIN_COLUMN = 40
_GIVE_WAY = ((5, 60), (3, 70), (1, 90))
"""(column, narrowest width) in the order columns give way when the window is too narrow:
Changes, Version, Name."""
NO_FLASHES = "—"
"""Flashed column of a version never flashed."""
ORIGIN_LABELS = {Origin.MANUAL: N_("manual"), Origin.AUTO: N_("auto"), Origin.CLI: "CLI"}
NAME_HINT = N_("Version name. Clear the field and confirm to restore the default name")
FLASH_SETTINGS = N_("flash settings")


def shown_version(version: Version) -> str:
    """git describe and the app version, both when they differ; the app version only when
    the build uses it (VersionParams.use_app_version)."""
    describe, app = version.git_describe or "", version.shown_app_version or ""
    if describe and app and app != describe:
        return f"{describe} · {app}"
    return describe or app


def changes(version: Version, previous: Version | None) -> str:
    """Entries whose content differs from the previous version (by name, also added or
    removed ones), then "flash settings" when chip, flash, reset or stub differ; "" for the
    first version."""
    if previous is None:
        return ""
    now = {e.name: e.sha256 for e in version.entries}
    before = {e.name: e.sha256 for e in previous.entries}
    names = [n for n in now if before.get(n) != now[n]]
    names += [n for n in before if n not in now]
    if version.chip != previous.chip or _settings(version.params) != _settings(previous.params):
        names.append(tr(FLASH_SETTINGS))
    return ", ".join(names)


def _settings(params: VersionParams) -> tuple[object, ...]:
    return (
        params.flash_mode,
        params.flash_freq,
        params.flash_size,
        params.before,
        params.after,
        params.stub,
    )


def flashed_tooltip(flashes: Sequence[tuple[str, str]]) -> str:
    """'Flashed to N boards:' and one line per board (MAC, flash time); "" when never
    flashed."""
    if not flashes:
        return ""
    lines = [f"{mac} · {minute(at)}" for mac, at in flashes]
    head = (
        tr("Flashed to 1 board:") if len(flashes) == 1 else tr(f"Flashed to {len(flashes)} boards:")
    )
    return "\n".join([head, *lines])


def footer_text(count: int, freed: int) -> str:
    size = size_text(freed)
    if count == 1:
        return tr(f"1 version; deleting it would free {size}")
    return tr(f"{count} versions; deleting all would free {size}")


class VersionsTab(QWidget):
    COLUMNS = (
        "#",
        N_("Name"),
        N_("Created"),
        N_("Version"),
        N_("Origin"),
        N_("Changes"),
        N_("Flashed"),
        N_("Note"),
        N_("Pinned"),
    )

    def __init__(self, owner: Owner) -> None:
        super().__init__()
        self._owner = owner
        self._firmware: Firmware | None = None
        self._versions: list[Version] = []
        self.backup_button = button(tr(BACK_UP), "ibtn")
        self.backup_button.clicked.connect(lambda: owner.firmware_action("backup"))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addStretch(1)
        top.addWidget(self.backup_button)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setObjectName("versions")
        self.table.setHorizontalHeaderLabels([tr(c) if c != "#" else c for c in self.COLUMNS])
        plain_table(self.table, ROW_PX)
        self.table.viewport().installEventFilter(self)  # refit columns, see eventFilter()
        header = self.table.horizontalHeader()
        for column in (0, NOTE, PINNED):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        self._sizing = False
        """True while _fit_columns() sets widths (not a drag by the user)."""
        self._chosen = self._load_widths()
        """Column -> width the user dragged it to (Name to Flashed)."""
        self._auto: dict[int, int] = {}
        """Column -> width _fit_columns() last gave it while the user had not dragged it."""
        header.sectionResized.connect(self._column_dragged)
        self.table.itemChanged.connect(self._pinned)
        self.empty = label(tr("No versions yet."), tone="3")
        self.footer = small(label("", tone="3"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addWidget(self.table)
        layout.addWidget(self.empty)
        layout.addWidget(self.footer)
        layout.addStretch(1)
        self._filling = False
        self._committed: tuple[str, int] | None = None
        """(version id, column) of the name just saved: the refresh that follows shows the
        stored name, not the text still in the focused field (an emptied name)."""

    @property
    def shown(self) -> list[Version]:
        """The versions in the table, newest first."""
        return list(self._versions)

    def show_firmware(self, status: TrackStatus) -> None:
        firmware = status.firmware
        same = self._firmware is not None and self._firmware.id == firmware.id
        typing = self._typing() if same else None
        if typing is not None and typing[:2] == self._committed:
            typing = None
        self._committed = None
        self._firmware = firmware
        owner = self._owner
        store = owner.context.versions
        self._versions = store.for_firmware(firmware.id)
        flashes: Mapping[str, list[tuple[str, str]]] = store.flash_counts(firmware.id)
        locked = owner.is_locked(firmware.id) or firmware.read_only
        self._show_backup(status, locked)
        self._filling = True
        self.table.setRowCount(len(self._versions))
        for row, version in enumerate(self._versions):
            older = self._versions[row + 1] if row + 1 < len(self._versions) else None
            self._fill_row(row, version, older, flashes.get(version.id, []), locked)
        self._filling = False
        has_rows = bool(self._versions)
        self.table.setVisible(has_rows)
        self.empty.setVisible(not has_rows)
        self.footer.setVisible(has_rows)
        if has_rows:
            freed = owner.library.versions.exclusive_size(firmware.id)
            self.footer.setText(footer_text(len(self._versions), freed))
        header_height = self.table.horizontalHeader().sizeHint().height()
        self._fit_columns()
        self.table.setFixedHeight(header_height + ROW_PX * len(self._versions) + 2)
        if typing is not None:
            self._restore_typing(*typing)

    def _show_backup(self, status: TrackStatus, locked: bool) -> None:
        busy = self._owner.library.versions.busy(status.firmware_id)
        self.backup_button.setText(tr(BACKING_UP) if busy else tr(BACK_UP))
        flashable = status.state.flashable
        self.backup_button.setEnabled(flashable and not busy and not locked)
        self.backup_button.setToolTip("" if flashable else tr(STATE_TEXT[status.state]))

    def _fill_row(
        self,
        row: int,
        version: Version,
        older: Version | None,
        flashes: Sequence[tuple[str, str]],
        locked: bool,
    ) -> None:
        p = self._owner.palette
        seq = QTableWidgetItem(f"#{version.seq}")
        seq.setData(Qt.ItemDataRole.UserRole, version.id)
        name = QLineEdit(version.name)
        name.setProperty("kind", "cell")
        name.setReadOnly(locked)
        name.setToolTip(tr(NAME_HINT))
        name.editingFinished.connect(lambda v=version, w=name: self._rename(v, w))
        self.table.setCellWidget(row, NAME, _cell(name))
        texts = (
            (0, seq, p.fg_3),
            (2, QTableWidgetItem(minute(version.created_at)), p.fg_2),
            (3, QTableWidgetItem(shown_version(version)), p.fg_2),
            (4, QTableWidgetItem(tr(ORIGIN_LABELS[version.origin])), p.fg_3),
            (5, QTableWidgetItem(changes(version, older)), p.fg_2),
            (6, QTableWidgetItem(str(len(flashes)) if flashes else NO_FLASHES), p.fg_2),
        )
        for column, item, color in texts:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            item.setForeground(QColor(color))
            tip = (
                flashed_tooltip(flashes) if column == 6 else item.text() if column in (3, 5) else ""
            )
            item.setToolTip(tip)
            self.table.setItem(row, column, item)
        note = QLineEdit(version.note)
        note.setProperty("kind", "cell")
        note.setPlaceholderText(tr("add note"))
        note.setReadOnly(locked)
        note.editingFinished.connect(lambda v=version, w=note: self._note(v, w))
        self.table.setCellWidget(row, NOTE, _cell(note))
        pin = QTableWidgetItem()
        pin.setData(Qt.ItemDataRole.UserRole, version.id)
        flags = Qt.ItemFlag.ItemIsEnabled
        if not locked:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        pin.setFlags(flags)
        pin.setCheckState(Qt.CheckState.Checked if version.pinned else Qt.CheckState.Unchecked)
        self.table.setItem(row, PINNED, pin)

    # ---- edits ------------------------------------------------------------------------

    def _rename(self, version: Version, widget: QLineEdit) -> None:
        if widget.isReadOnly() or widget.text().strip() == version.name:
            return
        try:
            renamed = self._owner.context.versions.rename(version.id, widget.text())
        except KeyError:  # deleted meanwhile
            return
        self._owner.note("dim", f"[version] #{version.seq} named '{renamed.name}'")
        widget.setText(renamed.name)
        self._committed = (version.id, NAME)
        self._owner.changed()

    def _note(self, version: Version, widget: QLineEdit) -> None:
        text = widget.text().strip()
        if widget.isReadOnly() or text == version.note:
            return
        self._owner.context.versions.set_note(version.id, text)
        self._versions = [
            v if v.id != version.id else dataclasses.replace(v, note=text) for v in self._versions
        ]

    def _pinned(self, item: QTableWidgetItem) -> None:
        if self._filling or item.column() != PINNED:
            return
        pinned = item.checkState() == Qt.CheckState.Checked
        self._owner.context.versions.set_pinned(str(item.data(Qt.ItemDataRole.UserRole)), pinned)

    # ---- typing kept over a refresh -------------------------------------------------

    def _typing(self) -> tuple[str, int, str, int] | None:
        """(version id, column, text, cursor) of a name or note being typed, so that a refresh
        does not throw the typing away."""
        focused = QApplication.focusWidget()
        if not isinstance(focused, QLineEdit) or not self.table.isAncestorOf(focused):
            return None
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            version_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            for column in (NAME, NOTE):
                cell = self.table.cellWidget(row, column)
                if isinstance(version_id, str) and cell.isAncestorOf(focused):
                    return version_id, column, focused.text(), focused.cursorPosition()
        return None

    def _restore_typing(self, version_id: str, column: int, text: str, cursor: int) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None or item.data(Qt.ItemDataRole.UserRole) != version_id:
                continue
            edit = self.table.cellWidget(row, column).findChild(QLineEdit)
            if isinstance(edit, QLineEdit) and not edit.isReadOnly():
                edit.setText(text)
                edit.setFocus()
                edit.setCursorPosition(cursor)
            return

    # ---- column widths ----------------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Refit on the table viewport's own resize, when it has its final width."""
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_columns()
        return super().eventFilter(watched, event)

    def _fit_columns(self) -> None:
        """Every column gets what its content wants (or the width the user dragged it to);
        when that is too wide, Changes, Version and Name give way in that order (elided,
        the tooltip has the full text). Note gets NOTE_MIN_WIDTH plus whatever is left, so
        the table never scrolls sideways."""
        if self._firmware is None:
            return
        header = self.table.horizontalHeader()
        widths: dict[int, int] = {}
        for column in range(len(self.COLUMNS)):
            if column == NOTE:
                continue
            content = self.table.sizeHintForColumn(column)
            widths[column] = max(header.sectionSizeHint(column), content)
        widths[NAME] = max(widths[NAME], self._name_width())
        widths.update(self._chosen)
        spare = self.table.viewport().width() - sum(widths.values()) - NOTE_MIN_WIDTH
        order = sorted(_GIVE_WAY, key=lambda pair: pair[0] in self._chosen)
        for column, floor in order:
            if spare >= 0:
                break
            give = min(widths[column] - floor, -spare)
            if give > 0:
                widths[column] -= give
                spare += give
        widths[NOTE] = NOTE_MIN_WIDTH + max(spare, 0)
        for column in _DRAGGABLE:
            if column not in self._chosen:
                self._auto[column] = widths[column]
        self._sizing = True
        try:
            for column, width in widths.items():
                self.table.setColumnWidth(column, width)
        finally:
            self._sizing = False

    def _name_width(self) -> int:
        """Cell widgets do not take part in content sizing: the names are measured."""
        text = self.table.fontMetrics()
        widest = 0
        for row in range(self.table.rowCount()):
            edit = self.table.cellWidget(row, NAME).findChild(QLineEdit)
            if isinstance(edit, QLineEdit):
                widest = max(widest, text.horizontalAdvance(edit.text()) + 32)
        return widest

    def _column_dragged(self, column: int, old: int, new: int) -> None:
        """The user dragged a column edge (Name to Flashed): keep the width, taking the space
        from Note, which never gets narrower than NOTE_MIN_WIDTH. Saved in settings.ini [ui]."""
        if self._sizing or column not in _DRAGGABLE:
            return
        header = self.table.horizontalHeader()
        floors = dict(_GIVE_WAY)
        others = sum(
            floors.get(c, header.sectionSize(c))
            for c in range(len(self.COLUMNS))
            if c not in (column, NOTE)
        )
        widest = self.table.viewport().width() - others - NOTE_MIN_WIDTH
        limit = max(widest, old, self._auto.get(column, 0))
        self._chosen[column] = max(_MIN_COLUMN, min(new, limit))
        self._save_widths()
        self._fit_columns()

    def _load_widths(self) -> dict[int, int]:
        saved = ui_settings.load_ints(self._owner.context.settings, COLUMN_WIDTHS) or []
        return {c: w for c, w in zip(_DRAGGABLE, saved, strict=False) if w > 0}

    def _save_widths(self) -> None:
        values = [self._chosen.get(c, 0) for c in _DRAGGABLE]  # 0: fit to the content
        ui_settings.save_ints(self._owner.context.settings, COLUMN_WIDTHS, values)


def _cell(widget: QWidget, left: int = 4) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(left, 0, 4, 0)
    row.addWidget(widget)
    return box
