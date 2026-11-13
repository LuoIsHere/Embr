"""Right column: port and baud, identify, checks, flash / cancel with progress. The log is in the
centre column (log_panel)."""

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QMouseEvent, QResizeEvent, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from embr.app.checklist import CheckItem, CheckState
from embr.app.identify import IdentifySnapshot
from embr.core.serial import ConnectionType, PortInfo, baud_adjustable
from embr.i18n import N_, tr
from embr.ui import icons
from embr.ui.icons import keys_pixmap
from embr.ui.sidebar import chip_name
from embr.ui.theme import Palette
from embr.ui.widgets import Switch, button, clear_layout, label, tag

BAUDS = (115200, 230400, 460800, 921600, 1500000, 2000000)
CONNECTION_LABELS = {
    ConnectionType.UART_BRIDGE: N_("UART bridge"),
    ConnectionType.USB_SERIAL_JTAG: "USB-Serial/JTAG",
    ConnectionType.USB_OTG: "USB-OTG",
    ConnectionType.UNKNOWN: N_("serial port"),
}
QUICK_TIP = N_(
    "Skip Connect & identify and flash directly. The chip is still identified and checked "
    "during the flash; writing is refused if the chip does not match or a security feature "
    "is on."
)
CHECK_TEXT = {
    CheckState.PASS: "PASS",
    CheckState.WARN: "WARN",
    CheckState.REFUSE: "REFUSE",
    CheckState.LATER: "LATER",
}


def port_text(port: PortInfo) -> str:
    text = f"{port.device} · {tr(CONNECTION_LABELS[port.connection])}"
    if port.vid is not None and port.pid is not None:
        text += f" · {port.vid:04X}:{port.pid:04X}"
    return text


class HintLabel(QLabel):
    """The hint above Flash; a click dismisses it."""

    dismissed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWordWrap(True)
        # contents margins, not style-sheet padding: a wrapped label's height for its width
        # leaves the padding out, so the text would touch the coloured box
        self.setContentsMargins(9, 7, 9, 7)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tr("Click to dismiss"))

    def fit_text(self) -> None:
        """Never lower than the wrapped text, whatever a layout has cached for its width.
        heightForWidth() never answers less than the minimum, so that goes first."""
        self.setMinimumHeight(0)
        self.setMinimumHeight(self.heightForWidth(self.width()))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.fit_text()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.fit_text()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.hide()
            self.dismissed.emit()
        super().mousePressEvent(event)


