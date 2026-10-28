"""Command-line demo of the flashing engine, for checks on real boards.

python -m embr.devtools ports
python -m embr.devtools identify -p COM3
python -m embr.devtools flash -p COM3 --chip esp32s3 0x0:bootloader.bin 0x8000:pt.bin ...
"""

import argparse
import hashlib
import sys
import threading
from pathlib import Path

from embr.core import serial
from embr.core.firmware.image_header import read_image_header
from embr.core.flasher import runner
from embr.core.flasher.plan import (
    FlashSettings,
    PlanEntry,
    build_flash_plan,
    build_identify_plan,
    command_line,
)
from embr.core.flasher.protocol import (
    ErrorEvent,
    Event,
    ProgressEvent,
    ResultEvent,
    ResultStatus,
    Task,
    encode_event,
)


def _port(device: str) -> serial.PortInfo:
    for p in serial.list_ports():
        if p.device.lower() == device.lower():
            return p
    return serial.PortInfo(device, None, None, None, "", serial.ConnectionType.UNKNOWN, None)


def _entry(spec: str) -> PlanEntry:
    address, _, path = spec.partition(":")
    file = Path(path).resolve()
    data = file.read_bytes()
    header = read_image_header(data)
    return PlanEntry(
        address=int(address, 16),
        path=str(file),
        name=file.name,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        image_chip=header.chip if header else None,
        min_rev_full=header.min_rev_full if header else None,
        max_rev_full=header.max_rev_full if header else None,
        legacy_min_rev=header.legacy_min_rev if header else None,
    )


def _run(task: Task) -> int:
    done = threading.Event()
    status: list[ResultStatus | None] = [None]

    def on_event(event: Event) -> None:
        if isinstance(event, ProgressEvent) and event.cur not in (1, event.total):
            return  # keep the output short
        print(encode_event(event), flush=True)
        if isinstance(event, ResultEvent):
            status[0] = event.status
        if isinstance(event, ResultEvent | ErrorEvent):
            done.set()

    job = runner.start(task, on_event)
    try:
        job.wait()
    except KeyboardInterrupt:
        job.cancel()
        job.wait(5)
    return 0 if status[0] == ResultStatus.OK else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="embr.devtools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ports")
    ident = sub.add_parser("identify")
    ident.add_argument("-p", "--port", required=True)
    flash = sub.add_parser("flash")
    flash.add_argument("-p", "--port", required=True)
    flash.add_argument("--chip", required=True)
    flash.add_argument("-b", "--baud", type=int)
    flash.add_argument("--before", default="default-reset")
    flash.add_argument("--after", default="hard-reset")
    flash.add_argument("--erase-all", action="store_true")
    flash.add_argument("entries", nargs="+", help="ADDRESS:FILE")
    args = parser.parse_args(argv)

    if args.command == "ports":
        for p in serial.list_ports():
            ids = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None and p.pid is not None else "-"
            print(f"{p.device:8} {ids:10} {p.connection:16} {p.description}")
        return 0
    port = _port(args.port)
    if args.command == "identify":
        return _run(build_identify_plan(port.device, port.connection))
    plan = build_flash_plan(
        port=port.device,
        connection=port.connection,
        chip=args.chip,
        entries=[_entry(s) for s in args.entries],
        before=args.before,
        after=args.after,
        baud=args.baud,
        flash=FlashSettings(),
        erase_all=args.erase_all,
    )
    print(command_line(plan), flush=True)
    return _run(plan)


if __name__ == "__main__":
    sys.exit(main())
