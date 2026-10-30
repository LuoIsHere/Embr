"""The log under the workspace in the centre column (design doc §6.1 "日志"): esptool output
of the current job, coloured by level, with Expand and Log folder."""

import html

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from embr.i18n import tr
from embr.ui.theme import Palette, mono_font
from embr.ui.widgets import button

LOG_HEIGHT = (100, 220)
"""min, default."""


class LogPanel(QWidget):
    expand_toggled = Signal(bool)
    """True: the log takes the whole centre column."""
    log_folder_clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._palette: Palette | None = None
        self.expand = button(tr("Expand"), "ibtn")
        self.log_folder = button(tr("Log folder"), "ibtn")
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setFont(mono_font(11))
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        head = QFrame()
        head.setObjectName("loghead")
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(12, 6, 12, 6)
        caption = QLabel(tr("Log"))
        caption.setStyleSheet("font-weight: 600;")
        head_layout.addWidget(caption, 1)
        head_layout.addWidget(self.expand)
        head_layout.addWidget(self.log_folder)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(head)
        layout.addWidget(self.log, 1)
        self.setMinimumHeight(LOG_HEIGHT[0])
        self.expand.clicked.connect(self._toggle_expand)
        self.log_folder.clicked.connect(self.log_folder_clicked.emit)

    def apply_palette(self, p: Palette) -> None:
        self._palette = p

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
        on = self.expand.text() == tr("Expand")
        self.expand.setText(tr("Restore") if on else tr("Expand"))
        self.expand_toggled.emit(on)
