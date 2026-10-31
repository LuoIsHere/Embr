"""Interface flows: .embr export and import, in English and Simplified Chinese (design doc
§5 stage four, §12.6 entries and default placement; §12.7 damaged packages and packages
written by a newer Embr).

Packages are made through the window's own Export dialogs; damaged copies are then
written by the test (a changed file, a cut-off zip, a newer format or schema)."""

import re
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import release_kit as kit
import ui_kit as ui
from PySide6.QtWidgets import QComboBox
from ui_kit import watchdog  # noqa: F401  # pyright: ignore[reportUnusedImport]

from embr.i18n import Language, tr

Start = Callable[..., kit.EmbrApp]
pytestmark = pytest.mark.usefixtures("watchdog")
APP = "hello_esp32s3.bin"


def _docs(root: Path) -> list[dict[str, Any]]:
    return ui.firmware_docs(root)


def _mark_dump(root: Path, firmware_id: str, entry: str) -> None:
    """Seed: mark an entry as read from a device (from_dump), as read flash makes it;
    no board is used in this batch. Written while Embr is closed."""
    path = root / "data" / "firmwares" / f"{firmware_id}.toml"
    text = path.read_text(encoding="utf-8")
    marker = f'name = "{entry}"\n'
    assert marker in text, text
    path.write_text(text.replace(marker, marker + "from_dump = true\n", 1), encoding="utf-8")


def _export_firmware(
    window: Any,
    dialogs: kit.FileDialogs,
    output: Path,
    handler: Callable[[kit.DialogDriver], None] | None = None,
) -> None:
    def fill(driver: kit.DialogDriver) -> None:
        if handler is not None:
            handler(driver)
        dialogs.queue_save(output)
        driver.press("Browse…")
        driver.ok()

    with kit.expect_dialog(fill, title="Export firmware"):
        ui.firmware_menu(window, "Export…")


def _import(
    window: Any,
    dialogs: kit.FileDialogs,
    package: Path,
    handler: Callable[[kit.DialogDriver], None],
    title: str = "Import .embr package",
) -> None:
    dialogs.queue_open(package)
    with kit.expect_dialog(handler, title=title):
        kit.new_menu(window, "Import .embr package…")


