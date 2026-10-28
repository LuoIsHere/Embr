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

An identify job sends InfoEvent, then ResultEvent(OK). LogEvents may appear anywhere.
"""

import io
import os
import sys
import threading
import traceback
from contextlib import redirect_stdout
from importlib.resources import files
from typing import TextIO

from embr.core.flasher.plan import IdentifyPlan
from embr.core.flasher.protocol import ErrorEvent, Event, ProtocolError, decode_task, encode_event


def esptool_config_path() -> str:
    """Bundled esptool.cfg, so user-level esptool configs cannot change behaviour."""
    return str(files("embr.resources").joinpath("esptool.cfg"))


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

    # esptool reads its config when imported; point it at ours first
    os.environ.setdefault("ESPTOOL_CFGFILE", esptool_config_path())
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
            else:
                jobs.flash(task, send, cursor)
    except BaseException as exc:  # KeyboardInterrupt included: always end with an event
        send(ErrorEvent(str(exc) or type(exc).__name__, traceback.format_exc()))
        return 1
    return 0
