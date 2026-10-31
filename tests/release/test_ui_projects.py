"""Interface flows: projects and groups, in English and Simplified Chinese (design doc
§3.1 names, groups, defaults, deletion; §6.1 project overview; §8.2 core/project).

Each test drives the real window through the New menu, the project overview and its
dialogs, then checks what the window shows and what is on disk (data/projects,
data/firmwares)."""

from collections.abc import Callable
from pathlib import Path

import pytest
import release_kit as kit
import ui_kit as ui
from PySide6.QtWidgets import QAbstractButton, QCheckBox, QComboBox, QLabel
from ui_kit import watchdog  # noqa: F401  # pyright: ignore[reportUnusedImport]

from embr.i18n import Language, tr
from embr.ui.widgets import ElidedLabel

Start = Callable[..., kit.EmbrApp]
pytestmark = pytest.mark.usefixtures("watchdog")


def _project(root: Path, name: str) -> dict[str, object]:
    return ui.doc_named(ui.project_docs(root), name)


def _firmware(root: Path, name: str) -> dict[str, object]:
    return ui.doc_named(ui.firmware_docs(root), name)


@ui.LANGUAGES
def test_project_create_rename_and_unique_names(embr: Start, language: Language) -> None:
    app = embr(language=language)
    window, root = app.window, app.paths.root
    ui.new_project(window, "Alpha", "first notes")
    kit.wait_log(window, "[project] created 'Alpha'")
    assert kit.sidebar_names(window, "project") == ["Alpha"]
    head = ui.header_texts(window)
    for text in ("Project", "Alpha", tr("0 firmware"), tr("0 groups"), "first notes"):
        assert tr(text) in head, head
    assert _project(root, "Alpha")["notes"] == "first notes"

    ui.new_project(window, "Alpha")  # same name: a numeric suffix (design doc §3.1)
    kit.wait_log(window, "[project] created 'Alpha (2)'")
    assert sorted(kit.sidebar_names(window, "project")) == ["Alpha", "Alpha (2)"]

    name = ui.overview_name(window)
    assert name.text() == "Alpha (2)"
    kit.type_text(name, "  Beta  ", finish="return")
    kit.wait_log(window, "[project] renamed 'Alpha (2)' → 'Beta'")
    assert sorted(kit.sidebar_names(window, "project")) == ["Alpha", "Beta"]
    assert ui.overview_name(window).text() == "Beta"

    kit.type_text(ui.overview_name(window), "Alpha", finish="return")  # taken: suffix
    kit.wait_log(window, "[project] renamed 'Beta' → 'Alpha (2)'")
    assert sorted(kit.sidebar_names(window, "project")) == ["Alpha", "Alpha (2)"]

    before = ui.data_snapshot(root)
    for empty in ("", "   "):  # an empty name is not accepted; the old name stays
        kit.type_text(ui.overview_name(window), empty, finish="return")
        kit.pump(100)
        assert ui.overview_name(window).text() == "Alpha (2)"
    assert ui.data_snapshot(root) == before
    assert sorted(kit.sidebar_names(window, "project")) == ["Alpha", "Alpha (2)"]