@ui.LANGUAGES
def test_export_dialog_and_import_loose_firmware(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Export… lists the firmware of the same scope, the chosen one checked; one that
    cannot be exported is greyed with the reason; a dump warns about secrets. Loose
    firmware are imported into the chosen project and group as archived firmware."""
    root = tmp_path / "embr"
    app = embr(root, language)
    window = app.window
    ui.new_project(window, "Src")
    ui.add_group(window, "G")
    for name in ("A", "B", "C"):
        files = kit.hello_files("esp32s3_hello", tmp_path / name)
        ui.fixed_firmware(window, file_dialogs, name, files, "Src", "G")
    dump_id = ui.firmware_id(window, "C")
    (tmp_path / "B" / APP).unlink()  # B: a file is missing, it cannot be exported
    app.close()
    _mark_dump(root, dump_id, APP)
    app = embr(root, language)
    window = app.window
    ui.click_row(window, "A")
    problems: list[str] = []
    out = tmp_path / "out"
    out.mkdir()

    def look(driver: kit.DialogDriver) -> None:
        boxes = {kit.text_of(b).split(" — ")[0]: b for b in driver.checkboxes()}
        a, b, c = boxes["A"], boxes["B"], boxes["C"]
        if not (a.isChecked() and a.isEnabled()):
            problems.append("the chosen firmware is not checked")
        if b.isEnabled() or b.isChecked():
            problems.append("a firmware with a missing file can be exported")
        reason = kit.text_of(b).partition(" — ")[2]
        if reason != tr("the build output or a file is missing"):
            problems.append(f"reason shown for B: {reason!r}")
        if c.isChecked() or not c.isEnabled():
            problems.append("C should be offered unchecked")
        kit.set_checked(c, True)
        kit.pump(50)
        warning = " ".join(driver.texts())
        if (
            ui.plain(
                tr(
                    "<b>C</b>: data read from a device. The package may hold Wi-Fi credentials and other secrets stored in NVS."  # noqa: E501
                )
            )
            not in warning
        ):
            problems.append(f"no dump warning after checking C: {warning}")
        kit.set_checked(c, False)
        if driver.line_edit("Save as").text() != str(Path.home() / "A.embr"):
            problems.append(f"default package path {driver.line_edit('Save as').text()!r}")

    package = out / "a.embr"
    _export_firmware(window, file_dialogs, package, look)
    kit.wait_log(window, f"[export] 1 firmware written to {package}")
    with zipfile.ZipFile(package) as zf:
        names = zf.namelist()
        assert "manifest.toml" in names and len([n for n in names if n.startswith("files/")]) == 3
        assert not any(
            str(tmp_path) in zf.read(n).decode("utf-8", "replace")
            for n in names
            if n.endswith(".toml")
        )

    ui.new_project(window, "Dest")
    ui.add_group(window, "Box")

    def place(driver: kit.DialogDriver) -> None:
        text = driver.text()
        if "a.embr" not in text or "A (G)" not in text:
            problems.append(f"import dialog does not describe the package: {text}")
        if driver.combo("Place in").currentText() != "Dest":
            problems.append(f"default project {driver.combo('Place in').currentText()!r}")
        if driver.combo("Group").currentText() != tr("(ungrouped)"):
            problems.append(f"default group {driver.combo('Group').currentText()!r}")
        ui.pick(driver, "Group", "Box")
        driver.ok()

    _import(window, file_dialogs, package, place)
    kit.wait_log(window, "[import] a.embr: 0 project(s), 1 firmware")
    dest = [
        n
        for _, n, s in kit.sidebar_rows(window)
        if n and n.kind == "firmware" and s == "A" and n.device == "Box"
    ]
    assert len(dest) == 1
    doc = next(d for d in _docs(root) if d["name"] == "A" and d.get("device") == "Box")
    assert doc["source"] == "archived" and "a.embr" in str(doc.get("archived_from"))
    assert sorted(e["name"] for e in doc["entries"]) == sorted(ui.S3_FILES)

    _import(window, file_dialogs, package, ui.ok)  # same name in the same project: suffix
    kit.wait_log(window, "[import] a.embr: 0 project(s), 1 firmware")
    assert "A (2)" in kit.sidebar_names(window)
    ui.no_problems(problems)


@ui.LANGUAGES
def test_export_project_refused_then_imported_whole(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Export project… is refused as a whole, listing why, while one firmware cannot be
    exported (§12.6); once fixed, the project with its groups and firmware comes back as
    a new project (name suffixed)."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    ui.new_project(window, "Whole")
    ui.add_group(window, "G1")
    ui.add_group(window, "G2")
    ui.fixed_firmware(
        window, file_dialogs, "X", kit.hello_files("esp32s3_hello", tmp_path / "x"), "Whole", "G1"
    )
    ui.fixed_firmware(
        window, file_dialogs, "Y", kit.hello_files("esp32s3_hello", tmp_path / "y"), "Whole", "G2"
    )
    (tmp_path / "y" / "bootloader.bin").unlink()
    ui.click_row(window, "Whole", "project")
    out = tmp_path / "out"
    out.mkdir()
    refused: list[str] = []
    with kit.expect_dialog(ui.record_and_ok(refused), title="Export project"):
        kit.click_button(window.project_view, "Export project…")
    assert (
        "Y" in refused[0]
        and ui.plain(tr("Fix these first, or use Export… in a firmware menu to export only some."))
        in refused[0]
    )
    assert list(out.iterdir()) == []

    ui.click_row(window, "Y")
    with kit.expect_dialog(ui.ok, title="Delete firmware"):
        ui.firmware_menu(window, "Delete…")
    ui.click_row(window, "Whole", "project")
    package = out / "whole.embr"

    def export(driver: kit.DialogDriver) -> None:
        file_dialogs.queue_save(package)
        driver.press("Browse…")
        driver.ok()

    with kit.expect_dialog(export, title="Export project"):
        kit.click_button(window.project_view, "Export project…")
    kit.wait_log(window, f"[export] 'Whole' with 1 firmware written to {package}")

    seen: list[str] = []

    def whole(driver: kit.DialogDriver) -> None:
        seen.append(driver.text())
        assert not kit.find_all(driver.dialog, QComboBox)  # no placement: a whole project
        driver.ok()

    _import(window, file_dialogs, package, whole)
    kit.wait_log(window, "[import] whole.embr: 1 project(s), 1 firmware")
    assert ui.plain(tr("Project <b>Whole</b> with 1 firmware")) in seen[0]
    assert sorted(kit.sidebar_names(window, "project")) == ["Whole", "Whole (2)"]
    project = ui.doc_named(ui.project_docs(root), "Whole (2)")
    assert project["devices"] == ["G1", "G2"]
    copy = [d for d in _docs(root) if d.get("project_id") == project["id"]]
    assert [(d["name"], d["device"], d["source"]) for d in copy] == [("X", "G1", "archived")]


@ui.LANGUAGES
def test_drop_embr_package_default_placement(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§12.6: a package dropped on a group defaults to that group, on a project to it
    (ungrouped), on the standalone area to standalone, on a firmware to its project and
    group; a package dropped with other files is refused."""
    app = embr(language=language)
    window = app.window
    ui.fixed_firmware(window, file_dialogs, "L", kit.hello_files("esp32s3_hello", tmp_path / "l"))
    package = tmp_path / "l.embr"
    _export_firmware(window, file_dialogs, package)
    kit.wait_log(window, "[export] 1 firmware written to")
    ui.new_project(window, "T")
    ui.add_group(window, "TG")
    ui.new_firmware(window, "F", "T", "TG")
    problems: list[str] = []

    def expect(place: str, group: str, accept: bool = False) -> Callable[[kit.DialogDriver], None]:
        def handler(driver: kit.DialogDriver) -> None:
            shown = (driver.combo("Place in").currentText(), driver.combo("Group").currentText())
            if shown != (tr(place), tr(group)):
                problems.append(f"default {shown}, expected {(tr(place), tr(group))}")
            driver.ok() if accept else driver.cancel()

        return handler

    targets = [
        ("project row", lambda: kit.sidebar_row(window, "T", "project"), "T", ui.UNGROUPED),
        ("standalone", lambda: ui.standalone_header(window), ui.STANDALONE, ui.UNGROUPED),
        ("firmware F", lambda: kit.sidebar_row(window, "F"), "T", "TG"),
    ]
    for what, row, place, group in targets:
        with (
            ui.collect(problems, what),
            kit.expect_dialog(expect(place, group), title="Import .embr package"),
        ):
            ui.drop_files_on_row(window, row(), [package])
    with kit.expect_dialog(expect("T", "TG", accept=True), title="Import .embr package"):
        ui.drop_files_on_row(window, ui.group_row(window, "T", "TG"), [package])
    kit.wait_log(window, "[import] l.embr: 0 project(s), 1 firmware")
    t_id = ui.project_id(window, "T")
    placed = [(n.project_id, n.device) for _, n, s in kit.sidebar_rows(window) if n and s == "L"]
    assert (t_id, "TG") in placed, placed
    other = tmp_path / "l" / APP
    reasons: list[str] = []
    with kit.expect_dialog(ui.record_and_ok(reasons), title="Nothing added"):
        ui.drop_files_on_row(window, ui.group_row(window, "T", "TG"), [package, other])
    assert ui.plain(tr("Drop a .embr package on its own.")) in reasons[0]
    ui.no_problems(problems)


def _rewrite(source: Path, target: Path, change: Callable[[str, bytes], bytes | None]) -> None:
    """Copy a zip, passing each member through change(name, data) (None drops it)."""
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = change(info.filename, zin.read(info.filename))
            if data is not None:
                zout.writestr(info.filename, data)


def _damaged(good: Path, folder: Path) -> dict[str, Path]:
    folder.mkdir()
    made: dict[str, Path] = {}

    def tamper(name: str, data: bytes) -> bytes:
        if name.startswith("files/") and not made.get("_tampered"):
            made["_tampered"] = good
            return data[:-1] + bytes([data[-1] ^ 0xFF])
        return data

    made["tampered"] = folder / "tampered.embr"
    _rewrite(good, made["tampered"], tamper)
    del made["_tampered"]
    made["truncated"] = folder / "truncated.embr"
    made["truncated"].write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    made["missing file"] = folder / "missing.embr"
    dropped: list[str] = []

    def drop(name: str, data: bytes) -> bytes | None:
        if name.startswith("files/") and not dropped:
            dropped.append(name)
            return None
        return data

    _rewrite(good, made["missing file"], drop)
    made["newer format"] = folder / "newer-format.embr"
    _rewrite(
        good,
        made["newer format"],
        lambda n, d: (
            re.sub(rb"format_version = \d+", b"format_version = 99", d)
            if n == "manifest.toml"
            else d
        ),
    )
    made["newer schema"] = folder / "newer-schema.embr"
    _rewrite(
        good,
        made["newer schema"],
        lambda n, d: (
            re.sub(rb"schema_version = \d+", b"schema_version = 99", d)
            if n.startswith("firmwares/")
            else d
        ),
    )
    made["not a zip"] = folder / "notes.embr"
    shutil.copy(kit.FIXTURES / "synthetic" / "negative" / "notes.txt", made["not a zip"])
    return made


@ui.LANGUAGES
def test_damaged_and_newer_packages_are_refused(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§5 stage four 导入: checked in full first; a changed, missing or cut-off file, a
    manifest that is not readable, a newer package format or schema → refused with the
    reason, nothing created (projects, firmware, archive unchanged)."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    ui.fixed_firmware(
        window, file_dialogs, "Good", kit.hello_files("esp32s3_hello", tmp_path / "g")
    )
    good = tmp_path / "good.embr"
    _export_firmware(window, file_dialogs, good)
    kit.wait_log(window, "[export] 1 firmware written to")
    problems: list[str] = []
    for case, package in _damaged(good, tmp_path / "bad").items():
        before = ui.data_snapshot(root)
        seen: list[str] = []

        def refused(driver: kit.DialogDriver, seen: list[str] = seen) -> None:
            seen.append(driver.text())
            driver.ok()

        with ui.collect(problems, case):
            _import(window, file_dialogs, package, refused, title="Not imported")
        if seen and ui.plain(tr(f"<b>{package.name}</b> cannot be imported.")) not in seen[0]:
            problems.append(f"{case}: {seen[0]!r}")
        if (
            case.startswith("newer")
            and seen
            and "newer" not in seen[0]
            and tr("update Embr") not in seen[0]
            and not any(w in seen[0] for w in ("更新", "新版本"))
        ):
            problems.append(f"{case}: the reason does not say a newer Embr is needed: {seen[0]!r}")
        if ui.data_snapshot(root) != before:
            problems.append(
                f"{case}: data changed: {kit.new_entries(before, ui.data_snapshot(root))}"
            )
    assert kit.sidebar_names(window) == ["Good"]
    ui.no_problems(problems)
