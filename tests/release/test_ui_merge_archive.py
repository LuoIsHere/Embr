"""Interface flows: Merge to .bin, Archive this version, archiving an entry and Clean
archive, in English and Simplified Chinese (design doc §3.4 归档, §5 合成工具, §6.1,
§12 items 3, 13, 15; §8.3 target file exists or is not writable)."""

import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import release_kit as kit
import ui_kit as ui
from ui_kit import watchdog  # noqa: F401  # pyright: ignore[reportUnusedImport]

from embr.i18n import Language, tr

Start = Callable[..., kit.EmbrApp]
pytestmark = pytest.mark.usefixtures("watchdog")
REFERENCE = kit.FIXTURES / "synthetic" / "merged" / "esp32s3_offset_0x0.bin"
"""esptool merge-bin of idf/esp32s3_hello at offset 0x0 (tests/fixtures/make_synthetic.py)."""
APP = "hello_esp32s3.bin"


def _doc(root: Path, name: str) -> dict[str, Any]:
    return ui.doc_named(ui.firmware_docs(root), name)


def _merge(
    window: Any,
    dialogs: kit.FileDialogs,
    output: Path | None,
    *,
    offset: str = "0x0",
    pad: bool = False,
    add: bool = True,
    seen: list[str] | None = None,
    then: Callable[[kit.DialogDriver], None] | None = None,
) -> None:
    """Merge to .bin… with these options; Save as through Browse… (the save dialog)."""

    def fill(driver: kit.DialogDriver) -> None:
        kit.type_text(driver.line_edit("Start offset (hex)"), offset, finish="tab")
        driver.check("pad to the whole flash size", pad)
        driver.check("Import into Embr as a firmware", add)
        if output is not None:
            dialogs.queue_save(output)
            driver.press("Browse…")
        kit.pump(100)
        if seen is not None:
            seen.append(" | ".join(ui.dialog_texts(driver)))
        if then is not None:
            then(driver)
        else:
            driver.ok()

    with kit.expect_dialog(fill, title="Merge to .bin"):
        kit.click(window.firmware_view.entries.merge_button)


