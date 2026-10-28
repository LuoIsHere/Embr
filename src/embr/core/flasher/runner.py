"""Run worker jobs in a child process. The UI only sees start(), JobHandle and events."""

from collections.abc import Callable, Sequence

from embr.core.flasher.protocol import Event, Task


def worker_command() -> list[str]:
    """Command that starts a worker: `Embr.exe --worker` when frozen,
    `python -m embr --worker` from source."""
    raise NotImplementedError


class JobHandle:
    """A running job. Events reach the callback given to start(), in order, on a
    background thread. Exactly one ResultEvent or ErrorEvent ends every job; after
    cancel() it is ResultEvent(status=CANCELLED)."""

    def cancel(self) -> None:
        """Kill the worker process (TerminateProcess on Windows), releasing the serial
        port. Returns within a second even while the worker is stuck connecting.
        No effect once the job has finished."""
        raise NotImplementedError

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the job has finished and its last event has been delivered.
        Returns False on timeout."""
        raise NotImplementedError

    @property
    def finished(self) -> bool:
        raise NotImplementedError

    @property
    def exit_code(self) -> int | None:
        """Worker exit code once finished, else None."""
        raise NotImplementedError

    @property
    def stderr(self) -> str:
        """Everything the worker wrote to stderr (tracebacks of worker crashes)."""
        raise NotImplementedError


def start(
    task: Task, on_event: Callable[[Event], None], *, command: Sequence[str] | None = None
) -> JobHandle:
    """Start a worker for `task`. `command` overrides worker_command() (tests).
    A worker that exits without a ResultEvent or ErrorEvent produces an ErrorEvent
    carrying its exit code and stderr."""
    raise NotImplementedError
