"""The Records tab: the flash records of a firmware."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from embr.core.firmware.model import Firmware
from embr.core.history import FlashRecord, RecordResult
from embr.i18n import tr
from embr.ui.connection import CONNECTION_LABELS
from embr.ui.owner import Owner
from embr.ui.widgets import (
    ClickRow,
    clear_layout,
    label,
    small,
    tag,
)

RESULT_TAGS = {
    RecordResult.OK: "ok",
    RecordResult.REFUSED: "warn",
    RecordResult.FAILED: "bad",
    RecordResult.CANCELLED: "bad",
}


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
        small(hint)
        layout.addWidget(hint)
        layout.addLayout(self.list)
        layout.addStretch(1)
        self._shown: tuple[str, int] | None = None
        """(firmware id, history version) of the rows shown."""

    def show_firmware(self, firmware: Firmware) -> None:
        """Rows for the firmware's records; kept as they are while neither the firmware nor
        the records changed (records are snapshots: nothing else alters a row)."""
        history = self._owner.context.history
        shown = (firmware.id, history.version)
        if shown == self._shown:
            return
        self._shown = shown
        clear_layout(self.list)
        records = history.query(firmware_id=firmware.id)
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


class RecordRow(ClickRow):
    clicked = Signal(str, str)
    """log file name, segment id."""

    def __init__(self, log_file: str, segment_id: str) -> None:
        super().__init__()
        self._target = (log_file, segment_id)
        self.pressed.connect(lambda: self.clicked.emit(*self._target))


def _summary(record: FlashRecord) -> str:
    if record.result == RecordResult.REFUSED and record.refuse_reason:
        return record.refuse_reason
    seconds = record.duration_ms / 1000
    written = sum(1 for e in record.entries if not e.skipped)
    skipped = len(record.entries) - written
    text = (
        tr(f"1 entry, {seconds:.1f} s")
        if len(record.entries) == 1
        else tr(f"{len(record.entries)} entries, {seconds:.1f} s")
    )
    if skipped:
        text += tr(f" · {skipped} already in flash")
    if record.reset_after:
        text += tr(f" · after={record.reset_after}")
    return text
