"""Identify, flash, erase and read_flash jobs: one worker process each, a log segment each,
a record for every flash, erase and read_flash (design doc §3.3, §4.1, §7.2). When a flash,
erase or read_flash job got the board's MAC (InfoEvent), its finished segment is also
appended to logs/boards/<mac>.log; identify segments and jobs without a MAC stay in the
daily log only.

Worker events arrive on a background thread. Operations hands each one to `dispatch`,
which must run it on the thread that owns Operations (the Qt adapter queues it to the main
thread; tests run it at once). Everything else - log writes, the record, listener calls -
happens there, so the sqlite connection is only used from one thread.
"""

import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import esptool

from embr.app.chips import for_chip
from embr.app.context import AppContext
from embr.app.flashplan import PlanError, assemble_plan, plan_chip
from embr.app.identify import IdentifyMemory
from embr.app.tracking import Tracker
from embr.core.firmware.files import FileState, entry_state
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, Entry, Firmware
from embr.core.flasher import runner
from embr.core.flasher.checks import CheckLevel
from embr.core.flasher.plan import (
    ErasePlan,
    FlashPlan,
    ReadPlan,
    build_erase_plan,
    build_identify_plan,
    build_read_plan,
    command_line,
)
from embr.core.flasher.protocol import (
    CheckEvent,
    EntryEvent,
    EntryStatus,
    ErrorEvent,
    Event,
    InfoEvent,
    LogEvent,
    LogLevel,
    ProgressEvent,
    ResultEvent,
    Task,
)
from embr.core.flasher.runner import JobHandle
from embr.core.history import FlashRecord, Operation, RecordEntry, RecordResult, result_from_event
from embr.core.logs import LogSegment
from embr.core.serial import PortInfo, baud_adjustable, remember_baud
from embr.core.storage.ids import new_id

Dispatch = Callable[[Callable[[], None]], None]
StartJob = Callable[[Task, Callable[[Event], None]], JobHandle]


class JobKind(StrEnum):
    IDENTIFY = "identify"
    FLASH = "flash"
    ERASE = "erase"
    READ_FLASH = "read_flash"


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
    """The record; None for identify (not recorded)."""
    log_file: str
    segment_id: str
    """Header id of the job's log segment (the record id for flash)."""
    native_usb: bool
    """The port was USB-Serial/JTAG or USB-OTG: the UI shows the manual reset hint."""
    firmware_id: str | None = None
    """read_flash that ended OK with import: the dump firmware created from it."""
    saved_to: str = ""
    """read_flash that ended OK: the file the content was written to."""


class Listener(Protocol):
    def on_event(self, event: Event) -> None:
        """Every worker event, in order (for the log panel and the checklist)."""
        ...

    def on_progress(self, progress: JobProgress) -> None: ...

    def on_finished(self, outcome: JobOutcome) -> None:
        """Called exactly once per job, after the log segment is closed and the record is
        saved."""
        ...


@dataclass
class _Job:
    kind: JobKind
    listener: Listener
    port: PortInfo
    segment: LogSegment
    segment_id: str
    start: datetime.datetime
    firmware: Firmware | None = None
    plan: FlashPlan | None = None
    other: ErasePlan | ReadPlan | None = None
    """The plan of an erase or read_flash job."""
    target: tuple[str | None, str] = (None, "")
    """Project and group for the dump firmware of a read_flash job."""
    handle: JobHandle | None = None
    info: InfoEvent | None = None
    kinds: dict[tuple[int, str], str] = field(default_factory=lambda: dict[tuple[int, str], str]())
    skipped: set[int] = field(default_factory=lambda: set[int]())
    refusals: list[str] = field(default_factory=lambda: list[str]())
    cancel_requested: bool = False
    finished: bool = False