class ConnectionPanel(QWidget):
    port_changed = Signal()
    baud_changed = Signal()
    identify_clicked = Signal()
    erase_clicked = Signal()
    read_clicked = Signal()
    board_log_clicked = Signal()
    """Open logs/boards/<mac>.log of the identified board."""
    flash_clicked = Signal()
    """Flash, or Cancel while a job runs."""
    erase_all_toggled = Signal(bool)
    """The "erase the whole flash first" box above Flash (the firmware's erase_all)."""
    quick_toggled = Signal(bool)
    """The Quick flash switch next to it (a global setting)."""
    hint_dismissed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("right")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._palette: Palette | None = None
        self._ports: tuple[PortInfo, ...] = ()
        self._wanted: PortInfo | None = None
        """The port last chosen (by the user, or picked when there was none): kept while it
        is away, e.g. a USB-Serial/JTAG board re-enumerating after a reset."""
        self._running = False
        self._keys: tuple[str, ...] = ("Ctrl", "Enter")

        # ---- port, baud, identify ----
        self.port = QComboBox()
        self.baud = QComboBox()
        for combo in (self.port, self.baud):  # narrow panel: elide instead of widening it
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(8)
        self.identify = button(tr("Connect && identify"))
        board_hint = tr("Available after Connect & identify on this port")
        self.erase = button(tr("Erase flash…"))
        self.erase.setToolTip(board_hint)
        self.read = button(tr("Read flash…"))
        self.read.setToolTip(board_hint)
        board = QHBoxLayout()
        board.setSpacing(6)
        board.addWidget(self.erase, 1)
        board.addWidget(self.read, 1)
        self.board_log = button(tr("Board log"))
        self.board_log.setToolTip(
            tr("Flash, erase and read operations on this board (logs/boards/<MAC>.log)")
        )
        self.identity = QVBoxLayout()
        self.identity.setContentsMargins(0, 0, 0, 0)
        connect = QVBoxLayout()
        connect.setSpacing(6)
        connect.addLayout(_row(tr("Port"), self.port))
        connect.addLayout(_row(tr("Baud"), self.baud))
        connect.addWidget(self.identify)
        connect.addLayout(board)
        connect.addWidget(self.board_log)
        connect.addLayout(self.identity)
        # ---- checks ----
        self.checks = QVBoxLayout()
        self.checks.setSpacing(5)
        checks = QVBoxLayout()
        checks.setSpacing(6)
        title = QLabel(tr("Checks"))
        title.setStyleSheet("font-weight: 600;")
        checks.addWidget(title)
        checks.addLayout(self.checks)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)
        top_layout.addWidget(_section(connect))
        top_layout.addWidget(_section(checks))
        top_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(top)
        # ---- action: hint, erase option, flash ----
        self.hint = HintLabel()
        self.hint.hide()
        self.erase_all = QCheckBox(tr("Erase the whole flash first"))
        self.erase_all.setToolTip(
            tr("Saved with the firmware. Clears NVS (Wi-Fi credentials and other stored data)")
        )
        self.quick = Switch()
        self.quick.setToolTip(tr(QUICK_TIP))
        self.quick.setAccessibleName(tr("Quick flash"))
        self.quick_text = label(tr("Quick flash"), tone="2")
        self.quick_help = QLabel()
        self.quick_help.setToolTip(tr(QUICK_TIP))
        self._quick_box = QWidget()
        quick = QHBoxLayout(self._quick_box)
        quick.setContentsMargins(0, 0, 0, 0)
        quick.setSpacing(4)
        quick.addWidget(self.quick_text)
        quick.addWidget(self.quick_help)
        quick.addSpacing(2)
        quick.addWidget(self.quick, 0, Qt.AlignmentFlag.AlignVCenter)
        # one row: [ ] Erase the whole flash first      Quick flash (?) [switch]; when the
        # column is too narrow for both, the switch goes to a row of its own (right aligned)
        self._options = QHBoxLayout()
        self._options.setSpacing(8)
        self._options.addWidget(self.erase_all)
        self._options.addStretch(1)
        self._options.addWidget(self._quick_box)
        self._wrapped_row = QWidget()
        self._wrapped_row.hide()
        self._wrapped = QHBoxLayout(self._wrapped_row)
        self._wrapped.setContentsMargins(0, 0, 0, 0)
        self._wrapped.addStretch(1)
        self.flash = button("", "primary")
        self._flash_text = QLabel()
        self._flash_text.setObjectName("flashtext")
        self._flash_key = QLabel()
        self._flash_key.setObjectName("keycap")
        face = QHBoxLayout(self.flash)
        face.setContentsMargins(10, 0, 10, 0)
        face.setSpacing(10)
        face.addStretch(1)
        face.addWidget(self._flash_text, 0, Qt.AlignmentFlag.AlignVCenter)
        face.addWidget(self._flash_key, 0, Qt.AlignmentFlag.AlignVCenter)
        face.addStretch(1)
        for part in (self._flash_text, self._flash_key):
            part.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress_text = label(tr("Idle"), tone="2")
        self.progress_pct = label("", tone="2")
        action = QFrame()
        action.setObjectName("raction")
        action_layout = QVBoxLayout(action)
        action_layout.setContentsMargins(12, 10, 12, 10)
        action_layout.setSpacing(6)
        action_layout.addWidget(self.hint)
        action_layout.addLayout(self._options)
        action_layout.addWidget(self._wrapped_row)
        action_layout.addWidget(self.flash)
        action_layout.addSpacing(4)
        action_layout.addWidget(self.progress)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_text, 1)
        progress_row.addWidget(self.progress_pct)
        action_layout.addLayout(progress_row)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(scroll, 1)
        layout.addWidget(action)

        self.port.activated.connect(lambda: self.port_changed.emit())
        self.baud.activated.connect(lambda: self.baud_changed.emit())
        self.identify.clicked.connect(self.identify_clicked.emit)
        self.erase.clicked.connect(self.erase_clicked.emit)
        self.read.clicked.connect(self.read_clicked.emit)
        self.board_log.clicked.connect(self.board_log_clicked.emit)
        self.flash.clicked.connect(self.flash_clicked.emit)
        self.erase_all.toggled.connect(self._erase_all_toggled)
        self.quick.toggled.connect(self.quick_toggled.emit)
        self.hint.dismissed.connect(self.hint_dismissed.emit)
        self.set_running(False)

    def apply_palette(self, p: Palette) -> None:
        self._palette = p
        self._paint_keys()
        self.quick.set_colors(p.accent, p.line, p.panel, p.fg_3)
        self.quick_help.setPixmap(icons.icon("circle-help", p.fg_3, 14).pixmap(14, 14))

    def set_quick(self, on: bool) -> None:
        """Show the Quick flash setting; changes come back as quick_toggled."""
        self.quick.blockSignals(True)
        self.quick.setChecked(on)
        self.quick.blockSignals(False)
        self.quick.update()

    # ---- ports and baud ---------------------------------------------------------------

    @property
    def wanted(self) -> PortInfo | None:
        """The chosen port, also while it is away (selected_port() is None then)."""
        return self._wanted

    def set_ports(self, ports: tuple[PortInfo, ...]) -> None:
        """Replace the list. The chosen port stays selected; while it is away a disabled
        "<device> · unavailable" item is shown and selected instead (no port selected, so
        nothing can run on another board), and the port is selected again when it comes
        back. Another port is only chosen by the user, or when none was chosen yet."""
        self._ports = ports
        self.port.clear()
        for index, port in enumerate(ports):
            self.port.addItem(port_text(port), index)
            self.port.setItemData(index, port.description, Qt.ItemDataRole.ToolTipRole)
        wanted = self._wanted
        if wanted is not None:
            back = [i for i, p in enumerate(ports) if p.device == wanted.device]
            if back:
                self._wanted = ports[back[0]]
                self.port.setCurrentIndex(back[0])
            else:
                self.port.insertItem(0, f"{wanted.device} · {tr('unavailable')}", None)
                model = self.port.model()
                if isinstance(model, QStandardItemModel):  # QComboBox's own model
                    model.item(0).setEnabled(False)  # shown, never picked
                self.port.setCurrentIndex(0)
        elif ports:
            # nothing chosen yet: prefer a USB device over e.g. Bluetooth serial ports
            usb = [i for i, p in enumerate(ports) if p.vid is not None]
            self.port.setCurrentIndex((usb or [0])[0])
            self._wanted = self.selected_port()
        else:
            self.port.addItem(tr("No serial port found"), None)
        self.port.setEnabled(not self._running and bool(ports))

    def keep_selection(self) -> None:
        """The selected port was picked (port_changed): it becomes the chosen one, and the
        placeholder of one chosen before goes."""
        picked = self.selected_port()
        if picked is not None and picked != self._wanted:
            self._wanted = picked
            self.set_ports(self._ports)

    def selected_port(self) -> PortInfo | None:
        index = self.port.currentData()
        return self._ports[index] if isinstance(index, int) and index < len(self._ports) else None

    def set_baud(self, port: PortInfo | None, default: int | None) -> None:
        """default: remembered or default rate; None for native USB."""
        self.baud.clear()
        if port is not None and not baud_adjustable(port):
            self.baud.addItem(tr("not applicable"), None)
            self.baud.setEnabled(False)
            self.baud.setToolTip(tr("Native USB: baud rate not applicable"))
            return
        rates = sorted({*BAUDS, *([default] if default else [])})
        for rate in rates:
            self.baud.addItem(str(rate), rate)
        if default is not None:
            self.baud.setCurrentIndex(self.baud.findData(default))
        self.baud.setEnabled(not self._running)
        self.baud.setToolTip("")

    def selected_baud(self) -> int | None:
        value = self.baud.currentData()
        return value if isinstance(value, int) else None

    # ---- identification ---------------------------------------------------------------

    def show_identity(self, snapshot: IdentifySnapshot | None) -> None:
        clear_layout(self.identity)
        if snapshot is None:
            note = label(
                tr(
                    "Not identified yet. After identifying, firmware for other chips is dimmed in "
                    "the sidebar."
                ),
                tone="3",
                wrap=True,
            )
            note.setStyleSheet("font-size: 12px; margin-top: 8px;")
            self.identity.addWidget(note)
            return
        info = snapshot.info
        grid = QGridLayout()
        grid.setContentsMargins(0, 8, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        security = info.security
        enabled = [
            name
            for name, on in (
                (tr("secure download mode"), security.secure_download_mode),
                (tr("flash encryption"), security.flash_encryption),
                (tr("secure boot"), security.secure_boot),
            )
            if on
        ]
        state: QWidget = (
            tag(tr("in download mode"), "ok")
            if info.in_download_mode
            else label(tr("reset, running"))
        )
        rows: list[tuple[str, QWidget]] = [
            (N_("Chip"), label(f"{chip_name(info.chip)} {info.revision}")),
            ("MAC", label(info.mac or "—", mono=True)),
            (N_("Flash"), label(tr(f"{info.flash_size} (detected)") if info.flash_size else "—")),
            (
                N_("Security"),
                label(
                    ", ".join(enabled)
                    if enabled
                    else tr("none (SDM / encryption / secure boot off)")
                ),
            ),
            (N_("Link"), label(tr(CONNECTION_LABELS[snapshot.port.connection]))),
            (N_("State"), _left(state)),
            (
                N_("Identified"),
                label(tr(f"{snapshot.time.strftime('%H:%M:%S')} · port released"), tone="3"),
            ),
        ]
        for row, (key, widget) in enumerate(rows):
            caption = label(tr(key), tone="3")
            grid.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignTop)
            widget.setStyleSheet("font-size: 12px;")
            if isinstance(widget, QLabel):
                widget.setWordWrap(True)
            grid.addWidget(widget, row, 1)
        grid.setColumnStretch(1, 1)
        self.identity.addLayout(grid)

    def set_board_actions(self, enabled: bool) -> None:
        """Erase / Read flash / Board log: only with a snapshot for the selected port and
        no job."""
        for widget in (self.erase, self.read, self.board_log):
            widget.setEnabled(enabled and not self._running)

    # ---- checks and hint --------------------------------------------------------------

    def show_checks(self, items: list[CheckItem] | None) -> None:
        """None: a project is selected, nothing to flash."""
        clear_layout(self.checks)
        if items is None:
            self.checks.addLayout(
                _check(
                    CheckItem(
                        CheckState.LATER,
                        N_("Select a firmware in the sidebar to run checks and flash."),
                    ),
                    "—",
                )
            )
            return
        for item in items:
            self.checks.addLayout(_check(item, CHECK_TEXT[item.state]))

    def set_hint(self, text: str, kind: str = "warn") -> None:
        """Shown above Flash until the user clicks it away (hint_dismissed)."""
        self.hint.setText(tr(text))
        self.hint.setProperty("hint", kind)
        _repolish(self.hint)
        self.hint.fit_text()
        self.hint.setVisible(bool(text))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_options()

    def _fit_options(self) -> None:
        """The Quick flash switch next to the erase box when both fit, else on a row of its
        own below it (an English text in the narrowest column)."""
        room = self.width() - 24  # the action frame's margins
        need = self._quick_box.sizeHint().width()
        if not self.erase_all.isHidden():
            need += self.erase_all.sizeHint().width() + self._options.spacing()
        wrap = need > room
        target, other = (self._wrapped, self._options) if wrap else (self._options, self._wrapped)
        if other.indexOf(self._quick_box) >= 0:
            other.removeWidget(self._quick_box)
            target.addWidget(self._quick_box)
        self._wrapped_row.setVisible(wrap)

    def wrapped(self) -> bool:
        """Whether the Quick flash switch is on a row of its own."""
        return self._wrapped.indexOf(self._quick_box) >= 0

    def set_erase_all(self, checked: bool, enabled: bool, visible: bool) -> None:
        """Show the selected firmware's erase_all; changes come back as erase_all_toggled."""
        self.erase_all.blockSignals(True)
        self.erase_all.setChecked(checked)
        self.erase_all.blockSignals(False)
        self.erase_all.setEnabled(enabled and not self._running)
        self.erase_all.setVisible(visible)
        self._mark_erase_all()
        self._fit_options()

    def _erase_all_toggled(self, checked: bool) -> None:
        self._mark_erase_all()
        self.erase_all_toggled.emit(checked)

    def _mark_erase_all(self) -> None:
        self.erase_all.setProperty("warn", "1" if self.erase_all.isChecked() else "0")
        _repolish(self.erase_all)

    # ---- flash and progress -----------------------------------------------------------

    def set_running(self, running: bool) -> None:
        self._running = running
        if running:
            self._flash_text.setText(tr("Cancel"))
            self._keys = ("Esc",)
            self.flash.setProperty("kind", "danger")
            self.flash.setEnabled(True)
        else:
            self._flash_text.setText(tr("Flash"))
            self._keys = ("Ctrl", "Enter")
            self.flash.setProperty("kind", "primary")
        self.flash.setAccessibleName(f"{self._flash_text.text()} ({'+'.join(self._keys)})")
        for widget in (self.flash, self._flash_text):
            _repolish(widget)
        self._paint_keys()
        self.erase_all.setEnabled(not running and self.erase_all.isEnabled())
        self.quick.setEnabled(not running)
        self.identify.setEnabled(not running)
        if running:
            self.set_board_actions(False)
        self.port.setEnabled(not running and bool(self._ports))
        self.baud.setEnabled(not running and self.selected_baud() is not None)

    def set_flash_enabled(self, enabled: bool) -> None:
        if not self._running and self.flash.isEnabled() != enabled:
            self.flash.setEnabled(enabled)
            self._paint_keys()

    def _paint_keys(self) -> None:
        """The shortcut on the Flash button as key icons, in the button's text colour."""
        p = self._palette
        if p is None:
            return
        enabled = p.accent_fg if self.flash.isEnabled() else p.fg_3
        color = "#ffffff" if self._running else enabled
        self._flash_key.setPixmap(keys_pixmap(self._keys, color))

    def show_progress(self, percent: int, text: str) -> None:
        self.progress.setValue(percent)
        self.progress_text.setText(tr(text))
        self.progress_pct.setText(f"{percent}%" if percent or self._running else "")


