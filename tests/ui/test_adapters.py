"""Qt adapters (design doc §7.3, §8.2 "app 与 ui/adapters"): worker events reach the GUI
thread in order, log lines are batched without reordering, hotplug polling is debounced,
and Qt values survive a trip through settings.ini."""

import ctypes
import ctypes.wintypes
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QCoreApplication, QThread

from embr.app.operations import JobKind, JobOutcome
from embr.core.flasher.protocol import Event, LogEvent, LogLevel, ProgressEvent
from embr.core.history import RecordResult
from embr.core.serial import PortChange, PortInfo, PortWatcher, classify_connection
from embr.core.storage.settings import Settings
from embr.ui.adapters import settings as ui_settings
from embr.ui.adapters.jobs import JobSignals, MainThreadDispatcher
from embr.ui.adapters.ports import SETTLE_MS, WM_DEVICECHANGE, PortMonitor


def _pump_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Run the Qt event loop until predicate() is true or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    QCoreApplication.processEvents()
    return predicate()


def _pump_for(seconds: float) -> None:
    _pump_until(lambda: False, seconds)


def _port(device: str, vid: int | None = 0x10C4, pid: int | None = 0xEA60) -> PortInfo:
    connection, otg_chip = classify_connection(vid, pid)
    return PortInfo(
        device=device,
        vid=vid,
        pid=pid,
        serial_number=None,
        description="test port",
        connection=connection,
        otg_chip=otg_chip,
    )


class _ScriptedPorts:
    """Enumerate function returning scripted lists; the last list repeats."""

    def __init__(self, *script: list[PortInfo]) -> None:
        self._script = script
        self.calls = 0

    def __call__(self) -> list[PortInfo]:
        result = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return list(result)


def _outcome() -> JobOutcome:
    return JobOutcome(
        kind=JobKind.FLASH,
        result=RecordResult.OK,
        message="done",
        record_id="rec",
        log_file="2026-10-29.log",
        segment_id="rec",
        native_usb=False,
    )


# --- MainThreadDispatcher ------------------------------------------------------------


def test_dispatcher_runs_calls_from_threads_on_gui_thread_in_order(
    qapp: QCoreApplication,
) -> None:
    dispatcher = MainThreadDispatcher()
    gui_thread = qapp.thread()
    threads_count, calls_per_thread = 4, 50
    received: list[tuple[int, int, bool, bool]] = []

    def make_call(worker: int, seq: int) -> Callable[[], None]:
        def call() -> None:
            received.append(
                (
                    worker,
                    seq,
                    threading.current_thread() is threading.main_thread(),
                    QThread.currentThread() == gui_thread,
                )
            )

        return call

    def worker_body(worker: int) -> None:
        for seq in range(calls_per_thread):
            dispatcher.dispatch(make_call(worker, seq))

    workers = [threading.Thread(target=worker_body, args=(i,)) for i in range(threads_count)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    total = threads_count * calls_per_thread
    assert _pump_until(lambda: len(received) >= total)
    _pump_for(0.05)
    assert len(received) == total
    assert all(on_main and on_gui for _, _, on_main, on_gui in received)
    for worker in range(threads_count):
        seqs = [seq for w, seq, _, _ in received if w == worker]
        assert seqs == list(range(calls_per_thread))


# --- JobSignals ----------------------------------------------------------------------


def _record_job_signals(signals: JobSignals) -> list[tuple[str, object]]:
    seen: list[tuple[str, object]] = []

    def on_event(event: object) -> None:
        seen.append(("event", event))

    def on_logs(batch: list[LogEvent]) -> None:
        seen.append(("logs", list(batch)))

    def on_finished(outcome: object) -> None:
        seen.append(("finished", outcome))

    signals.job_event.connect(on_event)
    signals.log_batch.connect(on_logs)
    signals.finished.connect(on_finished)
    return seen


def test_job_signals_keep_log_lines_and_events_in_order(qapp: QCoreApplication) -> None:
    signals = JobSignals()
    seen = _record_job_signals(signals)
    log1 = LogEvent(LogLevel.INFO, "Connecting...")
    log2 = LogEvent(LogLevel.INFO, "Chip is ESP32-S3")
    progress = ProgressEvent(entry_index=0, address=0x0, cur=1, total=2, text="")
    log3 = LogEvent(LogLevel.NOTE, "Hash of data verified.")
    outcome = _outcome()

    events: list[Event] = [log1, log2, progress, log3]
    for event in events:
        signals.on_event(event)
    signals.on_finished(outcome)

    assert seen == [
        ("logs", [log1, log2]),
        ("event", progress),
        ("logs", [log3]),
        ("finished", outcome),
    ]
    # nothing left pending after finished
    _pump_for(0.2)
    assert len(seen) == 4


def test_job_signals_batch_log_lines_on_timer(qapp: QCoreApplication) -> None:
    signals = JobSignals()
    seen = _record_job_signals(signals)
    lines = [LogEvent(LogLevel.INFO, f"line {i}") for i in range(20)]

    for line in lines:
        signals.on_event(line)
    assert seen == []  # collected, not emitted one by one

    assert _pump_until(lambda: len(seen) > 0, timeout=2.0)
    _pump_for(0.2)
    assert seen == [("logs", lines)]


# --- PortMonitor ---------------------------------------------------------------------


def test_port_monitor_start_reports_initial_ports_then_only_changes(
    qapp: QCoreApplication,
) -> None:
    com3, com10 = _port("COM3"), _port("COM10", vid=0x1A86, pid=0x7523)
    ports = _ScriptedPorts([com10, com3], [com10, com3], [com3])
    monitor = PortMonitor(PortWatcher(ports))
    changes: list[PortChange] = []
    monitor.changed.connect(changes.append)

    monitor.start()
    assert len(changes) == 1
    assert changes[0].added == (com3, com10)
    assert changes[0].removed == ()
    assert monitor.ports == (com3, com10)

    monitor.poll()  # same list: no signal
    assert len(changes) == 1

    monitor.poll()  # COM10 gone
    assert len(changes) == 2
    assert changes[1].added == ()
    assert changes[1].removed == (com10,)
    assert monitor.ports == (com3,)


def test_port_monitor_poll_soon_coalesces_into_one_poll(qapp: QCoreApplication) -> None:
    ports = _ScriptedPorts([_port("COM3")])
    monitor = PortMonitor(PortWatcher(ports))
    changes: list[PortChange] = []
    monitor.changed.connect(changes.append)

    for _ in range(5):
        monitor.poll_soon()
        _pump_for(0.01)
    assert ports.calls == 0  # waits for the settle delay

    assert _pump_until(lambda: ports.calls >= 1, timeout=SETTLE_MS / 1000 + 2.0)
    _pump_for(SETTLE_MS / 1000 + 0.2)
    assert ports.calls == 1
    assert len(changes) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="WM_DEVICECHANGE is Windows only")
