"""Interface flows: tracking firmware (ESP-IDF and PlatformIO builds), in English and
Simplified Chinese (design doc §3.4 跟随型固件 and 检测时机, §5 stages two and three, §6.1
banners, §12 items 2, 16, 19, §12.5).

Builds are copies of the tests/fixtures samples in tmp folders (release_kit.idf_build_copy,
pio_project_copy); the copies are then changed on disk as a rebuild, a clean or an
interrupted build would. PlatformIO: PLATFORMIO_CORE_DIR points at the sample's fake core
and pio is taken off PATH, so `pio project config` is not available and the board check
is skipped (documented behaviour); no PlatformIO installation is touched.

A re-check is triggered by selecting the firmware again (§3.4: on selection); the
building state (a file younger than 2 s) is seen the same way."""

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import release_kit as kit
import ui_kit as ui
from PySide6.QtWidgets import QAbstractButton, QCheckBox
from ui_kit import pio_env, watchdog  # noqa: F401  # pyright: ignore[reportUnusedImport]

from embr.i18n import Language, tr

Start = Callable[..., kit.EmbrApp]
pytestmark = pytest.mark.usefixtures("watchdog")
APP = "hello_esp32s3.bin"


def _doc(root: Path, name: str) -> dict[str, Any]:
    return ui.doc_named(ui.firmware_docs(root), name)


def _flasher(build: Path) -> dict[str, Any]:
    return json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))


def _write_flasher(build: Path, data: dict[str, Any]) -> None:
    path = build / "flasher_args.json"
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")
    ui.make_old(path, 1800)


def _move_app(build: Path, old: str, new: str, offset: str | None = None) -> None:
    """A rebuild that renamed the app and/or moved it."""
    data = _flasher(build)
    files = data["flash_files"]
    address = next(a for a, f in files.items() if f == old)
    del files[address]
    if old != new:
        (build / old).rename(build / new)
        ui.make_old(build / new, 1800)
    files[offset or address] = new
    data["app"] = {"offset": offset or address, "file": new, "encrypted": "false"}
    _write_flasher(build, data)


def _updated_tag(tags: list[str]) -> str | None:
    pattern = re.escape(tr("Build updated 12:34:56")).replace("12\\:34\\:56", r"\d\d:\d\d:\d\d")
    pattern = pattern.replace("12:34:56", r"\d\d:\d\d:\d\d")
    return next((t for t in tags if re.fullmatch(pattern, t)), None)


def _with_other(window: Any, dialogs: kit.FileDialogs, tmp_path: Path) -> None:
    """A fixed firmware to click on, so the tracking one can be selected again."""
    ui.fixed_firmware(
        window, dialogs, "Other", kit.hello_files("esp32s3_hello", tmp_path / "other")
    )


# ---- import -----------------------------------------------------------------------------

SAMPLES = {
    "esp32s3_hello": (
        "hello_esp32s3",
        [
            ("0x0", "bootloader.bin", "bootloader"),
            ("0x8000", "partition-table.bin", "partition_table"),
            ("0x10000", APP, "app"),
        ],
    ),
    "esp32s3_ota_custom": (
        "ota_custom",
        [
            ("0x0", "bootloader.bin", "bootloader"),
            ("0x10000", "partition-table.bin", "partition_table"),
            ("0x17000", "ota_data_initial.bin", "otadata"),
            ("0x20000", "ota_custom.bin", "app"),
        ],
    ),
}


