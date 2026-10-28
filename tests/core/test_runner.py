"""core/flasher/runner without hardware: crash handling and cancel while connecting.

Every wait has a timeout and every job is cancelled on the way out, so a broken
implementation fails instead of hanging.
"""

import re
import socket
import sys
import threading
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager

from embr_testkit import WORKER_TIMEOUT, idf_entries

from embr.core.flasher.plan import FlashPlan, FlashSettings, IdentifyPlan
from embr.core.flasher.protocol import (
    ErrorEvent,
    Event,
    LogEvent,
    LogLevel,
    ResultEvent,
    ResultStatus,
    Task,
    encode_event,
)
from embr.core.flasher.runner import JobHandle, start
from embr.core.serial import ConnectionType

IDENTIFY = IdentifyPlan("COM1", ConnectionType.UART_BRIDGE, "default-reset", "hard-reset")


@contextmanager
def running(
    task: Task, command: Sequence[str] | None = None
) -> Generator[tuple[JobHandle, list[Event]]]:
    events: list[Event] = []
    handle = start(task, events.append, command=command)
    try:
        yield handle, events
    finally:
        if not handle.finished:
            handle.cancel()
            handle.wait(5)


def _fake_worker(lines: list[str], stderr: str, exit_code: int) -> list[str]:
    script = (
        f"import sys\nfor line in {lines!r}:\n    print(line, flush=True)\n"
        f"sys.stderr.write({stderr!r})\nsys.exit({exit_code})\n"
    )
    return [sys.executable, "-c", script]


def test_worker_crash_gives_error_with_log_and_stderr() -> None:
    logs = [LogEvent(level=LogLevel.INFO, text=f"line {i}") for i in range(20)]
    command = _fake_worker([encode_event(e) for e in logs], "Traceback: boom-x", 7)
    with running(IDENTIFY, command) as (handle, events):
        assert handle.wait(WORKER_TIMEOUT)
        assert events[:-1] == logs  # everything written before the crash is kept, in order
        final = events[-1]
        assert isinstance(final, ErrorEvent)
        assert re.search(r"\b7\b", f"{final.message} {final.details}"), "exit code missing"
        assert "boom-x" in f"{final.message} {final.details}"
        assert handle.exit_code == 7
        assert "boom-x" in handle.stderr


def test_exit_without_result_is_error() -> None:
    with running(IDENTIFY, _fake_worker([], "", 0)) as (handle, events):
        assert handle.wait(WORKER_TIMEOUT)
        assert len(events) == 1 and isinstance(events[0], ErrorEvent)


class SilentServer:
    """Local TCP server that accepts connections and never sends a byte."""

    def __init__(self) -> None:
        self._sock = socket.create_server(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port: int = self._sock.getsockname()[1]
        self.accepted = threading.Event()
        self.closed = threading.Event()
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.accepted.set()
            with conn:  # read and ignore until the client goes away
                conn.settimeout(0.2)
                while not self._stop.is_set():
                    try:
                        if not conn.recv(4096):
                            break
                    except TimeoutError:
                        continue
                    except OSError:
                        break
            self.closed.set()

    def stop(self) -> None:
        self._stop.set()
        self._sock.close()


def test_cancel_while_connecting() -> None:
    # rfc2217:// (esptool special-cases it); pyserial waits up to 3 s for the silent
    # server's option negotiation, so the worker is stuck connecting when we cancel.
    server = SilentServer()
    try:
        plan = FlashPlan(
            port=f"rfc2217://127.0.0.1:{server.port}",
            connection=ConnectionType.UART_BRIDGE,
            baud=115200,
            chip="esp32s3",
            before="default-reset",
            after="hard-reset",
            stub=True,
            flash=FlashSettings(),
            entries=tuple(idf_entries("esp32s3_hello")),
            erase_all=False,
            skip_flashed=True,
        )
        with running(plan) as (handle, events):
            assert server.accepted.wait(WORKER_TIMEOUT), "worker never connected"
            assert not handle.finished, events
            started = time.monotonic()
            handle.cancel()
            assert handle.wait(5)
            assert time.monotonic() - started < 1.0
            final = events[-1]
            assert isinstance(final, ResultEvent) and final.status is ResultStatus.CANCELLED
            assert sum(isinstance(e, ResultEvent | ErrorEvent) for e in events) == 1
            assert server.closed.wait(5), "connection not released after cancel"
    finally:
        server.stop()
