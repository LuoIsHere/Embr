"""Tracking messages in the workspace: the banner when platformio.ini no longer declares the
followed env, and the log line for a renamed app file. Statuses are
built by hand; the tracker's own rules are tested in tests/app and tests/core."""

import dataclasses
import datetime
from collections.abc import Callable
from pathlib import Path

import pytest

from embr.app.tracking import TrackStatus
from embr.core.firmware.model import BuildRef, Firmware, Source
from embr.core.firmware.tracking import Change, ChangeKind, ResolveState
from embr.i18n import Language, current, set_language, tr
from embr.ui.window import EmbrWindow

# pyright: reportPrivateUsage=false

CHECKED = datetime.datetime(2026, 11, 8, 10, 0, 0)


def _status(tmp_path: Path, **fields: object) -> TrackStatus:
    firmware = Firmware(
        id="pio",
        name="esp32dev",
        project_id=None,
        device="",
        source=Source.PIO_BUILD,
        build_ref=BuildRef(str(tmp_path), env="esp32dev"),
    )
    base = TrackStatus(
        firmware_id=firmware.id,
        state=ResolveState.INCOMPLETE,
        firmware=firmware,
        changes=(),
        problems=("platformio.ini no longer declares [env:esp32dev]",),
        marks={},
        digest="",
        checked_at=CHECKED,
        updated_at=None,
    )
    return dataclasses.replace(base, **fields)


def _found(candidates: tuple[str, ...]) -> Callable[[str], tuple[str, ...]]:
    def env_candidates(firmware_id: str) -> tuple[str, ...]:
        return candidates

    return env_candidates


@pytest.mark.parametrize(
    ("candidates", "action", "mode"),
    [
        (("esp32dev_v2",), "Follow env esp32dev_v2", "follow_env"),
        ((), "Change directory…", "directory"),
        (("a", "b"), "Change directory…", "directory"),
    ],
)
def test_an_env_gone_offers_the_one_candidate(
    window: EmbrWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidates: tuple[str, ...],
    action: str,
    mode: str,
) -> None:
    monkeypatch.setattr(window.library.tracker, "env_candidates", _found(candidates))
    view = window.firmware_view
    view._tracking_banner(_status(tmp_path, env_gone=True))

    banner = view.banner
    assert banner.text.text() == (
        "platformio.ini no longer declares [env:esp32dev]. It may have been renamed."
    )
    assert (banner.action.text(), banner.mode) == (action, mode)


def test_the_banner_button_asks_the_window(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(window, "firmware_action", asked.append)
    monkeypatch.setattr(window.library.tracker, "env_candidates", _found(("esp32dev_v2",)))
    view = window.firmware_view
    status = _status(tmp_path, env_gone=True)
    view._firmware = status.firmware
    view._tracking_banner(status)

    view.banner.action.click()
    assert asked == ["follow_env"]


def test_the_env_gone_texts_are_translated() -> None:
    language = current()
    set_language(Language.ZH_CN)
    try:
        assert tr("platformio.ini no longer declares [env:x]. It may have been renamed.") == (
            "platformio.ini 已不再声明 [env:x]，可能已改名。"  # noqa: RUF001
        )
        assert tr("Follow env x") == "改为跟随 env：x"  # noqa: RUF001
        renamed = tr("App file renamed: a.bin -> b.bin")
        assert renamed == "应用文件已改名：a.bin -> b.bin"  # noqa: RUF001
    finally:
        set_language(language)


def test_a_renamed_app_is_logged_once(window: EmbrWindow, tmp_path: Path) -> None:
    renamed = Change(
        ChangeKind.ENTRY_RENAMED,
        "my_app.bin",
        "firmware.bin",
        "my_app.bin",
        "App file renamed: firmware.bin -> my_app.bin",
    )
    status = _status(tmp_path, state=ResolveState.UPDATED, changes=(renamed,), problems=())
    line = "[track] esp32dev: app file renamed firmware.bin -> my_app.bin"

    window._log_renames(status)
    window._log_renames(status)  # shown again: the same resolution
    assert window.log_panel.log.toPlainText().count(line) == 1

    later = dataclasses.replace(status, checked_at=CHECKED + datetime.timedelta(1))
    window._log_renames(later)
    assert window.log_panel.log.toPlainText().count(line) == 2
