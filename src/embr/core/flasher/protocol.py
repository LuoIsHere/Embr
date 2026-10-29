"""Worker protocol: one task JSON on stdin, one JSON event per line on stdout.

The protocol is one-way: the worker never waits for input after the task.
"""

import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from types import UnionType
from typing import Union, cast, get_args, get_origin, get_type_hints

from embr.core.flasher.checks import CheckLevel, SecurityState
from embr.core.flasher.plan import ErasePlan, FlashPlan, IdentifyPlan, ReadPlan


class ProtocolError(ValueError):
    """A task or event line that cannot be decoded."""


class ResultStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    """Only produced by the runner after cancel(); flash content may be incomplete."""


class LogLevel(StrEnum):
    INFO = "info"
    NOTE = "note"
    WARNING = "warning"
    ERROR = "error"
    STAGE = "stage"
    """Stage marker from esptool's logger; text is "begin" or "end"."""


class EntryStatus(StrEnum):
    WRITTEN = "written"
    SKIPPED = "skipped"
    """Flash already held identical content (skip_flashed)."""


@dataclass(frozen=True)
class InfoEvent:
    chip: str
    """esptool chip name."""
    revision: str
    """e.g. "v1.0"."""
    major: int
    minor: int
    mac: str
    """"aa:bb:cc:dd:ee:ff"."""
    flash_size: str | None
    security: SecurityState
    in_download_mode: bool
    """True when the job leaves the chip in ROM download mode (after = no-reset)."""


@dataclass(frozen=True)
class CheckEvent:
    name: str
    level: CheckLevel
    message: str


@dataclass(frozen=True)
class LogEvent:
    level: LogLevel
    text: str


@dataclass(frozen=True)
class ProgressEvent:
    entry_index: int
    address: int
    """Address of the entry being written; comes from the worker, not from esptool."""
    cur: int
    total: int
    text: str


@dataclass(frozen=True)
class EntryEvent:
    entry_index: int
    address: int
    status: EntryStatus


@dataclass(frozen=True)
class ResultEvent:
    status: ResultStatus
    message: str


@dataclass(frozen=True)
class ErrorEvent:
    message: str
    details: str
    """Traceback or stderr text."""


Event = InfoEvent | CheckEvent | LogEvent | ProgressEvent | EntryEvent | ResultEvent | ErrorEvent
Task = IdentifyPlan | FlashPlan | ErasePlan | ReadPlan


def encode_task(task: Task) -> str:
    """Task as a single JSON document (no trailing newline)."""
    kind = next(name for name, cls in _TASK_TYPES.items() if isinstance(task, cls))
    return json.dumps({"kind": kind, "plan": asdict(task)})


def decode_task(text: str) -> Task:
    """Inverse of encode_task(). Raises ProtocolError."""
    data = _loads(text)
    cls = _TASK_TYPES.get(str(data.get("kind")))
    if cls is None:
        raise ProtocolError(f"unknown task kind {data.get('kind')!r}")
    try:
        return cast(Task, _build(cls, data.get("plan")))
    except (ValueError, KeyError) as exc:
        raise ProtocolError(str(exc)) from exc


def encode_event(event: Event) -> str:
    """Event as one JSON line without the newline; includes a "type" field."""
    return json.dumps({"type": _EVENT_NAMES[type(event)], **asdict(event)})


def decode_event(line: str) -> Event:
    """Inverse of encode_event(). Raises ProtocolError."""
    data = _loads(line)
    cls = _EVENT_TYPES.get(str(data.pop("type", None)))
    if cls is None:
        raise ProtocolError(f"unknown event in {line[:80]!r}")
    try:
        return cast(Event, _build(cls, data))
    except (ValueError, KeyError) as exc:
        raise ProtocolError(str(exc)) from exc


_EVENT_NAMES: dict[type, str] = {
    InfoEvent: "info",
    CheckEvent: "check",
    LogEvent: "log",
    ProgressEvent: "progress",
    EntryEvent: "entry",
    ResultEvent: "result",
    ErrorEvent: "error",
}


_EVENT_TYPES: dict[str, type] = {name: cls for cls, name in _EVENT_NAMES.items()}


_TASK_TYPES: dict[str, type] = {
    "identify": IdentifyPlan,
    "flash": FlashPlan,
    "erase": ErasePlan,
    "read_flash": ReadPlan,
}


def _convert(tp: object, value: object) -> object:
    """Rebuild a value of annotated type `tp` from its JSON form."""
    origin = get_origin(tp)
    if origin in (UnionType, Union):
        args = get_args(tp)
        if value is None and type(None) in args:
            return None
        inner = [a for a in args if a is not type(None)]
        return _convert(inner[0], value)
    if origin is tuple:
        if not isinstance(value, list):
            raise ProtocolError(f"expected a list, got {value!r}")
        (item_type, _ellipsis) = get_args(tp)
        return tuple(_convert(item_type, v) for v in cast(list[object], value))
    if isinstance(tp, type) and is_dataclass(tp):
        return _build(tp, value)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if tp is bool:
        if not isinstance(value, bool):
            raise ProtocolError(f"expected a bool, got {value!r}")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError(f"expected an int, got {value!r}")
        return value
    if tp is str:
        if not isinstance(value, str):
            raise ProtocolError(f"expected a string, got {value!r}")
        return value
    raise ProtocolError(f"unsupported field type {tp!r}")


def _build(cls: type, value: object) -> object:
    if not isinstance(value, dict):
        raise ProtocolError(f"expected an object for {cls.__name__}, got {value!r}")
    data = cast(dict[str, object], value)
    hints = get_type_hints(cls)
    kwargs: dict[str, object] = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _convert(hints[f.name], data[f.name])
    try:
        return cls(**kwargs)
    except TypeError as exc:  # missing fields
        raise ProtocolError(f"bad {cls.__name__}: {exc}") from exc


def _loads(text: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("expected a JSON object")
    return cast(dict[str, object], value)
