"""ui/widgets ClickRow: a press anywhere on a record row or a project's firmware row reports
what it stands for."""

from PySide6.QtCore import QCoreApplication, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from embr.core.firmware.model import Firmware, Source
from embr.ui.project_view import FirmwareLink
from embr.ui.widgets import ClickRow
from embr.ui.workspace import RecordRow


def _press(row: ClickRow) -> None:
    point = QPointF(5, 5)
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        point,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QCoreApplication.sendEvent(row, event)


def test_rows_report_what_was_pressed(qapp: QCoreApplication) -> None:
    pressed: list[tuple[str, ...]] = []

    def on_record(log_file: str, segment_id: str) -> None:
        pressed.append((log_file, segment_id))

    def on_link(firmware_id: str) -> None:
        pressed.append((firmware_id,))

    record = RecordRow("2026-11-17.log", "seg-1")
    record.clicked.connect(on_record)
    firmware = Firmware(id="fw-1", name="App", project_id=None, device="", source=Source.MANUAL)
    link = FirmwareLink(firmware, ())
    link.clicked.connect(on_link)

    _press(record)
    _press(link)
    assert pressed == [("2026-11-17.log", "seg-1"), ("fw-1",)]
    assert record.objectName() == link.objectName() == "rec"  # styled as rows
    assert record.cursor().shape() == Qt.CursorShape.PointingHandCursor
