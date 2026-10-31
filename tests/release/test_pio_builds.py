"""Release tests with real PlatformIO builds of the test project (EMBR_TEST_PIO_PROJECT,
built with `pio run`; Embr itself never builds) and the ESP32-S3 on its UART bridge. Each
test writes its own platformio.ini (and sources) and builds what it needs; the pio_project
fixture restores the project after the session and rebuilds its original env.

Design rules checked (design doc §5 stage three, §3.4, §12.5, §12.7 "PIO"): several envs
imported at once; boot_app0.bin taken from the installed framework package and placed at
the partition table's otadata (0xE000, or where a custom table puts it); a changed board
switches the target until rebuilt, then the chip change needs confirmation; `pio run -t
clean` makes the build missing; an idedata.json older than platformio.ini is not used; envs
added and removed; two Arduino framework packages (official espressif32 6.12.0 and an older
6.5.0): highest version or the platform_packages pin, a manual package path set, invalid
and restored; an espidf env; Arduino core 3.x (pioarduino) is outside v0.1.0: imported,
flashed and booting correctly, or clearly refused, never flashed wrong.

Platforms needed in the PIO core besides espressif32 6.12.0 (installed once, kept):
espressif32@6.5.0 (official registry) and pioarduino PIOARDUINO (GitHub release).
"""

import json
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import builds_kit as bk
import pytest
import release_kit as kit
from PySide6.QtWidgets import QCheckBox, QLineEdit

from embr.core.firmware.tracking import ChangeKind, ResolveState
from embr.core.history import RecordResult

S3_BOARD = "esp32-s3-devkitc-1"
OLD_PLATFORM = "espressif32@6.5.0"
PIOARDUINO = (
    "https://github.com/pioarduino/platform-espressif32/releases/download/"
    "55.03.311/platform-espressif32.zip"
)
BOOT_APP0 = "boot_app0.bin"
HEAD = "; Embr release test: written by tests/release/test_pio_builds.py, restored afterwards\n\n"


def _ini(project: bk.PioProject, *sections: str) -> None:
    project.write_ini(HEAD + "".join(sections))


def _build(project: bk.PioProject, *envs: str) -> str:
    args = ["run"]
    for env in envs:
        args += ["-e", env]
    output = project.run(*args)
    time.sleep(bk.SETTLE_S)
    return output


def _start(project: bk.PioProject, embr: Callable[..., kit.EmbrApp]) -> kit.EmbrApp:
    kit.board_port(bk.UART)
    project.reset()
    project.mark_sketch()
    return embr()


def _boot_app0_of(entries: list[tuple[int | None, str, str, bool]]) -> tuple[int | None, Path]:
    found = [(a, Path(f.split(":", 1)[1])) for a, n, f, _ in entries if n == BOOT_APP0]
    assert len(found) == 1, entries
    return found[0]


def _erase(window: Any) -> None:
    with kit.expect_dialog(lambda d: d.ok(), title="Erase flash"):
        kit.click(window.connection.erase)
    assert kit.same_text(kit.wait_job_finished(window), "Done"), kit.log_text(window)


def _flash_boots(app: kit.EmbrApp, name: str, mark: str) -> None:
    uart = kit.board_port(bk.UART)
    window = app.window
    bk.identify(window, uart)
    kit.click_sidebar(window, name)
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    assert bk.last_record(app).result == RecordResult.OK
    out = bk.listen(uart, 5, reset=True)
    assert f"{bk.MARK} {mark}" in out, out[-1500:]


def _package_versions(core: Path) -> dict[Path, str]:
    found: dict[Path, str] = {}
    for folder in (core / "packages").glob("framework-arduinoespressif32*"):
        manifest = folder / "package.json"
        if (folder / "tools" / "partitions" / BOOT_APP0).is_file() and manifest.is_file():
            found[folder] = str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    return found


def _numeric(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("+", 1)[0].split(".") if p.isdigit())


# ---- several envs, boot_app0 at the otadata partition -----------------------------------