def _row(caption: str, widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(6)
    text = label(caption, tone="3")
    text.setFixedWidth(52)
    row.addWidget(text)
    row.addWidget(widget, 1)
    return row


def _section(layout: QVBoxLayout) -> QWidget:
    frame = QFrame()
    frame.setObjectName("section")
    layout.setContentsMargins(12, 10, 12, 10)
    frame.setLayout(layout)
    return frame


def _check(item: CheckItem, state_text: str) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    pill = QLabel(state_text)
    pill.setProperty("check", item.state.value)
    pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
    row.addWidget(pill, 0, Qt.AlignmentFlag.AlignTop)
    text = QVBoxLayout()
    text.setSpacing(1)
    title = label(tr(item.title), tone="2", wrap=True)
    title.setStyleSheet("font-size: 12px;")
    # the first line sits level with the pill: same height, text centred in it
    title.setMinimumHeight(pill.sizeHint().height())
    title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    text.addWidget(title)
    if item.detail:
        detail = label(tr(item.detail), tone="3", wrap=True)
        detail.setStyleSheet("font-size: 12px;")
        text.addWidget(detail)
    row.addLayout(text, 1)
    return row


def _left(widget: QWidget) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget)
    row.addStretch(1)
    return box


def _repolish(widget: QWidget) -> None:
    """Apply style sheet rules that depend on a property just changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
