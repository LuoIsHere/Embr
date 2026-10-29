"""Real-board tests; each skips unless its port variable is set:
EMBR_TEST_PORT_UART (ESP32/ESP32-S3 on a UART bridge), EMBR_TEST_PORT_USB (native USB),
EMBR_TEST_PORT_P4 (ESP32-P4). They write the hello_world fixtures to the board. Erase is
never run on a board (it would destroy its firmware); read_flash only reads a small region."""

import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest
import serial
from embr_testkit import IDF, idf_entries, idf_expect, make_entry

from embr.core.flasher.checks import REVISION, CheckLevel
from embr.core.flasher.plan import (
    FlashPlan,
    PlanEntry,
    ReadPlan,
    build_flash_plan,
    build_identify_plan,
    build_read_plan,
)
from embr.core.flasher.protocol import (
    CheckEvent,
    EntryEvent,
    EntryStatus,
    Event,
    InfoEvent,
    LogEvent,
    ProgressEvent,
    ResultEvent,
    ResultStatus,
    Task,
)
from embr.core.flasher.runner import start
from embr.core.serial import ConnectionType, list_ports

pytestmark = pytest.mark.hardware
PORT_VARS = ["EMBR_TEST_PORT_UART", "EMBR_TEST_PORT_USB", "EMBR_TEST_PORT_P4"]


def _port(var: str) -> str:
    port = os.environ.get(var, "").strip()
    if not port:
        pytest.skip(f"{var} not set")
    return port


def _run(task: Task, cancel_on_progress: bool = False) -> list[Event]:
    events: list[Event] = []
    progress = threading.Event()

    def on_event(event: Event) -> None:
        events.append(event)
        if isinstance(event, ProgressEvent):
            progress.set()

    handle = start(task, on_event)
    try:
        if cancel_on_progress:
            assert progress.wait(600), "no progress before cancel"
            started = time.monotonic()
            handle.cancel()
            assert handle.wait(5) and time.monotonic() - started < 1.0
        assert handle.wait(600), "job did not finish"
    finally:
        if not handle.finished:
            handle.cancel()
            handle.wait(5)
    assert isinstance(events[-1], ResultEvent), events[-1]
    return events


def _status(events: list[Event]) -> ResultStatus:
    final = events[-1]
    assert isinstance(final, ResultEvent)
    return final.status


def _setup(var: str) -> tuple[str, InfoEvent, list[PlanEntry]]:
    port = _port(var)
    connection = next(p.connection for p in list_ports() if p.device.upper() == port.upper())
    events = _run(build_identify_plan(port, connection))
    info = next(e for e in events if isinstance(e, InfoEvent))
    if info.chip == "esp32p4":
        sample = "esp32p4_rev3_hello" if info.major >= 3 else "esp32p4_rev1_hello"
    elif (IDF / f"{info.chip}_hello").is_dir():
        sample = f"{info.chip}_hello"
    else:
        pytest.skip(f"no fixture for {info.chip}")
    return port, info, idf_entries(sample)


def _plan(
    port: str, info: InfoEvent, entries: list[PlanEntry], erase_all: bool = False
) -> FlashPlan:
    connection = next(p.connection for p in list_ports() if p.device.upper() == port.upper())
    return build_flash_plan(
        port=port,
        connection=connection,
        chip=info.chip,
        entries=entries,
        before="default-reset",
        after="hard-reset",
        erase_all=erase_all,
    )


def _flash(plan: FlashPlan) -> dict[int, EntryStatus]:
    """Flash, check progress events, return the status of each entry."""
    events = _run(plan)
    assert _status(events) is ResultStatus.OK
    last: tuple[int, int] = (-1, -1)
    for event in events:
        if isinstance(event, ProgressEvent):
            assert event.address == plan.entries[event.entry_index].address
            assert (event.entry_index, event.cur) >= last  # entries in order, cur rising
            last = (event.entry_index, event.cur)
    statuses = {e.entry_index: e.status for e in events if isinstance(e, EntryEvent)}
    assert sorted(statuses) == list(range(len(plan.entries)))
    return statuses


@pytest.mark.parametrize("var", PORT_VARS)
def test_second_flash_skips_and_changed_entry_is_written(var: str, tmp_path: Path) -> None:
    port, info, entries = _setup(var)
    _flash(_plan(port, info, entries))
    skipped = dict.fromkeys(range(len(entries)), EntryStatus.SKIPPED)
    assert _flash(_plan(port, info, entries)) == skipped

    app = len(entries) - 1  # highest address
    copy = tmp_path / "app.bin"
    shutil.copyfile(entries[app].path, copy)
    data = bytearray(copy.read_bytes())
    data[-1] ^= 0xFF
    copy.write_bytes(bytes(data))
    changed = [*entries[:app], make_entry(entries[app].address, copy)]
    assert _flash(_plan(port, info, changed)) == skipped | {app: EntryStatus.WRITTEN}
    _flash(_plan(port, info, entries))  # restore