@pytest.mark.parametrize("sample", list(SAMPLES))
@ui.LANGUAGES
def test_import_idf_build(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path, sample: str
) -> None:
    """The project folder is not taken silently: the dialog offers its build folder
    (§12 item 2); the firmware follows the build, named after project_name, placed in the
    chosen project and group; entries come from flasher_args.json and cannot be added
    (§12 item 16); a build folder already followed is refused with a way to that
    firmware; a folder without a build is refused."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    project = tmp_path / "proj"
    build = kit.idf_build_copy(sample, project)
    default_name, expected = SAMPLES[sample]
    ui.new_project(window, "IDF")
    ui.add_group(window, "Main")
    seen: list[str] = []
    ui.import_idf(
        window, file_dialogs, project, place="IDF", group="Main", use_build=True, seen=seen
    )
    kit.wait_log(window, f"[import] '{default_name}' follows {build}")
    problems: list[str] = []
    assert str(build) in seen[0], seen[0]
    assert default_name in seen[1] and "esp32s3" in seen[1], seen[1]
    assert ui.placement_of(window, default_name) == ("IDF", "Main")
    row = ui.row_texts(window, default_name)
    assert "IDF" in row and str(build) in " ".join(row), row
    tags = ui.header_tags(window)
    assert "ESP32-S3" in tags and tr("tracks IDF build") in tags, tags
    shown = [(e.address, e.name, e.kind) for e in ui.entries(window)]
    assert shown == expected
    entries_tab = window.firmware_view.entries
    assert not entries_tab.add_button.isVisible() and entries_tab.from_build.isVisible()
    assert ui.check(window, "Build resolved").state == "PASS"
    doc = _doc(root, default_name)
    assert doc["source"] == "idf_build" and doc["build_ref"] == {"path": str(build)}
    ui.open_tab(window, "Parameters")
    facts = ui.param_texts(window, "Build directory")
    assert any(str(build) in t for t in facts) and any(tr("exists") in t for t in facts), facts
    flash = ui.param_texts(window, "Flash mode / freq / size")
    assert tr("derived") in flash and not ui.param_combos(window, "Flash mode / freq / size")
    before = ui.param_texts(window, "Reset before")
    assert "default-reset" in before and tr("derived") in before, before
    ui.open_tab(window, "Entries")

    loose = Path(
        shutil.copy(
            kit.FIXTURES / "synthetic" / "attachments" / "calib_at_address.bin", tmp_path / "x.bin"
        )
    )
    ui.drop_files_on_entries(window, [loose])  # refused: entries come from the build
    kit.wait_log(
        window, "come from its build directory; archive this version or create a fixed firmware"
    )
    assert len(ui.entries(window)) == len(expected)

    file_dialogs.queue_directory(build)  # the same build again: refused, offers the firmware
    again: list[str] = []

    def same(driver: kit.DialogDriver) -> None:
        driver.press("Browse…")
        kit.wait_until(
            lambda: kit.find_all(driver.dialog, QAbstractButton, "Show that firmware"),
            15,
            message=lambda: f"no 'Show that firmware': {driver.texts()}",
        )
        again.append(driver.text())
        driver.press("Show that firmware")

    ui.click_row(window, "IDF", "project")
    with kit.expect_dialog(same, title="Import ESP-IDF build"):
        kit.new_menu(window, "Import ESP-IDF build…")
    assert kit.sidebar_names(window).count(default_name) == 1
    assert ui.found(lambda: window.pages.currentWidget() is window.firmware_view, "firmware view")
    assert ui.header_texts(window).count(default_name) == 1

    empty = tmp_path / "not a build"
    empty.mkdir()
    file_dialogs.queue_directory(empty)

    def wrong(driver: kit.DialogDriver) -> None:
        driver.press("Browse…")
        kit.pump(200)
        text = driver.text()
        if "flasher_args.json" not in text:
            problems.append(f"no reason for a folder without a build: {text}")
        driver.ok()  # refused: the dialog stays open
        kit.pump(200)
        if not driver.dialog.isVisible():
            problems.append("a folder without a build was accepted")
        else:
            driver.cancel()

    with kit.expect_dialog(wrong, title="Import ESP-IDF build"):
        kit.new_menu(window, "Import ESP-IDF build…")
    assert len(ui.firmware_docs(root)) == 1
    if ui.in_chinese():
        problems += ui.english_leftovers(seen, [str(build), str(project), default_name, "proj"])
    ui.no_problems(problems)


@ui.LANGUAGES
def test_import_pio_multi_env_project(
    embr: Start,
    file_dialogs: kit.FileDialogs,
    language: Language,
    tmp_path: Path,
    pio_env: Path,  # noqa: F811
) -> None:
    """§12.5 多 env: every env of platformio.ini is listed, built ones can be checked (the
    picked one is), unbuilt ones are greyed; each checked env becomes a firmware named
    after it, all in the chosen project and group; PIO Arduino firmware get boot_app0
    from the framework package, whose path can be set on the Parameters tab."""
    project = tmp_path / "pio proj"
    kit.pio_project_copy("multi_env", project, pio_env)
    ini = project / "platformio.ini"
    ini.write_text(
        ini.read_text(encoding="utf-8") + "\n[env:esp32c3]\nboard = esp32-c3-devkitm-1\n",
        encoding="utf-8",
    )
    ui.make_old(ini, 7200)
    app = embr(language=language)
    window, root = app.window, app.paths.root
    ui.new_project(window, "PIO")
    ui.add_group(window, "Boards")
    boxes: list[tuple[str, bool, bool]] = []
    ui.import_pio(
        window,
        file_dialogs,
        project / ".pio" / "build" / "esp32dev",
        also=("esp32-s3-devkitc-1",),
        place="PIO",
        group="Boards",
        boxes=boxes,
    )
    kit.wait_log(window, "[import] 'esp32-s3-devkitc-1' follows")
    by_env = {text.split(" · ")[0]: (enabled, checked) for text, enabled, checked in boxes}
    assert by_env["esp32dev"] == (True, True), boxes
    assert by_env["esp32-s3-devkitc-1"] == (True, False), boxes
    assert by_env["esp32c3"] == (False, False), boxes
    c3 = next(text for text, _, _ in boxes if text.startswith("esp32c3"))
    assert c3 == tr("esp32c3 · not built"), c3
    for env, chip in (("esp32dev", "ESP32"), ("esp32-s3-devkitc-1", "ESP32-S3")):
        assert ui.placement_of(window, env) == ("PIO", "Boards")
        assert "PIO" in ui.row_texts(window, env)
        ui.click_row(window, env)
        assert chip in ui.header_tags(window) and tr("tracks PIO build") in ui.header_tags(window)
        assert _doc(root, env)["build_ref"] == {"path": str(project), "env": env}
    assert "esp32c3" not in kit.sidebar_names(window)

    ui.click_row(window, "esp32dev")
    names = {e.name: e.address for e in ui.entries(window)}
    assert names.get("bootloader.bin") == "0x1000" and names.get("firmware.bin") == "0x10000", names
    assert "boot_app0.bin" in names, names
    ui.open_tab(window, "Parameters")
    package = ui.param_edit(window, "Framework package")
    detected = pio_env / "packages" / "framework-arduinoespressif32"
    assert str(detected) in package.placeholderText(), package.placeholderText()
    chosen = tmp_path / "my package"
    shutil.copytree(detected, chosen)
    file_dialogs.queue_directory(chosen)
    kit.click_button(ui.param_widget(window, "Framework package"), "Browse…")
    kit.wait_log(window, f"[firmware] esp32dev: framework package {chosen}")
    assert _doc(root, "esp32dev")["framework_pkg"] == str(chosen)
    kit.type_text(ui.param_edit(window, "Framework package"), "", finish="return")
    kit.wait_until(lambda: _doc(root, "esp32dev").get("framework_pkg", "") == "", 5)
    last = kit.log_text(window).splitlines()[-1]
    if last != "[firmware] esp32dev: framework package the Settings path / auto-detection":
        pytest.fail(f"log line in the interface language (logs stay English, §12.5): {last!r}")
    _ = pytest


@ui.LANGUAGES
def test_drop_build_folders_on_the_sidebar(
    embr: Start,
    language: Language,
    tmp_path: Path,
    pio_env: Path,  # noqa: F811
) -> None:
    """§12 item 19 and §12.5: an IDF project folder dropped on a group opens the IDF import
    with its build folder and that project and group; a PlatformIO project folder dropped
    on a project opens the PlatformIO import with its first built env; a build folder
    dropped together with a file is refused."""
    idf = tmp_path / "idf proj"
    build = kit.idf_build_copy("esp32s3_hello", idf)
    pio = tmp_path / "pio proj"
    kit.pio_project_copy("multi_env", pio, pio_env)
    app = embr(language=language)
    window = app.window
    ui.new_project(window, "Drops")
    ui.add_group(window, "G")

    def idf_dialog(driver: kit.DialogDriver) -> None:
        driver.wait_text("ESP-IDF")
        assert driver.line_edit("Build directory (contains flasher_args.json)").text() == str(build)
        assert driver.combo("Place in").currentText() == "Drops"
        assert driver.combo("Group").currentText() == "G"
        driver.ok()

    with kit.expect_dialog(idf_dialog, title="Import ESP-IDF build"):
        ui.drop_files_on_row(window, ui.group_row(window, "Drops", "G"), [idf])
    kit.wait_log(window, f"[import] 'hello_esp32s3' follows {build}")
    assert ui.placement_of(window, "hello_esp32s3") == ("Drops", "G")

    def pio_dialog(driver: kit.DialogDriver) -> None:
        kit.wait_until(
            lambda: any(b.isEnabled() and b.isChecked() for b in driver.checkboxes()),
            60,
            message=lambda: f"no env: {driver.texts()}",
        )
        edit = driver.line_edit(r"Env build folder (.pio\build\<env>)")
        assert Path(edit.text()).parent == pio / ".pio" / "build", edit.text()
        assert driver.combo("Place in").currentText() == "Drops"
        assert driver.combo("Group").currentText() == tr("(ungrouped)")
        driver.ok()

    ui.click_row(window, "Drops", "project")
    with kit.expect_dialog(pio_dialog, title="Import PlatformIO build", timeout=90):
        ui.drop_files_on_row(window, kit.sidebar_row(window, "Drops", "project"), [pio])
    kit.wait_log(window, "[import] 'esp32dev' follows")  # the first built env of platformio.ini
    assert ui.placement_of(window, "esp32dev") == ("Drops", "")

    reasons: list[str] = []
    with kit.expect_dialog(ui.record_and_ok(reasons), title="Nothing added"):
        ui.drop_files_on_row(window, ui.standalone_header(window), [build, build / APP])
    assert ui.plain(tr("Drop a build folder on its own.")) in reasons[0]


# ---- changes in the build folder --------------------------------------------------------


@ui.LANGUAGES
def test_content_change_is_followed_automatically(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§3.4 case 1: same files, only contents changed → followed without confirmation;
    the header shows "Build updated HH:MM:SS", the changed entry "content changed"."""
    build = kit.idf_build_copy("esp32s3_hello", tmp_path / "proj")
    app = embr(language=language)
    window, root = app.window, app.paths.root
    _with_other(window, file_dialogs, tmp_path)
    ui.import_idf(window, file_dialogs, build)
    before = json.dumps(_doc(root, "hello_esp32s3")["last_resolved"])
    ui.click_row(window, "Other")  # the user works elsewhere while the project rebuilds
    ui.flip_byte(build / APP, (build / APP).stat().st_size - 8)  # inside the appended hash
    ui.make_old(build / APP, 1800)
    ui.click_row(window, "hello_esp32s3")
    problems: list[str] = []
    tags = ui.header_tags(window)
    if _updated_tag(tags) is None:
        problems.append(f"no 'Build updated HH:MM:SS' tag: {tags}")
    if tr("content changed") not in ui.entry(window, APP).tags:
        problems.append(f"app entry tags {ui.entry(window, APP).tags}")
    if ui.banner(window):
        problems.append(f"banner shown: {ui.banner(window)!r}")
    if not ui.has_check(window, "Build resolved", "PASS"):
        problems.append(f"checks {ui.checks(window)}")
    if "!" in ui.row_texts(window, "hello_esp32s3"):
        problems.append("sidebar marks the firmware as needing confirmation")
    if json.dumps(_doc(root, "hello_esp32s3")["last_resolved"]) == before:
        problems.append("last_resolved not updated on disk")
    ui.reselect(window, "hello_esp32s3", "Other")  # switching firmware ends the marks
    if tr("content changed") in ui.entry(window, APP).tags:
        problems.append("the content-changed mark stays after switching firmware")
    ui.no_problems(problems)


