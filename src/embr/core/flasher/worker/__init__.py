"""Worker process: the only place that opens serial ports.

Started as `Embr.exe --worker` (or `python -m embr --worker`).

Event order for a flash job:
1. CheckEvent(RESET, WARN) for each plan.adjustments entry, else one CheckEvent(RESET, PASS)
2. CheckEvent(FILES) from check_files(); REFUSE -> ResultEvent(REFUSED), port never opened
3. connect -> InfoEvent
4. CheckEvents from check_security(); any REFUSE -> ResultEvent(REFUSED), nothing written
   (with secure download mode nothing else is read from the chip)
5. CheckEvents from check_chip(), check_revisions(), check_capacity(); any REFUSE ->
   ResultEvent(REFUSED), nothing written
6. per entry, in address order: ProgressEvents (address = entry address, cur rising),
   then EntryEvent(WRITTEN or SKIPPED)
7. reset as plan.after -> ResultEvent(OK)

An identify job sends InfoEvent, then ResultEvent(OK). Erase and read_flash jobs: see
jobs.erase() and jobs.read_flash(). LogEvents may appear anywhere.

ESPTOOL_CFGFILE is set by embr.main before this package is imported: importing it imports
esptool, which reads its config file at that moment.
"""

import io
import sys
import threading
import traceback
from contextlib import redirect_stdout
from typing import TextIO

from embr.core.flasher.plan import ErasePlan, IdentifyPlan, ReadPlan
from embr.core.flasher.protocol import ErrorEvent, Event, ProtocolError, decode_task, encode_event


def run(stdin: TextIO, stdout: TextIO) -> int:
    """Read one task from `stdin`, run it, write events to `stdout` (one JSON line each,
    flushed per line) and return the process exit code: 0 when a ResultEvent was sent,
    non-zero after an ErrorEvent. Every exception becomes an ErrorEvent."""
    lock = threading.Lock()

    def send(event: Event) -> None:
        line = encode_event(event) + "\n"
        with lock:
            stdout.write(line)
            stdout.flush()

    try:
        task = decode_task(stdin.read())
    except ProtocolError as exc:
        send(ErrorEvent(f"invalid task: {exc}", ""))
        return 2

    from esptool.logger import log

    from embr.core.flasher.worker import jobs
    from embr.core.flasher.worker.logger import EntryCursor, EventLogger

    cursor = EntryCursor()
    log.set_logger(EventLogger(send, cursor))  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]  # proxy type hides set_logger
    # stray prints must not corrupt the event stream
    sink = sys.stderr if sys.stderr is not None else io.StringIO()
    try:
        with redirect_stdout(sink):
            if isinstance(task, IdentifyPlan):
                jobs.identify(task, send)
            elif isinstance(task, ErasePlan):
                jobs.erase(task, send)
            elif isinstance(task, ReadPlan):
                jobs.read_flash(task, send, cursor)
            else:
                jobs.flash(task, send, cursor)
    except BaseException as exc:  # KeyboardInterrupt included: always end with an event
        send(ErrorEvent(str(exc) or type(exc).__name__, traceback.format_exc()))
        return 1
    return 0