@pytest.mark.parametrize("var", PORT_VARS)
def test_log_has_connect_write_verify_reset_in_order(var: str) -> None:
    port, info, entries = _setup(var)
    events = _run(_plan(port, info, entries, erase_all=True))  # erase_all: really writes
    assert _status(events) is ResultStatus.OK
    lines = [e.text for e in events if isinstance(e, LogEvent)]
    position = 0
    for pattern in ("connect", "wrot|writing", "verif", "reset"):
        found = [i for i in range(position, len(lines)) if re.search(pattern, lines[i], re.I)]
        assert found, f"no {pattern!r} line after line {position}: {lines}"
        position = found[0] + 1


def test_p4_revision_mismatch_refused_without_writing() -> None:
    port, info, _ = _setup("EMBR_TEST_PORT_P4")
    assert info.chip == "esp32p4"
    wrong = "esp32p4_rev3_hello" if info.major < 3 else "esp32p4_rev1_hello"
    events = _run(_plan(port, info, idf_entries(wrong)))
    assert _status(events) is ResultStatus.REFUSED
    assert any(
        isinstance(e, CheckEvent) and e.name == REVISION and e.level is CheckLevel.REFUSE
        for e in events
    )
    assert not any(isinstance(e, ProgressEvent | EntryEvent) for e in events)


@pytest.mark.parametrize("var", PORT_VARS)
def test_port_reopenable_after_cancel(var: str) -> None:
    port, info, entries = _setup(var)
    events = _run(_plan(port, info, entries, erase_all=True), cancel_on_progress=True)
    assert _status(events) is ResultStatus.CANCELLED
    deadline = time.monotonic() + 10
    while True:
        try:
            serial.Serial(port).close()
            break
        except serial.SerialException:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
    _flash(_plan(port, info, entries))  # leave complete firmware on the board


def _assert_released(port: str) -> None:
    """The port opens right away; native USB gets a short window to re-enumerate after the
    reset that ends the job."""
    connection = next(p.connection for p in list_ports() if p.device.upper() == port.upper())
    window = 0.0 if connection is ConnectionType.UART_BRIDGE else 10.0
    deadline = time.monotonic() + window
    while True:
        try:
            serial.Serial(port).close()
            return
        except serial.SerialException:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


@pytest.mark.parametrize("var", PORT_VARS)
def test_port_released_after_identify_flash_and_refusal(var: str) -> None:
    port, info, entries = _setup(var)  # identify, ok
    _assert_released(port)

    _flash(_plan(port, info, entries))
    _assert_released(port)

    wrong = "esp32s3_hello" if info.chip == "esp32" else "esp32_hello"
    connection = next(p.connection for p in list_ports() if p.device.upper() == port.upper())
    refused = build_flash_plan(
        port=port,
        connection=connection,
        chip=idf_expect(wrong)["chip"],
        entries=idf_entries(wrong),
        before="default-reset",
        after="hard-reset",
    )
    assert _status(_run(refused)) is ResultStatus.REFUSED
    _assert_released(port)


def _read_plan(port: str, output: Path, address: int) -> ReadPlan:
    # a native USB port of a board just reset through its UART port re-enumerates briefly
    deadline = time.monotonic() + 5
    while True:
        found = [p for p in list_ports() if p.device.upper() == port.upper()]
        if found or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    assert found, f"{port} not present"
    return build_read_plan(port, found[0].connection, str(output), address=address, size=0x1000)


@pytest.mark.parametrize("var", PORT_VARS)
def test_read_flash_region_then_port_released(var: str, tmp_path: Path) -> None:
    port = _port(var)
    output = tmp_path / "dump.bin"
    events = _run(_read_plan(port, output, 0x0))
    assert _status(events) is ResultStatus.OK
    assert output.stat().st_size == 0x1000
    progress = [e for e in events if isinstance(e, ProgressEvent)]
    assert progress and all((e.entry_index, e.address) == (0, 0x0) for e in progress)
    _assert_released(port)


@pytest.mark.parametrize("var", PORT_VARS)
def test_read_beyond_the_flash_is_refused(var: str, tmp_path: Path) -> None:
    port = _port(var)
    output = tmp_path / "dump.bin"
    events = _run(_read_plan(port, output, 0x8000000))  # 128 MB: beyond any flash here
    assert _status(events) is ResultStatus.REFUSED
    assert any(isinstance(e, CheckEvent) and e.level is CheckLevel.REFUSE for e in events)
    assert not any(isinstance(e, ProgressEvent) for e in events)
    _assert_released(port)