def test_multi_env_import_boot_app0_from_package_flashed(
    pio_project: bk.PioProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
) -> None:
    uart = kit.board_port(bk.UART)
    app = _start(pio_project, embr)
    window = app.window
    _ini(
        pio_project,
        bk.ini_env("esp32dev", "esp32dev", "pio-dev"),
        bk.ini_env("s3", S3_BOARD, "pio-s3"),
    )
    _build(pio_project, "esp32dev", "s3")

    def inspect(driver: kit.DialogDriver) -> None:
        boxes = bk.env_boxes(driver)
        assert set(boxes) == {"esp32dev", "s3"}, list(boxes)
        assert all(b.isEnabled() for b in boxes.values())
        assert boxes["s3"].isChecked() and not boxes["esp32dev"].isChecked()  # the picked one
        assert S3_BOARD in kit.text_of(boxes["s3"]) and "esp32s3" in kit.text_of(boxes["s3"])

    bk.import_pio(
        window, file_dialogs, pio_project.build_dir("s3"), ["esp32dev", "s3"], inspect=inspect
    )
    assert {"esp32dev", "s3"} <= set(kit.sidebar_names(window)), kit.log_text(window)
    s3 = bk.wait_state(app, "s3", "ok", "updated")
    dev = bk.wait_state(app, "esp32dev", "ok", "updated")
    assert (s3.firmware.chip, dev.firmware.chip) == ("esp32s3", "esp32")
    s3_entries = bk.entries_of(app, "s3")
    by_name = {n: a for a, n, _, _ in s3_entries}
    assert by_name == {
        "bootloader.bin": 0x0,
        "partitions.bin": 0x8000,
        BOOT_APP0: 0xE000,
        "firmware.bin": 0x10000,
    }, s3_entries
    assert {n: a for a, n, _, _ in bk.entries_of(app, "esp32dev")}["bootloader.bin"] == 0x1000
    address, boot = _boot_app0_of(s3_entries)
    assert boot.parents[2].parent == bk.pio_core() / "packages", boot  # the installed package

    bk.identify(window, uart)
    assert bk.sidebar_dimmed(window, "esp32dev") and not bk.sidebar_dimmed(window, "s3")
    _erase(window)
    _flash_boots(app, "s3", "pio-s3")
    rb = tmp_path / "rb"
    assert bk.read_back(uart, 0xE000, 0x2000, rb) == boot.read_bytes()
    firmware = (pio_project.build_dir("s3") / "firmware.bin").read_bytes()
    assert bk.read_back(uart, 0x10000, len(firmware), rb) == firmware
    assert address == 0xE000


def test_custom_partition_table_moves_boot_app0_to_its_otadata(
    pio_project: bk.PioProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
) -> None:
    uart = kit.board_port(bk.UART)
    app = _start(pio_project, embr)
    window = app.window
    (pio_project.root / "embr_parts.csv").write_text(
        "# Name,   Type, SubType, Offset,   Size\n"
        "nvs,      data, nvs,     0x9000,   0x4000\n"
        "otadata,  data, ota,     0xD000,   0x2000\n"
        "app0,     app,  ota_0,   0x10000,  0x140000\n"
        "app1,     app,  ota_1,   0x150000, 0x140000\n"
        "spiffs,   data, spiffs,  0x290000, 0x160000\n",
        encoding="utf-8",
    )
    _ini(
        pio_project,
        bk.ini_env("s3", S3_BOARD, "pio-ota", extra="board_build.partitions = embr_parts.csv"),
    )
    _build(pio_project, "s3")
    bk.import_pio(window, file_dialogs, pio_project.build_dir("s3"), ["s3"])
    bk.wait_state(app, "s3", "ok", "updated")
    address, boot = _boot_app0_of(bk.entries_of(app, "s3"))
    assert address == 0xD000  # PlatformIO itself would write it at 0xE000
    status = bk.status_of(app, "s3")
    notes = " ".join([*status.entry_notes.values(), *status.build_notes])
    assert "0xE000" in notes and "0xD000" in notes, notes
    bk.identify(window, uart)
    _erase(window)
    _flash_boots(app, "s3", "pio-ota")
    rb = tmp_path / "rb"
    assert bk.read_back(uart, 0xD000, 0x2000, rb) == boot.read_bytes()
    # PlatformIO would have put the 8 KB boot_app0 at 0xE000-0xFFFF; Embr wrote it at the
    # otadata partition 0xD000-0xEFFF, so 0xF000 (erased before) stays blank
    assert bk.read_back(uart, 0xF000, 0x1000, rb) == b"\xff" * 0x1000


