"""Interface flows: fixed firmware, in English and Simplified Chinese (design doc §3.1,
§3.2 entries and addresses, §3.4 fixed firmware, §5 stage one, §6.1 entries and
parameters tabs, §12 items 7, 8, 13, 16, 17, 19).

Files are copies of the tests/fixtures samples in tmp folders; the file dialog is the
only replaced part (release_kit.FileDialogs). Drops from Explorer are delivered as Qt
drag events (ui_kit.drop_files_on_row / drop_files_on_entries)."""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import release_kit as kit
import ui_kit as ui
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QLineEdit
from ui_kit import watchdog  # noqa: F401  # pyright: ignore[reportUnusedImport]

from embr.i18n import Language, tr

Start = Callable[..., kit.EmbrApp]
pytestmark = pytest.mark.usefixtures("watchdog")
SYN = kit.FIXTURES / "synthetic"
BOOT, TABLE, APP = ui.S3_FILES


def _doc(root: Path, name: str) -> dict[str, Any]:
    return ui.doc_named(ui.firmware_docs(root), name)


def _entry_doc(root: Path, firmware: str, entry: str) -> dict[str, Any]:
    entries = cast(list[dict[str, Any]], _doc(root, firmware)["entries"])
    hits = [e for e in entries if e["name"] == entry]
    assert len(hits) == 1, entries
    return hits[0]


