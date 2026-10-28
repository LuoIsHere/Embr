"""Smoke-check a packaged build: the GUI-subsystem exe must deliver worker events over
pipes, and cancel must kill a worker stuck connecting.

    uv run python scripts/check_frozen_worker.py dist/Embr/Embr.exe
"""

import socket
import sys
import threading
import time
from pathlib import Path

from embr.core.flasher import runner
from embr.core.flasher.plan import IdentifyPlan
from embr.core.flasher.protocol import ErrorEvent, Event, ResultEvent, ResultStatus
from embr.core.serial import ConnectionType


def main() -> int:
    exe = Path(sys.argv[1]).resolve()
    command = [str(exe), "--worker"]
    cfg = exe.parent / "_internal" / "embr" / "resources" / "esptool.cfg"
    if not cfg.is_file():
        print(f"FAIL: {cfg} missing")
        return 1

    events: list[Event] = []
    plan = IdentifyPlan("COM250", ConnectionType.UART_BRIDGE, "default-reset", "hard-reset")
    job = runner.start(plan, events.append, command=command)
    if not job.wait(60) or not events or not isinstance(events[-1], ErrorEvent):
        print(f"FAIL: missing port should end with an ErrorEvent, got {events[-1:]}")
        return 1
    print("ok: events arrive over pipes")

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    held: list[object] = []  # keep the accepted connection open and silent
    threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
    url = f"rfc2217://127.0.0.1:{server.getsockname()[1]}"
    events = []
    job = runner.start(
        IdentifyPlan(url, ConnectionType.UNKNOWN, "default-reset", "hard-reset"),
        events.append,
        command=command,
    )
    time.sleep(2)
    started = time.monotonic()
    job.cancel()
    finished = job.wait(5)
    elapsed = time.monotonic() - started
    last = events[-1] if events else None
    if (
        not finished
        or elapsed > 1
        or not (isinstance(last, ResultEvent) and last.status == ResultStatus.CANCELLED)
    ):
        print(f"FAIL: cancel took {elapsed:.2f}s, last event {last}")
        return 1
    print(f"ok: cancel while connecting took {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