# ---- the board of an env changes -------------------------------------------------------


def test_board_change_switches_target_until_rebuilt_then_confirms(
    pio_project: bk.PioProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
) -> None:
    app = _start(pio_project, embr)
    window = app.window
    _ini(pio_project, bk.ini_env("esp32dev", "esp32dev", "pio-dev"))
    _build(pio_project, "esp32dev")
    bk.import_pio(window, file_dialogs, pio_project.build_dir("esp32dev"), ["esp32dev"])
    assert bk.wait_state(app, "esp32dev", "ok", "updated").firmware.chip == "esp32"

    _ini(pio_project, bk.ini_env("esp32dev", S3_BOARD, "pio-moved"))  # not rebuilt yet
    switched = bk.wait_state(app, "esp32dev", "target_switched", timeout=60)
    assert switched.configured_target == "esp32s3"
    banner = bk.banner_text(window)
    assert "esp32s3" in banner and "esp32" in banner, banner
    assert not window.connection.flash.isEnabled()

    _build(pio_project, "esp32dev")
    changed = bk.wait_state(app, "esp32dev", "needs_confirmation", timeout=60)
    assert changed.changes[0].kind == ChangeKind.CHIP, [c.message for c in changed.changes]
    bk.identify(window, kit.board_port(bk.UART))
    kit.click_sidebar(window, "esp32dev")
    assert not window.connection.flash.isEnabled()
    bk.confirm_banner(window)
    assert bk.wait_state(app, "esp32dev", "ok", "updated").firmware.chip == "esp32s3"
    _flash_boots(app, "esp32dev", "pio-moved")


# ---- clean, a stale idedata.json, envs added and removed --------------------------------


def test_clean_makes_the_build_missing_and_a_rebuild_is_followed_again(
    pio_project: bk.PioProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
) -> None:
    """design doc §3.4: output gone -> missing, not flashable, until it is built again."""
    app = _start(pio_project, embr)
    window = app.window
    _ini(pio_project, bk.ini_env("s3", S3_BOARD, "pio-s3"))
    _build(pio_project, "s3")
    bk.import_pio(window, file_dialogs, pio_project.build_dir("s3"), ["s3"])
    bk.wait_state(app, "s3", "ok", "updated")
    first = bk.entries_of(app, "s3")
    pio_project.run("run", "-e", "s3", "-t", "clean")
    time.sleep(bk.SETTLE_S)
    bk.wait_state(app, "s3", "missing", "incomplete")
    assert not window.connection.flash.isEnabled()
    _build(pio_project, "s3")
    bk.wait_state(app, "s3", "ok", "updated", timeout=75)  # beyond a 60 s round
    assert [(a, n) for a, n, _, _ in bk.entries_of(app, "s3")] == [(a, n) for a, n, _, _ in first]


