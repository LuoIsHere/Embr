"""Identify and flash jobs: one worker process each, a log segment each, a flash record
for every flash (design doc §3.3, §4.1, §7.2).

Worker events arrive on a background thread. Operations hands each one to `dispatch`,
which must run it on the thread that owns Operations (the Qt adapter queues it to the main
thread; tests run it at once). Everything else - log writes, the record, listener calls -
happens there, so the sqlite connection is only used from one thread.
"""

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from embr.app.context import AppContext
from embr.app.identify import IdentifyMemory
from embr.core.flasher.protocol import Event, Task
from embr.core.flasher.runner import JobHandle
from embr.core.history import RecordResult
from embr.core.serial import PortInfo

Dispatch = Callable[[Callable[[], None]], None]
StartJob = Callable[[Task, Callable[[Event], None]], JobHandle]


class JobKind(StrEnum):
    IDENTIFY = "identify"
    FLASH = "flash"


class BusyError(RuntimeError):
    """A job is already running; one board, one job at a time."""


@dataclass(frozen=True)
class JobProgress:
    entry_index: int
    entry_count: int
    address: int
    name: str
    """Entry being written."""
    percent: int
    """Whole job, 0-100: finished entries plus the fraction of the current one."""


@dataclass(frozen=True)
class JobOutcome:
    kind: JobKind
    result: RecordResult
    message: str
    record_id: str | None
    """The flash record; None for identify (not recorded)."""
    log_file: str
    segment_id: str
    """Header id of the job's log segment (the record id for flash)."""
    native_usb: bool
    """The port was USB-Serial/JTAG or USB-OTG: the UI shows the manual reset hint."""


class Listener(Protocol):
    def on_event(self, event: Event) -> None:
        """Every worker event, in order (for the log panel and the checklist)."""
        ...

    def on_progress(self, progress: JobProgress) -> None: ...

    def on_finished(self, outcome: JobOutcome) -> None:
        """Called exactly once per job, after the log segment is closed and the record is
        saved."""
        ...


class Operations:
    def __init__(
        self,
        context: AppContext,
        identify_memory: IdentifyMemory,
        dispatch: Dispatch,
        *,
        start_job: StartJob | None = None,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        """`start_job` defaults to core.flasher.runner.start; tests pass a fake."""
        raise NotImplementedError

    @property
    def busy(self) -> bool:
        raise NotImplementedError

    @property
    def running_firmware_id(self) -> str | None:
        """Firmware being flashed (locked for editing in the UI), else None."""
        raise NotImplementedError

    def identify(self, port: PortInfo, listener: Listener) -> None:
        """Start an identify job. Its log segment gets an id of its own; no record is
        written. The InfoEvent updates identify_memory. BusyError while a job runs."""
        raise NotImplementedError

    def flash(self, firmware_id: str, port: PortInfo, baud: int | None, listener: Listener) -> None:
        """Start a flash job for a fixed firmware. Before starting: the entry files are
        checked again; a firmware without a chip takes the identified chip (saved) or
        flashplan.PlanError is raised when nothing is identified; the plan is assembled
        (blank entries written to temp/). The log segment header carries the record id.
        At the end the record is saved with the plan's reset modes, connection type, baud,
        entries (skipped as reported by EntryEvents), the chip details of the InfoEvent,
        refuse reasons (the REFUSE check messages), duration, esptool version, equivalent
        command and log file; on OK over an adjustable port the baud is remembered for the
        port's VID/PID and settings are saved. The InfoEvent also updates identify_memory.
        BusyError while a job runs; flashplan.PlanError propagates."""
        raise NotImplementedError

    def cancel(self) -> None:
        """Kill the running job's worker; the job then finishes as CANCELLED. No effect
        when idle."""
        raise NotImplementedError
