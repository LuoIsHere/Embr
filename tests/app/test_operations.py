"""app/operations: identify, flash, erase and read-flash jobs with a fake worker process
(design doc §3.3, §7.2).

The fake worker is `python -c` printing protocol lines, started through the real runner.
`dispatch` queues each callable and the test thread runs the queue, as the Qt adapter
does on the main thread; every wait has a timeout.
"""

import datetime
import hashlib
import queue
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from app_testkit import BRIDGE, JTAG, MAC, build_files, fixed_firmware, info

from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.identify import IdentifyMemory
from embr.app.library import Library
from embr.app.operations import BusyError, JobKind, JobOutcome, JobProgress, Operations
from embr.app.tracking import BuildChangedError
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import FlashParams, Source
from embr.core.flasher import runner
from embr.core.flasher.checks import CHIP, SECURITY, CheckLevel
from embr.core.flasher.plan import ErasePlan, IdentifyPlan, ReadPlan
from embr.core.flasher.protocol import (
    CheckEvent,
    EntryEvent,
    EntryStatus,
    ErrorEvent,
    Event,
    ProgressEvent,
    ResultEvent,
    ResultStatus,
    Task,
    encode_event,
)
from embr.core.history import Operation, RecordResult
from embr.core.logs import BOARDS_FOLDER, HEADER_PREFIX
from embr.core.serial import BAUD_SECTION, PortInfo
from embr.core.storage.settings import Settings

TIMEOUT = 30.0
HELLO = build_files("esp32s3_hello")
INFO = info("esp32s3", 0, 2)
WRITTEN, SKIPPED = EntryStatus.WRITTEN, EntryStatus.SKIPPED
FLASH_OK: list[Event] = [
    INFO,
    CheckEvent(CHIP, CheckLevel.PASS, "chip esp32s3"),
    EntryEvent(0, 0x0, WRITTEN),
    EntryEvent(1, 0x8000, SKIPPED),
    EntryEvent(2, 0x10000, WRITTEN),
    ResultEvent(ResultStatus.OK, "done"),
]


class FakeWorker:
    """StartJob that runs a script printing `events`, then sleeping `sleep` seconds."""

    def __init__(self, events: Sequence[Event] = (), sleep: float = 0.0) -> None:
        lines = [encode_event(e) for e in events]
        self.script = (
            f"import time\nfor line in {lines!r}:\n    print(line, flush=True)\n"
            f"time.sleep({sleep})\n"
        )
        self.tasks: list[Task] = []

    def __call__(self, task: Task, on_event: Callable[[Event], None]) -> runner.JobHandle:
        self.tasks.append(task)
        return runner.start(task, on_event, command=[sys.executable, "-c", self.script])


class DumpWorker(FakeWorker):
    """FakeWorker that also writes `data` to the read plan's output, as the worker does."""

    def __init__(self, events: Sequence[Event], data: bytes) -> None:
        super().__init__(events)
        self.data = data

    def __call__(self, task: Task, on_event: Callable[[Event], None]) -> runner.JobHandle:
        if isinstance(task, ReadPlan):
            Path(task.output).write_bytes(self.data)
        return super().__call__(task, on_event)


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.progress: list[JobProgress] = []
        self.outcomes: list[JobOutcome] = []

    def on_event(self, event: Event) -> None:
        self.events.append(event)

    def on_progress(self, progress: JobProgress) -> None:
        self.progress.append(progress)

    def on_finished(self, outcome: JobOutcome) -> None:
        self.outcomes.append(outcome)


class Pump:
    def __init__(self) -> None:
        self.queue: queue.Queue[Callable[[], None]] = queue.Queue()

    def dispatch(self, call: Callable[[], None]) -> None:
        self.queue.put(call)

    def until_finished(self, recorder: Recorder) -> JobOutcome:
        deadline = time.monotonic() + TIMEOUT
        while not recorder.outcomes:
            remaining = deadline - time.monotonic()
            try:
                call = self.queue.get(timeout=max(remaining, 0.01))
            except queue.Empty:
                pytest.fail("the job did not finish")
            call()
        (outcome,) = recorder.outcomes
        return outcome