def test_stale_idedata_and_envs_added_and_removed(
    pio_project: bk.PioProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
) -> None:
    app = _start(pio_project, embr)
    window = app.window
    base = bk.ini_env("esp32dev", "esp32dev", "pio-dev") + bk.ini_env("s3", S3_BOARD, "pio-s3")
    _ini(pio_project, base)
    _build(pio_project, "esp32dev", "s3")
    bk.import_pio(window, file_dialogs, pio_project.build_dir("s3"), ["s3"])
    assert "esp32dev" not in kit.sidebar_names(window)  # unchecked: no firmware
    first = bk.entries_of(app, "s3")

    # idedata.json written by the IDE integration, then platformio.ini edited: stale
    pio_project.run("run", "-e", "s3", "-t", "idedata")
    idedata = pio_project.build_dir("s3") / "idedata.json"
    assert idedata.is_file()
    time.sleep(bk.SETTLE_S)
    status = bk.wait_state(app, "s3", "ok", "updated")
    assert not any("older than" in n for n in status.build_notes), status.build_notes
    time.sleep(1.1)
    _ini(pio_project, base + "; edited after the IDE wrote idedata.json\n")
    time.sleep(bk.SETTLE_S)
    stale = bk.wait_state(app, "s3", "ok", "updated", "needs_confirmation")
    assert any("idedata.json is older than platformio.ini" in n for n in stale.build_notes), (
        stale.build_notes
    )
    assert [(a, n) for a, n, _, _ in bk.entries_of(app, "s3")] == [(a, n) for a, n, _, _ in first]

    # an env added (not built) shows greyed in the import dialog
    _ini(pio_project, base + bk.ini_env("extra", S3_BOARD, "pio-extra"))

    def look(driver: kit.DialogDriver) -> None:
        file_dialogs.queue_directory(pio_project.build_dir("esp32dev"))
        driver.press("Browse…")
        kit.wait_until(
            lambda: "extra" in bk.env_boxes(driver),
            90,
            message=lambda: f"no env extra: {driver.texts()}",
        )
        kit.pump(200)
        boxes = bk.env_boxes(driver)
        assert not boxes["extra"].isEnabled(), kit.text_of(boxes["extra"])
        assert not boxes["s3"].isEnabled()  # already followed
        assert boxes["esp32dev"].isEnabled()
        driver.cancel()

    with kit.expect_dialog(look, title="Import PlatformIO build", timeout=120):
        bk.new_menu(window, "Import PlatformIO build…")

    # the followed env removed from platformio.ini: not flashable, and said why
    _ini(pio_project, bk.ini_env("esp32dev", "esp32dev", "pio-dev"))
    time.sleep(bk.SETTLE_S)
    removed = bk.wait_state(app, "s3", "missing", "incomplete")
    assert any("[env:s3]" in p for p in removed.problems), removed.problems
    assert not window.connection.flash.isEnabled()


# ---- two Arduino framework packages, the platform_packages pin, a manual path ----------


def _settings_package(window: Any, text: str) -> None:
    def fill(driver: kit.DialogDriver) -> None:
        edit = bk.first_line_edit(driver)
        if text:
            kit.type_text(edit, text)
        else:
            kit.type_text(edit, "x")
            kit.type_text(edit, "")
        driver.ok()

    with kit.expect_dialog(fill, title="Settings"):
        kit.window_menu(window, "Settings…")


def test_two_framework_packages_pin_and_manual_path(
    pio_project: bk.PioProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
) -> None:
    core = bk.pio_core()
    if not any((core / "platforms").glob("espressif32@6.5.0*")) and not _has_platform(
        core, "6.5.0"
    ):
        pytest.fail(
            f"install the older official platform first: pio pkg install -g -p {OLD_PLATFORM}"
        )
    app = _start(pio_project, embr)
    window = app.window
    old = bk.ini_env("old", S3_BOARD, "pio-old", extra=f"platform = {OLD_PLATFORM}")
    _ini(pio_project, old)
    _build(pio_project, "old")
    packages = _package_versions(core)
    assert len(packages) >= 2, packages  # 6.12.0's and 6.5.0's Arduino packages
    pinned = next((p for p, v in packages.items() if v.startswith("3.20014")), None)
    assert pinned is not None, packages
    highest = max(packages, key=lambda p: _numeric(packages[p]))

    bk.import_pio(window, file_dialogs, pio_project.build_dir("old"), ["old"])
    bk.wait_state(app, "old", "ok", "updated")
    _, boot = _boot_app0_of(bk.entries_of(app, "old"))
    assert boot.parents[2] == highest, (boot, packages)  # auto: the highest version

    # pinned with platform_packages (as PlatformIO then uses it)
    pin = f"platform_packages = platformio/framework-arduinoespressif32@~{packages[pinned]}"
    _ini(
        pio_project,
        bk.ini_env("old", S3_BOARD, "pio-old", extra=f"platform = {OLD_PLATFORM}\n{pin}"),
    )
    _build(pio_project, "old")
    _wait_package(app, "old", pinned)

    # a manual path in Settings wins over auto-detection
    other = next(p for p in packages if p != pinned)
    _settings_package(window, str(other))
    _wait_package(app, "old", other)

    # an invalid manual path: said so, not silently replaced, not flashable
    wrong = tmp_path / "no-package"
    wrong.mkdir()
    _settings_package(window, str(wrong))
    _wait_problem(app, "old", "framework package not found")
    assert not window.connection.flash.isEnabled()

    # the firmware's own override (Parameters tab) wins over Settings
    _open_tab(window, "Parameters")
    edit = _package_edit(window)
    kit.type_text(edit, str(pinned), finish="return")
    _wait_package(app, "old", pinned)

    # restored: Settings empty, override empty -> auto-detection (the pin) again
    _settings_package(window, "")
    _open_tab(window, "Parameters")
    kit.type_text(_package_edit(window), "x")
    kit.type_text(_package_edit(window), "", finish="return")
    _wait_package(app, "old", pinned)
    assert bk.firmware_by_name(app, "old").framework_pkg == ""
    _open_tab(window, "Entries")
    _flash_boots(app, "old", "pio-old")


