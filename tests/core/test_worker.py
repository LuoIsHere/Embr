"""Worker process (`python -m embr --worker`) without hardware; each run has a timeout."""

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from embr_testkit import WORKER_TIMEOUT, idf_entries, missing_port, run_worker

from embr.core.flasher.checks import FILES, RESET, CheckLevel
from embr.core.flasher.plan import FlashPlan, FlashSettings, IdentifyPlan, PlanEntry
from embr.core.flasher.protocol import (
    CheckEvent,
    ErrorEvent,
    Event,
    InfoEvent,
    LogEvent,
    LogLevel,
    ProgressEvent,
    ResultEvent,
    ResultStatus,
    Task,
    encode_task,
)
from embr.core.flasher.worker.logger import EntryCursor, install
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


_TIMEOUT_PROBE = """
import io, sys
sys.stdin = io.StringIO("not json")
import embr.main
embr.main.main(["--worker"])
import esptool.loader
print("DEFAULT_TIMEOUT=" + repr(esptool.loader.DEFAULT_TIMEOUT))
"""


def test_worker_ignores_user_esptool_config(tmp_path: Path) -> None:
    hostile = tmp_path / "esptool.cfg"
    hostile.write_text("[esptool]\ntimeout = 99\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", _TIMEOUT_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WORKER_TIMEOUT,
        cwd=tmp_path,
        env={**os.environ, "ESPTOOL_CFGFILE": str(hostile)},
        check=False,
    )
    marked = [line for line in proc.stdout.splitlines() if line.startswith("DEFAULT_TIMEOUT=")]
    assert len(marked) == 1, (proc.stdout, proc.stderr)
    assert float(marked[0].removeprefix("DEFAULT_TIMEOUT=")) == 3.0


@pytest.fixture
def esptool_log() -> Iterator[Any]:
    """esptool's global logger, put back as it was afterwards (class and attributes)."""
    import esptool.logger

    log: Any = esptool.logger.log
    saved_class, saved_dict = log.__class__, dict(log.__dict__)
    yield log
    log.__class__ = saved_class
    log.__dict__.clear()
    log.__dict__.update(saved_dict)


def test_installed_logger_turns_esptool_output_into_events(esptool_log: Any) -> None:
    # esptool 5.3.1's set_logger() keeps the global object's attributes (design doc §12.1)
    events: list[Event] = []
    cursor = EntryCursor()
    install(events.append, cursor)
    cursor.index, cursor.address = 1, 0x10000  # the worker moves the cursor afterwards

    esptool_log.print("Connecting", end="")
    esptool_log.print("...", "done")
    esptool_log.print("partial", end="")
    esptool_log.note("n")
    esptool_log.warning("w")
    esptool_log.error("e")
    esptool_log.stage()
    esptool_log.stage(finish=True)
    esptool_log.progress_bar(50, 100, prefix="Writing")

    assert events == [
        LogEvent(LogLevel.INFO, "Connecting... done"),
        LogEvent(LogLevel.INFO, "partial"),
        LogEvent(LogLevel.NOTE, "n"),
        LogEvent(LogLevel.WARNING, "w"),
        LogEvent(LogLevel.ERROR, "e"),
        LogEvent(LogLevel.STAGE, "begin"),
        LogEvent(LogLevel.STAGE, "end"),
        ProgressEvent(1, 0x10000, 50, 100, "Writing"),
    ]
    assert cursor.progressed
