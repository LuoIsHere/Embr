"""Run worker jobs in a child process. The UI only sees start(), JobHandle and events."""

import contextlib
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from typing import IO

from embr.core.flasher.protocol import (
    ErrorEvent,
    Event,
    LogEvent,
    LogLevel,
    ProtocolError,
    ResultEvent,
    ResultStatus,
    Task,
    decode_event,
    encode_task,
)


def worker_command() -> list[str]:
    """Command that starts a worker: `Embr.exe --worker` when frozen,
    `python -m embr --worker` from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker"]
    return [sys.executable, "-m", "embr", "--worker"]


class JobHandle:
    """A running job. Events reach the callback given to start(), in order, on a
    background thread. Exactly one ResultEvent or ErrorEvent ends every job; after
    cancel() it is ResultEvent(status=CANCELLED)."""

    def __init__(self, process: subprocess.Popen[bytes], on_event: Callable[[Event], None]) -> None:
        self._process = process
        self._on_event = on_event
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._ended = False  # a terminal event was delivered
        self._cancelled = False
        self._stderr: list[str] = []
        self._exit_code: int | None = None
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread.start()
        self._stdout_thread.start()

    def cancel(self) -> None:
        """Kill the worker process (TerminateProcess on Windows), releasing the serial
        port. Returns within a second even while the worker is stuck connecting.
        No effect once the job has finished."""
        with self._lock:
            if self._ended or self._done.is_set():
                return
            self._cancelled = True
        with contextlib.suppress(OSError):  # already gone
            self._process.kill()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the job has finished and its last event has been delivered.
        Returns False on timeout."""
        return self._done.wait(timeout)

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    @property
    def exit_code(self) -> int | None:
        """Worker exit code once finished, else None."""
        return self._exit_code if self._done.is_set() else None

    @property
    def stderr(self) -> str:
        """Everything the worker wrote to stderr (tracebacks of worker crashes)."""
        return "".join(self._stderr)

    def _deliver(self, event: Event) -> None:
        with self._lock:
            if self._ended:
                return
            if isinstance(event, ResultEvent | ErrorEvent):
                self._ended = True
        self._on_event(event)

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        assert stream is not None
        for raw in iter(stream.readline, b""):
            self._stderr.append(raw.decode("utf-8", errors="replace"))

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        assert stream is not None
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line or self._cancelled:
                    continue
                try:
                    event = decode_event(line)
                except ProtocolError:
                    event = LogEvent(LogLevel.INFO, line)
                self._deliver(event)
        finally:
            self._finish()

    def _finish(self) -> None:
        code = self._process.wait()
        self._stderr_thread.join(timeout=1)
        self._exit_code = code
        if self._cancelled:
            self._deliver(
                ResultEvent(ResultStatus.CANCELLED, "cancelled; flash content may be incomplete")
            )
        elif not self._ended:
            self._deliver(
                ErrorEvent(f"worker exited with code {code} without a result", self.stderr)
            )
        self._done.set()


def start(
    task: Task, on_event: Callable[[Event], None], *, command: Sequence[str] | None = None
) -> JobHandle:
    """Start a worker for `task`. `command` overrides worker_command() (tests).
    A worker that exits without a ResultEvent or ErrorEvent produces an ErrorEvent
    carrying its exit code and stderr."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        list(command or worker_command()),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=flags,
    )
    stdin: IO[bytes] | None = process.stdin
    assert stdin is not None
    try:
        stdin.write(encode_task(task).encode("utf-8"))
        stdin.close()
    except OSError:  # worker died before reading; the reader reports it
        pass
    return JobHandle(process, on_event)