def _wait_package(app: kit.EmbrApp, name: str, folder: Path, timeout: float = 60) -> None:
    """Re-check (as selecting the firmware does) until its boot_app0.bin comes from
    `folder`; a change that needs confirmation is confirmed. The configured env is read by
    `pio project config` in the background, so this may take a few seconds."""
    seen: list[object] = []

    def done() -> bool:
        status = bk.wait_state(app, name, "ok", "updated", "needs_confirmation", "incomplete")
        if str(status.state.value) == "needs_confirmation":
            bk.confirm_banner(app.window)
            return False
        found = [e.file for e in status.firmware.entries if e.name == BOOT_APP0]
        seen[:] = [found, status.problems]
        return bool(found) and Path(found[0].split(":", 1)[1]).parents[2] == folder

    kit.wait_until(
        done, timeout, message=lambda: f"boot_app0 not from {folder}: {seen}", interval_ms=1000
    )


def _wait_problem(app: kit.EmbrApp, name: str, text: str, timeout: float = 60) -> None:
    seen: list[object] = []

    def done() -> bool:
        status = bk.wait_state(app, name, *[s.value for s in ResolveState])
        seen[:] = [status.state, status.problems]
        return not status.state.flashable and any(text in p for p in status.problems)

    kit.wait_until(
        done, timeout, message=lambda: f"{text!r} not reported: {seen}", interval_ms=1000
    )


def _has_platform(core: Path, version: str) -> bool:
    for manifest in (core / "platforms").glob("espressif32*/platform.json"):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("version") == version:
            return True
    return False


def _open_tab(window: Any, name: str) -> None:
    for button in window.firmware_view.tab_group.buttons():
        if kit.same_text(button.text(), name):
            kit.click(button)
            return
    pytest.fail(f"no tab {name!r}")


def _package_edit(window: Any) -> QLineEdit:
    params = window.firmware_view.params
    edits = [
        e
        for e in kit.find_all(params, QLineEdit)
        if any(v in e.placeholderText() for v in ("Settings path", "auto-detect", "设置", "自动"))
    ]
    if len(edits) != 1:
        shown = [e.placeholderText() for e in kit.find_all(params, QLineEdit)]
        pytest.fail(f"framework package field not found: {shown}")
    return edits[0]


# ---- an espidf env -------------------------------------------------------------------------


IDF_MAIN = """#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

void app_main(void)
{
    for (int i = 0;; i++) {
        printf("tick %d EMBR-MARK pio-idf\\n", i);
        vTaskDelay(1000 / portTICK_PERIOD_MS);
    }
}
"""


def test_espidf_env_imported_and_flashed(
    pio_project: bk.PioProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
) -> None:
    uart = kit.board_port(bk.UART)
    app = _start(pio_project, embr)
    window = app.window
    root = pio_project.root
    (root / "src" / "idf_main.c").write_text(IDF_MAIN, encoding="utf-8")
    (root / "src" / "CMakeLists.txt").write_text(
        'idf_component_register(SRCS "idf_main.c")\n', encoding="utf-8"
    )
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16.0)\n"
        "include($ENV{IDF_PATH}/tools/cmake/project.cmake)\n"
        "project(env_test_)\n",
        encoding="utf-8",
    )
    _ini(
        pio_project,
        "[env:idf]\nplatform = espressif32\nboard = "
        + S3_BOARD
        + "\nframework = espidf\nbuild_src_filter = -<*> +<idf_main.c>\n\n",
    )
    _build(pio_project, "idf")
    bk.import_pio(window, file_dialogs, pio_project.build_dir("idf"), ["idf"])
    status = bk.wait_state(app, "idf", "ok", "updated")
    assert status.firmware.chip == "esp32s3"
    entries = {n: a for a, n, _, _ in bk.entries_of(app, "idf")}
    assert entries == {"bootloader.bin": 0x0, "partitions.bin": 0x8000, "firmware.bin": 0x10000}, (
        entries
    )
    _flash_boots(app, "idf", "pio-idf")
    firmware = (pio_project.build_dir("idf") / "firmware.bin").read_bytes()
    assert bk.read_back(uart, 0x10000, len(firmware), tmp_path / "rb") == firmware