@ui.LANGUAGES
def test_project_odd_and_overlong_names(embr: Start, language: Language) -> None:
    """Names are free text: shown literally (never as markup), kept as typed; a long name
    is elided in the sidebar and does not widen the window or push controls out."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    odd = '<b>bold</b> & "q" / \\ : * ? | 中文 ✓ <i>'
    problems: list[str] = []
    ui.new_project(window, odd)
    kit.wait_log(window, f"[project] created '{odd}'")
    assert kit.sidebar_names(window, "project") == [odd]
    assert _project(root, odd)["name"] == odd
    row = ui.row_widget(window, kit.sidebar_row(window, odd, "project"))
    names = row.findChildren(ElidedLabel)
    shown = ui.shown_text(names[0])
    if shown != " ".join(names[0].text().split()):
        problems.append(f"sidebar shows {shown!r} for the project name {odd!r} (markup)")
    heading = ui.found(
        lambda: [w for w in kit.find_all(window.header, QLabel) if w.property("heading") == "1"],
        "header name",
    )
    if ui.shown_text(heading[0]) != " ".join(odd.split()):
        problems.append(f"header shows {ui.shown_text(heading[0])!r} for {odd!r} (markup)")

    width = window.width()
    long = "Long-" + "x" * 240 + "-end"
    ui.new_project(window, long)
    kit.wait_log(window, f"[project] created '{long}'")
    assert _project(root, long)["name"] == long
    assert long in kit.sidebar_names(window, "project")
    kit.pump(200)
    if window.width() != width:
        problems.append(f"a long project name widened the window: {width} -> {window.width()}")
    problems += [f"sidebar: {p}" for p in ui.clipped(window.sidebar_panel)]
    problems += [f"header: {p}" for p in ui.clipped(window.header)]
    ui.no_problems(problems)


@ui.LANGUAGES
def test_project_notes_and_defaults(embr: Start, language: Language) -> None:
    """Notes are saved when the field loses the focus; defaults are copied once into new
    firmware (design doc §3.1 项目的默认值)."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    ui.new_project(window, "Defaults")
    kit.wait_log(window, "[project] created 'Defaults'")
    ui.commit_notes(
        ui.notes_edit(window.project_view), "rev B 板 notes", window.sidebar_panel.search
    )
    kit.wait_until(lambda: _project(root, "Defaults")["notes"] == "rev B 板 notes", 5)
    assert "rev B 板 notes" in ui.header_texts(window)

    combos = kit.find_all(window.project_view, QComboBox)
    assert len(combos) == 2, combos  # before, after
    ui.choose(combos[0], "no-reset")
    ui.choose(combos[1], "watchdog-reset")
    kit.set_checked(kit.find_checkbox(window.project_view, "erase all before flashing"), True)
    kit.wait_until(
        lambda: (
            _project(root, "Defaults")["defaults"]
            == {"before": "no-reset", "after": "watchdog-reset", "erase_all": True}
        ),
        5,
        message=lambda: f"defaults on disk: {_project(root, 'Defaults')['defaults']}",
    )

    ui.new_firmware(window, "First")  # placed in the selected project
    kit.wait_log(window, "[new] fixed firmware 'First' created in Defaults")
    first = _firmware(root, "First")
    assert first["reset"] == {"before": "no-reset", "after": "watchdog-reset"}
    assert first["erase_all"] is True
    assert window.connection.erase_all.isChecked()
    ui.open_tab(window, "Parameters")
    assert [c.currentText() for c in ui.param_combos(window, "Reset before")] == ["no-reset"]
    assert [c.currentText() for c in ui.param_combos(window, "Reset after")] == ["watchdog-reset"]

    ui.click_row(window, "Defaults", "project")
    combos = kit.find_all(window.project_view, QComboBox)
    ui.choose(combos[0], "default-reset")
    kit.set_checked(kit.find_checkbox(window.project_view, "erase all before flashing"), False)
    kit.wait_until(lambda: _project(root, "Defaults")["defaults"]["erase_all"] is False, 5)  # type: ignore[index]
    assert _firmware(root, "First")["reset"] == first["reset"]  # existing firmware untouched
    assert _firmware(root, "First")["erase_all"] is True
    ui.new_firmware(window, "Second")
    kit.wait_log(window, "[new] fixed firmware 'Second' created in Defaults")
    second = _firmware(root, "Second")
    assert second["reset"] == {"before": "default-reset", "after": "watchdog-reset"}
    assert second["erase_all"] is False


def _remove_buttons(window: object) -> list[QAbstractButton]:
    found = kit.find_all(window.project_view, QAbstractButton, "Remove")  # type: ignore[attr-defined]
    return sorted(found, key=lambda b: b.mapTo(window, b.rect().topLeft()).y())  # type: ignore[arg-type]


