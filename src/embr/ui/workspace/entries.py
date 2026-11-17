"""The Entries tab: the table of a firmware's entries and the columns' widths."""

import dataclasses
import html
from collections.abc import Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from embr.app.chips import Mcu, chip_choosable, entry_mcu, firmware_mcus, fits, image_header
from embr.app.library import AddedFiles, AlreadyInFirmwareError
from embr.app.locations import entry_location
from embr.core import chipinfo
from embr.core.firmware.entries import AddressError, FolderError, parse_address
from embr.core.firmware.files import local_file, read_entry
from embr.core.firmware.image_header import format_revision
from embr.core.firmware.image_sniff import parse_partition_table
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, Entry, Firmware
from embr.core.firmware.tracking import EntryMark
from embr.i18n import N_, tr
from embr.ui import dialogs
from embr.ui.adapters import settings as ui_settings
from embr.ui.owner import Owner
from embr.ui.sidebar import PLUS
from embr.ui.widgets import (
    ElidedLabel,
    button,
    elided,
    label,
    tag,
)
from embr.ui.workspace.header import crumb

EN_DASH = f" {chr(0x2013)} "
MARK_TAGS = {
    EntryMark.CHANGED: (N_("content changed"), "warn"),
    EntryMark.NEW: (N_("new file"), "warn"),
    EntryMark.MISSING: (N_("file missing"), "bad"),
}


def entry_error(exc: Exception) -> str:
    """Why an entry action changed nothing, for a dialog (rich text)."""
    if isinstance(exc, FolderError):
        names = html.escape(", ".join(p.name for p in exc.outside))
        return tr(f"{names} is not in this firmware's folder {html.escape(exc.folder)}.")
    if isinstance(exc, OSError) and exc.strerror:
        where = f"{exc.filename}: " if exc.filename else ""
        return html.escape(f"{where}{exc.strerror}")
    return html.escape(str(exc))


def kept_notes(result: AddedFiles) -> list[str]:
    """Log lines for files that were already entries of the firmware (added nothing)."""
    lines = [f"[entry] {e.name}: changed content accepted" for e in result.updated]
    if result.unchanged:
        names = ", ".join(e.name for e in result.unchanged)
        lines.append(f"[entry] already in this firmware, unchanged: {names}")
    return lines


NOTE_MIN_WIDTH = 120
ADDRESS_PX = 104
"""Entry table: the address column without status tags."""
ADDRESS_FIELD_PX = 84
"""Entry table: the address field, what that column leaves it inside the cell's padding."""
CELL_TAG_GAP = 4
"""Entry table: before each status tag in an address cell."""
ROW_PX = 36
"""Height of an entries row."""
_DRAGGABLE = (1, 2, 3, 4, 5, 6)
"""Address to Image requires; Note takes what is left."""
_MIN_COLUMN = 40
COLUMN_WIDTHS = "entry_columns"
"""settings.ini [ui]: widths the user dragged the entries columns to, 0 = fit."""
"""Entry table: the Note column never gets narrower than this."""
MCU_WIDEST = f"ESP32-P4 v0{chr(0x2013)}v1"
"""Entry table: the longest MCU label; the MCU column is sized to show its choice
"auto (<label>)" whole, in the current language."""
_GIVE_WAY = ((3, 90), (5, 0), (4, 70), (6, 80), (2, 110), (1, 84))
"""(column, narrowest width) in the order columns give way; 0 hides the column."""
"""Entry table columns that shrink, in this order and down to these widths, when the
window is too narrow for all of them (0: the column is hidden)."""


_HIDDEN_LAST = (6, 4)
"""Entry table columns hidden, in this order, when _GIVE_WAY's widths still do not fit."""


