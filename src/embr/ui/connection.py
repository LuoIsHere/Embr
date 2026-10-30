"""Right column: port and baud, identify, checks, flash / cancel with progress, and the log
(design doc §6.1 "右侧栏")."""

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from embr.app.checklist import CheckItem, CheckState
from embr.app.identify import IdentifySnapshot
from embr.core.serial import ConnectionType, PortInfo, baud_adjustable
from embr.ui.icons import keys_pixmap
from embr.ui.sidebar import chip_name
from embr.ui.theme import Palette, mono_font
from embr.ui.widgets import button, clear_layout, label, tag

BAUDS = (115200, 230400, 460800, 921600, 1500000, 2000000)
LOG_HEIGHT = (100, 220)
"""min, default."""
CONNECTION_LABELS = {
    ConnectionType.UART_BRIDGE: "UART bridge",
    ConnectionType.USB_SERIAL_JTAG: "USB-Serial/JTAG",
    ConnectionType.USB_OTG: "USB-OTG",
    ConnectionType.UNKNOWN: "serial port",
}
CHECK_TEXT = {
    CheckState.PASS: "PASS",
    CheckState.WARN: "WARN",
    CheckState.REFUSE: "REFUSE",
    CheckState.LATER: "LATER",
}


def port_text(port: PortInfo) -> str:
    text = f"{port.device} · {CONNECTION_LABELS[port.connection]}"
    if port.vid is not None and port.pid is not None:
        text += f" · {port.vid:04X}:{port.pid:04X}"
    return text


