"""core/logs/crash: exceptions no code handled are written to logs/crash-<date>.log; on the
main thread the window is told as well."""

import datetime
import faulthandler
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from embr.core.logs.crash import FAULT_FILE, CrashLog, crash_log_name

NOW = datetime.datetime(2026, 11, 9, 10, 30)


def _silent(*_args: object) -> None:
    """A default hook that prints nothing (pytest's own would report the exception)."""


@pytest.fixture
def crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CrashLog]:
    # the hooks passed on to are silent ones, not pytest's
    monkeypatch.setattr(sys, "excepthook", _silent)
    monkeypatch.setattr(threading, "excepthook", _silent)
    log = CrashLog(tmp_path / "logs", clock=lambda: NOW)
    log.install()
    yield log
    log.uninstall()
    faulthandler.enable()  # pytest's, which install() replaced


def _raise() -> None:
    raise ValueError("bad value")


def _report(kind: type[BaseException]) -> None:
    try:
        raise kind("x")
    except kind as exc:
        sys.excepthook(kind, exc, exc.__traceback__)


def test_an_unhandled_exception_is_written_and_reported(crash: CrashLog, tmp_path: Path) -> None:
    told: list[tuple[str, Path]] = []
    crash.on_error = lambda summary, path: told.append((summary, path))
    try:
        _raise()
    except ValueError as exc:
        sys.excepthook(ValueError, exc, exc.__traceback__)

    path = tmp_path / "logs" / crash_log_name(NOW.date())
    text = path.read_text(encoding="utf-8")
    assert text.startswith("=== 2026-11-09 10:30:00 thread MainThread\n")
    assert "in _raise" in text and "ValueError: bad value" in text
    assert told == [("ValueError: bad value", path)]
    assert (tmp_path / "logs" / FAULT_FILE).is_file()


def test_a_thread_exception_is_written_but_not_reported(crash: CrashLog, tmp_path: Path) -> None:
    told: list[str] = []
    crash.on_error = lambda summary, _path: told.append(summary)
    worker = threading.Thread(target=_raise, name="reader")
    worker.start()
    worker.join()

    text = (tmp_path / "logs" / crash_log_name(NOW.date())).read_text(encoding="utf-8")
    assert "thread reader" in text and "ValueError: bad value" in text
    assert told == []  # the window is only told from the main thread


def test_ctrl_c_and_a_failing_reporter_write_nothing_more(crash: CrashLog, tmp_path: Path) -> None:
    def broken(_summary: str, _path: Path) -> None:
        raise RuntimeError("the window is gone")

    crash.on_error = broken
    _report(KeyboardInterrupt)
    assert not (tmp_path / "logs" / crash_log_name(NOW.date())).exists()
    _report(OSError)  # the reporter raising does not raise out of the hook
    text = (tmp_path / "logs" / crash_log_name(NOW.date())).read_text(encoding="utf-8")
    assert text.count("===") == 1


def test_uninstall_puts_the_previous_hooks_back(tmp_path: Path) -> None:
    before = (sys.excepthook, threading.excepthook)
    log = CrashLog(tmp_path / "logs")
    log.install()
    assert sys.excepthook != before[0]
    log.uninstall()
    faulthandler.enable()
    assert (sys.excepthook, threading.excepthook) == before
