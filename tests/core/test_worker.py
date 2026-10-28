"""Worker process (`python -m embr --worker`) without hardware; each run has a timeout."""

from dataclasses import replace

import pytest
from embr_testkit import idf_entries, missing_port, run_worker

from embr.core.flasher.checks import FILES, RESET, CheckLevel
from embr.core.flasher.plan import FlashPlan, FlashSettings, IdentifyPlan, PlanEntry
from embr.core.flasher.protocol import (
    CheckEvent,
    ErrorEvent,
    Event,
    InfoEvent,
    LogEvent,
    ResultEvent,
    ResultStatus,
    Task,
    encode_task,
)
from embr.core.serial import ConnectionType


def _plan(entries: list[PlanEntry]) -> FlashPlan:
    return FlashPlan(
        port=missing_port(),
        connection=ConnectionType.UART_BRIDGE,
        baud=460800,
        chip="esp32s3",
        before="default-reset",
        after="hard-reset",
        stub=True,
        flash=FlashSettings(),
        entries=tuple(entries),
        erase_all=False,
        skip_flashed=True,
    )


def _terminal(events: list[Event]) -> Event:
    """The single ResultEvent/ErrorEvent, which must be the last event."""
    terminal = [e for e in events if isinstance(e, ResultEvent | ErrorEvent)]
    assert len(terminal) == 1 and events[-1] is terminal[0], events
    return terminal[0]


def test_changed_file_refused_before_opening_port() -> None:
    entries = idf_entries("esp32s3_hello")
    entries[1] = replace(entries[1], sha256="0" * 64)
    code, events = run_worker(encode_task(_plan(entries)))
    checks = [(e.name, e.level) for e in events if isinstance(e, CheckEvent)]
    assert checks == [(RESET, CheckLevel.PASS), (FILES, CheckLevel.REFUSE)]
    final = _terminal(events)
    assert isinstance(final, ResultEvent) and final.status is ResultStatus.REFUSED
    assert code == 0


@pytest.mark.parametrize("kind", ["flash", "identify"])
def test_missing_port_ends_cleanly(kind: str) -> None:
    task: Task = (
        _plan(idf_entries("esp32s3_hello"))
        if kind == "flash"
        else IdentifyPlan(missing_port(), ConnectionType.UART_BRIDGE, "default-reset", "hard-reset")
    )
    code, events = run_worker(encode_task(task))
    final = _terminal(events)
    if isinstance(final, ResultEvent):
        assert final.status in (ResultStatus.FAILED, ResultStatus.REFUSED)
        assert code == 0
    else:
        assert code != 0
    assert not any(isinstance(e, InfoEvent) for e in events)


@pytest.mark.parametrize("text", ["", "this is not json", '{"hello": "world"}'])
def test_malformed_task_gives_error_and_nonzero_exit(text: str) -> None:
    code, events = run_worker(text)
    assert code != 0
    assert isinstance(_terminal([e for e in events if not isinstance(e, LogEvent)]), ErrorEvent)