# ---- Arduino core 3.x (pioarduino): correct, or refused -------------------------------------


@pytest.fixture
def pioarduino(pio_project: bk.PioProject) -> Iterator[None]:
    """pioarduino is installed by the build (platform = its release URL). Installed in the
    PIO core it is the highest "espressif32" and shadows the official platform for every
    `platform = espressif32` without a version (seen: the original project then failed with
    "Failed to install Python dependencies into penv"), so its platform folder is removed
    again afterwards; the packages it downloaded stay in the core."""
    try:
        yield
    finally:
        _remove_pioarduino_platform(pio_project)


def _remove_pioarduino_platform(project: bk.PioProject) -> None:
    for manifest in (bk.pio_core() / "platforms").glob("espressif32*/platform.json"):
        version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
        if version and not version.startswith("6."):  # the official ones are 6.x
            semver = ".".join(str(int(p)) if p.isdigit() else p for p in version.split("."))
            project.run("pkg", "uninstall", "-g", "-p", f"espressif32@{semver}", check=False)


def test_pioarduino_core3_imported_and_flashed_correctly_or_refused(
    pio_project: bk.PioProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
    pioarduino: None,
) -> None:
    uart = kit.board_port(bk.UART)
    app = _start(pio_project, embr)
    window = app.window
    _ini(pio_project, bk.ini_env("core3", S3_BOARD, "pio-core3", extra=f"platform = {PIOARDUINO}"))
    _build(pio_project, "core3")
    build = pio_project.build_dir("core3")
    shown: dict[str, str] = {}

    def look(driver: kit.DialogDriver) -> None:
        file_dialogs.queue_directory(build)
        driver.press("Browse…")
        kit.wait_until(
            lambda: "core3" in bk.env_boxes(driver) or _refusal(driver),
            90,
            message=lambda: f"nothing shown: {driver.texts()}",
        )
        kit.pump(300)
        box = bk.env_boxes(driver).get("core3")
        shown["text"] = driver.text()
        if box is not None and box.isEnabled():
            kit.set_checked(box, True)
            driver.pick("Place in", "Standalone (no project)")
            driver.ok()
        else:
            shown["refused"] = "1"
            driver.cancel()

    with kit.expect_dialog(look, title="Import PlatformIO build", timeout=180):
        bk.new_menu(window, "Import PlatformIO build…")
    if shown.get("refused"):
        assert "core3" not in kit.sidebar_names(window)
        assert len(shown["text"]) > 0
        return
    status = bk.wait_state(app, "core3", "ok", "updated", "incomplete", "needs_confirmation")
    if not status.state.flashable:  # imported but blocked: a clear reason, nothing flashed
        assert status.problems or status.changes, status
        assert not window.connection.flash.isEnabled()
        return
    entries = bk.entries_of(app, "core3")
    by_name = {n: a for a, n, _, _ in entries}
    assert by_name.get("bootloader.bin") == 0x0 and by_name.get("firmware.bin") == 0x10000, entries
    bk.identify(window, uart)
    _erase(window)
    _flash_boots(app, "core3", "pio-core3")
    rb = tmp_path / "rb"
    for address, name, ref, enabled in entries:
        if not enabled or address is None:
            continue
        data = Path(ref.split(":", 1)[1]).read_bytes()
        assert bk.read_back(uart, address, len(data), rb) == data, name


def _refusal(driver: kit.DialogDriver) -> bool:
    return any(t.startswith("<b>") or "not" in t for t in driver.texts()) and not bk.env_boxes(
        driver
    )


_ = (shutil, QCheckBox)