@ui.LANGUAGES
def test_groups_add_rename_remove(embr: Start, language: Language) -> None:
    app = embr(language=language)
    window, root = app.window, app.paths.root
    ui.new_project(window, "Board kit")
    kit.wait_log(window, "[project] created 'Board kit'")
    ui.add_group(window, "Main board")
    kit.wait_log(window, "[group] added 'Main board'")
    group = ui.group_row(window, "Board kit", "Main board")
    assert tr("empty") in kit.all_texts(ui.row_widget(window, group))
    assert tr("1 groups") in ui.header_texts(window)

    ui.add_group(window, "Main board")  # duplicate: refused
    kit.wait_log(window, "[group] 'Main board' already exists")
    ui.add_group(window, "   ")  # empty: nothing happens
    ui.add_group(window, "Sensor")
    kit.wait_log(window, "[group] added 'Sensor'")
    assert _project(root, "Board kit")["devices"] == ["Main board", "Sensor"]

    ui.new_firmware(window, "S3 main", group="Main board")
    kit.wait_log(window, "[new] fixed firmware 'S3 main' created in Board kit")
    assert ui.placement_of(window, "S3 main") == ("Board kit", "Main board")
    assert _firmware(root, "S3 main")["device"] == "Main board"

    ui.click_row(window, "Board kit", "project")
    kit.type_text(ui.group_edit(window, "Main board"), "Main", finish="return")
    kit.wait_log(window, "[group] renamed 'Main board' → 'Main'")
    assert _firmware(root, "S3 main")["device"] == "Main"
    assert ui.placement_of(window, "S3 main") == ("Board kit", "Main")

    kit.type_text(ui.group_edit(window, "Main"), "Sensor", finish="return")  # taken
    kit.wait_log(window, "[group] 'Sensor' already exists")
    kit.pump(100)
    assert sorted(e.text() for e in ui.group_edits(window)) == ["Main", "Sensor"]
    kit.type_text(ui.group_edit(window, "Main"), "", finish="return")  # empty: kept
    kit.pump(100)
    assert sorted(e.text() for e in ui.group_edits(window)) == ["Main", "Sensor"]
    assert _project(root, "Board kit")["devices"] == ["Main", "Sensor"]

    seen: list[str] = []

    def look_and_cancel(driver: kit.DialogDriver) -> None:
        seen.append(driver.text())
        assert ui.is_danger(driver.dialog.ok)  # type: ignore[attr-defined]
        driver.cancel()

    with kit.expect_dialog(look_and_cancel, title="Remove group"):
        kit.click(_remove_buttons(window)[0])
    assert (
        ui.plain(
            tr("1 firmware in it will stay in the project as <b>Ungrouped</b>. Nothing is deleted.")
        )
        in seen[0]
    )
    assert _firmware(root, "S3 main")["device"] == "Main"

    with kit.expect_dialog(ui.ok, title="Remove group"):
        kit.click(_remove_buttons(window)[0])
    kit.wait_log(window, "[group] removed 'Main'; 1 firmware now ungrouped")
    assert _firmware(root, "S3 main")["device"] == ""
    assert _project(root, "Board kit")["devices"] == ["Sensor"]
    assert ui.placement_of(window, "S3 main") == ("Board kit", "")

    empty: list[str] = []

    def look_ok(driver: kit.DialogDriver) -> None:
        empty.append(driver.text())
        driver.ok()

    with kit.expect_dialog(look_ok, title="Remove group"):
        kit.click(_remove_buttons(window)[0])
    assert tr("The group is empty.") in empty[0]
    kit.wait_log(window, "[group] removed 'Sensor'; 0 firmware now ungrouped")
    assert _project(root, "Board kit")["devices"] == []
    assert len(ui.firmware_docs(root)) == 1


@ui.LANGUAGES
def test_delete_project_keeps_or_deletes_its_firmware(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Default: the firmware become standalone; with the check box they are deleted too.
    Archive files stay either way (design doc §3.1 删除)."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    files = kit.hello_files("esp32s3_hello", tmp_path / "bins")
    for project, firmware in (("Keep me", "K1"), ("Drop me", "D1")):
        ui.new_project(window, project)
        kit.wait_log(window, f"[project] created '{project}'")
        ui.new_firmware(window, firmware)
        kit.wait_log(window, f"[new] fixed firmware '{firmware}' created in {project}")
        ui.add_files(window, file_dialogs, files)
        kit.wait_log(window, "[entry] added hello_esp32s3.bin")
        ui.entry_context_menu(window, "hello_esp32s3.bin", "Archive this file")
        kit.wait_log(window, "[archive] hello_esp32s3.bin copied into the archive")
    archived = ui.archive_files(root)
    assert len(archived) == 1  # same content, stored once

    ui.click_row(window, "Keep me", "project")
    texts: list[str] = []

    def keep(driver: kit.DialogDriver) -> None:
        texts.append(driver.text())
        assert ui.is_danger(driver.dialog.ok)  # type: ignore[attr-defined]
        assert not driver.checkbox("Also delete all firmware in this project").isChecked()
        driver.ok()

    with kit.expect_dialog(keep, title="Delete project"):
        kit.click_button(window.project_view, "Delete project…")
    kit.wait_log(window, "[project] 'Keep me' deleted ; firmware kept as standalone")
    assert (
        ui.plain(tr("Delete <b>Keep me</b>? By default its 1 firmware become standalone."))
        in texts[0]
    )
    assert kit.sidebar_names(window, "project") == ["Drop me"]
    assert ui.placement_of(window, "K1") == (None, "")
    assert "project_id" not in _firmware(root, "K1")
    assert [p["name"] for p in ui.project_docs(root)] == ["Drop me"]

    ui.click_row(window, "Drop me", "project")

    def drop(driver: kit.DialogDriver) -> None:
        driver.check("Also delete all firmware in this project")
        driver.ok()

    with kit.expect_dialog(drop, title="Delete project"):
        kit.click_button(window.project_view, "Delete project…")
    kit.wait_log(window, "[project] 'Drop me' deleted with its firmware")
    assert kit.sidebar_names(window, "project") == []
    assert kit.sidebar_names(window) == ["K1"]
    assert [f["name"] for f in ui.firmware_docs(root)] == ["K1"]
    assert ui.archive_files(root) == archived  # archive files are never deleted here
    assert window.isVisible()
    _ = QCheckBox
