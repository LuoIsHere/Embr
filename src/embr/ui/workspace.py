"""Center column: firmware header, Entries / Parameters / Records tabs (design doc §6.1
"中间工作区 — 选中固件时")."""

import html
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QFontMetricsF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from embr.app.chips import Mcu, chip_choosable, entry_mcu, firmware_mcus, fits, image_header
from embr.app.library import FirmwareFlag
from embr.app.tracking import TrackStatus
from embr.core import chipinfo
from embr.core.firmware.entries import AddressError, FolderError
from embr.core.firmware.files import read_entry
from embr.core.firmware.image_header import format_revision
from embr.core.firmware.image_sniff import parse_partition_table
from embr.core.firmware.import_pio import BOOT_APP0
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, Entry, Firmware, Source, editable_params
from embr.core.firmware.tracking import ChangeKind, EntryMark, ResolveState
from embr.core.flasher.plan import AFTER_MODES, BEFORE_MODES
from embr.core.history import FlashRecord, RecordResult
from embr.i18n import N_, tr
from embr.ui.adapters import settings as ui_settings
from embr.ui.owner import Owner
from embr.ui.sidebar import PLUS
from embr.ui.widgets import (
    ElidedLabel,
    button,
    clear_layout,
    danger_action,
    elided,
    label,
    tag,
)

EN_DASH = f" {chr(0x2013)} "
MARK_TAGS = {
    EntryMark.CHANGED: (N_("content changed"), "warn"),
    EntryMark.NEW: (N_("new file"), "warn"),
    EntryMark.MISSING: (N_("file missing"), "bad"),
}

SOURCE_LABELS = {
    Source.MANUAL: (N_("fixed"), "gray"),
    Source.ARCHIVED: (N_("archived"), "gray"),
    Source.IDF_BUILD: (N_("tracks IDF build"), "info"),
    Source.PIO_BUILD: (N_("tracks PIO build"), "info"),
}
CONNECTION_LABELS = {
    "uart_bridge": N_("UART bridge"),
    "usb_serial_jtag": "USB-Serial/JTAG",
    "usb_otg": "USB-OTG",
    "unknown": N_("serial port"),
}
STATE_TAGS = {
    ResolveState.TARGET_SWITCHED: N_("target switched"),
    ResolveState.INCOMPLETE: N_("build incomplete"),
}
RESULT_TAGS = {
    RecordResult.OK: "ok",
    RecordResult.REFUSED: "warn",
    RecordResult.FAILED: "bad",
    RecordResult.CANCELLED: "bad",
}


def crumb(owner: Owner, firmware: Firmware) -> str:
    if firmware.project_id is None:
        return tr("Standalone")
    try:
        project = owner.library.project(firmware.project_id).name
    except KeyError:
        project = "?"
    return f"{project} / {firmware.device or 'Ungrouped'}"


class Header(QWidget):
    """Breadcrumb, name, tags, the ⋯ menu and the grey notes line."""

    def __init__(self, owner: Owner) -> None:
        super().__init__()
        self._owner = owner
        self.row = QHBoxLayout()
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(8)
        self.notes = label("", tone="3")
        self.notes.setStyleSheet("font-size: 12px;")
        self.menu_button = button("⋯", "ibtn", tooltip=tr("Firmware actions"))
        self.menu = QMenu(self)
        self.menu.addAction(tr("Rename…\tF2"), lambda: owner.firmware_action("rename"))
        self.menu.addAction(tr("Duplicate"), lambda: owner.firmware_action("duplicate"))
        self.menu.addAction(tr("Move to project…"), lambda: owner.firmware_action("move"))
        self.archive_action = self.menu.addAction(
            tr("Archive this version…"), lambda: owner.firmware_action("archive")
        )
        self.menu.addAction(tr("Export…"), lambda: owner.firmware_action("export"))
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
        layout.addWidget(self.notes)

    def show_firmware(self, status: TrackStatus, flag: FirmwareFlag) -> None:
        firmware = status.firmware
        clear_layout(self.row)
        self.menu_button.show()
        path = label(crumb(self._owner, firmware), tone="3")
        self.row.addWidget(path)
        name = QLabel(firmware.name)
        name.setProperty("heading", "1")
        lift_to_baseline(name, path)
        self.row.addWidget(name)
        for mcu in firmware_mcus(self._owner.context, firmware):
            self.row.addWidget(tag(mcu.label, "gray"))
        self.row.addWidget(tag(*SOURCE_LABELS[firmware.source]))
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
        text = firmware.notes or tr("no note — edit on the Parameters tab")
        self.notes.setText(
            self.notes.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, 900)
        )
        self.notes.setToolTip(firmware.notes or tr("No note yet. Edit it on the Parameters tab."))
        self.archive_action.setEnabled(
            status.state.flashable and any(e.file.startswith("path:") for e in firmware.entries)
        )

    def show_project(self, owner: Owner, project_id: str) -> None:
        clear_layout(self.row)
        project = owner.library.project(project_id)
        count = len(owner.library.project_firmware(project_id))
        caption = label(tr("Project"), tone="3")
        self.row.addWidget(caption)
        name = QLabel(project.name)
        name.setProperty("heading", "1")
        lift_to_baseline(name, caption)
        self.row.addWidget(name)
        self.row.addWidget(tag(tr(f"{count} firmware"), "gray"))
        self.row.addWidget(tag(tr(f"{len(project.devices)} groups"), "gray"))
        self.menu_button.hide()
        self.notes.setText(project.notes or tr("no note — edit below"))
        self.notes.setToolTip(project.notes or tr("No note yet."))


