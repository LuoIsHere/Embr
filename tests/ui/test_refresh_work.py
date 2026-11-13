"""Work a refresh leaves out: the records page stays as built while its records are
unchanged, and the sidebar searches once typing pauses."""

from PySide6.QtTest import QTest

from embr.core.history import FlashRecord, Operation, RecordResult
from embr.core.storage.ids import new_id
from embr.ui.sidebar import SEARCH_DELAY_MS
from embr.ui.window import EmbrWindow


def _record(firmware_id: str, minute: int) -> FlashRecord:
    return FlashRecord(
        id=new_id(),
        operation=Operation.FLASH,
        timestamp=f"2026-01-01T12:{minute:02d}:00",
        result=RecordResult.OK,
        firmware_id=firmware_id,
        port="COM7",
    )


def _rows(window: EmbrWindow) -> list[object]:
    rows = window.firmware_view.records.list
    return [item.widget() for i in range(rows.count()) if (item := rows.itemAt(i)) is not None]


def test_records_page_is_rebuilt_only_when_its_records_change(window: EmbrWindow) -> None:
    first = window.library.create_firmware("First")
    second = window.library.create_firmware("Second")
    history = window.context.history
    history.add(_record(first.id, 1))
    window.select_firmware(first.id)
    shown = _rows(window)
    assert len(shown) == 1

    window.changed()
    assert _rows(window) == shown  # the same row widgets, not rebuilt

    history.add(_record(first.id, 2))
    window.changed()
    assert len(_rows(window)) == 2

    window.select_firmware(second.id)
    assert len(_rows(window)) == 1  # "No flash records yet."
    assert _rows(window) != shown


def test_search_waits_for_typing_to_pause(window: EmbrWindow) -> None:
    for name in ("alpha", "beta"):
        window.library.create_firmware(name)
    panel = window.sidebar_panel
    sent: list[str] = []
    panel.query_changed.connect(sent.append)
    for text in ("a", "al", "alp"):
        panel.search.setText(text)
    assert sent == []
    QTest.qWait(SEARCH_DELAY_MS + 100)
    assert sent == ["alp"]
