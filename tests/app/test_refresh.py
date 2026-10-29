"""app/refresh: which firmware the periodic re-check visits (design doc §3.4 "检测时机")."""

from embr.app.context import AppContext
from embr.app.refresh import ROUND_LIMIT, RefreshSchedule
from embr.core.history import FlashRecord, Operation, RecordResult
from embr.core.storage.ids import new_id


def test_round_puts_the_selection_first_without_duplicates() -> None:
    schedule = RefreshSchedule(limit=3)
    for firmware_id in ("a", "b", "c"):
        schedule.touch(firmware_id)
    everything = {"a", "b", "c", "x"}

    assert ROUND_LIMIT == 8
    assert schedule.round("b", everything) == ["b", "c", "a"]
    assert schedule.round(None, everything) == ["c", "b", "a"]
    assert schedule.round("x", everything) == ["x", "c", "b"]  # at most `limit`
    assert schedule.round("gone", {"a", "c"}) == ["c", "a"]  # deleted ids are skipped

    schedule.touch("a")
    schedule.forget("c")
    assert schedule.round(None, everything) == ["a", "b"]


def test_seed_takes_the_newest_flashed_firmware(context: AppContext) -> None:
    flashed = ["f1", "f2", None, "f1", "f3"]  # oldest first; None: no firmware (erase)
    for second, firmware_id in enumerate(flashed):
        context.history.add(
            FlashRecord(
                id=new_id(),
                operation=Operation.FLASH if firmware_id else Operation.ERASE,
                timestamp=f"2026-01-01T12:00:0{second}",
                result=RecordResult.OK,
                firmware_id=firmware_id,
            )
        )
    everything = {"f1", "f2", "f3"}

    full = RefreshSchedule()
    full.seed(context.history)
    assert full.round(None, everything) == ["f3", "f1", "f2"]
    short = RefreshSchedule(limit=2)
    short.seed(context.history)
    assert short.round(None, everything) == ["f3", "f1"]
