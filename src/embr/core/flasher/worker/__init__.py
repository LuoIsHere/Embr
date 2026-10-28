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

from typing import TextIO


def run(stdin: TextIO, stdout: TextIO) -> int:
    """Read one task from `stdin`, run it, write events to `stdout` (one JSON line each,
    flushed per line) and return the process exit code: 0 when a ResultEvent was sent,
    non-zero after an ErrorEvent. Every exception becomes an ErrorEvent."""
    raise NotImplementedError
