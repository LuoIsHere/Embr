"""The Records tab builds the newest records only; a button below them shows earlier ones,
a page at a time, without building the shown rows again."""

from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from embr.core.history import FlashRecord, Operation, RecordResult
from embr.core.storage.ids import new_id
from embr.ui.window import EmbrWindow
from embr.ui.workspace.records import PAGE


def _add(window: EmbrWindow, firmware_id: str, count: int, start: int = 0) -> None:
    for n in range(start, start + count):
        window.context.history.add(
            FlashRecord(
                id=new_id(),
                operation=Operation.FLASH,
                timestamp=f"2026-01-01T{n // 60:02d}:{n % 60:02d}:00",
                result=RecordResult.OK,
                firmware_id=firmware_id,
                port="COM7",
            )
        )


def _widgets(window: EmbrWindow) -> list[QWidget]:
    rows = window.firmware_view.records.list
    return [w for i in range(rows.count()) if (item := rows.itemAt(i)) and (w := item.widget())]


def _more(window: EmbrWindow) -> QPushButton | None:
    found = [w for w in _widgets(window) if isinstance(w, QPushButton)]
    return found[0] if found else None


def _records(window: EmbrWindow) -> list[QWidget]:
    return [w for w in _widgets(window) if not isinstance(w, QPushButton)]


def test_earlier_records_come_a_page_at_a_time(window: EmbrWindow) -> None:
    firmware = window.library.create_firmware("fw").id
    _add(window, firmware, PAGE * 2 + 20)
    window.select_firmware(firmware)

    assert len(_records(window)) == PAGE
    more = _more(window)
    assert more is not None and more.text() == f"Show {PAGE} earlier records"
    first = _records(window)

    more.click()
    QApplication.processEvents()
    assert _records(window)[:PAGE] == first  # the shown rows stay
    assert len(_records(window)) == PAGE * 2
    more = _more(window)
    assert more is not None and more.text() == "Show 20 earlier records"

    more.click()
    QApplication.processEvents()
    assert len(_records(window)) == PAGE * 2 + 20
    assert _more(window) is None


def test_a_new_record_keeps_the_rows_opened_and_another_firmware_starts_again(
    window: EmbrWindow,
) -> None:
    firmware = window.library.create_firmware("fw").id
    other = window.library.create_firmware("other").id
    _add(window, firmware, PAGE + 1)
    window.select_firmware(firmware)
    more = _more(window)
    assert more is not None and more.text() == "Show 1 earlier record"
    more.click()
    QApplication.processEvents()

    _add(window, firmware, 1, start=PAGE + 1)  # a job ended
    window.changed()
    assert len(_records(window)) == PAGE + 1
    more = _more(window)
    assert more is not None and more.text() == "Show 1 earlier record"

    window.select_firmware(other)
    window.select_firmware(firmware)
    assert len(_records(window)) == PAGE