class EntriesTab(QWidget):
    COLUMNS = (
        "",
        N_("Address"),
        N_("MCU"),
        N_("Name"),
        N_("Kind"),
        N_("Address source"),
        N_("Image requires"),
        N_("Note"),
    )
    archive_requested = Signal(str)
    """entry id"""
    merge_requested = Signal()

    def __init__(self, owner: Owner) -> None:
        super().__init__()
        self._owner = owner
        self._firmware: Firmware | None = None
        self.setAcceptDrops(True)
        self._mcus: tuple[Mcu, ...] = ()
        """The shown firmware's MCUs (app.chips.firmware_mcus())."""
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setObjectName("entries")
        self.table.setHorizontalHeaderLabels([tr(c) for c in self.COLUMNS])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.viewport().installEventFilter(self)  # refit columns, see eventFilter()
        self.table.setWordWrap(False)  # one line per row; long text is elided
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.verticalHeader().setDefaultSectionSize(ROW_PX)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)  # Note takes the rest
        self._sizing = False
        """True while _fit_columns() sets widths (not a drag by the user)."""
        self._chosen = self._load_widths()
        self._header_hints: dict[int, int] = {}
        """Column -> header size hint, kept from when the column was last visible."""
        self._auto: dict[int, int] = {}
        """Column -> width _fit_columns() last gave it while the user had not dragged it."""
        """Column -> width the user dragged it to (Address to Image requires)."""
        self.table.setColumnWidth(0, 28)
        self.table.setColumnWidth(1, ADDRESS_PX)
        header.sectionResized.connect(self._column_dragged)  # after the initial widths
        self.table.itemChanged.connect(self._toggled)
        self.add_button = button(tr(f"{PLUS} Add entry (or drop bin files here)"), "addrow")
        self.add_menu = QMenu(self)
        self.add_menu.addAction(tr("Files…"), self._add_files)
        self.add_menu.addAction(tr("Attachment…"), self._add_attachment)
        self.add_button.clicked.connect(
            lambda: self.add_menu.popup(
                self.add_button.mapToGlobal(QPoint(0, self.add_button.height()))
            )
        )
        add_row = QFrame()
        add_row.setObjectName("addrow")
        add_row.setFixedHeight(44)
        row_layout = QHBoxLayout(add_row)
        row_layout.setContentsMargins(6, 0, 6, 0)
        row_layout.addWidget(self.add_button, 1)
        self.from_build = label(tr("Entries come from the build directory"), tone="3")
        self.from_build.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.from_build.setToolTip(
            tr("To add files, archive this version (⋯ menu) or create a fixed firmware")
        )
        row_layout.addWidget(self.from_build, 1)
        self.merge_button = button(tr("Merge to .bin…"), "ibtn")
        self.merge_button.setFixedWidth(130)
        self.merge_button.clicked.connect(self.merge_requested.emit)
        row_layout.addWidget(self.merge_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.table)
        layout.addWidget(add_row)
        layout.addStretch(1)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._filling = False
        self._committed: tuple[str, int] | None = None
        """(entry id, column) of the address just committed: the refresh that follows shows
        the stored value, not the text still in the focused edit (an emptied address)."""

    def show_firmware(
        self,
        firmware: Firmware,
        marks: Mapping[str, EntryMark],
        notes: Mapping[str, str] | None = None,
        removed: Sequence[Entry] = (),
    ) -> None:
        """`notes`: importer remarks by entry id (TrackStatus.entry_notes). `removed`:
        entries whose file left the build (TrackStatus.removed), shown read-only below the
        others with their "file missing" mark."""
        notes = notes or {}
        same = self._firmware is not None and self._firmware.id == firmware.id
        typing = self._typing() if same else None
        if typing is not None and typing[:2] == self._committed:
            typing = None
        self._committed = None
        self._firmware = firmware
        self._mcus = firmware_mcus(self._owner.context, firmware)
        locked = self._owner.is_locked(firmware.id) or firmware.read_only
        self._filling = True
        self.table.setRowCount(0)
        rows = sorted(firmware.entries, key=lambda e: (e.address is None, e.address or 0))
        gone = [dataclasses.replace(e, enabled=False) for e in removed]
        tracking = firmware.source.tracking
        self.table.setRowCount(len(rows) + len(gone))
        for row, entry in enumerate(rows):
            self._fill_row(row, entry, marks.get(entry.id), locked, notes.get(entry.id, ""))
        for row, entry in enumerate(gone, start=len(rows)):
            self._fill_row(row, entry, marks.get(entry.id, EntryMark.MISSING), True)
        self._filling = False
        self.add_button.setEnabled(not locked)
        self.add_button.setVisible(not tracking)
        self.from_build.setVisible(tracking)
        enabled = [e for e in firmware.entries if e.enabled]
        # nothing to merge, or the build output / a file is missing
        self.merge_button.setEnabled(
            bool(enabled) and all(marks.get(e.id) != EntryMark.MISSING for e in enabled)
        )
        header_height = self.table.horizontalHeader().sizeHint().height()
        self._fit_columns()
        self.table.setFixedHeight(header_height + ROW_PX * (len(rows) + len(gone)) + 2)
        if typing is not None:
            self._restore_typing(*typing)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Refit on the table viewport's own resize: by then it has its final width (this
        tab's resizeEvent comes before the table lays out, so the old width would be used and
        the columns would jump and miss newly gained space)."""
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_columns()
        return super().eventFilter(watched, event)

    def _fit_columns(self) -> None:
        """Fit every column into the visible width, so the table never scrolls sideways
        and the Note column stays usable in a narrow window. Each column first gets what
        its content wants (cell widgets do not take part in content sizing, see
        _name_width()); when that is too wide, the name gives way first (elided, the tooltip
        has the full path), then Address source (hidden), then Kind and Image requires
        (elided). When the status tags still leave no room, Image requires and then Kind are
        hidden too (_HIDDEN_LAST). Note gets NOTE_MIN_WIDTH plus whatever is left."""
        if self._firmware is None:
            return
        header = self.table.horizontalHeader()
        widest = tr(f"auto ({MCU_WIDEST})")
        mcu = self.table.fontMetrics().horizontalAdvance(widest) + 34  # frame, arrow
        widths = {0: 28, 1: ADDRESS_PX + self._widest_tags(), 2: mcu, 3: self._name_width()}
        for column in (4, 5, 6):
            # a hidden section's size hint is 0, which would show the column again on the
            # next fit and hide it after that (columns jumping while the window is resized)
            if not header.isSectionHidden(column):
                self._header_hints[column] = header.sectionSizeHint(column)
            content = self.table.sizeHintForColumn(column)
            widths[column] = max(self._header_hints.get(column, 0), content)
        widths.update(self._chosen)  # dragged by the user
        floors = self._floors()
        widths[1] = max(widths[1], floors[1])  # a dragged width does not cut a status tag
        spare = self.table.viewport().width() - sum(widths.values()) - NOTE_MIN_WIDTH
        # columns the user did not drag give way first, so a dragged width is kept if possible
        order = sorted(floors, key=lambda column: column in self._chosen)
        for column in order:
            if spare >= 0:
                break
            floor = floors[column]
            give = min(widths[column] - floor, -spare) if floor else widths[column]  # 0: hide
            if give > 0:
                widths[column] -= give
                spare += give
        for column in _HIDDEN_LAST:
            if spare < 0:
                spare += widths[column]
                widths[column] = 0
        widths[7] = NOTE_MIN_WIDTH + max(spare, 0)
        for column in _DRAGGABLE:
            if column not in self._chosen:
                self._auto[column] = widths[column]
        self._sizing = True
        try:
            for column in (4, 5, 6):
                self.table.setColumnHidden(column, widths[column] == 0)
            for column, width in widths.items():
                self.table.setColumnWidth(column, width)
        finally:
            self._sizing = False

    def _column_dragged(self, column: int, old: int, new: int) -> None:
        """The user dragged a column edge (Address to Image requires): keep the width, taking
        the space from Note, which never gets narrower than NOTE_MIN_WIDTH, so the table still
        never scrolls sideways. Saved in settings.ini [ui]."""
        if self._sizing or column not in _DRAGGABLE:
            return
        header = self.table.horizontalHeader()
        floors = self._floors()
        # other columns that can give way count at their floor: dragging one column wider
        # first takes Note's spare width, then what the others can give (_fit_columns())
        others = sum(
            floors.get(c, header.sectionSize(c))
            for c in range(7)
            if c != column and not header.isSectionHidden(c)
        )
        widest = self.table.viewport().width() - others - NOTE_MIN_WIDTH
        # no room left: a column can still be made narrower, and dragged back to the width
        # it had before the user first changed it
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

    def _name_width(self) -> int:
        """What the name column would like: cell widgets do not take part in content
        sizing, so the name is measured with the style sheet's font."""
        text = self.table.fontMetrics()
        widest = self.table.horizontalHeader().sectionSizeHint(3)
        for row in range(self.table.rowCount()):
            for name in self.table.cellWidget(row, 3).findChildren(ElidedLabel):
                widest = max(widest, text.horizontalAdvance(name.full_text()) + 32)
        return widest

    def _widest_tags(self) -> int:
        """What the status tags of the widest row add to the address column; 0 without any."""
        widest = 0
        for row in range(self.table.rowCount()):
            width = 0
            for widget in self.table.cellWidget(row, 1).findChildren(QLabel):
                if widget.property("tag") is not None:
                    widget.ensurePolished()
                    width += widget.sizeHint().width() + CELL_TAG_GAP
            widest = max(widest, width)
        return widest

    def _floors(self) -> dict[int, int]:
        """_GIVE_WAY as column -> narrowest width: the address column keeps its status tags."""
        floors = dict(_GIVE_WAY)
        floors[1] += self._widest_tags()
        return floors

    def _typing(self) -> tuple[str, int, str, int] | None:
        """(entry id, column, text, cursor) of an address or note being typed, so that a
        refresh (window activation, the periodic check) does not throw the typing away."""
        focused = QApplication.focusWidget()
        if not isinstance(focused, QLineEdit) or not self.table.isAncestorOf(focused):
            return None
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            entry_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            for column in (1, 7):
                cell = self.table.cellWidget(row, column)
                if isinstance(entry_id, str) and cell.isAncestorOf(focused):
                    return entry_id, column, focused.text(), focused.cursorPosition()
        return None

    def _restore_typing(self, entry_id: str, column: int, text: str, cursor: int) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None or item.data(Qt.ItemDataRole.UserRole) != entry_id:
                continue
            edit = self.table.cellWidget(row, column).findChild(QLineEdit)
            if isinstance(edit, QLineEdit) and not edit.isReadOnly():
                edit.setText(text)
                edit.setFocus()
                edit.setCursorPosition(cursor)
            return

    def _fill_row(
        self, row: int, entry: Entry, mark: EntryMark | None, locked: bool, remark: str = ""
    ) -> None:
        assert self._firmware is not None
        mcu = entry_mcu(self._owner.context, self._firmware, entry)
        board = self._owner.identified_chip()
        revision = self._owner.identified_revision()
        unfit = board is not None and not fits(mcu, board, revision)  # not for the identified board
        dim = "3" if not entry.enabled or unfit else None
        check = QTableWidgetItem()
        check.setData(Qt.ItemDataRole.UserRole, entry.id)
        flags = Qt.ItemFlag.ItemIsEnabled
        if not locked and not unfit:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        check.setFlags(flags)
        if board is not None and unfit:
            name = chipinfo.display_name(board)
            check.setToolTip(
                tr(f"For {mcu.label}; the board is {name}")
                if mcu
                else tr(f"No MCU set; the board is {name}")
            )
        check.setCheckState(Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked)
        self.table.setItem(row, 0, check)
        address = QLineEdit("" if entry.address is None else f"0x{entry.address:X}")
        address.setProperty("kind", "cell")
        address.setProperty("mono", "1")
        address.setPlaceholderText(tr("address"))
        address.setReadOnly(locked)
        hint = tr("Hex address. Clear the field and confirm to restore the detected address")
        if entry.auto_address is not None and entry.auto_address != entry.address:
            hint += f" (0x{entry.auto_address:X})"
        address.setToolTip(hint)
        address.editingFinished.connect(lambda e=entry, w=address: self._address(e, w))
        self.table.setCellWidget(row, 1, self._address_cell(address, entry, mark, remark))
        self.table.setCellWidget(row, 2, self._mcu_cell(entry, mcu, locked, dim))
        self.table.setCellWidget(row, 3, self._name_cell(entry, dim))
        p = self._owner.palette
        for column, text, color in (
            (4, entry.kind.value, p.fg_3 if dim else p.fg_2),
            (5, _address_source(entry), p.fg_3),
            (6, self._requires(entry), p.fg_3),
        ):
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            item.setForeground(QColor(color))
            self.table.setItem(row, column, item)
        note = QLineEdit(entry.note)
        note.setProperty("kind", "cell")
        note.setPlaceholderText(tr("add note"))
        note.setReadOnly(locked)
        note.editingFinished.connect(lambda e=entry, w=note: self._note(e, w))
        self.table.setCellWidget(row, 7, _cell(note))

    def _mcu_cell(self, entry: Entry, mcu: Mcu | None, locked: bool, dim: str | None) -> QWidget:
        """The image header's chip (and P4 line), read-only; a chooser for files that name
        no chip, with one item per revision line for chips that have lines."""
        assert self._firmware is not None
        if locked or not chip_choosable(self._owner.context, self._firmware, entry):
            shown = label(mcu.label if mcu else "—", tone=dim or "2")
            return _cell(shown)
        combo = QComboBox()
        combo.setProperty("kind", "cell")
        auto = self._mcus[0] if len(self._mcus) == 1 and not entry.chip else None
        combo.addItem(tr(f"auto ({auto.label})") if auto else "—", "")
        for chip in chipinfo.supported_chips():
            for choice in [
                Mcu(chip.name, ln.name) for ln in chipinfo.revision_lines(chip.name)
            ] or [Mcu(chip.name)]:
                combo.addItem(choice.label, f"{choice.chip}|{choice.line}")
        current = f"{entry.chip}|{entry.chip_line}" if entry.chip else ""
        combo.setCurrentIndex(max(combo.findData(current), 0))
        combo.setToolTip(
            tr(
                "This file names no chip: choose the MCU it is for. Left empty, it follows the "
                "firmware's only chip; with several chips it is not flashed."
            )
        )

        def chosen(_index: int, e: Entry = entry, w: QComboBox = combo) -> None:
            self._set_chip(e, w)

        combo.activated.connect(chosen)
        return _cell(combo, left=0)  # its text starts where the read-only names start

    def _address_cell(
        self, address: QLineEdit, entry: Entry, mark: EntryMark | None, note: str
    ) -> QWidget:
        """The address, and to its right the status tags of the entry. The field keeps its
        width, so the addresses line up, and gives way only when the column has no room."""
        address.setMaximumWidth(ADDRESS_FIELD_PX)
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(CELL_TAG_GAP)
        layout.addWidget(address, 1)  # up to its maximum; what is left goes to the stretch
        tags: list[QLabel] = []
        if mark is not None:
            tags.append(tag(*MARK_TAGS[mark]))
        if entry.auto_added:
            tags.append(tag(tr("added automatically"), "gray"))
        if note:  # PIO: boot_app0 placed by the table, or a listed address the image disputes
            moved = entry.kind == EntryKind.OTADATA
            explained = tag(
                tr("placed by partition table") if moved else tr("address differs"),
                "info" if moved else "warn",
            )
            explained.setToolTip(tr(note))
            tags.append(explained)
        for shown in tags:
            shown.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            layout.addWidget(shown)
        layout.addStretch(0)
        return widget

    def _name_cell(self, entry: Entry, dim: str | None) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(4)
        name = elided(entry.name, tone=dim)
        layout.addWidget(name, 1)
        scheme, _, rest = entry.file.partition(":")
        where = (
            rest
            if scheme == "path"
            else tr(f"data/archive/{rest}.bin, original name {entry.name}")
            if scheme == "archive"
            else tr("0xFF bytes, generated when flashing")
        )
        widget.setToolTip(where)
        name.setToolTip(where)
        return widget

    def _requires(self, entry: Entry) -> str:
        header = image_header(self._owner.context, entry)
        if header is None or header.chip is None:
            return "—"
        low, high = format_revision(header.min_rev_full), format_revision(header.max_rev_full)
        return tr(f"chip {low}{EN_DASH}{high}")

    # ---- edits ------------------------------------------------------------------------

    def _toggled(self, item: QTableWidgetItem) -> None:
        if self._filling or item.column() != 0 or self._firmware is None:
            return
        entry_id = item.data(Qt.ItemDataRole.UserRole)
        enabled = item.checkState() == Qt.CheckState.Checked
        self._owner.library.set_entry_enabled(self._firmware.id, str(entry_id), enabled)
        self._owner.changed()

    def _address(self, entry: Entry, widget: QLineEdit) -> None:
        if self._firmware is None or widget.isReadOnly():
            return
        current = "" if entry.address is None else f"0x{entry.address:X}"
        text = widget.text().strip()
        if text == current:
            return
        try:
            updated = self._owner.library.set_entry_address(self._firmware.id, entry.id, text)
        except AddressError:
            kept = f"kept {current}" if current else "the address stays empty"
            self._owner.note(
                "error", f"[entry] {entry.name}: '{text}' is not a hex address; {kept}"
            )
            widget.setText(current)
            return
        if not text:
            restored = "none" if updated.address is None else f"0x{updated.address:X}"
            self._owner.note(
                "dim", f"[entry] {entry.name}: address cleared, restored detected {restored}"
            )
        self._committed = (entry.id, 1)
        self._owner.changed()

    def _note(self, entry: Entry, widget: QLineEdit) -> None:
        if self._firmware is None or widget.isReadOnly() or widget.text() == entry.note:
            return
        self._owner.library.set_entry_note(self._firmware.id, entry.id, widget.text().strip())
        entry.note = widget.text().strip()

    def _set_chip(self, entry: Entry, widget: QComboBox) -> None:
        if self._firmware is None:
            return
        data = str(widget.currentData() or "")
        chip, _, line = data.partition("|")
        if (chip or None, line) == (entry.chip, entry.chip_line):
            return
        try:
            self._owner.library.set_entry_chip(self._firmware.id, entry.id, chip or None, line)
        except ValueError as exc:
            self._owner.note("error", f"[entry] {exc}")
            return
        shown = Mcu(chip, line).label if chip else tr("not set")
        self._owner.note("dim", f"[entry] {entry.name}: MCU -> {shown}")
        self._owner.changed()

    def _add_files(self) -> None:
        if self._firmware is None:
            return
        names, _ = QFileDialog.getOpenFileNames(
            self, tr("Add entries"), "", tr("Binary files (*.bin);;All files (*)")
        )
        if names:
            self._add_paths([Path(n) for n in names])

    def _add_paths(self, paths: list[Path]) -> None:
        if self._firmware is None:
            return
        try:
            result = self._owner.library.add_files(self._firmware.id, paths)
        except (OSError, ValueError) as exc:
            self._owner.note("error", f"[entry] {exc}")
            return
        for entry in result.added:
            where = tr("no address") if entry.address is None else f"0x{entry.address:X}"
            self._owner.note("dim", f"[entry] added {entry.name} as {entry.kind.value} at {where}")
        for line in kept_notes(result):
            self._owner.note("dim", line)
        self._owner.changed()

    def _add_attachment(self) -> None:
        firmware = self._firmware
        if firmware is None:
            return
        name, _ = QFileDialog.getOpenFileName(self, tr("Add attachment"), "", tr("All files (*)"))
        if not name:
            return
        partitions = self._partitions(firmware)
        choices = [tr(f"Partition: {p}") for p in partitions] + [tr("At an address…")]
        # chosen by index: the texts are translated
        index = dialogs.ask_choice(self, tr("Place attachment"), tr("Placement"), choices, tr("OK"))
        if index is None:
            return
        try:
            if index < len(partitions):
                partition = partitions[index]
                self._owner.library.add_attachment(firmware.id, Path(name), partition=partition)
            else:
                text = dialogs.ask_text(
                    self, tr("Place attachment"), tr("Hex address"), "", "", tr("OK")
                )
                if text is None:
                    return
                address = parse_address(text)  # AddressError is a ValueError
                self._owner.library.add_attachment(firmware.id, Path(name), address=address)
        except (OSError, ValueError) as exc:
            self._owner.note("error", f"[entry] {exc}")
            return
        self._owner.changed()

    def _partitions(self, firmware: Firmware) -> list[str]:
        for entry in firmware.entries:
            if entry.enabled and entry.kind.value == "partition_table":
                try:
                    context = self._owner.context
                    table = parse_partition_table(
                        read_entry(entry, context.paths.archive, context.files)
                    )
                except OSError:
                    return []
                return [p.name for p in table.partitions] if table else []
        return []

    def _context_menu(self, pos: QPoint) -> None:
        firmware = self._firmware
        row = self.table.rowAt(pos.y())
        item = self.table.item(row, 0) if row >= 0 else None
        if firmware is None or item is None or self._owner.is_locked(firmware.id):
            return
        entry_id = str(item.data(Qt.ItemDataRole.UserRole))
        entry = next((e for e in firmware.entries if e.id == entry_id), None)
        if entry is None:
            return
        derived = firmware.source.tracking
        menu = QMenu(self)
        menu.setToolTipsVisible(True)  # explains a disabled Remove entry
        relocate = menu.addAction(tr("Re-locate…"))
        relocate.setEnabled(local_file(entry) is not None and not derived)
        archive = menu.addAction(tr("Archive this file"))
        archive.setEnabled(local_file(entry) is not None and not derived)
        copy_to = menu.addAction(tr("Copy to firmware…"))
        location = entry_location(entry)
        reveal = menu.addAction(tr("Show in Explorer"))
        reveal.setEnabled(location is not None)
        menu.addSeparator()
        remove = menu.addAction(tr("Remove entry"))
        remove.setEnabled(not derived)
        if derived:
            remove.setToolTip(tr("Comes from the build output; untick it to leave it out"))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is relocate:
            name, _ = QFileDialog.getOpenFileName(
                self, tr(f"Re-locate {entry.name}"), "", tr("All files (*)")
            )
            if not name or not self._relocate_to(firmware, entry, Path(name)):
                return
        elif chosen is archive:
            self.archive_requested.emit(entry.id)
            return
        elif chosen is copy_to:
            self._copy_to(firmware, entry)
        elif chosen is reveal:
            self._owner.show_in_explorer(location)
            return
        elif chosen is remove:
            self._owner.library.remove_entry(firmware.id, entry.id)
            self._owner.note("dim", f"[entry] removed {entry.name}")
        else:
            return
        self._owner.changed()

    def _relocate_to(self, firmware: Firmware, entry: Entry, path: Path) -> bool:
        """Re-locate… with the file chosen: False (and why, in a dialog) when nothing
        changed, e.g. a file outside the firmware's folder."""
        try:
            self._owner.library.relocate_entry(firmware.id, entry.id, path)
        except (OSError, ValueError) as exc:
            dialogs.notice(self, tr("Not re-located"), [entry_error(exc)])
            return False
        self._owner.note("dim", f"[entry] {entry.name} now points at {path}")
        return True

    def _copy_to(self, firmware: Firmware, entry: Entry) -> None:
        targets = [
            f
            for f in self._owner.context.firmware.all()
            if f.id != firmware.id and not f.source.tracking
        ]
        if not targets:
            self._owner.note("note", "[entry] no other firmware to copy to")
            return
        names = [f"{f.name}  ({crumb(self._owner, f)})" for f in targets]
        index = dialogs.ask_choice(
            self, tr("Copy entry"), tr(f"Copy {entry.name} to"), names, tr("Copy")
        )
        if index is not None:
            target = targets[index]
            try:
                self._owner.library.copy_entry_to(firmware.id, entry.id, target.id)
            except AlreadyInFirmwareError as exc:
                self._owner.note("note", f"[entry] not copied: {exc}")
                return
            except (OSError, ValueError) as exc:
                dialogs.notice(self, tr("Not copied"), [entry_error(exc)])
                return
            self._owner.note("dim", f"[entry] copied {entry.name} to '{target.name}'")

    # ---- dropping files from Explorer ---------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        firmware = self._firmware
        if (
            event.mimeData().hasUrls()
            and firmware is not None
            and not self._owner.is_locked(firmware.id)
        ):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        files = [p for p in paths if p.is_file()]
        if files:
            event.acceptProposedAction()
            self._add_paths(files)  # tracking firmware: the library refuses, the log says why


ADDRESS_SOURCES = {AddressSource.AUTO: N_("auto"), AddressSource.MANUAL: N_("manual")}
"""Address source column: detected from the file, or typed by the user."""


def _address_source(entry: Entry) -> str:
    if entry.address_source == AddressSource.PARTITION:
        return tr(f"partition {entry.partition}")
    return tr(ADDRESS_SOURCES[entry.address_source])


def _cell(widget: QWidget, left: int = 4) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(left, 0, 4, 0)
    row.addWidget(widget)
    return box