def lift_to_baseline(heading: QLabel, reference: QLabel) -> None:
    """Put the baseline of `heading` (a larger font) on that of `reference`, both centred
    in the same row: a bottom margin of twice the difference of their baselines' offsets
    from the text centre (a margin moves centred text by half of it). Computed from the
    fonts as styled, so it holds for any font, size and hinting."""
    heading.ensurePolished()
    reference.ensurePolished()
    big, small = QFontMetricsF(heading.font()), QFontMetricsF(reference.font())
    offset = (big.ascent() - big.height() / 2) - (small.ascent() - small.height() / 2)
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
        """`mode` tells the owner what the button does ("confirm", "directory")."""
        self.mode = mode
        self.text.setText(text)
        self.action.setText(action)
        self.action.setVisible(bool(action))
        self.show()


NOTE_MIN_WIDTH = 120
ROW_PX = 36
"""Height of an entries row."""
_DRAGGABLE = (1, 2, 3, 4, 5, 6)
"""Address to Image requires; Note takes what is left."""
_MIN_COLUMN = 40
COLUMN_WIDTHS = "entry_columns"
"""settings.ini [ui]: widths the user dragged the entries columns to, 0 = fit."""
"""Entry table: the Note column never gets narrower than this."""
MCU_WIDEST = f"auto (ESP32-P4 v0{chr(0x2013)}v1)"
"""Entry table: the longest MCU choice; the MCU column is sized to show it whole."""
_GIVE_WAY = ((3, 90), (5, 0), (4, 70), (6, 80), (2, 110), (1, 84))
"""(column, narrowest width) in the order columns give way; 0 hides the column."""
"""Entry table columns that shrink, in this order and down to these widths, when the
window is too narrow for all of them (0: the column is hidden)."""


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
        self.table.setColumnWidth(1, 104)
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

    def show_firmware(
        self,
        firmware: Firmware,
        marks: Mapping[str, EntryMark],
        notes: Mapping[str, str] | None = None,
    ) -> None:
        """`notes`: importer remarks by entry id (TrackStatus.entry_notes)."""
        notes = notes or {}
        same = self._firmware is not None and self._firmware.id == firmware.id
        typing = self._typing() if same else None
        self._firmware = firmware
        self._mcus = firmware_mcus(self._owner.context, firmware)
        locked = self._owner.is_locked(firmware.id) or firmware.read_only
        self._filling = True
        self.table.setRowCount(0)
        rows = sorted(firmware.entries, key=lambda e: (e.address is None, e.address or 0))
        tracking = firmware.source.tracking
        self.table.setRowCount(len(rows))
        for row, entry in enumerate(rows):
            self._fill_row(row, entry, marks.get(entry.id), locked, notes.get(entry.id, ""))
        self._filling = False
        self.add_button.setEnabled(not locked)
        self.add_button.setVisible(not tracking)
        self.from_build.setVisible(tracking)
        self.merge_button.setEnabled(any(e.enabled for e in firmware.entries))
        header_height = self.table.horizontalHeader().sizeHint().height()
        self._fit_columns()
        self.table.setFixedHeight(header_height + ROW_PX * len(rows) + 2)
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
        (elided). Note gets NOTE_MIN_WIDTH plus whatever is left."""
        if self._firmware is None:
            return
        header = self.table.horizontalHeader()
        mcu = self.table.fontMetrics().horizontalAdvance(MCU_WIDEST) + 34  # frame, arrow
        widths = {0: 28, 1: 104, 2: mcu, 3: self._name_width()}
        for column in (4, 5, 6):
            # a hidden section's size hint is 0, which would show the column again on the
            # next fit and hide it after that (columns jumping while the window is resized)
            if not header.isSectionHidden(column):
                self._header_hints[column] = header.sectionSizeHint(column)
            content = self.table.sizeHintForColumn(column)
            widths[column] = max(self._header_hints.get(column, 0), content)
        widths.update(self._chosen)  # dragged by the user
        spare = self.table.viewport().width() - sum(widths.values()) - NOTE_MIN_WIDTH
        # columns the user did not drag give way first, so a dragged width is kept if possible
        order = sorted(_GIVE_WAY, key=lambda item: item[0] in self._chosen)
        for column, floor in order:
            if spare >= 0:
                break
            give = min(widths[column] - floor, -spare) if floor else widths[column]  # 0: hide
            if give > 0:
                widths[column] -= give
                spare += give
        widths[7] = NOTE_MIN_WIDTH + max(spare, 0)
        for column in _DRAGGABLE:
            if column not in self._chosen:
                self._auto[column] = widths[column]
        self._sizing = True
        try:
            self.table.setColumnHidden(5, widths[5] == 0)
            for column, width in widths.items():
                self.table.setColumnWidth(column, width)
        finally:
            self._sizing = False

    def _column_dragged(self, column: int, old: int, new: int) -> None:
        """The user dragged a column edge (Address to Image requires): keep the width, taking
        the space from Note, which never gets narrower than NOTE_MIN_WIDTH, so the table still
        never scrolls sideways (design doc §12 item 7). Saved in settings.ini [ui]."""
        if self._sizing or column not in _DRAGGABLE:
            return
        header = self.table.horizontalHeader()
        floors = dict(_GIVE_WAY)
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
        sizing, so name plus status tags are measured with the style sheet's fonts."""
        text = self.table.fontMetrics()
        small = QFont(self.table.font())
        small.setPixelSize(10)
        small.setBold(True)
        tags = QFontMetrics(small)
        widest = self.table.horizontalHeader().sectionSizeHint(3)
        for row in range(self.table.rowCount()):
            cell = self.table.cellWidget(row, 3)
            names = [w.full_text() for w in cell.findChildren(ElidedLabel)]
            labels = [w.text() for w in cell.findChildren(QLabel) if w.property("tag") is not None]
            width = sum(text.horizontalAdvance(n) for n in names) + 24
            width += sum(tags.horizontalAdvance(t) + 10 + 4 for t in labels)
            widest = max(widest, width + 8)
        return widest

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
        unfit = board is not None and not fits(mcu, board, revision)  # design doc §12 item 13
        dim = "3" if not entry.enabled or unfit else None
        check = QTableWidgetItem()
        check.setData(Qt.ItemDataRole.UserRole, entry.id)
        flags = Qt.ItemFlag.ItemIsEnabled
        if not locked and not unfit:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        check.setFlags(flags)
        if unfit:
            check.setToolTip(tr(f"For {mcu.label if mcu else 'no MCU'}; the board is {board}"))
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
        self.table.setCellWidget(row, 1, _cell(address))
        self.table.setCellWidget(row, 2, self._mcu_cell(entry, mcu, locked, dim))
        self.table.setCellWidget(row, 3, self._name_cell(entry, mark, dim, remark))
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

    def _name_cell(
        self, entry: Entry, mark: EntryMark | None, dim: str | None, note: str = ""
    ) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(4)
        name = elided(entry.name, tone=dim)
        layout.addWidget(name, 1)
        if mark is not None:
            layout.addWidget(tag(*MARK_TAGS[mark]))
        if entry.auto_added:
            layout.addWidget(tag(tr("added automatically"), "gray"))
        if note:  # PIO: boot_app0 placed by the table, or a listed address the image disputes
            moved = entry.kind == EntryKind.OTADATA
            layout.addWidget(
                tag(
                    tr("placed by partition table") if moved else tr("address differs"),
                    "info" if moved else "warn",
                )
            )
        scheme, _, rest = entry.file.partition(":")
        where = (
            rest
            if scheme == "path"
            else tr(f"data/archive/{rest}.bin, original name {entry.name}")
            if scheme == "archive"
            else tr("0xFF bytes, generated when flashing")
        )
        widget.setToolTip(f"{where}\n{tr(note)}" if note else where)
        name.setToolTip(widget.toolTip())
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
            self._owner.note(
                "error", f"[entry] {entry.name}: '{text}' is not a hex address; kept {current}"
            )
            widget.setText(current)
            return
        if not text:
            restored = "none" if updated.address is None else f"0x{updated.address:X}"
            self._owner.note(
                "dim", f"[entry] {entry.name}: address cleared, restored detected {restored}"
            )
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
            added = self._owner.library.add_files(self._firmware.id, paths)
        except (OSError, ValueError) as exc:
            self._owner.note("error", f"[entry] {exc}")
            return
        for entry in added:
            where = tr("no address") if entry.address is None else f"0x{entry.address:X}"
            self._owner.note("dim", f"[entry] added {entry.name} as {entry.kind.value} at {where}")
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
        choice, ok = QInputDialog.getItem(
            self, tr("Place attachment"), tr("Placement"), choices, 0, False
        )
        if not ok:
            return
        try:
            if choice.startswith("Partition: "):
                partition = choice.removeprefix("Partition: ")
                self._owner.library.add_attachment(firmware.id, Path(name), partition=partition)
            else:
                text, ok = QInputDialog.getText(self, tr("Place attachment"), tr("Hex address"))
                if not ok:
                    return
                address = int(text.strip(), 16)
                self._owner.library.add_attachment(firmware.id, Path(name), address=address)
        except (OSError, ValueError) as exc:
            self._owner.note("error", f"[entry] {exc}")
            return
        self._owner.changed()

    def _partitions(self, firmware: Firmware) -> list[str]:
        for entry in firmware.entries:
            if entry.enabled and entry.kind.value == "partition_table":
                try:
                    table = parse_partition_table(
                        read_entry(entry, self._owner.context.paths.archive)
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
        relocate = menu.addAction(tr("Re-locate…"))
        relocate.setEnabled(entry.file.startswith("path:") and not derived)
        archive = menu.addAction(tr("Archive this file"))
        archive.setEnabled(entry.file.startswith("path:") and not derived)
        copy_to = menu.addAction(tr("Copy to firmware…"))
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
            if name:
                self._owner.library.relocate_entry(firmware.id, entry.id, Path(name))
                self._owner.note("dim", f"[entry] {entry.name} now points at {name}")
        elif chosen is archive:
            self.archive_requested.emit(entry.id)
            return
        elif chosen is copy_to:
            self._copy_to(firmware, entry)
        elif chosen is remove:
            self._owner.library.remove_entry(firmware.id, entry.id)
            self._owner.note("dim", f"[entry] removed {entry.name}")
        else:
            return
        self._owner.changed()

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
        choice, ok = QInputDialog.getItem(
            self, tr("Copy entry"), tr(f"Copy {entry.name} to"), names, 0, False
        )
        if ok:
            target = targets[names.index(choice)]
            self._owner.library.copy_entry_to(firmware.id, entry.id, target.id)
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


class ParamsTab(QWidget):
    def __init__(self, owner: Owner) -> None:
        super().__init__()
        self._owner = owner
        self._firmware: Firmware | None = None
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnMinimumWidth(0, 150)
        self.grid.setColumnStretch(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.grid)
        self.extra = QVBoxLayout()
        layout.addLayout(self.extra)
        layout.addStretch(1)

    def show_firmware(self, firmware: Firmware) -> None:
        self._firmware = firmware
        clear_layout(self.grid)
        clear_layout(self.extra)
        locked = self._owner.is_locked(firmware.id) or firmware.read_only
        editable: frozenset[str] = frozenset() if locked else editable_params(firmware)
        self._row = 0
        name = QLineEdit(firmware.name)
        name.setMaximumWidth(420)
        name.setReadOnly(locked)
        name.editingFinished.connect(lambda: self._rename(name))
        self._add(tr("Name"), name, tr("unique within the project, or among standalone firmware"))
        self._add(
            tr("Chip"), self._chip(firmware), tr("from the entries: MCU column on the Entries tab")
        )
        self._add(*self._origin(firmware))
        if firmware.source == Source.PIO_BUILD:
            self._add(
                tr("Framework package"),
                self._package(firmware, locked),
                tr(
                    "Arduino: boot_app0.bin comes from this package; empty uses the Settings path, "
                    "else auto-detection"
                ),
            )
        self._add(tr("Flash mode / freq / size"), self._flash(firmware, "flash_mode" in editable))
        self._add(
            tr("Reset before"),
            self._combo(firmware, "before", BEFORE_MODES, firmware.before, "before" in editable),
            tr("saved setting; may be adjusted to the connection type, see Checks"),
        )
        self._add(
            tr("Reset after"),
            self._combo(firmware, "after", AFTER_MODES, firmware.after, "after" in editable),
        )
        group = (
            (firmware.device or tr("(ungrouped)"))
            if firmware.project_id
            else tr("— (standalone firmware)")
        )
        self._add(
            tr("Group"),
            label(group, tone="2"),
            tr("change via the ⋯ menu → Move, or drag in the sidebar"),
        )
        notes = NotesEdit(
            firmware.notes, tr("What this firmware is, which board revision, why it exists…")
        )
        notes.setReadOnly("notes" not in editable)
        notes.committed.connect(self._set_notes)
        self._add(
            tr("Notes"),
            notes,
            tr("shown in grey next to the name; saved when the field loses focus"),
        )
        tags = QLineEdit(", ".join(firmware.tags))
        tags.setPlaceholderText(tr("comma-separated, e.g. release, s3"))
        tags.setMaximumWidth(420)
        tags.setReadOnly("tags" not in editable)
        tags.editingFinished.connect(
            lambda: self._set("tags", [t.strip() for t in tags.text().split(",") if t.strip()])
        )
        self._add(tr("Tags"), tags)
        if firmware.source == Source.ARCHIVED:
            self.extra.addWidget(
                _box(
                    tr("Archive"),
                    [
                        tr(
                            "Fixed snapshot: files are referenced by content hash "
                            "(archive:<sha256>), the original path is no longer needed."
                        ),
                        tr("Entry names are the recorded original file names; notes are editable."),
                    ],
                )
            )

    def _add(self, title: str, widget: QWidget, note: str = "") -> None:
        caption = label(title, tone="3")
        caption.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.grid.addWidget(caption, self._row, 0)
        if note:
            box = QWidget()
            column = QVBoxLayout(box)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            column.addWidget(widget)
            small = label(note, tone="3")
            small.setStyleSheet("font-size: 12px;")
            small.setWordWrap(True)
            column.addWidget(small)
            widget = box
        self.grid.addWidget(widget, self._row, 1)
        self._row += 1

    def _origin(self, firmware: Firmware) -> tuple[str, QWidget]:
        if firmware.source == Source.ARCHIVED:
            return tr("Archived from"), label(firmware.archived_from or "—", tone="2")
        if firmware.build_ref is not None:
            return tr("Build directory"), self._build_dir(firmware)
        text = firmware.folder or "—"
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(elided(text, mono=True, middle=True), 1)
        move = button(tr("Re-locate folder…"), "ibtn")
        move.setEnabled(bool(firmware.folder) and not self._owner.is_locked(firmware.id))
        move.setToolTip(tr("Point every file at the same place in another folder"))
        move.clicked.connect(lambda: self._relocate_folder(firmware))
        row.addWidget(move)
        column.addLayout(row)
        small = label(
            tr(
                "fixed firmware: files must be in this folder or below it; entries are recorded "
                "by name, path and hash"
            ),
            tone="3",
        )
        small.setStyleSheet("font-size: 12px;")
        small.setWordWrap(True)
        column.addWidget(small)
        return tr("Files folder"), box

    def _relocate_folder(self, firmware: Firmware) -> None:
        chosen = QFileDialog.getExistingDirectory(self, tr("Re-locate folder"), firmware.folder)
        if not chosen:
            return
        try:
            left = self._owner.library.relocate_folder(firmware.id, Path(chosen))
        except (FolderError, ValueError) as exc:
            self._owner.note("error", f"[firmware] {exc}")
            return
        if left:
            names = ", ".join(left)
            self._owner.note("warning", f"[firmware] not found under {chosen}, unchanged: {names}")
        else:
            self._owner.note("ok", f"[firmware] {firmware.name}: files now in {chosen}")
        self._owner.changed()

    def _build_dir(self, firmware: Firmware) -> QWidget:
        assert firmware.build_ref is not None
        status = self._owner.library.tracker.cached(firmware.id)
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(6)
        env = firmware.build_ref.env
        shown = f"{firmware.build_ref.path}  ·  env {env}" if env else firmware.build_ref.path
        row.addWidget(elided(shown, mono=True, middle=True), 1)
        change = button(tr("Change…"), "ibtn")
        change.setEnabled(not self._owner.is_locked(firmware.id))
        change.clicked.connect(lambda: self._owner.firmware_action("directory"))
        row.addWidget(change)
        column.addLayout(row)
        resolved = (firmware.last_resolved or {}).get("resolved_at")
        facts = [
            tr("exists") if Path(firmware.build_ref.path).is_dir() else tr("not found"),
            tr(f"last resolved {str(resolved).replace('T', ' ')}") if resolved else "",
            f"ESP-IDF {status.idf_version}" if status and status.idf_version else "",
            tr(f"{'board' if env else 'sdkconfig'} target {status.configured_target}")
            if status and status.configured_target
            else "",
        ]
        small = label(" · ".join(f for f in facts if f), tone="3")
        small.setStyleSheet("font-size: 12px;")
        column.addWidget(small)
        for line in status.build_notes if status else ():
            note = label(tr(line), tone="3", wrap=True)
            note.setStyleSheet("font-size: 12px;")
            column.addWidget(note)
        return box

    def _package(self, firmware: Firmware, locked: bool) -> QWidget:
        status = self._owner.library.tracker.cached(firmware.id)
        entries = status.firmware.entries if status else []
        boot = next(
            (e.file[5:] for e in entries if e.name == BOOT_APP0 and e.file.startswith("path:")),
            None,
        )
        auto = str(Path(boot).parents[2]) if boot else ""
        default = self._owner.library.framework_pkg_default()
        path = QLineEdit(firmware.framework_pkg)
        path.setPlaceholderText(
            tr(f"Settings path: {default}")
            if default
            else tr(f"auto-detected: {auto}")
            if auto
            else tr("auto-detect (nothing found)")
        )
        path.setReadOnly(locked)
        path.editingFinished.connect(lambda: self._set_package(firmware, path.text()))
        browse = button(tr("Browse…"), "ibtn")
        browse.setEnabled(not locked)

        def pick() -> None:
            chosen = QFileDialog.getExistingDirectory(
                self, tr("Framework package"), path.text() or auto
            )
            if chosen:
                path.setText(chosen)
                self._set_package(firmware, chosen)

        browse.clicked.connect(pick)
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(path, 1)
        row.addWidget(browse)
        return box

    def _set_package(self, firmware: Firmware, text: str) -> None:
        if text.strip() == firmware.framework_pkg:
            return
        self._owner.library.set_framework_pkg(firmware.id, text)
        shown = text.strip() or tr("the Settings path / auto-detection")
        self._owner.note("dim", f"[firmware] {firmware.name}: framework package {shown}")
        self._owner.changed()

    def _chip(self, firmware: Firmware) -> QWidget:
        mcus = firmware_mcus(self._owner.context, firmware)
        return _derived(", ".join(m.label for m in mcus) or "—")

    def _flash(self, firmware: Firmware, editable: bool) -> QWidget:
        flash = firmware.flash
        if not editable:
            return _derived(f"{flash.mode} / {flash.freq} / {flash.size}")
        chips = {m.chip for m in firmware_mcus(self._owner.context, firmware)}
        chip = chipinfo.get_chip(chips.pop()) if len(chips) == 1 else None
        freqs = (
            chip.flash_freqs
            if chip
            else sorted({f for c in chipinfo.supported_chips() for f in c.flash_freqs})
        )
        sizes = (
            chip.flash_sizes
            if chip
            else sorted({s for c in chipinfo.supported_chips() for s in c.flash_sizes})
        )
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(
            self._combo(firmware, "flash_mode", ("keep", *chipinfo.FLASH_MODES), flash.mode, True)
        )
        row.addWidget(self._combo(firmware, "flash_freq", ("keep", *freqs), flash.freq, True))
        row.addWidget(
            self._combo(firmware, "flash_size", ("keep", "detect", *sizes), flash.size, True)
        )
        row.addWidget(label(tr("keep = leave the bootloader header as built"), tone="3"))
        row.addStretch(1)
        return widget

    def _combo(
        self, firmware: Firmware, name: str, options: tuple[str, ...], value: str, editable: bool
    ) -> QWidget:
        if not editable:
            return _derived(value)
        combo = QComboBox()
        combo.addItems(list(options))
        combo.setCurrentText(value)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.activated.connect(lambda: self._set(name, combo.currentText()))
        return _left(combo) if name in ("before", "after") else combo

    def _set_notes(self, text: str) -> None:
        self._set("notes", text)

    def _set(self, name: str, value: object) -> None:
        firmware = self._firmware
        if firmware is None:
            return
        try:
            self._owner.library.set_param(firmware.id, name, value)
        except ValueError as exc:
            self._owner.note("error", f"[firmware] {firmware.name}: {exc}")
        else:
            shown = ", ".join(cast(list[str], value)) if isinstance(value, list) else value
            if name != "notes":
                self._owner.note("dim", f"[firmware] {firmware.name}: {name} → {shown}")
        self._owner.changed()

    def _rename(self, widget: QLineEdit) -> None:
        firmware = self._firmware
        if firmware is None or widget.isReadOnly() or widget.text().strip() == firmware.name:
            return
        try:
            renamed = self._owner.library.rename_firmware(firmware.id, widget.text())
        except ValueError:
            widget.setText(firmware.name)
            return
        self._owner.note("dim", f"[firmware] renamed to '{renamed.name}'")
        self._owner.changed()


class RecordsTab(QWidget):
    def __init__(self, owner: Owner) -> None:
        super().__init__()
        self._owner = owner
        self.list = QVBoxLayout()
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = label(
            tr("Click a record to show its section of that day's log (logs/YYYY-MM-DD.log)."),
            tone="3",
        )
        hint.setStyleSheet("font-size: 12px;")
        layout.addWidget(hint)
        layout.addLayout(self.list)
        layout.addStretch(1)

    def show_firmware(self, firmware: Firmware) -> None:
        clear_layout(self.list)
        records = self._owner.context.history.query(firmware_id=firmware.id)
        if not records:
            self.list.addWidget(label(tr("No flash records yet."), tone="3"))
            return
        for record in records:
            self.list.addWidget(self._row(record))

    def _row(self, record: FlashRecord) -> QWidget:
        row = RecordRow(record.log_file, record.id)
        row.clicked.connect(self._owner.show_log_segment)
        grid = QHBoxLayout(row)
        grid.setContentsMargins(4, 7, 4, 7)
        grid.setSpacing(12)
        stamp = record.timestamp.replace("T", " ")[:16]
        time = label(stamp)
        time.setFixedWidth(130)
        grid.addWidget(time)
        middle = QVBoxLayout()
        middle.setSpacing(2)
        link = tr(CONNECTION_LABELS.get(record.connection_type, record.connection_type))
        port = f"{record.port} · {link}" + (f" · {record.baud}" if record.baud else "")
        if record.mac:
            port += f" · MAC {record.mac}"
        middle.addWidget(label(port))
        middle.addWidget(label(_summary(record), tone="3"))
        grid.addLayout(middle, 1)
        grid.addWidget(tag(record.result.value, RESULT_TAGS[record.result]))
        return row


class RecordRow(QFrame):
    clicked = Signal(str, str)
    """log file name, segment id."""

    def __init__(self, log_file: str, segment_id: str) -> None:
        super().__init__()
        self.setObjectName("rec")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._target = (log_file, segment_id)

    def mousePressEvent(self, event: object) -> None:  # type: ignore[override]
        self.clicked.emit(*self._target)


class NotesEdit(QPlainTextEdit):
    """Multi-line notes, committed when the field loses focus."""

    committed = Signal(str)

    def __init__(self, text: str, placeholder: str) -> None:
        super().__init__(text)
        self.setProperty("kind", "notes")
        self.setPlaceholderText(placeholder)
        self.setMaximumWidth(420)
        self.setFixedHeight(76)
        self._initial = text

    def focusOutEvent(self, event: object) -> None:  # type: ignore[override]
        super().focusOutEvent(event)  # type: ignore[arg-type]
        text = self.toPlainText().strip()
        if text != self._initial:
            self._initial = text
            self.committed.emit(text)


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
        for index, (name, page) in enumerate(
            (
                (N_("Entries"), self.entries),
                (N_("Parameters"), self.params),
                (N_("Records"), self.records),
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
        self.entries.show_firmware(firmware, status.marks, status.entry_notes)
        self.params.show_firmware(firmware)
        self.records.show_firmware(firmware)

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
                    f"Files changed on disk since the last check: {', '.join(changed)} (content). "
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
        if status.state == ResolveState.TARGET_SWITCHED:
            self.banner.show_text(
                tr(
                    f"Target switched, rebuild needed: the configuration targets "
                    f"<b>{status.configured_target}</b>, the build output is still "
                    f"<b>{firmware.chip}</b>. {rebuild}; flashing, archiving and merging are "
                    "blocked until then."
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
            when = str(resolved).replace("T", " ") if resolved else "never"
            self.banner.show_text(
                tr(
                    f"{problems}. Last resolved {when}; the entries below are that snapshot and "
                    "cannot be flashed until the directory exists again."
                ),
                tr("Change directory…"),
                "directory",
            )
        elif status.state == ResolveState.NEEDS_CONFIRMATION:
            first = [c for c in status.changes if c.kind != ChangeKind.GROUP_HINT]
            lines = "".join(f"<br>•&nbsp;{html.escape(tr(c.message))}" for c in first[:8])
            more = tr(f"<br>…and {len(first) - 8} more") if len(first) > 8 else ""
            hint = (
                tr("<br>The device group may need adjusting.")
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
        if self.banner.mode == "directory":
            self._owner.firmware_action("directory")
            return
        try:
            self._owner.library.confirm_entries(firmware.id)
        except ValueError:  # the build changed again since the banner was shown
            self._owner.note("note", f"[entries] {firmware.name}: nothing left to confirm")
        else:
            self._owner.note("ok", f"[entries] {firmware.name}: entries confirmed")
        self._owner.changed()


def _summary(record: FlashRecord) -> str:
    if record.result == RecordResult.REFUSED and record.refuse_reason:
        return record.refuse_reason
    seconds = record.duration_ms / 1000
    written = sum(1 for e in record.entries if not e.skipped)
    skipped = len(record.entries) - written
    text = tr(f"{len(record.entries)} entries, {seconds:.1f} s")
    if skipped:
        text += tr(f" · {skipped} already in flash")
    if record.reset_after:
        text += tr(f" · after={record.reset_after}")
    return text


def _address_source(entry: Entry) -> str:
    if entry.address_source == AddressSource.PARTITION:
        return tr(f"partition {entry.partition}")
    return entry.address_source.value


def _derived(text: str) -> QWidget:
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(label(text, tone="2"))
    row.addWidget(tag(tr("derived"), "gray"))
    row.addStretch(1)
    return widget


def _left(widget: QWidget) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget)
    row.addStretch(1)
    return box


def _cell(widget: QWidget, left: int = 4) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(left, 0, 4, 0)
    row.addWidget(widget)
    return box


def _box(title: str, lines: list[str]) -> QWidget:
    frame = QFrame()
    frame.setObjectName("box")
    frame.setMaximumWidth(640)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 8)
    layout.setSpacing(4)
    head = QLabel(title)
    head.setObjectName("boxhead")
    layout.addWidget(head)
    for line in lines:
        item = label(f"•  {line}", tone="2", wrap=True)
        item.setContentsMargins(10, 0, 10, 0)
        layout.addWidget(item)
    return frame


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