@ui.LANGUAGES
def test_build_changes_that_need_confirmation(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§3.4 cases 2 and 3 and manual addresses: a renamed, added or removed file or a
    moved entry needs confirmation (banner listing the changes, "needs confirmation",
    "!", Flash blocked); Confirm entries accepts it once."""
    build = kit.idf_build_copy("esp32s3_hello", tmp_path / "proj")
    app = embr(language=language)
    window, root = app.window, app.paths.root
    _with_other(window, file_dialogs, tmp_path)
    ui.import_idf(window, file_dialogs, build)
    name = "hello_esp32s3"
    problems: list[str] = []

    def expect_confirmation(*lines: str) -> None:
        ui.reselect(window, name, "Other")
        text = ui.banner(window)
        for line in lines:
            if ui.plain(tr(line)) not in text:
                problems.append(f"banner lacks {tr(line)!r}: {text!r}")
        if tr("needs confirmation") not in ui.header_tags(window):
            problems.append(f"header tags {ui.header_tags(window)}")
        if "!" not in ui.row_texts(window, name):
            problems.append(f"sidebar row {ui.row_texts(window, name)}")
        if not ui.has_check(window, "Build changed: confirm entries", "REFUSE"):
            problems.append(f"checks {ui.checks(window)}")
        button = ui.banner_button(window)
        if not (button.isVisible() and kit.same_text(button.text(), "Confirm entries")):
            problems.append("no Confirm entries button")
            return
        kit.click(button)
        kit.wait_log(window, f"[entries] {name}: entries confirmed")
        if ui.banner(window) or tr("needs confirmation") in ui.header_tags(window):
            problems.append(f"still shown after confirming: {ui.banner(window)!r}")
        if not ui.has_check(window, "Build resolved", "PASS"):
            problems.append(f"checks after confirming {ui.checks(window)}")

    _move_app(build, APP, "hello_renamed.bin")
    ui.reselect(window, name, "Other")
    tags = {e.name: e.tags for e in ui.entries(window)}
    if tr("new file") not in tags.get("hello_renamed.bin", []):
        problems.append(f"renamed app: entry tags {tags}")
    if tr("file missing") not in tags.get(APP, []):
        problems.append(f"old app entry not marked missing: {tags}")
    expect_confirmation(f"Removed: {APP}", "New file: hello_renamed.bin")
    assert [e.name for e in ui.entries(window)][-1] == "hello_renamed.bin"

    _move_app(build, "hello_renamed.bin", "hello_renamed.bin", "0x20000")
    expect_confirmation("hello_renamed.bin: address 0x10000 -> 0x20000")
    assert ui.entry(window, "hello_renamed.bin").address == "0x20000"

    ui.set_address(window, "hello_renamed.bin", "0x30000")  # manual, then the build moves it
    assert ui.entry(window, "hello_renamed.bin").source == "manual"
    _move_app(build, "hello_renamed.bin", "hello_renamed.bin", "0x40000")
    expect_confirmation("0x30000", "0x40000")  # the build's new address and the kept manual one
    assert ui.entry(window, "hello_renamed.bin").address == "0x30000"

    data = _flasher(build)
    (build / "ota_data_initial.bin").write_bytes(b"\xff" * 0x2000)
    ui.make_old(build / "ota_data_initial.bin", 1800)
    data["flash_files"]["0xd000"] = "ota_data_initial.bin"
    _write_flasher(build, data)
    expect_confirmation("New file: ota_data_initial.bin")
    data = _flasher(build)
    del data["flash_files"]["0xd000"]
    _write_flasher(build, data)
    expect_confirmation("Removed: ota_data_initial.bin")
    assert "ota_data_initial.bin" not in ui.entry_names(window)
    assert _doc(root, name)["source"] == "idf_build"
    ui.no_problems(problems)


@ui.LANGUAGES
def test_build_folder_missing_and_change_directory(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§3.4 / §6.1: a build folder that is gone (renamed, fullclean) → "build output
    missing": banner with the last resolution and Change directory…, the snapshot still
    listed, ×, flash / merge / archive blocked; Change directory… follows the new place."""  # noqa: RUF002
    build = kit.idf_build_copy("esp32s3_hello", tmp_path / "proj")
    app = embr(language=language)
    window, root = app.window, app.paths.root
    _with_other(window, file_dialogs, tmp_path)
    ui.import_idf(window, file_dialogs, build)
    name = "hello_esp32s3"
    moved = tmp_path / "proj" / "build-moved"
    build.rename(moved)
    ui.reselect(window, name, "Other")
    problems: list[str] = []
    text = ui.banner(window)
    if f"{build}" not in text or tr("Change directory…") != kit.text_of(ui.banner_button(window)):
        problems.append(f"banner {text!r} / button {kit.text_of(ui.banner_button(window))!r}")
    if tr("build output missing") not in ui.header_tags(window):
        problems.append(f"header tags {ui.header_tags(window)}")
    if chr(0xD7) not in ui.row_texts(window, name):
        problems.append(f"sidebar row {ui.row_texts(window, name)}")
    if not ui.has_check(window, "Build output missing", "REFUSE"):
        problems.append(f"checks {ui.checks(window)}")
    if ui.entry_names(window) != ui.S3_FILES:
        problems.append(f"snapshot entries {ui.entry_names(window)}")
    if window.firmware_view.entries.merge_button.isEnabled():
        problems.append("Merge to .bin… enabled with the build output missing (§5 合成工具)")
    seen: list[ui.MenuSeen] = []
    ui.firmware_menu(window, seen=seen)
    if seen[0].enabled.get(ui.plain(tr("Archive this version…"))):
        problems.append("Archive this version… enabled with the build output missing")

    file_dialogs.queue_directory(moved)
    kit.click(ui.banner_button(window))
    kit.wait_log(window, f"[build] {name} now follows {moved}")
    if ui.banner(window) or not ui.has_check(window, "Build resolved", "PASS"):
        problems.append(f"after Change directory: {ui.banner(window)!r} {ui.checks(window)}")
    if _doc(root, name)["build_ref"] != {"path": str(moved)}:
        problems.append(f"build_ref on disk {_doc(root, name)['build_ref']}")

    shutil.rmtree(moved)  # idf.py fullclean
    ui.reselect(window, name, "Other")
    if not ui.has_check(window, "Build output missing", "REFUSE"):
        problems.append(f"after deleting the folder: {ui.checks(window)}")
    ui.no_problems(problems)


@ui.LANGUAGES
def test_build_incomplete_and_being_written(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§3.4: unreadable images or missing files → "build incomplete" (not flashable); a
    file written less than 2 s ago → building, checked again shortly."""
    build = kit.idf_build_copy("esp32s3_hello", tmp_path / "proj")
    app = embr(language=language)
    window = app.window
    _with_other(window, file_dialogs, tmp_path)
    ui.import_idf(window, file_dialogs, build)
    name = "hello_esp32s3"
    problems: list[str] = []
    good = {
        p: p.read_bytes()
        for p in (
            build / APP,
            build / "bootloader" / "bootloader.bin",
            build / "partition_table" / "partition-table.bin",
        )
    }

    def restore() -> None:
        for path, data in good.items():
            path.write_bytes(data)
            ui.make_old(path, 1800)

    def expect_incomplete(case: str) -> None:
        ui.reselect(window, name, "Other")
        text = ui.banner(window)
        prefix = tr(
            "Build incomplete: XYZ. Wait for the build to finish or rebuild; flashing is blocked."
        )
        if not text.startswith(prefix.split("XYZ")[0]):
            problems.append(f"{case}: banner {text!r}")
        if tr("build incomplete") not in ui.header_tags(window):
            problems.append(f"{case}: header tags {ui.header_tags(window)}")
        if not ui.has_check(window, "Build incomplete", "REFUSE"):
            problems.append(f"{case}: checks {ui.checks(window)}")
        if chr(0xD7) not in ui.row_texts(window, name):
            problems.append(f"{case}: sidebar row {ui.row_texts(window, name)}")

    (build / APP).write_bytes(good[build / APP][:96])  # truncated
    ui.make_old(build / APP, 1800)
    expect_incomplete("truncated app")
    restore()
    table = build / "partition_table" / "partition-table.bin"
    table.write_bytes(b"\x00" * len(good[table]))  # garbled partition table
    ui.make_old(table, 1800)
    expect_incomplete("garbled partition table")
    restore()
    (build / "bootloader" / "bootloader.bin").unlink()
    expect_incomplete("bootloader missing")
    restore()
    ui.reselect(window, name, "Other")
    if not ui.has_check(window, "Build resolved", "PASS"):
        problems.append(f"after restoring: {ui.checks(window)}")

    ui.flip_byte(build / APP, (build / APP).stat().st_size - 8)  # being written right now
    ui.reselect(window, name, "Other")
    building = ui.plain(tr("The build output is being written; checking again shortly."))
    if ui.banner(window) != building:
        problems.append(f"fresh file: banner {ui.banner(window)!r}")
    if not ui.has_check(window, "Build in progress"):
        problems.append(f"fresh file: checks {ui.checks(window)}")
    kit.wait_until(lambda: ui.banner(window) != building, 10, message="still 'being written'")
    if _updated_tag(ui.header_tags(window)) is None:
        problems.append(f"after settling: header tags {ui.header_tags(window)}")
    ui.no_problems(problems)
    _ = QCheckBox