class HintLabel(QLabel):
    """The hint above Flash; a click dismisses it."""

    dismissed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWordWrap(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to dismiss")

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
    hint_dismissed = Signal()
    expand_toggled = Signal(bool)
    log_folder_clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("right")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._palette: Palette | None = None
        self._ports: tuple[PortInfo, ...] = ()
        self._running = False
        self._keys: tuple[str, ...] = ("Ctrl", "Enter")

        # ---- port, baud, identify ----
        self.port = QComboBox()
        self.baud = QComboBox()
        self.identify = button("Connect && identify")
        board_hint = "Available after Connect & identify on this port"
        self.erase = button("Erase flash…")
        self.erase.setToolTip(board_hint)
        self.read = button("Read flash…")
        self.read.setToolTip(board_hint)
        board = QHBoxLayout()
        board.setSpacing(6)
        board.addWidget(self.erase, 1)
        board.addWidget(self.read, 1)
        self.board_log = button("Board log")
        self.board_log.setToolTip(
            "Flash, erase and read operations on this board (logs/boards/<MAC>.log)"
        )
        self.identity = QVBoxLayout()
        self.identity.setContentsMargins(0, 0, 0, 0)
        connect = QVBoxLayout()
        connect.setSpacing(6)
        connect.addLayout(_row("Port", self.port))
        connect.addLayout(_row("Baud", self.baud))
        connect.addWidget(self.identify)
        connect.addLayout(board)
        connect.addWidget(self.board_log)
        connect.addLayout(self.identity)
        # ---- checks ----
        self.checks = QVBoxLayout()
        self.checks.setSpacing(5)
        checks = QVBoxLayout()
        checks.setSpacing(6)
        title = QLabel("Checks")
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
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(top)
        # ---- action: hint, erase option, flash ----
        self.hint = HintLabel()
        self.hint.hide()
        self.erase_all = QCheckBox("Erase the whole flash first")
        self.erase_all.setToolTip(
            "Saved with the firmware. Clears NVS (Wi-Fi credentials and other stored data)"
        )
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
        self.progress_text = label("Idle", tone="2")
        self.progress_pct = label("", tone="2")
        action = QFrame()
        action.setObjectName("raction")
        action_layout = QVBoxLayout(action)
        action_layout.setContentsMargins(12, 10, 12, 10)
        action_layout.setSpacing(6)
        action_layout.addWidget(self.hint)
        action_layout.addWidget(self.erase_all)
        action_layout.addWidget(self.flash)
        action_layout.addSpacing(4)
        action_layout.addWidget(self.progress)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_text, 1)
        progress_row.addWidget(self.progress_pct)
        action_layout.addLayout(progress_row)
        upper = QWidget()
        upper_layout = QVBoxLayout(upper)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(0)
        upper_layout.addWidget(scroll, 1)
        upper_layout.addWidget(action)
        # ---- log ----
        self.expand = button("Expand", "ibtn")
        self.log_folder = button("Log folder", "ibtn")
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setFont(mono_font(11))
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        head = QFrame()
        head.setObjectName("loghead")
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(12, 6, 12, 6)
        caption = QLabel("Log")
        caption.setStyleSheet("font-weight: 600;")
        head_layout.addWidget(caption, 1)
        head_layout.addWidget(self.expand)
        head_layout.addWidget(self.log_folder)
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)
        log_layout.addWidget(head)
        log_layout.addWidget(self.log, 1)
        log_box.setMinimumHeight(LOG_HEIGHT[0])
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.setHandleWidth(7)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(upper)
        self.split.addWidget(log_box)
        self.split.setStretchFactor(0, 1)
        self.split.setSizes([500, LOG_HEIGHT[1]])
        self._upper = upper
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.split)

        self.port.activated.connect(lambda: self.port_changed.emit())
        self.baud.activated.connect(lambda: self.baud_changed.emit())
        self.identify.clicked.connect(self.identify_clicked.emit)
        self.erase.clicked.connect(self.erase_clicked.emit)
        self.read.clicked.connect(self.read_clicked.emit)
        self.board_log.clicked.connect(self.board_log_clicked.emit)
        self.flash.clicked.connect(self.flash_clicked.emit)
        self.erase_all.toggled.connect(self._erase_all_toggled)
        self.hint.dismissed.connect(self.hint_dismissed.emit)
        self.expand.clicked.connect(self._toggle_expand)
        self.log_folder.clicked.connect(self.log_folder_clicked.emit)
        self.set_running(False)

    def apply_palette(self, p: Palette) -> None:
        self._palette = p
        self._paint_keys()

    # ---- ports and baud ---------------------------------------------------------------

    def set_ports(self, ports: tuple[PortInfo, ...]) -> None:
        """Replace the list, keeping the selected device when it is still there."""
        current = self.selected_port()
        self._ports = ports
        self.port.clear()
        if not ports:
            self.port.addItem("No serial port found", None)
            self.port.setEnabled(False)
            return
        self.port.setEnabled(not self._running)
        for index, port in enumerate(ports):
            self.port.addItem(port_text(port), index)
            self.port.setItemData(index, port.description, Qt.ItemDataRole.ToolTipRole)
        keep = [
            i for i, p in enumerate(ports) if current is not None and p.device == current.device
        ]
        # without a previous choice prefer a USB device over e.g. Bluetooth serial ports
        usb = [i for i, p in enumerate(ports) if p.vid is not None]
        self.port.setCurrentIndex((keep or usb or [0])[0])

    def selected_port(self) -> PortInfo | None:
        index = self.port.currentData()
        return self._ports[index] if isinstance(index, int) and index < len(self._ports) else None

    def set_baud(self, port: PortInfo | None, default: int | None) -> None:
        """default: remembered or default rate; None for native USB."""
        self.baud.clear()
        if port is not None and not baud_adjustable(port):
            self.baud.addItem("not applicable", None)
            self.baud.setEnabled(False)
            self.baud.setToolTip("Native USB: baud rate not applicable")
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
                "Not identified yet. After identifying, firmware for other chips is dimmed in "
                "the sidebar.",
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
                ("secure download mode", security.secure_download_mode),
                ("flash encryption", security.flash_encryption),
                ("secure boot", security.secure_boot),
            )
            if on
        ]
        state: QWidget = (
            tag("in download mode", "ok") if info.in_download_mode else label("reset, running")
        )
        rows: list[tuple[str, QWidget]] = [
            ("Chip", label(f"{chip_name(info.chip)} {info.revision}")),
            ("MAC", label(info.mac or "—", mono=True)),
            ("Flash", label(f"{info.flash_size} (detected)" if info.flash_size else "—")),
            (
                "Security",
                label(
                    ", ".join(enabled) if enabled else "none (SDM / encryption / secure boot off)"
                ),
            ),
            ("Link", label(CONNECTION_LABELS[snapshot.port.connection])),
            ("State", _left(state)),
            (
                "Identified",
                label(f"{snapshot.time.strftime('%H:%M:%S')} · port released", tone="3"),
            ),
        ]
        for row, (key, widget) in enumerate(rows):
            caption = label(key, tone="3")
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
                        "Select a firmware in the sidebar to run checks and flash.",
                    ),
                    "—",
                )
            )
            return
        for item in items:
            self.checks.addLayout(_check(item, CHECK_TEXT[item.state]))

    def set_hint(self, text: str, kind: str = "warn") -> None:
        """Shown above Flash until the user clicks it away (hint_dismissed)."""
        self.hint.setText(text)
        self.hint.setProperty("hint", kind)
        _repolish(self.hint)
        self.hint.setVisible(bool(text))

    def set_erase_all(self, checked: bool, enabled: bool, visible: bool) -> None:
        """Show the selected firmware's erase_all; changes come back as erase_all_toggled."""
        self.erase_all.blockSignals(True)
        self.erase_all.setChecked(checked)
        self.erase_all.blockSignals(False)
        self.erase_all.setEnabled(enabled and not self._running)
        self.erase_all.setVisible(visible)
        self._mark_erase_all()

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
            self._flash_text.setText("Cancel")
            self._keys = ("Esc",)
            self.flash.setProperty("kind", "danger")
            self.flash.setEnabled(True)
        else:
            self._flash_text.setText("Flash")
            self._keys = ("Ctrl", "Enter")
            self.flash.setProperty("kind", "primary")
        self.flash.setAccessibleName(f"{self._flash_text.text()} ({'+'.join(self._keys)})")
        for widget in (self.flash, self._flash_text):
            _repolish(widget)
        self._paint_keys()
        self.erase_all.setEnabled(not running and self.erase_all.isEnabled())
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
        self.progress_text.setText(text)
        self.progress_pct.setText(f"{percent}%" if percent or self._running else "")

    # ---- log --------------------------------------------------------------------------

    def clear_log(self) -> None:
        self.log.clear()

    def log_lines(self, lines: list[tuple[str, str]]) -> None:
        """(level, text): level info, note, warning, error, ok, dim."""
        p = self._palette
        colors = (
            {
                "note": p.log_note,
                "warning": p.log_warn,
                "error": p.log_error,
                "ok": p.log_ok,
                "dim": p.log_dim,
            }
            if p
            else {}
        )
        for level, text in lines:
            color = colors.get(level)
            body = html.escape(text)
            style = "white-space: pre-wrap;" + (f" color: {color};" if color else "")
            self.log.appendHtml(f'<span style="{style}">{body}</span>')
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _toggle_expand(self) -> None:
        on = self.expand.text() == "Expand"
        self.expand.setText("Restore" if on else "Expand")
        self.expand_toggled.emit(on)


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
    title = label(item.title, tone="2", wrap=True)
    title.setStyleSheet("font-size: 12px;")
    text.addWidget(title)
    if item.detail:
        detail = label(item.detail, tone="3", wrap=True)
        detail.setStyleSheet("font-size: 11px;")
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