class Setup:
    def __init__(
        self,
        context: AppContext,
        worker: FakeWorker,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        self.context = context
        self.worker = worker
        self.memory = IdentifyMemory()
        self.pump = Pump()
        self.recorder = Recorder()
        self.ops = Operations(
            context, self.memory, self.pump.dispatch, start_job=worker, clock=clock
        )

    def flash(
        self, firmware_id: str, port: PortInfo = BRIDGE, baud: int | None = 921600
    ) -> JobOutcome:
        self.ops.flash(firmware_id, port, baud, self.recorder)
        return self.pump.until_finished(self.recorder)


def test_identify_updates_memory_and_logs_without_a_record(context: AppContext) -> None:
    setup = Setup(context, FakeWorker([INFO, ResultEvent(ResultStatus.OK, "identified")]))
    setup.ops.identify(BRIDGE, setup.recorder)
    outcome = setup.pump.until_finished(setup.recorder)

    assert isinstance(setup.worker.tasks[0], IdentifyPlan)
    assert (outcome.kind, outcome.result, outcome.record_id) == (
        JobKind.IDENTIFY,
        RecordResult.OK,
        None,
    )
    assert context.history.query() == []
    snapshot = setup.memory.snapshot
    assert snapshot is not None
    assert (snapshot.port, snapshot.info) == (BRIDGE, INFO)
    assert context.logs.find_segment(outcome.log_file, outcome.segment_id) is not None
    assert not setup.ops.busy


def test_flash_ok_writes_one_record(library: Library, context: AppContext) -> None:
    firmware = fixed_firmware(library, HELLO)
    setup = Setup(context, FakeWorker(FLASH_OK))
    outcome = setup.flash(firmware.id)

    (record,) = context.history.query()
    assert (record.operation, record.result) == (Operation.FLASH, RecordResult.OK)
    assert record.id == outcome.record_id == outcome.segment_id
    assert (record.firmware_id, record.port, record.baud) == (firmware.id, "COM3", 921600)
    assert [(e.address, e.skipped) for e in record.entries] == [
        (0x0, False),
        (0x8000, True),
        (0x10000, False),
    ]
    assert (record.chip_detected, record.mac) == ("esp32s3", MAC)
    assert record.log_file == outcome.log_file
    assert context.logs.find_segment(record.log_file, record.id) is not None
    snapshot = setup.memory.snapshot
    assert snapshot is not None and snapshot.info == INFO
    saved = Settings(context.paths.settings, context.paths.backup)
    assert saved.get(BAUD_SECTION, "10c4:ea60") == "921600"
    assert not outcome.native_usb


def test_flash_on_usb_serial_jtag_does_not_remember_baud(
    library: Library, context: AppContext
) -> None:
    firmware = fixed_firmware(library, HELLO)
    outcome = Setup(context, FakeWorker(FLASH_OK)).flash(firmware.id, JTAG, None)

    assert outcome.result == RecordResult.OK
    assert outcome.native_usb
    (record,) = context.history.query()
    assert record.baud is None
    assert context.settings.keys(BAUD_SECTION) == []


@pytest.mark.parametrize(
    ("events", "result", "reason"),
    [
        ([INFO, ErrorEvent("connection lost", "trace")], RecordResult.FAILED, None),
        (
            [
                info("esp32c3", 0, 4),
                CheckEvent(CHIP, CheckLevel.REFUSE, "chip mismatch (board: esp32c3)"),
                ResultEvent(ResultStatus.REFUSED, "refused"),
            ],
            RecordResult.REFUSED,
            "chip mismatch (board: esp32c3)",
        ),
    ],
    ids=["error", "refused"],
)
def test_flash_failure_is_recorded(
    library: Library,
    context: AppContext,
    events: list[Event],
    result: RecordResult,
    reason: str | None,
) -> None:
    firmware = fixed_firmware(library, HELLO)
    outcome = Setup(context, FakeWorker(events)).flash(firmware.id)

    (record,) = context.history.query()
    assert outcome.result == record.result == result
    if reason is not None:
        assert reason in (record.refuse_reason or "")


def test_second_job_is_busy_and_cancel_records_cancelled(
    library: Library, context: AppContext
) -> None:
    firmware = fixed_firmware(library, HELLO)
    setup = Setup(context, FakeWorker(sleep=60))
    setup.ops.flash(firmware.id, BRIDGE, None, setup.recorder)
    try:
        assert setup.ops.busy
        assert setup.ops.running_firmware_id == firmware.id
        with pytest.raises(BusyError):
            setup.ops.identify(BRIDGE, Recorder())
    finally:
        setup.ops.cancel()
    outcome = setup.pump.until_finished(setup.recorder)

    assert outcome.result == RecordResult.CANCELLED
    (record,) = context.history.query()
    assert record.result == RecordResult.CANCELLED
    assert len(setup.worker.tasks) == 1


def test_progress_is_weighted_by_entry_count(library: Library, context: AppContext) -> None:
    firmware = fixed_firmware(library, HELLO)
    events: list[Event] = [
        INFO,
        ProgressEvent(0, 0x0, 100, 100, "w"),
        ProgressEvent(1, 0x8000, 50, 100, "w"),
        ProgressEvent(2, 0x10000, 100, 100, "w"),
        ResultEvent(ResultStatus.OK, "done"),
    ]
    setup = Setup(context, FakeWorker(events))
    setup.flash(firmware.id)

    progress = setup.recorder.progress
    percents = [p.percent for p in progress]
    assert percents == sorted(percents)
    assert {p.percent for p in progress if p.entry_index == 1} == {50}
    assert percents[-1] == 100
    assert {p.entry_count for p in progress} == {3}


def test_firmware_without_chip_needs_identification(
    library: Library, context: AppContext, tmp_path: Path
) -> None:
    data = tmp_path / "data.bin"
    data.write_bytes(b"\x01" * 64)
    firmware = library.create_firmware("data")
    library.add_attachment(firmware.id, data, address=0x100000)
    setup = Setup(context, FakeWorker(FLASH_OK))

    with pytest.raises(PlanError):
        setup.ops.flash(firmware.id, BRIDGE, None, setup.recorder)
    assert setup.worker.tasks == []
    assert context.history.query() == []
    assert not setup.ops.busy


def test_flash_refuses_a_firmware_that_differs_from_the_shown_one(
    library: Library, context: AppContext
) -> None:
    firmware = fixed_firmware(library, HELLO)
    setup = Setup(context, FakeWorker(FLASH_OK))
    with pytest.raises(BuildChangedError):
        setup.ops.flash(firmware.id, BRIDGE, None, setup.recorder, shown_digest="0" * 16)
    assert setup.worker.tasks == []
    assert context.history.query() == []
    assert not setup.ops.busy


def test_erase_writes_a_record_without_firmware(context: AppContext) -> None:
    setup = Setup(context, FakeWorker([INFO, ResultEvent(ResultStatus.OK, "erased")]))
    setup.ops.erase(BRIDGE, setup.recorder)
    outcome = setup.pump.until_finished(setup.recorder)

    assert isinstance(setup.worker.tasks[0], ErasePlan)
    (record,) = context.history.query()
    assert (outcome.kind, outcome.record_id) == (JobKind.ERASE, record.id)
    assert (record.operation, record.result, record.entries) == (
        Operation.ERASE,
        RecordResult.OK,
        (),
    )
    assert (record.firmware_id, record.project_id) == (None, None)
    assert (record.chip_detected, record.mac) == ("esp32s3", MAC)
    assert "erase-flash" in record.command


def test_read_flash_creates_a_dump_firmware(library: Library, context: AppContext) -> None:
    project = library.create_project("p")
    data = bytes(range(256)) * 16
    sha = hashlib.sha256(data).hexdigest()
    events: list[Event] = [
        INFO,
        ProgressEvent(0, 0x0, len(data), len(data), "r"),
        ResultEvent(ResultStatus.OK, f"{len(data)} bytes read"),
    ]
    start = datetime.datetime(2026, 1, 2, 3, 4)
    setup = Setup(context, DumpWorker(events, data), clock=lambda: start)
    setup.ops.read_flash(
        BRIDGE, 921600, setup.recorder, size=len(data), project_id=project.id, device="board"
    )
    outcome = setup.pump.until_finished(setup.recorder)

    (task,) = setup.worker.tasks
    assert isinstance(task, ReadPlan) and (task.address, task.size) == (0x0, len(data))
    assert Path(task.output).resolve().is_relative_to(context.paths.temp.resolve())
    assert outcome.firmware_id is not None
    firmware = library.firmware(outcome.firmware_id)
    assert (firmware.name, firmware.source, firmware.chip) == (
        "esp32s3 dump 2026-01-02 03-04",
        Source.MANUAL,
        "esp32s3",
    )
    assert (firmware.project_id, firmware.device, firmware.flash) == (
        project.id,
        "board",
        FlashParams(),
    )
    assert firmware.notes  # warns that a dump may contain credentials
    (entry,) = firmware.entries
    assert (entry.kind, entry.address, entry.file, entry.size) == (
        EntryKind.MERGED,
        0x0,
        f"archive:{sha}",
        len(data),
    )
    assert entry.enabled and entry.from_dump and context.archive.has(sha)

    (record,) = context.history.query()
    assert (record.operation, record.result, record.firmware_id) == (
        Operation.READ_FLASH,
        RecordResult.OK,
        firmware.id,
    )
    assert [(e.address, e.size, e.sha256, e.kind) for e in record.entries] == [
        (0x0, len(data), sha, "merged")
    ]


def test_refused_read_creates_no_firmware(context: AppContext) -> None:
    events: list[Event] = [
        INFO,
        CheckEvent(SECURITY, CheckLevel.REFUSE, "secure download mode"),
        ResultEvent(ResultStatus.REFUSED, "refused"),
    ]
    setup = Setup(context, FakeWorker(events))
    setup.ops.read_flash(BRIDGE, None, setup.recorder)
    outcome = setup.pump.until_finished(setup.recorder)

    assert (outcome.kind, outcome.result, outcome.firmware_id) == (
        JobKind.READ_FLASH,
        RecordResult.REFUSED,
        None,
    )
    assert context.firmware.all() == []
    (record,) = context.history.query()
    assert (record.operation, record.entries, record.firmware_id) == (
        Operation.READ_FLASH,
        (),
        None,
    )


def _log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("kind", [JobKind.FLASH, JobKind.ERASE, JobKind.READ_FLASH])
def test_job_with_a_mac_copies_its_segment_to_the_board_log(
    library: Library, context: AppContext, kind: JobKind
) -> None:
    ok: list[Event] = [INFO, ResultEvent(ResultStatus.OK, "done")]
    setup = Setup(context, DumpWorker(FLASH_OK if kind is JobKind.FLASH else ok, b"\xff" * 16))
    if kind is JobKind.FLASH:
        setup.ops.flash(fixed_firmware(library, HELLO).id, BRIDGE, None, setup.recorder)
    elif kind is JobKind.ERASE:
        setup.ops.erase(BRIDGE, setup.recorder)
    else:
        setup.ops.read_flash(BRIDGE, None, setup.recorder, size=16)
    outcome = setup.pump.until_finished(setup.recorder)

    daily = _log_lines(context.paths.logs / outcome.log_file)
    assert daily[0].startswith(f"{HEADER_PREFIX}{outcome.segment_id} ")
    assert _log_lines(context.logs.board_log_path(MAC)) == daily  # the finished segment


def test_identify_with_a_mac_writes_no_board_log(context: AppContext) -> None:
    setup = Setup(context, FakeWorker([INFO, ResultEvent(ResultStatus.OK, "identified")]))
    setup.ops.identify(BRIDGE, setup.recorder)
    outcome = setup.pump.until_finished(setup.recorder)

    assert context.logs.find_segment(outcome.log_file, outcome.segment_id) is not None
    assert not (context.paths.logs / BOARDS_FOLDER).exists()


def test_job_without_a_mac_stays_in_the_daily_log(library: Library, context: AppContext) -> None:
    firmware = fixed_firmware(library, HELLO)
    outcome = Setup(context, FakeWorker([ErrorEvent("no serial data", "trace")])).flash(firmware.id)

    assert outcome.result == RecordResult.FAILED
    assert context.logs.find_segment(outcome.log_file, outcome.segment_id) is not None
    assert not (context.paths.logs / BOARDS_FOLDER).exists()
