"""Worker jobs refusing a chip with security features: the chip is reset as the
plan's `after` mode, like after any other refusal, and nothing is written, erased or read.
The connection is faked; no port is opened."""

from collections.abc import Callable
from typing import Any

import pytest
from embr_testkit import idf_entries

from embr.core.flasher.checks import ChipState, SecurityState
from embr.core.flasher.plan import ErasePlan, FlashPlan, FlashSettings, IdentifyPlan, ReadPlan
from embr.core.flasher.protocol import (
    Event,
    InfoEvent,
    LogEvent,
    LogLevel,
    ResultEvent,
    ResultStatus,
)
from embr.core.flasher.worker import jobs
from embr.core.flasher.worker.logger import EntryCursor
from embr.core.serial import ConnectionType

SECURED = SecurityState(secure_download_mode=False, flash_encryption=True, secure_boot=False)


class FakeChip:
    """What the jobs do to the connected chip."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reset_fails: bool = False,
        security: SecurityState = SECURED,
    ) -> None:
        self.calls: list[str] = []
        self.reset_fails = reset_fails
        self.security = security
        monkeypatch.setattr(jobs, "_connect", self._connect)
        monkeypatch.setattr(jobs, "_read_chip", self._read_chip)
        monkeypatch.setattr(jobs, "_close", self._close)
        monkeypatch.setattr(jobs, "reset_chip", self._reset)
        for name in ("write_flash", "erase_flash", "esptool_read_flash"):
            monkeypatch.setattr(jobs, name, self._touched(name))

    def _connect(self, *_args: object) -> "FakeChip":
        return self

    def _read_chip(self, esp: object, *_args: object) -> tuple[object, InfoEvent, ChipState]:
        info = InfoEvent("esp32s3", "v0.2", 0, 2, "aa:bb:cc:00:00:01", "8MB", self.security, False)
        return esp, info, ChipState("esp32s3", 0, 2, "8MB", self.security)

    def _close(self, _esp: object) -> None:
        self.calls.append("close")

    def _reset(self, _esp: object, mode: str) -> None:
        self.calls.append(f"reset {mode}")
        if self.reset_fails:
            raise OSError("no reset in this mode")

    def _touched(self, name: str) -> Callable[..., None]:
        def touched(*_args: Any, **_kwargs: Any) -> None:
            self.calls.append(name)

        return touched


def _run(job: str, sent: list[Event]) -> None:
    if job == "flash":
        plan = FlashPlan(
            port="COM250",
            connection=ConnectionType.UART_BRIDGE,
            baud=460800,
            chip="esp32s3",
            before="default-reset",
            after="hard-reset",
            stub=True,
            flash=FlashSettings(),
            entries=tuple(idf_entries("esp32s3_hello")),
            erase_all=False,
            skip_flashed=True,
        )
        jobs.flash(plan, sent.append, EntryCursor())
    elif job == "erase":
        jobs.erase(
            ErasePlan("COM250", ConnectionType.UART_BRIDGE, "default-reset", "hard-reset"),
            sent.append,
        )
    else:
        read = ReadPlan(
            "COM250", ConnectionType.UART_BRIDGE, None, "default-reset", "hard-reset", 0, 16, "x"
        )
        jobs.read_flash(read, sent.append, EntryCursor())


@pytest.mark.parametrize("job", ["flash", "erase", "read"])
def test_a_secured_chip_is_reset_after_the_refusal(
    monkeypatch: pytest.MonkeyPatch, job: str
) -> None:
    chip = FakeChip(monkeypatch)
    sent: list[Event] = []
    _run(job, sent)

    assert chip.calls == ["reset hard-reset", "close"]  # nothing written, erased or read
    result = sent[-1]
    assert isinstance(result, ResultEvent) and result.status == ResultStatus.REFUSED
    assert result.message.startswith("security features enabled; nothing ")


def test_a_reset_that_fails_is_a_warning_and_the_refusal_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeChip(monkeypatch, reset_fails=True)
    sent: list[Event] = []
    _run("erase", sent)

    warning = [e for e in sent if isinstance(e, LogEvent) and e.level == LogLevel.WARNING]
    assert [w.text for w in warning] == ["reset after the refusal failed: no reset in this mode"]
    result = sent[-1]
    assert isinstance(result, ResultEvent) and result.status == ResultStatus.REFUSED


@pytest.mark.parametrize("sdm", [True, False], ids=["secure-download-mode", "plain"])
def test_identify_says_where_it_leaves_the_chip(monkeypatch: pytest.MonkeyPatch, sdm: bool) -> None:
    # a chip in secure download mode is not reset, so it stays in download mode
    security = SecurityState(secure_download_mode=sdm, flash_encryption=False, secure_boot=False)
    chip = FakeChip(monkeypatch, security=security)
    sent: list[Event] = []
    plan = IdentifyPlan("COM250", ConnectionType.UART_BRIDGE, "default-reset", "hard-reset")
    jobs.identify(plan, sent.append)

    assert chip.calls == (["close"] if sdm else ["reset hard-reset", "close"])
    info = [e for e in sent if isinstance(e, InfoEvent)]
    assert [i.in_download_mode for i in info] == [sdm]