def test_port_monitor_native_event_polls_only_on_device_change(qapp: QCoreApplication) -> None:
    ports = _ScriptedPorts([_port("COM3")])
    monitor = PortMonitor(PortWatcher(ports))
    settle = SETTLE_MS / 1000 + 0.3

    monitor.handle_native_event(b"windows_generic_MSG", 0)
    monitor.handle_native_event(QByteArray(b"windows_generic_MSG"), 0)
    paint = ctypes.wintypes.MSG()
    paint.message = 0x000F  # WM_PAINT
    monitor.handle_native_event(b"windows_generic_MSG", ctypes.addressof(paint))
    _pump_for(settle)
    assert ports.calls == 0

    device = ctypes.wintypes.MSG()
    device.message = WM_DEVICECHANGE
    monitor.handle_native_event(QByteArray(b"windows_generic_MSG"), ctypes.addressof(device))
    assert ports.calls == 0
    assert _pump_until(lambda: ports.calls >= 1, timeout=settle + 2.0)
    assert ports.calls == 1


# --- settings helpers ----------------------------------------------------------------


def test_settings_round_trip_through_file(tmp_path: Path) -> None:
    ini, backup = tmp_path / "settings.ini", tmp_path / "backup"
    geometry = QByteArray(b"\x00\x01geom\xff")
    settings = Settings(ini, backup)
    ui_settings.save_bytes(settings, "main_geometry", geometry)
    ui_settings.save_ints(settings, "main_splitter", [236, 600, 330])
    ui_settings.save_flag(settings, "show_log", False)
    settings.save()

    reloaded = Settings(ini, backup)
    loaded = ui_settings.load_bytes(reloaded, "main_geometry")
    assert loaded is not None
    assert bytes(loaded.data()) == b"\x00\x01geom\xff"
    assert ui_settings.load_ints(reloaded, "main_splitter") == [236, 600, 330]
    assert ui_settings.load_flag(reloaded, "show_log", default=True) is False


def test_settings_absent_or_malformed_values(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "settings.ini", tmp_path / "backup")
    assert ui_settings.load_bytes(settings, "main_geometry") is None
    assert ui_settings.load_ints(settings, "main_splitter") is None
    assert ui_settings.load_flag(settings, "show_log", default=True) is True
    assert ui_settings.load_flag(settings, "show_log", default=False) is False

    section = ui_settings.UI_SECTION
    settings.set(section, "main_geometry", "not base64 at all!")
    settings.set(section, "main_splitter", "236,abc,330")
    settings.set(section, "show_log", "yes")
    assert ui_settings.load_bytes(settings, "main_geometry") is None
    assert ui_settings.load_ints(settings, "main_splitter") is None
    assert ui_settings.load_flag(settings, "show_log", default=True) is True
