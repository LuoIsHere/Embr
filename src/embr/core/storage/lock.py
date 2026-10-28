"""Single-instance lock: a Windows file lock released by the OS when the process ends."""

from pathlib import Path
from types import TracebackType


class InstanceLock:
    """Lock on a file (normally temp/embr.lock). Only one holder per file at a time;
    two Embr copies in different folders use different files and do not conflict."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def acquire(self) -> bool:
        """Take the lock without waiting. False if another process holds it."""
        raise NotImplementedError

    def release(self) -> None:
        """Release the lock if held. Safe to call twice."""
        raise NotImplementedError

    @property
    def held(self) -> bool:
        raise NotImplementedError

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
