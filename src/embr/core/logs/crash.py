"""Unexpected exceptions: an exception no code handled (in a Qt slot, on the main thread or
in a background thread) is written to logs/crash-<date>.log with its traceback, so it is
never lost (the packaged Embr has no console). A crash of the interpreter itself
(faulthandler) goes to logs/fault.log. On the main thread `on_error` is called too, for
the window's log panel."""

import contextlib
import datetime
import faulthandler
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import TextIO

FAULT_FILE = "fault.log"

ExcHook = Callable[[type[BaseException], BaseException, TracebackType | None], object]
OnError = Callable[[str, Path], None]
"""(one-line summary, crash log file)."""


def crash_log_name(day: datetime.date) -> str:
    """e.g. "crash-2026-11-09.log"."""
    return f"crash-{day.isoformat()}.log"


class CrashLog:
    def __init__(
        self,
        folder: Path,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        self._folder = folder
        self._clock = clock
        self.on_error: OnError | None = None
        self._previous: ExcHook | None = None
        self._previous_thread: Callable[[threading.ExceptHookArgs], object] | None = None
        self._fault: TextIO | None = None

    def install(self) -> None:
        self._previous = sys.excepthook
        self._previous_thread = threading.excepthook
        sys.excepthook = self._hook
        threading.excepthook = self._thread_hook
        with contextlib.suppress(OSError):
            self._folder.mkdir(parents=True, exist_ok=True)
            self._fault = (self._folder / FAULT_FILE).open("a", encoding="utf-8")
            faulthandler.enable(self._fault)

    def uninstall(self) -> None:
        if self._previous is not None:
            sys.excepthook = self._previous
        if self._previous_thread is not None:
            threading.excepthook = self._previous_thread
        if self._fault is not None:
            faulthandler.disable()
            self._fault.close()
            self._fault = None
        self.on_error = None

    def write(
        self,
        kind: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
        where: str,
    ) -> Path:
        """Append one report; returns the file. Raises OSError when it cannot be written."""
        now = self._clock()
        path = self._folder / crash_log_name(now.date())
        text = "".join(traceback.format_exception(kind, exc, tb))
        with path.open("a", encoding="utf-8") as f:
            f.write(f"=== {now:%Y-%m-%d %H:%M:%S} {where}\n{text}\n")
        return path

    def _hook(
        self, kind: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        if not issubclass(kind, KeyboardInterrupt):
            self._report(kind, exc, tb, threading.current_thread())
        self._pass_on(lambda: self._previous and self._previous(kind, exc, tb))

    def _thread_hook(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None and not issubclass(args.exc_type, SystemExit):
            self._report(args.exc_type, args.exc_value, args.exc_traceback, args.thread)
        self._pass_on(lambda: self._previous_thread and self._previous_thread(args))

    def _report(
        self,
        kind: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
        thread: threading.Thread | None,
    ) -> None:
        where = f"thread {thread.name}" if thread is not None else "unknown thread"
        try:
            path = self.write(kind, exc, tb, where)
        except OSError:
            return  # nowhere to write it; the default hook still prints it in a console
        if self.on_error is not None and thread is threading.main_thread():
            with contextlib.suppress(Exception):  # reporting must never raise again
                self.on_error(f"{kind.__name__}: {exc}", path)

    @staticmethod
    def _pass_on(call: Callable[[], object]) -> None:
        """The default hooks print to stderr (a console when running from source); the
        packaged Embr has none (sys.stderr is None)."""
        if sys.stderr is not None:
            with contextlib.suppress(Exception):
                call()
