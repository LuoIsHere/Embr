"""esptool logger that turns log output and progress callbacks into worker events."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from esptool.logger import TemplateLogger

from embr.core.flasher.protocol import Event, LogEvent, LogLevel, ProgressEvent


@dataclass
class EntryCursor:
    """The entry being written; the worker updates it before each write_flash call."""

    index: int = -1
    address: int = 0
    progressed: bool = False


class EventLogger(TemplateLogger):
    """Forwards esptool's messages unchanged; nothing is parsed out of the text."""

    def __init__(self, send: Callable[[Event], None], cursor: EntryCursor) -> None:
        self._send = send
        self._cursor = cursor
        self._line = ""

    def print(self, *args: Any, **kwargs: Any) -> None:
        sep = str(kwargs.get("sep", " "))
        end = str(kwargs.get("end", "\n"))
        self._line += sep.join(str(a) for a in args) + end
        while "\n" in self._line:
            line, self._line = self._line.split("\n", 1)
            self._send(LogEvent(LogLevel.INFO, line))

    def note(self, message: str) -> None:
        self._flush()
        self._send(LogEvent(LogLevel.NOTE, message))

    def warning(self, message: str) -> None:
        self._flush()
        self._send(LogEvent(LogLevel.WARNING, message))

    def error(self, message: str) -> None:
        self._flush()
        self._send(LogEvent(LogLevel.ERROR, message))

    def stage(self, finish: bool = False) -> None:
        self._flush()
        self._send(LogEvent(LogLevel.STAGE, "end" if finish else "begin"))

    def progress_bar(
        self,
        cur_iter: int,
        total_iters: int,
        prefix: str = "",
        suffix: str = "",
        bar_length: int = 30,
    ) -> None:
        self._cursor.progressed = True
        text = f"{prefix}{suffix}".strip()
        self._send(
            ProgressEvent(self._cursor.index, self._cursor.address, cur_iter, total_iters, text)
        )

    def set_verbosity(self, verbosity: str) -> None:
        pass

    def _flush(self) -> None:
        if self._line:
            self._send(LogEvent(LogLevel.INFO, self._line))
            self._line = ""


def install(send: Callable[[Event], None], cursor: EntryCursor) -> None:
    """Route esptool's global logger to an EventLogger. esptool 5.3.1's log.set_logger()
    only swaps the class of the global `log` object and keeps its old instance attributes,
    so the EventLogger state must end up on that object too: afterwards every esptool
    message, note, warning, error, stage marker and progress callback becomes an event."""
    raise NotImplementedError
