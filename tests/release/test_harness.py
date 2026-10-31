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


# ---- child-process verification: never touch an unrelated process -----------------------


def test_process_tree_never_follows_a_reused_parent_pid() -> None:
    """Snapshot rows through the seam: PID 100 is our root. 201 records parent 100 but is
    older than the root, so its real parent was an earlier owner of PID 100 (a user's
    video player, say); it and its child are excluded, as is a younger process with a
    foreign image."""
    rows = [
        kit.ProcessEntry(100, 4, "python.exe"),  # our root
        kit.ProcessEntry(101, 100, "python.exe"),  # the interpreter the launcher started
        kit.ProcessEntry(102, 101, "python.exe"),  # a worker
        kit.ProcessEntry(200, 100, "vlc.exe"),  # recorded parent 100, foreign image
        kit.ProcessEntry(201, 100, "python.exe"),  # recorded parent 100, older than it
        kit.ProcessEntry(202, 201, "python.exe"),  # child of that stale one
        kit.ProcessEntry(203, 999, "python.exe"),  # unrelated
        kit.ProcessEntry(204, 102, "conhost.exe"),  # not one of our images
    ]
    created = {100: 1000, 101: 1001, 102: 1002, 200: 1500, 201: 500, 202: 1600, 203: 1700}
    created[204] = 1800
    tree = kit.verified_tree(100, entries=rows, created=created.get)
    assert {p: v.depth for p, v in tree.items()} == {100: 0, 101: 1, 102: 2}
    gone = {**created, 100: None}
    assert kit.verified_tree(100, entries=rows, created=gone.get) == {}
    foreign_root = [kit.ProcessEntry(100, 4, "vlc.exe"), *rows[1:]]
    assert kit.verified_tree(100, entries=foreign_root, created=created.get) == {}


def test_orphan_with_stale_parent_pid_is_not_picked_up(tmp_path: Path) -> None:
    """A real orphan: a python parent starts a sleeping child and exits, so the child's
    recorded parent PID is stale. Asking for that dead parent's tree yields nothing; the
    orphan is then ended through its own verified tree only."""
    import subprocess
    import sys
    import time

    script = (
        "import subprocess, sys, os\n"
        "null = subprocess.DEVNULL\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],\n"
        "    stdin=null, stdout=null, stderr=null)\n"
        "print(os.getpid(), child.pid, flush=True)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    parent, orphan = (int(v) for v in done.stdout.split())
    try:
        deadline = time.monotonic() + 10
        while kit.creation_time(parent) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        rows = {r.pid: r for r in kit.process_snapshot()}
        assert orphan in rows and rows[orphan].parent == parent  # the stale relation exists
        assert kit.creation_time(parent) is None
        assert kit.verified_tree(parent) == {}
        assert kit.process_tree(parent) == set()
    finally:
        tree = kit.verified_tree(orphan)
        assert orphan in tree
        kit.terminate_tree(tree)
    for process in tree.values():
        deadline = time.monotonic() + 10
        while kit.still_running(process) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not kit.still_running(process)


def test_embr_process_kill_ends_exactly_its_verified_processes(tmp_path: Path) -> None:
    import sys

    sentinels = kit.Sentinels(tmp_path / "sentinels")
    with kit.EmbrProcess(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env=sentinels.env(),
        cwd=sentinels.folder("cwd"),
        logs=tmp_path / "logs",
        name="sleeper",
    ) as process:
        kit.wait_until(lambda: len(process.tree()) >= 2, 10, message="interpreter not started")
        tree = process.tree()
        rows = {r.pid: r for r in kit.process_snapshot()}
        assert {rows[p].image.lower() for p in tree} <= kit.OUR_IMAGES
        process.kill()
        assert not process.running()
    for verified in tree.values():
        kit.wait_until(lambda v=verified: not kit.still_running(v), 10, message=f"{verified} alive")
