"""Messages made outside the UI and shown in it translate as whole sentences: each matches
its own template (not a shorter, more general one) and names chips by display name."""

# ruff: noqa: RUF001  # full-width punctuation of the expected Chinese text

from collections.abc import Iterator

import pytest

from embr.app.chips import Mcu, not_for_board
from embr.core.flasher.checks import ChipState, SecurityState, check_chip, revision_incompatibility
from embr.core.flasher.plan import PlanEntry, adjust_reset
from embr.core.serial import ConnectionType
from embr.i18n import Language, current, set_language, tr

OFF = SecurityState(secure_download_mode=False, flash_encryption=False, secure_boot=False)


def _entry(
    image_chip: str | None = None, low: int | None = None, high: int | None = None
) -> PlanEntry:
    return PlanEntry(0x10000, "C:\\fw\\app.bin", "app.bin", "0" * 64, 16, image_chip, low, high, 0)


def _chip(chip: str, major: int, minor: int) -> ChipState:
    return ChipState(chip, major, minor, "4MB", OFF)


def _revision(state: ChipState, entry: PlanEntry) -> str:
    reason = revision_incompatibility(state, entry)
    assert reason is not None
    return reason


CASES = [
    (
        check_chip("esp32s3", "esp32p4", []).message,
        "芯片不符：设备是 ESP32-S3，固件适用于 ESP32-P4",
    ),
    (
        check_chip("esp32s3", "esp32p4", [_entry("esp32p4")]).message,
        "芯片不符：设备是 ESP32-S3，固件适用于 ESP32-P4；适用于其他芯片的镜像：app.bin (ESP32-P4)",
    ),
    (
        check_chip("esp32s3", None, [_entry("esp32p4")]).message,
        "芯片不符：设备是 ESP32-S3；适用于其他芯片的镜像：app.bin (ESP32-P4)",
    ),
    (
        check_chip("esp32s3", "esp32s3", [_entry("esp32s3")]).message,
        "ESP32-S3：设备、固件与镜像一致",
    ),
    (
        _revision(_chip("esp32p4", 1, 0), _entry(low=300, high=0xFFFF)),
        "app.bin 要求芯片版本 v3.0 或更高，芯片为 v1.0",
    ),
    (
        _revision(_chip("esp32p4", 3, 0), _entry(low=0, high=199)),
        "app.bin 要求芯片版本 v1.99 或更低，芯片为 v3.0",
    ),
    (
        _revision(_chip("esp32p4", 0, 0), _entry(low=100, high=199)),
        "app.bin 要求芯片版本 v1.0 至 v1.99，芯片为 v0.0",
    ),
    (
        adjust_reset(ConnectionType.UART_BRIDGE, "usb-reset", "hard-reset")[2][0],
        "before=usb-reset -> default-reset：此端口不是 USB-Serial/JTAG",
    ),
    (
        not_for_board((Mcu("esp32p4"),), "esp32s3", "v0.2"),
        "固件适用于 ESP32-P4，设备是 ESP32-S3 v0.2",
    ),
    (
        not_for_board((Mcu("esp32p4"),), "esp32s3", ""),  # revision not read
        "固件适用于 ESP32-P4，设备是 ESP32-S3",
    ),
    # not taken by the more general "{0} has no {1}"
    ("No MCU set for data.bin", "未为 data.bin 设置 MCU"),
]


@pytest.fixture
def chinese() -> Iterator[None]:
    before = current()
    set_language(Language.ZH_CN)
    yield
    set_language(before)


@pytest.mark.parametrize(("text", "expected"), CASES, ids=[str(i) for i in range(len(CASES))])
def test_the_whole_message_translates(chinese: None, text: str, expected: str) -> None:
    assert tr(text) == expected
