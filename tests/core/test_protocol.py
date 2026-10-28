"""core/flasher/protocol: task/event round trips, JSON Lines, ProtocolError on garbage."""

import json

import pytest
from embr_testkit import idf_entries

from embr.core.flasher.checks import CheckLevel, SecurityState
from embr.core.flasher.plan import FlashPlan, FlashSettings, IdentifyPlan
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
    ProtocolError,
    ResultEvent,
    ResultStatus,
    Task,
    decode_event,
    decode_task,
    encode_event,
    encode_task,
)
from embr.core.serial import ConnectionType


def test_task_round_trip() -> None:
    tasks: list[Task] = [
        FlashPlan(
            port="COM3",
            connection=ConnectionType.USB_SERIAL_JTAG,
            baud=None,
            chip="esp32s3",
            before="usb-reset",
            after="watchdog-reset",
            stub=False,
            flash=FlashSettings("dio", "80m", "4MB"),
            entries=tuple(idf_entries("esp32s3_hello")),
            erase_all=False,
            skip_flashed=True,
            declared_flash_size="4MB",
            partitions_end=0x110000,
            adjustments=("after: hard-reset -> watchdog-reset",),
        ),
        IdentifyPlan(
            port="COM12", connection=ConnectionType.USB_OTG, before="no-reset", after="no-reset"
        ),
    ]
    for task in tasks:
        text = encode_task(task)
        assert isinstance(json.loads(text), dict) and not text.endswith("\n")
        assert decode_task(text) == task


def test_event_round_trip_single_json_line() -> None:
    events: list[Event] = [
        InfoEvent(
            "esp32p4",
            "v1.0",
            1,
            0,
            "aa:bb:cc:dd:ee:ff",
            None,
            SecurityState(True, False, True),
            True,
        ),
        ProgressEvent(entry_index=2, address=0x10000, cur=4096, total=200000, text="w"),
        ErrorEvent(message="crashed", details="Traceback:\n  line 1\nValueError: ü"),
    ]
    events += [CheckEvent(name="chip", level=level, message="m") for level in CheckLevel]
    events += [LogEvent(level=level, text="a\nb\r\n中文") for level in LogLevel]
    events += [EntryEvent(entry_index=1, address=0x8000, status=status) for status in EntryStatus]
    events += [ResultEvent(status=status, message="x") for status in ResultStatus]
    for event in events:
        line = encode_event(event)
        assert "\n" not in line and "\r" not in line
        assert "type" in json.loads(line)
        assert decode_event(line) == event


@pytest.mark.parametrize(
    "text", ["", "garbage", "{", "null", "[]", "{}", '{"type": "no-such-event"}']
)
def test_garbage_raises_protocol_error(text: str) -> None:
    with pytest.raises(ProtocolError):
        decode_event(text)
    with pytest.raises(ProtocolError):
        decode_task(text)
