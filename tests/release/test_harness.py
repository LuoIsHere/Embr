"""Self-tests of the release harness (no board): operator-step sequencing, the file
dialog replacement, and driving the real window through its New menu and dialogs in both
languages."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import release_kit as kit
from PySide6.QtWidgets import QFileDialog

from embr.i18n import Language, tr


def _press(text: str) -> Callable[[kit.DialogDriver], None]:
    def handler(driver: kit.DialogDriver) -> None:
        driver.press(text)

    return handler


def test_operator_step_asks_ready_then_starts_then_asks_action(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(kit.INTERACTIVE_VAR, "1")
    events: list[str] = []

    def ready(driver: kit.DialogDriver) -> None:
        assert "插上" in driver.text() and "test_harness" in driver.text()
        events.append("ready shown")
        driver.press(kit.READY_TEXT)

    def action(driver: kit.DialogDriver) -> None:
        assert "拔掉" in driver.text()
        events.append("action shown")
        driver.press(kit.DONE_TEXT)

    with (
        kit.expect_dialog(ready, name=kit.READY_NAME),
        kit.expect_dialog(action, name=kit.ACTION_NAME),
    ):
        kit.operator_step(
            "即将要求, 插上 S3 的 USB 线", "现在拔掉 USB 线", start=lambda: events.append("start")
        )
    assert events == ["ready shown", "start", "action shown"]


def test_operator_step_skip_at_ready_does_not_start(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(kit.INTERACTIVE_VAR, "1")
    started: list[bool] = []
    with (
        kit.expect_dialog(_press(kit.SKIP_TEXT), name=kit.READY_NAME),
        pytest.raises(pytest.skip.Exception, match="get-ready"),
    ):
        kit.operator_step("准备", "操作", start=lambda: started.append(True))
    assert started == []


def test_operator_step_skip_at_action_skips_after_start(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(kit.INTERACTIVE_VAR, "1")
    started: list[bool] = []
    with (
        kit.expect_dialog(_press(kit.READY_TEXT), name=kit.READY_NAME),
        kit.expect_dialog(_press(kit.SKIP_TEXT), name=kit.ACTION_NAME),
        pytest.raises(pytest.skip.Exception, match="skipped the step"),
    ):
        kit.operator_step("准备", "操作", start=lambda: started.append(True))
    assert started == [True]


def test_operator_step_times_out_and_completes_by_condition(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(kit.INTERACTIVE_VAR, "1")
    with (
        kit.expect_dialog(_press(kit.READY_TEXT), name=kit.READY_NAME),
        pytest.raises(pytest.skip.Exception, match="timed out"),
    ):
        kit.operator_step("准备", "操作", timeout=0.5)
    flag: list[bool] = []
    with kit.expect_dialog(_press(kit.READY_TEXT), name=kit.READY_NAME):
        kit.operator_step(
            "准备", "操作", start=lambda: flag.append(True), done_when=lambda: bool(flag)
        )


def test_operator_step_not_interactive_skips_without_dialog(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(kit.INTERACTIVE_VAR, "0")
    with pytest.raises(pytest.skip.Exception, match=kit.INTERACTIVE_VAR):
        kit.operator_step("准备", "操作", start=lambda: pytest.fail("must not start"))


def test_file_dialogs_hand_out_queued_answers(qapp: Any, tmp_path: Path) -> None:
    dialogs = kit.FileDialogs()
    dialogs.queue_open(tmp_path / "a.bin")
    dialogs.queue_open_many([tmp_path / "b.bin", tmp_path / "c.bin"])
    dialogs.queue_save(tmp_path / "d.embr")
    dialogs.queue_directory(tmp_path)
    dialogs.queue_cancel("open")
    with dialogs:
        assert QFileDialog.getOpenFileName(None, "x")[0] == str(tmp_path / "a.bin")
        assert QFileDialog.getOpenFileNames(None, "x")[0] == [
            str(tmp_path / "b.bin"),
            str(tmp_path / "c.bin"),
        ]
        assert QFileDialog.getSaveFileName(None, "x")[0] == str(tmp_path / "d.embr")
        assert QFileDialog.getExistingDirectory(None, "x") == str(tmp_path)
        assert QFileDialog.getOpenFileName(None, "x")[0] == ""
    dialogs.assert_consumed()
    assert QFileDialog.getOpenFileName is not dialogs  # restored
    unused = kit.FileDialogs()
    unused.queue_save(tmp_path / "e.embr")
    with pytest.raises(pytest.fail.Exception, match="not used"):
        unused.assert_consumed()


@pytest.mark.parametrize("language", [Language.ENGLISH, Language.ZH_CN])
def test_real_window_new_project_and_firmware_through_menus(
    embr: Callable[..., kit.EmbrApp], language: Language, tmp_path: Path
) -> None:
    app = embr(language=language)
    window = app.window
    assert window.isVisible() and kit.qt_app().platformName() == kit.REAL_PLATFORM
    assert kit.find_button(window, "Connect && identify").isVisible()

    def new_project(driver: kit.DialogDriver) -> None:
        driver.type("Name (unique)", "发布测试 demo")
        driver.type("Notes", "harness")
        driver.ok()

    with kit.expect_dialog(new_project, title="New project"):
        kit.new_menu(window, "New project")
    kit.wait_log(window, "[project] created '发布测试 demo'")
    assert kit.sidebar_names(window, "project") == ["发布测试 demo"]

    def new_firmware(driver: kit.DialogDriver) -> None:
        driver.type("Name", "loose")
        driver.pick("Place in", "Standalone (no project)")
        driver.ok()

    with kit.expect_dialog(new_firmware, title="New firmware"):
        kit.new_menu(window, "New firmware")
    assert "loose" in kit.sidebar_names(window)
    kit.click_sidebar(window, "发布测试 demo", "project")
    assert kit.find_button(window, "Export project…").isVisible()
    shown = kit.find_button(window, "Connect && identify").text()
    assert shown == tr("Connect && identify")
    assert (shown == "Connect && identify") == (language == Language.ENGLISH)
    app.close()
    assert (tmp_path / "embr" / "settings.ini").is_file()