@ui.LANGUAGES
def test_merge_to_bin(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """The merged image equals esptool merge-bin's; "Import into Embr" adds a fixed
    firmware "<name> (merged)" with one merged entry at the start offset, in the source's
    project and group; the source is unchanged; pad needs a flash size; an invalid offset
    keeps Merge disabled."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    files = kit.hello_files("esp32s3_hello", tmp_path / "bins")
    ui.new_project(window, "M")
    ui.add_group(window, "G")
    ui.fixed_firmware(window, file_dialogs, "Mergeable", files, "M", "G")
    source = _doc(root, "Mergeable")
    out = tmp_path / "out"
    out.mkdir()
    problems: list[str] = []

    def bad_offset(driver: kit.DialogDriver) -> None:
        # Enter: editingFinished (the dialog's default button is disabled meanwhile)
        kit.type_text(driver.line_edit("Start offset (hex)"), "zz", finish="return")
        kit.pump(50)
        if driver.ok_enabled():
            problems.append("Merge enabled with the start offset 'zz'")
        driver.wait_text("The start offset is not a hex number.")
        default = driver.line_edit("Save as").text()
        if Path(default).name != "Mergeable_merged.bin":
            problems.append(f"default Save as {default!r}")
        driver.cancel()

    with kit.expect_dialog(bad_offset, title="Merge to .bin"):
        kit.click(window.firmware_view.entries.merge_button)

    merged = out / "merged.bin"
    _merge(window, file_dialogs, merged)
    kit.wait_log(window, f"[merge] written to {merged}")
    assert merged.read_bytes() == REFERENCE.read_bytes()
    name = "Mergeable (merged)"
    assert kit.sidebar_names(window).count(name) == 1
    assert ui.placement_of(window, name) == ("M", "G")
    rows = ui.entries(window)
    assert [(e.address, e.kind) for e in rows] == [("0x0", "merged")], rows
    assert "ESP32-S3" in ui.header_tags(window)
    assert _doc(root, "Mergeable")["entries"] == source["entries"]

    # flash size "keep": the bootloader header's size (2MB in this build), or refused
    ui.click_row(window, "Mergeable")
    unpadded = out / "pad-keep.bin"
    count = kit.log_text(window).count("[merge] ")

    def pad_without_size(driver: kit.DialogDriver) -> None:
        kit.pump(100)
        if driver.ok_enabled():
            driver.ok()
        else:
            driver.cancel()

    _merge(window, file_dialogs, unpadded, pad=True, add=False, then=pad_without_size)
    kit.pump(300)
    if unpadded.exists():  # padded to the bootloader header's size (2MB)
        if unpadded.stat().st_size != 2 * 1024 * 1024:
            problems.append(f"padded without a flash size: {unpadded.stat().st_size} bytes")
    elif kit.log_text(window).count("[merge] ") > count:
        last = [line for line in kit.log_text(window).splitlines() if "[merge] " in line][-1]
        if "flash size" not in last:
            problems.append(f"merge with pad and flash size keep: {last}")
    ui.open_tab(window, "Parameters")
    ui.choose(ui.param_combos(window, "Flash mode / freq / size")[2], "4MB")
    ui.open_tab(window, "Entries")
    padded = out / "padded.bin"
    _merge(window, file_dialogs, padded, pad=True, add=False)
    kit.wait_log(window, f"[merge] written to {padded}")
    assert padded.stat().st_size == 4 * 1024 * 1024
    assert kit.sidebar_names(window).count(name) == 1  # not imported this time
    ui.no_problems(problems)


@ui.LANGUAGES
def test_merge_target_exists_or_is_not_writable(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§8.3 abnormal operations, as decided in §5 合成工具 (2026-10-31): an existing target
    file of Merge to .bin… is overwritten directly, without a Replace file question, and
    nothing else changes; a folder that cannot be written gives an error, no firmware and
    no crash."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    bins = tmp_path / "bins"
    files = kit.hello_files("esp32s3_hello", bins)
    ui.fixed_firmware(window, file_dialogs, "Target", files)
    out = tmp_path / "out"
    out.mkdir()
    existing = out / "existing.bin"
    existing.write_bytes(b"old target")
    (out / "neighbour.bin").write_bytes(b"neighbour")
    problems: list[str] = []
    asked: list[str] = []

    def replace(driver: kit.DialogDriver) -> None:
        asked.append(driver.text())
        driver.cancel()

    names = kit.sidebar_names(window)
    docs = ui.firmware_docs(root)
    sources = kit.snapshot(bins)
    neighbours = {k: v for k, v in kit.snapshot(out).items() if k != existing.name}
    try:
        with kit.expect_dialog(replace, title="Replace file", timeout=3):
            _merge(window, file_dialogs, existing, add=False)
    except AssertionError as exc:
        if "did not appear" not in str(exc):
            raise
    if asked:
        problems.append(f"a Replace file question for an existing target: {asked[0]}")
    kit.wait_log(window, f"[merge] written to {existing}")
    kit.assert_no_dialog("Replace file", seconds=1.0)
    if existing.read_bytes() != REFERENCE.read_bytes():
        problems.append(
            f"the existing target does not hold the merged image ({existing.stat().st_size} bytes)"
        )
    after = {k: v for k, v in kit.snapshot(out).items() if k != existing.name}
    if after != neighbours:
        problems.append(f"other files in the target folder changed: {neighbours} -> {after}")
    if kit.snapshot(bins) != sources:
        problems.append("the source files changed")
    if kit.sidebar_names(window) != names:
        problems.append(f"the firmware list changed: {names} -> {kit.sidebar_names(window)}")
    if ui.firmware_docs(root) != docs:
        problems.append("a firmware document changed after a merge without import")
    if leftovers := sorted(p.name for p in app.paths.temp.glob("merge-*")):
        problems.append(f"temporary merge files left behind: {leftovers}")

    locked = tmp_path / "locked"
    locked.mkdir()
    before = kit.sidebar_names(window)
    count = kit.log_text(window).count("[merge] ")
    with kit.deny_access(locked, "(OI)(CI)(W,AD,WD)"):
        _merge(window, file_dialogs, locked / "merged.bin", add=True)
        kit.wait_until(lambda: kit.log_text(window).count("[merge] ") > count, 15)
    last = [line for line in kit.log_text(window).splitlines() if "[merge] " in line][-1]
    if (locked / "merged.bin").exists() or "[merge] written to" in last:
        problems.append("merge into a folder without write access reported success")
    if kit.sidebar_names(window) != before:
        problems.append(f"a firmware was added after a failed merge: {kit.sidebar_names(window)}")
    ui.no_problems(problems)


@ui.LANGUAGES
def test_archive_this_version(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§3.4 归档此版本: a new archived firmware next to the tracking one (same project and
    group), named "<name> (archived YYYY-MM-DD)", its files copied into data/archive/; the
    tracking firmware and the build files are unchanged."""
    build = kit.idf_build_copy("esp32s3_hello", tmp_path / "proj")
    app = embr(language=language)
    window, root = app.window, app.paths.root
    ui.new_project(window, "P")
    ui.add_group(window, "G")
    ui.import_idf(window, file_dialogs, build, place="P", group="G")
    name = "hello_esp32s3"
    stamps = {p: p.stat().st_mtime_ns for p in build.rglob("*.bin")}
    with kit.expect_dialog(ui.cancel, title="Archive this version"):
        ui.firmware_menu(window, "Archive this version…")
    assert ui.archive_files(root) == []
    archived = f"{name} (archived {date.today().isoformat()})"
    seen: list[str] = []

    def archive(driver: kit.DialogDriver) -> None:
        seen.append(driver.text())
        assert driver.line_edit("Name").text() == archived
        driver.type("Notes", "release 1.0")
        driver.ok()

    with kit.expect_dialog(archive, title="Archive this version"):
        ui.firmware_menu(window, "Archive this version…")
    kit.wait_log(window, f"[archive] '{archived}' created; '{name}' is unchanged")
    for file in ui.S3_FILES:
        assert file in seen[0], seen[0]
    assert ui.placement_of(window, archived) == ("P", "G")
    row = ui.row_texts(window, archived)
    assert "ARC" in row and tr("Archive") in row, row
    assert tr("archived") in ui.header_tags(window)
    assert ui.entry_names(window) == ui.S3_FILES
    assert "data/archive/" in ui.entry(window, APP).tooltip
    assert len(ui.archive_files(root)) == 3
    doc = _doc(root, archived)
    assert doc["source"] == "archived" and doc["notes"] == "release 1.0"
    assert all(str(e["file"]).startswith("archive:") for e in doc["entries"])
    assert {p: p.stat().st_mtime_ns for p in build.rglob("*.bin")} == stamps
    assert _doc(root, name)["source"] == "idf_build"
    ui.open_tab(window, "Parameters")
    assert any(name in t for t in ui.param_texts(window, "Archived from"))


def _clean_dialog(window: Any, handler: Callable[[kit.DialogDriver], None]) -> None:
    with kit.expect_dialog(handler, title="Clean archive"), ui.menu("Clean archive…"):
        kit.click(window.topbar.menu_button)


@ui.LANGUAGES
def test_archive_entry_and_clean_archive(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Archive this file (entry menu) points the entry at data/archive/; Clean archive
    first shows what the archive holds and who refers to it (§12 item 15), deletes only
    unreferenced files after a red confirmation."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    files = kit.hello_files("esp32s3_hello", tmp_path / "keep")
    ui.fixed_firmware(window, file_dialogs, "Keep", files)
    ui.entry_context_menu(window, APP, "Archive this file")
    kit.wait_log(window, f"[archive] {APP} copied into the archive")
    assert str(_doc(root, "Keep")["entries"][2]["file"]).startswith("archive:")
    assert "data/archive/" in ui.entry(window, APP).tooltip
    other = ui.copy_files(
        [kit.FIXTURES / "synthetic" / "attachments" / "calib_at_address.bin"], tmp_path / "gone"
    )
    ui.fixed_firmware(window, file_dialogs, "Gone", other)
    ui.entry_context_menu(window, "calib_at_address.bin", "Archive this file")
    kit.wait_log(window, "[archive] calib_at_address.bin copied into the archive")
    assert len(ui.archive_files(root)) == 2
    problems: list[str] = []
    seen: list[str] = []

    def nothing(driver: kit.DialogDriver) -> None:
        seen.append(driver.text())
        if driver.dialog.cancel.isVisible():  # type: ignore[attr-defined]
            problems.append("Cancel shown when there is nothing to clean")
        driver.ok()

    _clean_dialog(window, nothing)
    holds = sum(p.stat().st_size for p in (root / "data" / "archive").glob("*.bin"))
    if ui.plain(tr(f"The archive holds 2 files ({holds:,} bytes).")) not in seen[0]:
        problems.append(f"no archive summary: {seen[0]}")
    if "Gone (1)" not in seen[0] or "Keep (1)" not in seen[0]:
        problems.append(f"the referring firmware are not listed: {seen[0]}")
    if ui.plain(tr("<b>Nothing to clean:</b> every archived file is in use.")) not in seen[0]:
        problems.append(f"no 'nothing to clean' line: {seen[0]}")

    ui.click_row(window, "Gone")
    with kit.expect_dialog(ui.ok, title="Delete firmware"):
        ui.firmware_menu(window, "Delete…")
    kit.wait_log(window, "[firmware] 'Gone' deleted")
    assert len(ui.archive_files(root)) == 2  # deleting firmware keeps archive files

    def cancel(driver: kit.DialogDriver) -> None:
        if not ui.is_danger(driver.dialog.ok):  # type: ignore[attr-defined]
            problems.append("the Delete files button is not red")
        driver.cancel()

    _clean_dialog(window, cancel)
    assert len(ui.archive_files(root)) == 2
    texts: list[str] = []

    def delete(driver: kit.DialogDriver) -> None:
        texts.append(driver.text())
        driver.ok()

    _clean_dialog(window, delete)
    kit.wait_log(window, "[archive] 1 unreferenced files deleted")
    if "4,096" not in texts[0]:
        problems.append(f"the size of the file to delete is not shown: {texts[0]}")
    kept = ui.archive_files(root)
    assert len(kept) == 1
    sha = str(_doc(root, "Keep")["entries"][2]["file"]).removeprefix("archive:")
    assert kept == [f"{sha}.bin"]
    ui.click_row(window, "Keep")
    assert ui.check(window, "Files match").state == "PASS"
    ui.no_problems(problems)
    _ = shutil