class Operations:
    def __init__(
        self,
        context: AppContext,
        identify_memory: IdentifyMemory,
        dispatch: Dispatch,
        *,
        start_job: StartJob | None = None,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
        tracker: Tracker | None = None,
    ) -> None:
        """`start_job` defaults to core.flasher.runner.start; tests pass a fake. `tracker`
        is shared with the Library and the UI; None creates one."""
        self._ctx = context
        self._tracker = tracker or Tracker(context)
        self._memory = identify_memory
        self._dispatch = dispatch
        self._start_job: StartJob = start_job or runner.start
        self._clock = clock
        self._job: _Job | None = None

    @property
    def busy(self) -> bool:
        return self._job is not None

    @property
    def running_firmware_id(self) -> str | None:
        """Firmware being flashed (locked for editing in the UI), else None."""
        job = self._job
        return job.firmware.id if job is not None and job.firmware is not None else None

    def identify(self, port: PortInfo, listener: Listener) -> None:
        """Start an identify job. Its log segment gets an id of its own; no record is
        written. The InfoEvent updates identify_memory. BusyError while a job runs."""
        self._ensure_idle()
        segment_id = new_id()
        start = self._clock()
        segment = self._ctx.logs.open_segment(segment_id, JobKind.IDENTIFY, port=port.device)
        job = _Job(JobKind.IDENTIFY, listener, port, segment, segment_id, start)
        self._run(job, build_identify_plan(port.device, port.connection))

    def flash(
        self,
        firmware_id: str,
        port: PortInfo,
        baud: int | None,
        listener: Listener,
        *,
        shown_digest: str | None = None,
    ) -> None:
        """Start a flash job. Before starting: Tracker.verify_for_flash(firmware_id,
        shown_digest) (app.tracking.BuildChangedError when the firmware differs from the shown
        one or is being built, PlanError when it is not flashable; no job, no record), so a
        tracking firmware is flashed as currently resolved; the plan is assembled for the
        chip of the identification snapshot of `port` (none: the firmware's only chip;
        app.flashplan.assemble_plan()), so only the entries for that chip are written, and
        nothing is saved to the firmware; the files of those entries are checked again
        (blank entries written to temp/). The log segment header carries the record id.
        At the end the record is saved with the plan's reset modes, connection type, baud,
        entries (skipped as reported by EntryEvents), the chip details of the InfoEvent,
        refuse reasons (the REFUSE check messages), duration, esptool version, equivalent
        command and log file; on OK over an adjustable port the baud is remembered for the
        port's VID/PID and settings are saved. The InfoEvent also updates identify_memory.
        BusyError while a job runs; flashplan.PlanError propagates."""
        self._ensure_idle()
        firmware = self._tracker.verify_for_flash(firmware_id, shown_digest)
        snapshot = self._memory.snapshot
        identified = (
            snapshot.info.chip if snapshot and snapshot.port.device == port.device else None
        )
        chip = plan_chip(self._ctx, firmware, identified)
        if chip is None:
            raise PlanError(f"{firmware.name}: identify the board to choose which entries to flash")
        enabled = [e for e in for_chip(self._ctx, firmware, chip).entries if e.enabled]
        bad = [e.name for e in enabled if entry_state(e, self._ctx.paths.archive) != FileState.OK]
        if bad:
            raise PlanError(f"{', '.join(bad)}: file missing or changed since it was recorded")
        plan = assemble_plan(self._ctx, firmware, port, baud, chip=chip)
        record_id = new_id()
        start = self._clock()
        segment = self._ctx.logs.open_segment(
            record_id,
            JobKind.FLASH,
            port=port.device,
            project_name=self._project_name(firmware.project_id),
            firmware_name=firmware.name,
        )
        segment.write(f"$ {command_line(plan)}")
        job = _Job(JobKind.FLASH, listener, port, segment, record_id, start, firmware, plan)
        job.kinds = {(e.address, e.name): str(e.kind) for e in enabled if e.address is not None}
        self._run(job, plan)

    def erase(self, port: PortInfo, listener: Listener) -> None:
        """Start an erase-flash job. Its record (operation ERASE) has no entries, project or
        firmware; chip details, MAC and the equivalent command are filled as for flash.
        BusyError while a job runs."""
        self._ensure_idle()
        plan = build_erase_plan(port.device, port.connection)
        self._start_other(JobKind.ERASE, port, listener, plan)

    def read_flash(
        self,
        port: PortInfo,
        baud: int | None,
        listener: Listener,
        *,
        address: int = 0,
        size: int | None = None,
        project_id: str | None = None,
        device: str = "",
        output: Path | None = None,
        import_firmware: bool = True,
    ) -> None:
        """Start a read-flash job writing to temp/ (design doc §12 item 3). On OK the
        content is moved to `output` (replacing an existing file; folders created), and
        with `import_firmware` it is also archived and a fixed MANUAL firmware is created
        in the given project and group: one enabled MERGED entry at `address`
        ("archive:<sha256>", from_dump, MCU = the identified chip), flash parameters
        "keep", name "<chip> dump YYYY-MM-DD HH-MM" (made unique), notes saying it may
        contain credentials. Without `output` only the import happens; without either
        nothing is kept but the record. The record (operation READ_FLASH) has one entry
        (address, size read, sha256, kind merged) and points at the firmware if one was
        created. A failed, refused or cancelled read, or an output that cannot be written
        (the result becomes FAILED with the reason), leaves no file at `output`, creates no
        firmware and records no entry; the temporary file is always removed.
        JobOutcome.firmware_id is the new firmware, JobOutcome.saved_to the written file.
        BusyError while a job runs."""
        self._ensure_idle()
        record_id = new_id()
        output = self._ctx.paths.temp / f"read-{record_id}.bin"
        plan = build_read_plan(
            port.device, port.connection, str(output), address=address, size=size, baud=baud
        )
        self._start_other(
            JobKind.READ_FLASH,
            port,
            listener,
            plan,
            record_id=record_id,
            target=(project_id, device),
        )

    def cancel(self) -> None:
        """Kill the running job's worker; the job then finishes as CANCELLED. No effect
        when idle."""
        job = self._job
        if job is None:
            return
        job.cancel_requested = True
        if job.handle is not None:
            job.handle.cancel()

    # ---- running a job ----------------------------------------------------------------

    def _ensure_idle(self) -> None:
        if self._job is not None:
            raise BusyError("a job is already running")

    def _run(self, job: _Job, task: Task) -> None:
        self._job = job
        try:
            job.handle = self._start_job(
                task, lambda event: self._dispatch(lambda: self._handle(job, event))
            )
        except BaseException:
            job.segment.close()
            self._job = None
            raise
        if job.cancel_requested:  # cancel() came before the handle existed
            job.handle.cancel()

    def _handle(self, job: _Job, event: Event) -> None:
        if job.finished:
            return
        self._log(job, event)
        if isinstance(event, InfoEvent):
            job.info = event
            self._memory.record(job.port, event, self._clock())
        elif isinstance(event, CheckEvent) and event.level == CheckLevel.REFUSE:
            job.refusals.append(event.message)
        elif isinstance(event, EntryEvent) and event.status == EntryStatus.SKIPPED:
            job.skipped.add(event.entry_index)
        job.listener.on_event(event)
        if isinstance(event, ProgressEvent) and job.plan is not None:
            job.listener.on_progress(_progress(job.plan, event))
        elif isinstance(event, ProgressEvent) and isinstance(job.other, ReadPlan):
            percent = int(100 * min(event.cur / event.total, 1.0)) if event.total else 0
            job.listener.on_progress(JobProgress(0, 1, event.address, "flash", percent))
        if isinstance(event, ResultEvent | ErrorEvent):
            self._finish(job, event)

    def _finish(self, job: _Job, event: ResultEvent | ErrorEvent) -> None:
        job.finished = True
        result = result_from_event(event)
        job.segment.write(f"[result] {result}: {event.message}")
        record_id: str | None = None
        dump_id: str | None = None
        if job.kind == JobKind.FLASH and job.plan is not None and job.firmware is not None:
            record = self._record(job, result, event.message)
            self._ctx.history.add(record)
            record_id = record.id
            job.segment.write(
                f"[record] {record.id} {result}, {len(record.entries)} entries, "
                f"{record.duration_ms} ms"
            )
            if result == RecordResult.OK and remember_baud(
                self._ctx.settings, job.port, job.plan.baud or 0
            ):
                self._ctx.settings.save()
        elif job.other is not None:
            record = self._other_record(job, result, event.message)
            self._ctx.history.add(record)
            record_id = record.id
            dump_id = record.firmware_id
            job.segment.write(f"[record] {record.id} {result}, {record.duration_ms} ms")
            baud = job.other.baud if isinstance(job.other, ReadPlan) else None
            if (
                result == RecordResult.OK
                and baud
                and remember_baud(self._ctx.settings, job.port, baud)
            ):
                self._ctx.settings.save()
        job.segment.close()
        if job.kind != JobKind.IDENTIFY and job.info is not None and job.info.mac:
            self._ctx.logs.copy_to_board(job.segment, job.info.mac)
        self._job = None
        job.listener.on_finished(
            JobOutcome(
                kind=job.kind,
                result=result,
                message=event.message,
                record_id=record_id,
                log_file=job.segment.file_name,
                segment_id=job.segment_id,
                native_usb=not baud_adjustable(job.port),
                firmware_id=dump_id,
            )
        )

    def _start_other(
        self,
        kind: JobKind,
        port: PortInfo,
        listener: Listener,
        plan: ErasePlan | ReadPlan,
        *,
        record_id: str | None = None,
        target: tuple[str | None, str] = (None, ""),
    ) -> None:
        record_id = record_id or new_id()
        start = self._clock()
        segment = self._ctx.logs.open_segment(record_id, kind, port=port.device)
        segment.write(f"$ {command_line(plan)}")
        job = _Job(kind, listener, port, segment, record_id, start, other=plan, target=target)
        self._run(job, plan)

    def _other_record(self, job: _Job, result: RecordResult, message: str) -> FlashRecord:
        """Record of an erase or read_flash job; a read that ended OK creates the dump
        firmware first."""
        plan, info = job.other, job.info
        assert plan is not None
        dump: Firmware | None = None
        entries: tuple[RecordEntry, ...] = ()
        if isinstance(plan, ReadPlan):
            output = Path(plan.output)
            if result == RecordResult.OK and info is not None:
                dump = self._dump_firmware(job, plan, info, output.read_bytes())
                entry = dump.entries[0]
                entries = (
                    RecordEntry(entry.name, plan.address, entry.sha256, entry.size, "merged"),
                )
            output.unlink(missing_ok=True)
        refused = result == RecordResult.REFUSED
        return FlashRecord(
            id=job.segment_id,
            operation=Operation.ERASE if job.kind == JobKind.ERASE else Operation.READ_FLASH,
            timestamp=job.start.isoformat(timespec="seconds"),
            result=result,
            project_id=dump.project_id if dump else None,
            project_name=self._project_name(dump.project_id) if dump else "",
            firmware_id=dump.id if dump else None,
            firmware_name=dump.name if dump else "",
            device=dump.device if dump else "",
            port=plan.port,
            baud=plan.baud if isinstance(plan, ReadPlan) else None,
            connection_type=plan.connection.value,
            reset_before=plan.before,
            reset_after=plan.after,
            chip_detected=info.chip if info else None,
            chip_revision=info.revision if info else None,
            mac=info.mac if info and info.mac else None,
            flash_size_detected=info.flash_size if info else None,
            entries=entries,
            refuse_reason=("; ".join(job.refusals) or message) if refused else None,
            duration_ms=int((self._clock() - job.start).total_seconds() * 1000),
            esptool_version=esptool.__version__,
            command=command_line(plan),
            log_file=job.segment.file_name,
        )

    def _dump_firmware(self, job: _Job, plan: ReadPlan, info: InfoEvent, data: bytes) -> Firmware:
        name = f"{info.chip} dump {job.start:%Y-%m-%d %H-%M}"
        archived = self._ctx.archive.add(
            data, original_name=f"{name}.bin", note=f"Read from {plan.port} at {plan.address:#x}"
        )
        project_id, device = job.target
        if project_id is None:
            dump = self._ctx.firmware.create(name)
        else:
            dump = self._ctx.projects.create_firmware(
                self._ctx.firmware, project_id, name, device=device
            )
        dump.notes = (
            f"Read from flash on {plan.port} ({info.chip} {info.revision}, MAC {info.mac}). "
            "May contain Wi-Fi credentials and other secrets stored in NVS."
        )
        dump.entries = [
            Entry(
                id=new_id(),
                name=f"{dump.name}.bin",
                file=f"archive:{archived.sha256}",
                sha256=archived.sha256,
                size=len(data),
                kind=EntryKind.MERGED,
                address=plan.address,
                auto_address=plan.address,
                address_source=AddressSource.AUTO,
                from_dump=True,
                chip=info.chip,
            )
        ]
        self._ctx.firmware.save(dump)
        return dump

    def _record(self, job: _Job, result: RecordResult, message: str) -> FlashRecord:
        plan, firmware, info = job.plan, job.firmware, job.info
        assert plan is not None and firmware is not None
        refused = result == RecordResult.REFUSED
        return FlashRecord(
            id=job.segment_id,
            operation=Operation.FLASH,
            timestamp=job.start.isoformat(timespec="seconds"),
            result=result,
            project_id=firmware.project_id,
            project_name=self._project_name(firmware.project_id),
            firmware_id=firmware.id,
            firmware_name=firmware.name,
            device=firmware.device,
            port=plan.port,
            baud=plan.baud,
            connection_type=plan.connection.value,
            reset_before=plan.before,
            reset_after=plan.after,
            chip_detected=info.chip if info else None,
            chip_revision=info.revision if info else None,
            mac=info.mac if info and info.mac else None,
            flash_size_detected=info.flash_size if info else None,
            entries=tuple(
                RecordEntry(
                    e.name,
                    e.address,
                    e.sha256,
                    e.size,
                    job.kinds.get((e.address, e.name), ""),
                    i in job.skipped,
                )
                for i, e in enumerate(plan.entries)
            ),
            refuse_reason=("; ".join(job.refusals) or message) if refused else None,
            duration_ms=int((self._clock() - job.start).total_seconds() * 1000),
            esptool_version=esptool.__version__,
            command=command_line(plan),
            log_file=job.segment.file_name,
        )

    def _log(self, job: _Job, event: Event) -> None:
        segment = job.segment
        if isinstance(event, LogEvent):
            if event.level != LogLevel.STAGE:
                prefix = "" if event.level == LogLevel.INFO else f"[{event.level}] "
                segment.write(prefix + event.text)
        elif isinstance(event, CheckEvent):
            segment.write(f"[check {event.level}] {event.name}: {event.message}")
        elif isinstance(event, InfoEvent):
            segment.write(
                f"[chip] {event.chip} {event.revision}, MAC {event.mac or '-'}, "
                f"flash {event.flash_size or '-'}"
            )
        elif isinstance(event, EntryEvent):
            segment.write(f"[entry] {event.address:#x} {event.status}")
        elif isinstance(event, ErrorEvent):
            segment.write(f"[error] {event.message}")
            if event.details:
                segment.write(event.details.rstrip("\n"))

    def _project_name(self, project_id: str | None) -> str:
        if project_id is None:
            return ""
        try:
            return self._ctx.projects.get(project_id).name
        except KeyError:
            return ""


def _progress(plan: FlashPlan, event: ProgressEvent) -> JobProgress:
    count = len(plan.entries)
    index = min(max(event.entry_index, 0), count - 1) if count else 0
    fraction = event.cur / event.total if event.total else 0.0
    percent = int(100 * (index + min(fraction, 1.0)) / count) if count else 0
    name = plan.entries[index].name if count else ""
    return JobProgress(index, count, event.address, name, max(0, min(percent, 100)))