@ui.LANGUAGES
def test_new_fixed_firmware_and_add_files(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """An unnamed new firmware takes the first file's name (design doc §5 stage one);
    entries are classified and placed automatically, sorted by address."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    folder = tmp_path / "s3 bins"
    files = kit.hello_files("esp32s3_hello", folder)
    ui.new_firmware(window, "", ui.STANDALONE)
    kit.wait_log(window, "[new] fixed firmware 'untitled' created as standalone")
    ui.add_files(window, file_dialogs, files)
    kit.wait_log(window, f"[entry] added {APP} as app at 0x10000")
    problems: list[str] = []
    assert kit.sidebar_names(window) == ["bootloader"]
    rows = ui.entries(window)
    assert [(e.address, e.name, e.kind, e.source) for e in rows] == [
        ("0x0", BOOT, "bootloader", "auto"),
        ("0x8000", TABLE, "partition_table", "auto"),
        ("0x10000", APP, "app", "auto"),
    ]
    assert all(e.enabled and e.checkable for e in rows)
    assert [e.mcu for e in rows if not e.mcu_choosable] == ["ESP32-S3", "ESP32-S3"]
    table_row = ui.entry(window, TABLE)
    assert table_row.mcu_choosable and table_row.mcu == tr("auto (ESP32-S3)")
    assert ui.entry(window, APP).requires.startswith(tr("chip v0.0").split(" ")[0])
    assert str(folder / APP) in ui.entry(window, APP).tooltip
    tags = ui.header_tags(window)
    assert "ESP32-S3" in tags and tr("fixed") in tags, tags
    texts = ui.row_texts(window, "bootloader")
    assert str(folder) in " ".join(texts), texts
    assert ui.check(window, "No address overlap").state == "PASS"
    files_check = ui.check(window, "Files match")
    assert files_check.state == "PASS"
    assert files_check.detail == ui.plain(
        tr("3 files checked: name, path and hash agree with the recorded values")
    )
    assert ui.check(window, "Chip / revision / size / security").state == "LATER"
    assert not window.connection.flash.isEnabled()  # nothing identified (§12 item 5)
    command = window.cmdbar._full
    for part in ("write-flash", "0x0", "0x8000", "0x10000", APP):
        assert part in command, command
    doc = _doc(root, "bootloader")
    assert doc["folder"] == str(folder)
    app_doc = _entry_doc(root, "bootloader", APP)
    assert app_doc["file"] == f"path:{folder / APP}"
    assert app_doc["placement"] == "address:0x10000"
    import hashlib

    assert app_doc["sha256"] == hashlib.sha256((folder / APP).read_bytes()).hexdigest()
    ui.open_tab(window, "Parameters")
    assert any(str(folder) in t for t in ui.param_texts(window, "Files folder"))
    if ui.in_chinese():
        problems += ui.english_leftovers(
            ui.window_texts(window), [str(folder), "bootloader", APP, BOOT, TABLE, "s3 bins"]
        )
    ui.no_problems(problems)


@ui.LANGUAGES
def test_drop_files_on_the_sidebar(embr: Start, language: Language, tmp_path: Path) -> None:
    """Explorer drops (design doc §12 item 19): .bin files on a group create a firmware
    there after the name is confirmed; on a firmware they are added; bad drops are
    refused as a whole with every reason listed; the folder rule applies."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    folder = tmp_path / "drop"
    files = kit.hello_files("esp32s3_hello", folder)
    ui.new_project(window, "Drops")
    kit.wait_log(window, "[project] created 'Drops'")
    ui.add_group(window, "G")
    kit.wait_log(window, "[group] added 'G'")
    seen: list[str] = []

    def name_it(driver: kit.DialogDriver) -> None:
        seen.append(driver.text())
        edit = driver.line_edit("Name shown in the sidebar")
        assert edit.text() == "bootloader"
        driver.type("Name shown in the sidebar", "Dropped")
        driver.ok()

    with kit.expect_dialog(name_it, title="New firmware"):
        assert ui.drop_files_on_row(window, ui.group_row(window, "Drops", "G"), files)
    kit.wait_log(window, "[new] fixed firmware 'Dropped' from 3 file(s)")
    assert tr(f"3 file(s) from {folder}") in seen[0]
    assert ui.placement_of(window, "Dropped") == ("Drops", "G")
    assert ui.entry_names(window) == ui.S3_FILES

    bad = tmp_path / "bad"
    bad.mkdir()
    text = shutil.copy(SYN / "negative" / "notes.txt", bad / "notes.txt")
    empty = bad / "empty.bin"
    empty.write_bytes(b"")
    link = bad / "app.lnk"
    link.write_bytes(b"L\x00\x00\x00")
    plain = bad / "plain folder"
    plain.mkdir()
    missing = bad / "gone.bin"
    before = ui.data_snapshot(root)
    reasons: list[str] = []

    def refused(driver: kit.DialogDriver) -> None:
        reasons.append(driver.text())
        driver.ok()

    with kit.expect_dialog(refused, title="Nothing added"):
        ui.drop_files_on_row(
            window,
            kit.sidebar_row(window, "Drops", "project"),
            [Path(text), empty, link, plain, missing, files[0]],
        )
    for expected in (
        "<b>notes.txt</b>: not a .bin file. To flash another kind of file, open a firmware "
        "and use Add entry, then Attachment, on its Entries tab.",
        "<b>empty.bin</b>: empty file (0 bytes)",
        "<b>app.lnk</b>: a shortcut; drop the file it points to",
        "<b>plain folder</b>: a folder without an ESP-IDF or PlatformIO build in it",
        "<b>gone.bin</b>: not found",
    ):
        assert ui.plain(tr(expected)) in reasons[0], reasons[0]
    assert ui.data_snapshot(root) == before

    extra = Path(shutil.copy(SYN / "attachments" / "calib_at_address.bin", folder / "calib.bin"))
    with ui.no_dialog():
        ui.drop_files_on_row(window, kit.sidebar_row(window, "Dropped"), [extra])
    kit.wait_log(window, "[entry] 1 entries added to 'Dropped'")
    assert "calib.bin" in ui.entry_names(window)

    outside = Path(shutil.copy(SYN / "attachments" / "assets_fits.bin", tmp_path / "assets.bin"))
    outside_reason: list[str] = []
    with kit.expect_dialog(ui.record_and_ok(outside_reason), title="Nothing added"):
        ui.drop_files_on_row(window, kit.sidebar_row(window, "Dropped"), [outside])
    assert f"assets.bin: not in the firmware's folder {folder}" in outside_reason[0]
    assert "assets.bin" not in ui.entry_names(window)

    sub = folder / "sub"
    sub.mkdir()
    nested = Path(shutil.copy(SYN / "attachments" / "assets_fits.bin", sub / "nested.bin"))
    assert ui.drop_files_on_entries(window, [nested])  # below the folder: allowed
    kit.wait_log(window, "[entry] added nested.bin")
    assert "nested.bin" in ui.entry_names(window)

    with kit.expect_dialog(
        ui.type_and_ok("Name shown in the sidebar", "Loose"), title="New firmware"
    ):
        ui.drop_files_on_row(window, ui.standalone_header(window), [outside])
    kit.wait_log(window, "[new] fixed firmware 'Loose' from 1 file(s)")
    assert ui.placement_of(window, "Loose") == (None, "")


def _place_attachment(
    choice: str, address: str | None = None
) -> list[tuple[str, Callable[[kit.DialogDriver], None]]]:
    def placement(driver: kit.DialogDriver) -> None:
        ui.pick(driver, "Placement", choice)
        driver.ok()

    steps: list[tuple[str, Callable[[kit.DialogDriver], None]]] = [("Place attachment", placement)]
    if address is not None:

        def hex_address(driver: kit.DialogDriver) -> None:
            edit = driver.dialog.findChild(QLineEdit)
            assert isinstance(edit, QLineEdit)
            kit.type_text(edit, address)
            driver.ok()

        steps.append(("Place attachment", hex_address))
    return steps


@ui.LANGUAGES
def test_attachments_by_partition_and_by_address(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Attachments (design doc §5 stage one 附件): by partition the address is the
    partition's offset and a file larger than the partition is refused; by address the
    address is kept."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    folder = tmp_path / "ota"
    build = kit.IDF / "esp32s3_ota_custom" / "build"
    files = ui.copy_files(
        [
            build / "bootloader" / "bootloader.bin",
            build / "partition_table" / "partition-table.bin",
            build / "ota_custom.bin",
            build / "ota_data_initial.bin",
            SYN / "attachments" / "assets_fits.bin",
            SYN / "attachments" / "assets_too_big.bin",
            SYN / "attachments" / "calib_at_address.bin",
        ],
        folder,
    )
    ui.fixed_firmware(window, file_dialogs, "OTA", files[:4])
    assert ui.entry(window, "partition-table.bin").address == "0x10000"
    assert ui.entry(window, "ota_custom.bin").address == "0x20000"
    assert ui.entry(window, "ota_data_initial.bin").address == "0x17000"
    problems: list[str] = []

    file_dialogs.queue_open(files[4])
    seen: list[list[str]] = []

    def placement(driver: kit.DialogDriver) -> None:
        seen.append(ui.dialog_texts(driver))
        ui.pick(driver, "Placement", "Partition: assets")
        driver.ok()

    with (
        ui.collect(problems, "by partition 'assets'"),
        ui.dialogs_in_order(("Place attachment", placement)),
    ):
        ui.add_entry_menu(window, "Attachment…")
    kit.pump(100)
    if "assets_fits.bin" not in ui.entry_names(window):
        problems.append(f"by partition 'assets': not added; log: {kit.log_text(window)[-400:]}")
    else:
        row = ui.entry(window, "assets_fits.bin")
        if (row.address, row.kind, row.source) != ("0x1A000", "attachment", tr("partition assets")):
            problems.append(f"by partition: shown as {row}")
        doc = _entry_doc(root, "OTA", "assets_fits.bin")
        if doc.get("placement") != "partition:assets":
            problems.append(f"by partition: saved as {doc}")

    file_dialogs.queue_open(files[5])
    with (
        ui.collect(problems, "too big by partition"),
        ui.dialogs_in_order(*_place_attachment("Partition: assets")),
    ):
        ui.add_entry_menu(window, "Attachment…")
    kit.pump(100)
    if "assets_too_big.bin" in ui.entry_names(window):
        layout = [c for c in ui.checks(window) if kit.same_text(c.title, "Entry layout")]
        if not any(
            (c.state == "REFUSE" and "does not fit" in c.detail) or tr("does not fit") in c.detail
            for c in layout
        ):
            problems.append(f"too big for 'assets' is not refused: {ui.checks(window)}")
        if window.connection.flash.isEnabled():
            problems.append("Flash enabled with an attachment larger than its partition")
    else:
        problems.append("the too-big attachment was not added at all (the checklist refuses it)")

    file_dialogs.queue_open(files[6])
    with ui.dialogs_in_order(*_place_attachment("At an address…", "3ff000")):
        ui.add_entry_menu(window, "Attachment…")
    kit.pump(100)
    if "calib_at_address.bin" not in ui.entry_names(window):
        problems.append("by address: not added")
    else:
        row = ui.entry(window, "calib_at_address.bin")
        if row.address != "0x3FF000" or row.kind != "attachment":
            problems.append(f"by address: shown as {row}")
        if _entry_doc(root, "OTA", "calib_at_address.bin").get("placement") != "address:0x3ff000":
            problems.append("by address: not saved at 0x3ff000")

    file_dialogs.queue_cancel("open")  # cancel the file dialog: nothing happens
    before = ui.data_snapshot(root)
    with ui.no_dialog(0.5):
        ui.add_entry_menu(window, "Attachment…")
    assert ui.data_snapshot(root) == before
    if ui.in_chinese():
        problems += ui.english_leftovers(
            seen[0], ["assets", "nvs", "otadata", "phy_init", "ota_0", "ota_1", "factory"]
        )
    ui.no_problems(problems)


@ui.LANGUAGES
def test_entry_address_editing(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Design doc §3.2: a typed address is manual; invalid input is refused and the old
    value kept; clearing restores the detected address; typing the detected value counts
    as auto; overlapping enabled entries are refused."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    files = kit.hello_files("esp32s3_hello", tmp_path / "bins")
    ui.fixed_firmware(window, file_dialogs, "Addr", files)

    ui.set_address(window, APP, "0x20000")
    row = ui.entry(window, APP)
    assert (row.address, row.source) == ("0x20000", "manual")
    doc = _entry_doc(root, "Addr", APP)
    assert (doc["placement"], doc["address_source"], doc["auto_address"]) == (
        "address:0x20000",
        "manual",
        "0x10000",
    )
    edit = window.firmware_view.entries.table.cellWidget(row.row, 1).findChild(QLineEdit)
    assert "(0x10000)" in edit.toolTip()

    ui.set_address(window, APP, "zz-not-hex")
    kit.wait_log(window, f"[entry] {APP}: 'zz-not-hex' is not a hex address; kept 0x20000")
    assert ui.entry(window, APP).address == "0x20000"
    assert _entry_doc(root, "Addr", APP)["placement"] == "address:0x20000"

    ui.set_address(window, APP, "")
    kit.wait_log(window, f"[entry] {APP}: address cleared, restored detected 0x10000")
    assert _entry_doc(root, "Addr", APP)["placement"] == "address:0x10000"
    problems: list[str] = []
    shown = (ui.entry(window, APP).address, ui.entry(window, APP).source)
    if shown != ("0x10000", "auto"):
        problems.append(f"after clearing, the row shows {shown} (restored 0x10000 / auto is saved)")

    ui.set_address(window, APP, "0x30000")
    ui.set_address(window, APP, "10000")  # the detected value again: auto
    assert ui.entry(window, APP).source == "auto"
    assert _entry_doc(root, "Addr", APP)["address_source"] == "auto"

    ui.set_address(window, TABLE, "0x0")  # overlaps the bootloader
    layout = [c for c in ui.checks(window) if kit.same_text(c.title, "Entry layout")]
    assert layout and layout[0].state == "REFUSE", ui.checks(window)
    assert "overlap" in layout[0].detail or tr("overlap") in layout[0].detail or layout[0].detail
    assert not window.connection.flash.isEnabled()
    ui.tick(window, BOOT, False)  # unticked entries are not checked for overlap
    ui.wait_check(window, "No address overlap", "PASS")
    ui.tick(window, BOOT, True)
    ui.set_address(window, TABLE, "")
    ui.wait_check(window, "No address overlap", "PASS")
    if ui.entry(window, TABLE).address != "0x8000":
        problems.append(f"after clearing, the table row shows {ui.entry(window, TABLE).address!r}")

    ui.set_address(window, APP, "0x1FF0000")  # beyond any 16 MB flash: kept as typed;
    assert ui.entry(window, APP).address == "0x1FF0000"  # refused by the capacity check
    assert ui.check(window, "Chip / revision / size / security").state == "LATER"
    ui.no_problems(problems)


@ui.LANGUAGES
def test_mcu_column_and_multi_chip_firmware(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Design doc §12 item 13: files without chip information get an MCU chooser (empty by
    default, "auto (chip)" in a single-chip firmware); a firmware with entries for two
    MCUs shows both in the header, "multi" in the sidebar, a read-only chip on the
    Parameters tab, no command before identification, and cannot be merged."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    folder = tmp_path / "kit"
    folder.mkdir()
    calib = Path(shutil.copy(SYN / "attachments" / "calib_at_address.bin", folder / "calib.bin"))
    s3 = kit.hello_files("esp32s3_hello", folder / "s3")
    p4_build = kit.IDF / "esp32p4_rev3_hello" / "build"
    p4 = ui.copy_files(
        [p4_build / "bootloader" / "bootloader.bin", *sorted(p4_build.glob("*.bin"))], folder / "p4"
    )
    p4_app = next(p.name for p in p4 if p.name != "bootloader.bin")
    ui.fixed_firmware(window, file_dialogs, "Kit", [calib, *s3])
    calib_row = ui.entry(window, "calib.bin")
    assert calib_row.mcu_choosable and calib_row.mcu == tr("auto (ESP32-S3)")
    problems: list[str] = []

    ui.set_address(window, "calib.bin", "0x3FF000")
    ui.add_files(window, file_dialogs, p4)
    kit.wait_log(window, f"[entry] added {p4_app}")
    tags = ui.header_tags(window)
    p4_label = next((t for t in tags if t.startswith("ESP32-P4")), None)
    if "ESP32-S3" not in tags or p4_label is None:
        problems.append(f"header tags {tags} do not list both chips")
    if "multi" not in ui.row_texts(window, "Kit"):
        problems.append(f"sidebar row {ui.row_texts(window, 'Kit')} does not say multi")
    calib_row = ui.entry(window, "calib.bin")
    if calib_row.mcu != "—":
        problems.append(f"calib.bin MCU shows {calib_row.mcu!r} with two chips (expected empty)")
    if "write-flash" in window.cmdbar._full:
        problems.append(f"multi-chip command shown before identifying: {window.cmdbar._full}")
    ui.choose_entry_mcu(window, "calib.bin", "ESP32-S3")
    kit.wait_log(window, "[entry] calib.bin: MCU -> ESP32-S3")
    if _entry_doc(root, "Kit", "calib.bin").get("chip") != "esp32s3":
        problems.append("the chosen MCU is not saved")
    ui.open_tab(window, "Parameters")
    chip_texts = ui.param_texts(window, "Chip")
    if not any("ESP32-S3" in t and "ESP32-P4" in t for t in chip_texts):
        problems.append(f"Parameters chip {chip_texts} does not list both")
    if ui.param_combos(window, "Chip"):
        problems.append("the chip of a multi-chip firmware is editable")
    ui.open_tab(window, "Entries")

    merge_seen: list[str] = []
    ui.choose_entry_mcu(window, "partition-table.bin", "ESP32-S3")  # every entry has an MCU

    def merge(driver: kit.DialogDriver) -> None:
        kit.pump(200)
        merge_seen.append(" | ".join(ui.dialog_texts(driver)))
        if driver.ok_enabled():
            problems.append("Merge is enabled for entries of two chips")
        driver.cancel()

    with kit.expect_dialog(merge, title="Merge to .bin"):
        kit.click(window.firmware_view.entries.merge_button)
    if not ("esp32s3" in merge_seen[0] and "esp32p4" in merge_seen[0]):
        problems.append(f"the merge dialog does not name both chips: {merge_seen[0]}")
    ui.no_problems(problems)


@ui.LANGUAGES
def test_enable_disable_and_entry_notes(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    app = embr(language=language)
    window, root = app.window, app.paths.root
    files = kit.hello_files("esp32s3_hello", tmp_path / "bins")
    ui.fixed_firmware(window, file_dialogs, "Ticks", files)
    ui.fixed_firmware(window, file_dialogs, "Other", files)
    ui.click_row(window, "Ticks")

    ui.tick(window, APP, False)
    assert _entry_doc(root, "Ticks", APP)["enabled"] is False
    assert ui.check(window, "Files match").detail == ui.plain(
        tr("2 files checked: name, path and hash agree with the recorded values")
    )
    assert APP not in window.cmdbar._full
    for name in (BOOT, TABLE):
        ui.tick(window, name, False)
    assert ui.check(window, "No entries enabled").state == "REFUSE"
    assert not window.firmware_view.entries.merge_button.isEnabled()
    for name in ui.S3_FILES:
        ui.tick(window, name, True)
    assert window.firmware_view.entries.merge_button.isEnabled()

    ui.set_note(window, APP, "主程序 main v1")
    assert _entry_doc(root, "Ticks", APP)["note"] == "主程序 main v1"
    ui.reselect(window, "Ticks", "Other")
    assert ui.entry(window, APP).note == "主程序 main v1"
    name_cell = window.firmware_view.entries.table.cellWidget(ui.entry(window, APP).row, 3)
    assert name_cell.findChild(QLineEdit) is None  # the entry name is the file name, read-only


@ui.LANGUAGES
def test_entry_column_widths_are_kept(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Design doc §12.5: columns Address…Image requires can be dragged; widths are saved in
    settings.ini [ui] entry_columns; Note keeps at least 120 px; no horizontal scrolling."""
    root = tmp_path / "embr"
    app = embr(root, language)
    window = app.window
    files = kit.hello_files("esp32s3_hello", tmp_path / "bins")
    ui.fixed_firmware(window, file_dialogs, "Cols", files)
    table = window.firmware_view.entries.table
    header = table.horizontalHeader()
    before = header.sectionSize(4)  # Kind
    edge = header.sectionViewportPosition(4) + header.sectionSize(4) - 1
    y = header.height() // 2
    viewport = header.viewport()
    QTest.mousePress(
        viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(edge, y)
    )
    for step in range(1, 9):
        QTest.mouseMove(viewport, QPoint(edge + step * 5, y))
        kit.pump(10)
    QTest.mouseRelease(
        viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(edge + 40, y)
    )
    kit.pump(100)
    widened = header.sectionSize(4)
    assert widened >= before + 30, (before, widened)
    assert header.sectionSize(7) >= 120
    shown = sum(
        header.sectionSize(c) for c in range(table.columnCount()) if not header.isSectionHidden(c)
    )
    assert shown <= table.viewport().width() + 1
    app.close()
    import configparser

    parser = configparser.RawConfigParser(delimiters=("=",))
    parser.read(root / "settings.ini", encoding="utf-8")
    saved = parser.get("ui", "entry_columns")
    assert str(widened) in saved.replace(" ", "").split(","), saved
    again = embr(root, language)
    ui.click_row(again.window, "Cols")
    assert again.window.firmware_view.entries.table.horizontalHeader().sectionSize(4) == widened


@ui.LANGUAGES
def test_parameters_tab(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Fixed firmware: flash mode / freq / size, reset modes, notes and tags are edited on
    the Parameters tab and saved at once; erase_all is the check box above Flash; the
    command follows (no --skip-flashed with erase_all, §8.2); Re-locate folder…"""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    folder = tmp_path / "params"
    files = kit.hello_files("esp32s3_hello", folder)
    ui.fixed_firmware(window, file_dialogs, "Params", files)
    ui.open_tab(window, "Parameters")
    mode, freq, size = ui.param_combos(window, "Flash mode / freq / size")
    assert (mode.currentText(), freq.currentText(), size.currentText()) == ("keep",) * 3
    ui.choose(mode, "qio")
    ui.choose(ui.param_combos(window, "Flash mode / freq / size")[1], "40m")
    ui.choose(ui.param_combos(window, "Flash mode / freq / size")[2], "4MB")
    assert _doc(root, "Params")["flash"] == {"mode": "qio", "freq": "40m", "size": "4MB"}
    command = window.cmdbar._full
    assert "qio" in command and "40m" in command and "4MB" in command, command
    ui.choose(ui.param_combos(window, "Reset before")[0], "no-reset")
    ui.choose(ui.param_combos(window, "Reset after")[0], "watchdog-reset")
    assert _doc(root, "Params")["reset"] == {"before": "no-reset", "after": "watchdog-reset"}
    assert "--before no-reset" in window.cmdbar._full
    assert "--skip-flashed" in window.cmdbar._full
    kit.set_checked(window.connection.erase_all, True)
    assert _doc(root, "Params")["erase_all"] is True
    assert "--skip-flashed" not in window.cmdbar._full
    assert "--erase-all" in window.cmdbar._full
    kit.set_checked(window.connection.erase_all, False)
    assert _doc(root, "Params")["erase_all"] is False

    tags = ui.param_edit(window, "Tags")
    kit.type_text(tags, "release, s3 ,, ", finish="return")
    assert _doc(root, "Params")["tags"] == ["release", "s3"]
    ui.commit_notes(
        ui.notes_edit(window.firmware_view.params), "工厂测试 factory", window.sidebar_panel.search
    )
    kit.wait_until(lambda: _doc(root, "Params")["notes"] == "工厂测试 factory", 5)
    assert any("工厂测试 factory" in t for t in ui.header_texts(window))
    chip = ui.param_texts(window, "Chip")
    assert any("ESP32-S3" in t for t in chip) and tr("derived") in chip

    name = ui.param_edit(window, "Name")
    kit.type_text(name, "Params v2", finish="return")
    kit.wait_log(window, "[firmware] renamed to 'Params v2'")
    assert "Params v2" in kit.sidebar_names(window)

    moved = tmp_path / "moved params"
    shutil.move(folder, moved)
    file_dialogs.queue_directory(moved)
    ui.open_tab(window, "Parameters")
    kit.click_button(window.firmware_view.params, "Re-locate folder…")
    kit.wait_log(window, f": files now in {moved}")
    assert _doc(root, "Params v2")["folder"] == str(moved)
    assert _entry_doc(root, "Params v2", APP)["file"] == f"path:{moved / APP}"
    ui.open_tab(window, "Entries")
    assert ui.check(window, "Files match").state == "PASS"


@ui.LANGUAGES
def test_rename_duplicate_move_and_delete_firmware(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    app = embr(language=language)
    window, root = app.window, app.paths.root
    files = kit.hello_files("esp32s3_hello", tmp_path / "bins")
    ui.new_project(window, "Home")
    kit.wait_log(window, "[project] created 'Home'")
    ui.add_group(window, "A")
    ui.add_group(window, "B")
    ui.fixed_firmware(window, file_dialogs, "fw", files, "Home", "A")
    ui.fixed_firmware(window, file_dialogs, "other", files, "Home", "A")
    ui.click_row(window, "fw")

    def rename(to: str) -> Callable[[kit.DialogDriver], None]:
        def handler(driver: kit.DialogDriver) -> None:
            assert driver.line_edit("Name").text() in ("fw", "other (2)", "fw2")
            driver.type("Name", to)
            driver.ok()

        return handler

    with kit.expect_dialog(rename("other"), title="Rename firmware"):
        ui.firmware_menu(window, "Rename…\tF2")
    kit.wait_log(window, "[firmware] renamed to 'other (2)'")  # duplicate: suffix
    with kit.expect_dialog(rename("   "), title="Rename firmware"):  # empty: unchanged
        QTest.keyClick(window, Qt.Key.Key_F2)
    assert "other (2)" in kit.sidebar_names(window)
    with kit.expect_dialog(rename("fw2"), title="Rename firmware"):
        QTest.keyClick(window, Qt.Key.Key_F2)
    kit.wait_log(window, "[firmware] renamed to 'fw2'")
    with kit.expect_dialog(ui.cancel, title="Rename firmware"):
        ui.firmware_menu(window, "Rename…\tF2")
    assert "fw2" in kit.sidebar_names(window)

    ui.firmware_menu(window, "Duplicate")
    kit.wait_log(window, "[firmware] duplicated as 'fw2 copy'")
    assert ui.placement_of(window, "fw2 copy") == ("Home", "A")
    assert ui.entry_names(window) == ui.S3_FILES
    assert _doc(root, "fw2 copy")["id"] != _doc(root, "fw2")["id"]

    ui.click_row(window, "fw2")

    def move(driver: kit.DialogDriver) -> None:
        assert driver.combo("Place in").currentText() == "Home"
        ui.pick(driver, "Group", "B")
        driver.ok()

    with kit.expect_dialog(move, title="Move firmware"):
        ui.firmware_menu(window, "Move to project…")
    kit.wait_log(window, "[move] 'fw2' → Home / B")
    assert _doc(root, "fw2")["device"] == "B"

    assert ui.drag_firmware(window, "fw2", kit.sidebar_row(window, "Home", "project"))
    kit.wait_log(window, "[move] 'fw2' → Home (ungrouped)")
    assert ui.placement_of(window, "fw2") == ("Home", "")
    assert ui.drag_firmware(window, "fw2", ui.group_row(window, "Home", "A"))
    kit.wait_log(window, "[move] 'fw2' → Home / A")
    assert ui.drag_firmware(window, "fw2", ui.standalone_header(window))
    kit.wait_log(window, "[move] 'fw2' → standalone")
    assert "project_id" not in _doc(root, "fw2") and _doc(root, "fw2")["device"] == ""
    spare = kit.sidebar_rows(window)[-1][0]
    assert ui.drag_firmware(window, "other", spare)  # the empty row at the end: standalone
    kit.wait_log(window, "[move] 'other' → standalone")

    ui.new_firmware(window, "fw2", "Home", "A")  # same name in the project
    kit.wait_log(window, "[new] fixed firmware 'fw2' created in Home")
    standalone_id = next(
        n.firmware_id
        for _, n, s in kit.sidebar_rows(window)
        if n and s == "fw2" and n.project_id is None
    )
    tree = window.sidebar_panel.tree
    from PySide6.QtCore import QMimeData

    mime = QMimeData()
    mime.setData(ui.FIRMWARE_MIME, standalone_id.encode())
    at = tree.visualItemRect(ui.group_row(window, "Home", "A")).center()
    ui.drop(tree.viewport(), at, mime, Qt.DropAction.MoveAction)
    kit.wait_log(window, "[move] 'fw2 (2)' → Home / A")  # moved in: suffix

    ui.click_row(window, "fw2 copy")
    archive = ui.archive_files(root)
    seen: list[ui.MenuSeen] = []
    with kit.expect_dialog(ui.cancel, title="Delete firmware"):
        ui.firmware_menu(window, "Delete…", seen=seen)
    assert seen[0].danger == [ui.plain(tr("Delete…"))]
    assert "fw2 copy" in kit.sidebar_names(window)
    texts: list[str] = []

    def delete(driver: kit.DialogDriver) -> None:
        texts.append(driver.text())
        assert ui.is_danger(driver.dialog.ok)  # type: ignore[attr-defined]
        driver.ok()

    with kit.expect_dialog(delete, title="Delete firmware"):
        ui.context_menu_on_firmware(window, "fw2 copy", "Delete…")
    kit.wait_log(window, "[firmware] 'fw2 copy' deleted")
    assert "fw2 copy" not in kit.sidebar_names(window)
    assert all(d["name"] != "fw2 copy" for d in ui.firmware_docs(root))
    assert ui.archive_files(root) == archive
    assert (
        ui.plain(
            tr(
                "Delete <b>fw2 copy</b>? Archived files are kept (use Clean archive to remove unreferenced ones) and flash records are not affected."  # noqa: E501
            )
        )
        in texts[0]
    )
    _ = QComboBox


@ui.LANGUAGES
def test_folder_rule_for_later_files(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Design doc §12 item 8: later files must be in the first file's folder or below it;
    others are refused and nothing changes."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    folder = tmp_path / "root"
    files = kit.hello_files("esp32s3_hello", folder)
    ui.fixed_firmware(window, file_dialogs, "Rule", files[:1])
    other = Path(shutil.copy(files[1], tmp_path / "partition-table.bin"))
    before = _doc(root, "Rule")
    ui.add_files(window, file_dialogs, [other])
    kit.wait_log(window, f"[entry] partition-table.bin: not in the firmware's folder {folder}")
    assert _doc(root, "Rule") == before
    assert ui.entry_names(window) == [BOOT]
    deep = folder / "a" / "b"
    deep.mkdir(parents=True)
    nested = Path(shutil.copy(files[2], deep / APP))
    ui.add_files(window, file_dialogs, [nested])
    kit.wait_log(window, f"[entry] added {APP}")
    assert ui.entry_names(window) == [BOOT, APP]
    assert _doc(root, "Rule")["folder"] == str(folder)


@ui.LANGUAGES
def test_entry_files_missing_changed_and_relocated(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Design doc §3.4 固定型固件: a missing file blocks flashing until it is re-located or
    removed; changed content needs confirmation."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    folder = tmp_path / "files"
    files = kit.hello_files("esp32s3_hello", folder)
    ui.fixed_firmware(window, file_dialogs, "Watched", files)
    ui.fixed_firmware(
        window, file_dialogs, "Elsewhere", kit.hello_files("esp32s3_hello", tmp_path / "e")
    )
    ui.click_row(window, "Watched")
    problems: list[str] = []

    spare = Path(shutil.copy(files[2], tmp_path / "spare.bin"))
    files[2].unlink()
    ui.reselect(window, "Watched", "Elsewhere")
    expected = ui.plain(
        tr(
            f"Files missing: {APP}. Re-locate them (right-click an entry) or remove the entries; flashing is blocked until then."  # noqa: E501
        )
    )
    if ui.banner(window) != expected:
        problems.append(f"banner {ui.banner(window)!r}, expected {expected!r}")
    if tr("file missing") not in ui.entry(window, APP).tags:
        problems.append(f"entry tags {ui.entry(window, APP).tags}")
    if tr("file missing") not in ui.header_tags(window):
        problems.append(f"header tags {ui.header_tags(window)}")
    if chr(0xD7) not in ui.row_texts(window, "Watched"):
        problems.append(f"sidebar row {ui.row_texts(window, 'Watched')} has no ×")  # noqa: RUF001
    if not ui.has_check(window, "File missing", "REFUSE"):
        problems.append(f"checks {ui.checks(window)}")

    back = Path(shutil.copy(spare, folder / APP))
    file_dialogs.queue_open(back)
    ui.entry_context_menu(window, APP, "Re-locate…")
    kit.wait_log(window, f"[entry] {APP} now points at {back}")
    if ui.banner(window):
        problems.append(f"banner after re-locating: {ui.banner(window)!r}")
    if not ui.has_check(window, "Files match", "PASS"):
        problems.append(f"checks after re-locating: {ui.checks(window)}")

    ui.flip_byte(files[0])
    ui.reselect(window, "Watched", "Elsewhere")
    changed = ui.plain(
        tr(
            f"Files changed on disk since the last check: {BOOT} (content). Flashing is blocked until you confirm the list below."  # noqa: E501
        )
    )
    if ui.banner(window) != changed:
        problems.append(f"banner {ui.banner(window)!r}, expected {changed!r}")
    if tr("content changed") not in ui.entry(window, BOOT).tags:
        problems.append(f"bootloader tags {ui.entry(window, BOOT).tags}")
    if "!" not in ui.row_texts(window, "Watched"):
        problems.append(f"sidebar row {ui.row_texts(window, 'Watched')} has no !")
    if not ui.has_check(window, "Needs confirmation", "REFUSE"):
        problems.append(f"checks {ui.checks(window)}")
    button = ui.banner_button(window)
    if not button.isVisible() or not kit.same_text(button.text(), "Confirm entries"):
        problems.append("no Confirm entries button")
    else:
        kit.click(button)
        kit.wait_log(window, "[entries] Watched: entries confirmed")
        if ui.banner(window) or not ui.has_check(window, "Files match", "PASS"):
            problems.append(f"after confirming: {ui.banner(window)!r} {ui.checks(window)}")
        import hashlib

        sha = hashlib.sha256(files[0].read_bytes()).hexdigest()
        if _entry_doc(root, "Watched", BOOT)["sha256"] != sha:
            problems.append("the confirmed hash is not saved")
    ui.no_problems(problems)
    _ = pytest
