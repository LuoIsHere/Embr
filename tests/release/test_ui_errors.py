"""Abnormal operations in the interface, in English and Simplified Chinese (design doc
§8.3 规则 and §12.7 必须覆盖的情况): double clicks and a second dialog, cancelling every
dialog, damaged and foreign files added as entries, projects and firmware written by a
newer Embr (read-only, never rewritten)."""

import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import release_kit as kit
import ui_kit as ui
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit
from ui_kit import watchdog  # noqa: F401  # pyright: ignore[reportUnusedImport]

from embr.i18n import Language, tr

Start = Callable[..., kit.EmbrApp]
pytestmark = pytest.mark.usefixtures("watchdog")
SYN = kit.FIXTURES / "synthetic"
APP = "hello_esp32s3.bin"


@ui.LANGUAGES
def test_double_clicks_and_a_second_dialog(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """A double click on Create makes one project; while a dialog is open the window
    behind it takes no clicks or shortcuts (no second dialog, no menu); quick repeated
    Duplicate gives distinct names."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    problems: list[str] = []

    def double(driver: kit.DialogDriver) -> None:
        driver.type("Name (unique)", "Twice")
        button = driver.button("Create")
        at = button.mapToGlobal(button.rect().center())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        # the second click of the double click lands on whatever is there now
        under = QApplication.widgetAt(at)
        if under is not None:
            QTest.mouseClick(
                under,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                under.mapFromGlobal(at),
            )
        kit.pump(100)

    with kit.expect_dialog(double, title="New project"):
        kit.new_menu(window, "New project")
    kit.pump(300)
    if [p["name"] for p in ui.project_docs(root)] != ["Twice"]:
        problems.append(f"double click on Create: {[p['name'] for p in ui.project_docs(root)]}")

    ui.fixed_firmware(
        window, file_dialogs, "Dup", kit.hello_files("esp32s3_hello", tmp_path / "b"), "Twice"
    )

    def blocked(driver: kit.DialogDriver) -> None:
        # Application-modal: Qt then drops real mouse and keyboard input to the window
        # behind it, so no second dialog or menu can be opened. (QTest input is injected
        # past that filter, so the clicks themselves are not replayed here.)
        if not driver.dialog.isModal() or QApplication.activeModalWidget() is not driver.dialog:
            problems.append("the New firmware dialog is not application-modal")
        driver.cancel()

    with kit.expect_dialog(blocked, title="New firmware"):
        kit.new_menu(window, "New firmware")
    assert kit.sidebar_names(window) == ["Dup"]

    for _ in range(3):
        ui.firmware_menu(window, "Duplicate")
        ui.click_row(window, "Dup")
    names = sorted(n for n in kit.sidebar_names(window) if n.startswith("Dup"))
    if names != ["Dup", "Dup copy", "Dup copy (2)", "Dup copy (3)"]:
        problems.append(f"repeated Duplicate: {names}")
    ui.no_problems(problems)


@ui.LANGUAGES
def test_cancel_changes_nothing(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Every dialog and file dialog cancelled: projects, firmware and archive unchanged."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    ui.new_project(window, "Keep")
    ui.add_group(window, "G")
    ui.fixed_firmware(
        window, file_dialogs, "Stay", kit.hello_files("esp32s3_hello", tmp_path / "b"), "Keep", "G"
    )
    before = ui.data_snapshot(root)
    steps: list[tuple[str, Callable[[], None]]] = [
        ("New project", lambda: kit.new_menu(window, "New project")),
        ("New firmware", lambda: kit.new_menu(window, "New firmware")),
        ("Rename firmware", lambda: ui.firmware_menu(window, "Rename…\tF2")),
        ("Move firmware", lambda: ui.firmware_menu(window, "Move to project…")),
        ("Delete firmware", lambda: ui.firmware_menu(window, "Delete…")),
        ("Archive this version", lambda: ui.firmware_menu(window, "Archive this version…")),
        ("Export firmware", lambda: ui.firmware_menu(window, "Export…")),
        ("Merge to .bin", lambda: kit.click(window.firmware_view.entries.merge_button)),
        ("Import ESP-IDF build", lambda: kit.new_menu(window, "Import ESP-IDF build…")),
        ("Import PlatformIO build", lambda: kit.new_menu(window, "Import PlatformIO build…")),
    ]
    for title, opener in steps:
        ui.click_row(window, "Stay")
        with kit.expect_dialog(ui.cancel, title=title):
            opener()
    for kind, opener in (
        ("open_many", lambda: ui.add_entry_menu(window, "Files…")),
        ("open", lambda: ui.add_entry_menu(window, "Attachment…")),
        ("open", lambda: kit.new_menu(window, "Import .embr package…")),
        ("open", lambda: ui.entry_context_menu(window, APP, "Re-locate…")),
    ):
        file_dialogs.queue_cancel(kind)
        with ui.no_dialog(0.3):
            opener()
    ui.click_row(window, "Keep", "project")
    for title, opener in (
        ("Delete project", lambda: kit.click_button(window.project_view, "Delete project…")),
        ("Remove group", lambda: kit.click_button(window.project_view, "Remove")),
        ("Export project", lambda: kit.click_button(window.project_view, "Export project…")),
    ):
        with kit.expect_dialog(ui.cancel, title=title):
            opener()
    with kit.expect_dialog(ui.cancel, title="Clean archive"), ui.menu("Clean archive…"):
        kit.click(window.topbar.menu_button)
    assert ui.data_snapshot(root) == before
    assert kit.sidebar_names(window) == ["Stay"]
    assert "[error]" not in kit.log_text(window)


@ui.LANGUAGES
def test_damaged_and_foreign_files_as_entries(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§5 stage one 识别 / §8.2 image_sniff: an app with a bad checksum is still an app;
    8 KiB of 0xFF is otadata; random data, an ELF, a text file and 4 KiB of 0xFF are
    unknown (no address: flashing is refused); a damaged partition table is not taken as
    one; a P4 app in an S3 firmware is shown as ESP32-P4 (two MCUs). Nothing crashes."""
    app = embr(language=language)
    window = app.window
    folder = tmp_path / "mixed"
    good = kit.hello_files("esp32s3_hello", folder)
    bad = folder / "bad_checksum.bin"
    shutil.copy(good[2], bad)
    ui.flip_byte(bad, 0x1000)
    (folder / "blank8k.bin").write_bytes(b"\xff" * 0x2000)
    for name in ("ff_4k.bin", "random_64k.bin"):
        shutil.copy(SYN / "negative" / name, folder / name)
    shutil.copy(SYN / "negative" / "program.elf", folder / "program.bin")
    shutil.copy(SYN / "negative" / "notes.txt", folder / "notes.bin")
    table = bytearray(good[1].read_bytes())
    table[0:2] = b"\x00\x00"  # the first entry's magic
    (folder / "table_damaged.bin").write_bytes(bytes(table))
    (folder / "truncated.bin").write_bytes(good[2].read_bytes()[:96])
    p4 = kit.IDF / "esp32p4_rev3_hello" / "build"
    p4_app = next(p for p in p4.glob("*.bin"))
    shutil.copy(p4_app, folder / "p4_app.bin")
    extra = [
        bad,
        folder / "blank8k.bin",
        folder / "ff_4k.bin",
        folder / "random_64k.bin",
        folder / "program.bin",
        folder / "notes.bin",
        folder / "table_damaged.bin",
        folder / "truncated.bin",
    ]
    ui.fixed_firmware(window, file_dialogs, "Mixed", [*good, *extra])
    rows = {e.name: e for e in ui.entries(window)}
    problems: list[str] = []
    expected = {
        "bad_checksum.bin": "app",
        "blank8k.bin": "otadata",
        "ff_4k.bin": "unknown",
        "random_64k.bin": "unknown",
        "program.bin": "unknown",
        "notes.bin": "unknown",
        "table_damaged.bin": "unknown",
    }
    for name, kind in expected.items():
        if name not in rows:
            problems.append(f"{name}: not added")
        elif rows[name].kind != kind:
            problems.append(f"{name}: kind {rows[name].kind!r}, expected {kind!r}")
    if "truncated.bin" not in rows:
        problems.append("truncated.bin: not added")
    layout = [c for c in ui.checks(window) if kit.same_text(c.title, "Entry layout")]
    for name in ("random_64k.bin", "ff_4k.bin"):
        if not any(c.state == "REFUSE" and name in c.detail for c in layout):
            problems.append(f"{name} (unknown, no address) is not refused: {ui.checks(window)}")
    if window.connection.flash.isEnabled():
        problems.append("Flash enabled")

    ui.add_files(window, file_dialogs, [folder / "p4_app.bin"])  # the wrong chip's app
    kit.wait_log(window, "[entry] added p4_app.bin")
    p4_row = ui.entry(window, "p4_app.bin")
    if p4_row.kind != "app" or not p4_row.mcu.startswith("ESP32-P4"):
        problems.append(f"p4_app.bin shown as {p4_row}")
    tags = ui.header_tags(window)
    if not ("ESP32-S3" in tags and any(t.startswith("ESP32-P4") for t in tags)):
        problems.append(f"header tags {tags}")
    if "multi" not in ui.row_texts(window, "Mixed"):
        problems.append(f"sidebar row {ui.row_texts(window, 'Mixed')}")
    ui.no_problems(problems)


def _newer(path: Path) -> bytes:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("schema_version = 1\n"), text[:40]
    data = text.replace("schema_version = 1\n", "schema_version = 99\n", 1).encode()
    data += b'future_field = "from a newer Embr"\n'
    path.write_bytes(data)
    return data


@ui.LANGUAGES
def test_newer_project_and_firmware_are_read_only(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§3.5: a project or firmware written by a newer Embr opens read-only with the hint
    "created by a newer Embr" and is never rewritten; it cannot be exported (§12.6)."""
    root = tmp_path / "embr"
    app = embr(root, language)
    window = app.window
    ui.new_project(window, "Future")
    ui.add_group(window, "G")
    files = kit.hello_files("esp32s3_hello", tmp_path / "b")
    newer_id = ui.fixed_firmware(window, file_dialogs, "Newer", files, "Future", "G")
    ui.fixed_firmware(
        window,
        file_dialogs,
        "Current",
        kit.hello_files("esp32s3_hello", tmp_path / "c"),
        "Future",
        "G",
    )
    project = ui.project_id(window, "Future")
    app.close()
    firmware_file = root / "data" / "firmwares" / f"{newer_id}.toml"
    project_file = root / "data" / "projects" / f"{project}.toml"
    firmware_bytes = _newer(firmware_file)
    project_bytes = _newer(project_file)
    app = embr(root, language)
    window = app.window
    problems: list[str] = []
    ui.click_row(window, "Newer")
    if tr("created by a newer Embr") not in ui.header_tags(window):
        problems.append(f"firmware header tags {ui.header_tags(window)}")
    rows = ui.entries(window)
    if not all(r.address_read_only for r in rows):
        problems.append("address cells editable")
    if window.firmware_view.entries.add_button.isEnabled():
        problems.append("Add entry enabled")
    if any(r.checkable for r in rows):
        problems.append("entries can be ticked")
    if window.connection.erase_all.isEnabled():
        problems.append("erase the whole flash check box enabled")
    ui.open_tab(window, "Parameters")
    if not ui.param_edit(window, "Name").isReadOnly():
        problems.append("Name editable on the Parameters tab")
    ui.open_tab(window, "Entries")
    with ui.collect(problems, "Rename… of a read-only firmware"):
        with kit.expect_dialog(
            ui.type_and_ok("Name", "Renamed"), title="Rename firmware", timeout=5
        ):
            ui.firmware_menu(window, "Rename…\tF2")
        kit.pump(200)
    ui.click_row(window, "Current")
    export: list[str] = []

    def look(driver: kit.DialogDriver) -> None:
        boxes = {kit.text_of(b).split(" — ")[0]: b for b in driver.checkboxes()}
        newer = boxes.get("Newer") or boxes.get("Renamed")
        if newer is None or newer.isEnabled():
            problems.append(
                f"a newer firmware can be exported: {[kit.text_of(b) for b in driver.checkboxes()]}"
            )
        export.extend(kit.text_of(b) for b in driver.checkboxes())
        driver.cancel()

    with kit.expect_dialog(look, title="Export firmware"):
        ui.firmware_menu(window, "Export…")
    if not any(tr("written by a newer Embr") in t for t in export):
        problems.append(f"no reason 'written by a newer Embr' in {export}")

    ui.click_row(window, "Future", "project")
    shown = " ".join(ui.header_texts(window) + kit.all_texts(window.project_view))
    if tr("created by a newer Embr") not in shown:
        problems.append("the project overview does not say the project was created by a newer Embr")
    name = ui.overview_name(window)
    if not name.isReadOnly():
        problems.append("the project name is editable")
        with ui.collect(problems, "renaming the newer project"):
            kit.type_text(name, "Future renamed", finish="return")
            kit.pump(200)
    with ui.collect(problems, "adding a group to the newer project"):
        edit = ui.new_group_edit(window)
        if edit.isEnabled() and not edit.isReadOnly():
            problems.append("groups can be added to the newer project")
            ui.add_group(window, "H")
    app.close()
    if firmware_file.read_bytes() != firmware_bytes:
        problems.append("the newer firmware file was rewritten")
    if project_file.read_bytes() != project_bytes:
        problems.append("the newer project file was rewritten")
    ui.no_problems(problems)
    _ = (re, QLineEdit)
