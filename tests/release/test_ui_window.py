"""Interface flows of the window itself, in English and Simplified Chinese: search, the
≡ menu (Theme, Language with its restart dialog, Settings, About), the sidebar context
menu, narrow window and sidebar widths, and a sweep of the Chinese interface for English
left untranslated (design doc §6.1, §12 items 10, 15, 18, 22, §12.5).

Window sizes are set with QWidget.resize() (the frameless window's own edge dragging is
not driven); the sidebar width is changed by dragging the splitter handle."""

import configparser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import esptool
import pytest
import release_kit as kit
import ui_kit as ui
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMenu
from ui_kit import pio_env, watchdog  # noqa: F401  # pyright: ignore[reportUnusedImport]

from embr import __version__
from embr.i18n import Language, tr

Start = Callable[..., kit.EmbrApp]
pytestmark = pytest.mark.usefixtures("watchdog")
PLUS = chr(0xFF0B)


def _ini(root: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(delimiters=("=",), interpolation=None)
    parser.read(root / "settings.ini", encoding="utf-8")
    return parser


def _window_menu(window: Any, *path: str) -> None:
    with ui.menu(*path):
        kit.click(window.topbar.menu_button)


@ui.LANGUAGES
def test_search_filters_the_sidebar(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    app = embr(language=language)
    window = app.window
    ui.new_project(window, "Rover")
    ui.new_firmware(window, "alpha s3", "Rover")
    ui.new_firmware(window, "主控 beta", "Rover")
    ui.new_project(window, "Spare")
    ui.new_firmware(window, "gamma", ui.STANDALONE)
    ui.search(window, "ALP")  # case-insensitive
    assert kit.sidebar_names(window) == ["alpha s3"]
    assert "Rover" in kit.sidebar_names(window, "project")
    ui.search(window, "主控")
    assert kit.sidebar_names(window) == ["主控 beta"]
    ui.search(window, "rover")  # a project name keeps all its firmware
    assert sorted(kit.sidebar_names(window)) == ["alpha s3", "主控 beta"]
    ui.search(window, "no such thing")
    assert kit.sidebar_names(window) == []
    ui.search(window, "")
    assert sorted(kit.sidebar_names(window)) == ["alpha s3", "gamma", "主控 beta"]
    assert sorted(kit.sidebar_names(window, "project")) == ["Rover", "Spare"]


@ui.LANGUAGES
def test_theme_menu(embr: Start, language: Language) -> None:
    """Theme: System / Light / Dark, kept in settings.ini [ui] theme (§12 item 18)."""
    app = embr(language=language)
    window, root = app.window, app.paths.root

    def lightness() -> int:
        return QApplication.palette().color(QPalette.ColorRole.Window).lightness()

    _window_menu(window, "Theme", "Dark")
    assert lightness() < 100
    _window_menu(window, "Theme", "Light")
    assert lightness() > 150
    seen: list[ui.MenuSeen] = []
    with ui.menu(seen=seen):
        kit.click(window.topbar.menu_button)
    items = [
        ui.plain(tr(t)) for t in ("Settings…", "Clean archive…", "Theme", "Language", "About Embr")
    ]
    assert seen[0].items == items, seen[0].items
    _window_menu(window, "Theme", "System")
    app.close()
    assert _ini(root).get("ui", "theme") == "system"


@ui.LANGUAGES
def test_language_menu_and_restart_dialog(embr: Start, language: Language) -> None:
    """§12.5: the choice is saved; the dialog says it applies after a restart, buttons
    Restart and OK; OK keeps Embr open, Restart closes it for a restart."""
    root_language = language
    app = embr(language=root_language)
    window, root = app.window, app.paths.root
    other = "简体中文" if root_language == Language.ENGLISH else "English"
    other_value = Language.ZH_CN if root_language == Language.ENGLISH else Language.ENGLISH
    seen: list[list[str]] = []

    def keep_open(driver: kit.DialogDriver) -> None:
        seen.append(ui.dialog_texts(driver))
        assert kit.same_text(kit.text_of(driver.dialog.ok), "Restart")  # type: ignore[attr-defined]
        assert kit.same_text(kit.text_of(driver.dialog.cancel), "OK")  # type: ignore[attr-defined]
        driver.cancel()

    with kit.expect_dialog(keep_open, title="Language"):
        _window_menu(window, "Language", other)
    assert ui.plain(tr("The new language is used after Embr restarts.")) in seen[0]
    assert window.isVisible() and not window.restart_requested
    assert _ini(root).get("ui", "language") == other_value.value
    current = "English" if root_language == Language.ENGLISH else "简体中文"
    with ui.no_dialog(1.0):  # back to the language shown: nothing to restart for
        _window_menu(window, "Language", current)
    assert _ini(root).get("ui", "language") == root_language.value
    with kit.expect_dialog(ui.ok, title="Language"):
        _window_menu(window, "Language", other)
    kit.wait_until(lambda: not window.isVisible(), 10, message="Restart did not close Embr")
    assert window.restart_requested
    app.close()
    assert _ini(root).get("ui", "language") == other_value.value


@ui.LANGUAGES
def test_settings_dialog(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """Settings…: the PlatformIO framework package path (saved on Close, Browse… picks a
    folder) and the program folder, read-only (§6.1, §7.6)."""
    app = embr(language=language)
    window, root = app.window, app.paths.root
    package = tmp_path / "framework-arduinoespressif32"
    package.mkdir()
    file_dialogs.queue_directory(package)

    def fill(driver: kit.DialogDriver) -> None:
        text = driver.text()
        assert str(root) in text, text
        assert "data\\ · logs\\ · temp\\ · settings.ini" in text
        assert not driver.dialog.cancel.isVisible()  # type: ignore[attr-defined]
        driver.press("Browse…")
        driver.ok()

    with kit.expect_dialog(fill, title="Settings"):
        _window_menu(window, "Settings…")
    assert _ini(root).get("pio", "framework_pkg") == str(package)

    def clear(driver: kit.DialogDriver) -> None:
        edits = [e for e in kit.find_all(driver.dialog, ui.QLineEdit)]
        assert edits[0].text() == str(package)
        kit.type_text(edits[0], "")
        driver.ok()

    with kit.expect_dialog(clear, title="Settings"):
        _window_menu(window, "Settings…")
    assert not (
        _ini(root).has_option("pio", "framework_pkg") and _ini(root).get("pio", "framework_pkg")
    )


@ui.LANGUAGES
def test_about_dialog(embr: Start, language: Language) -> None:
    """About Embr: version, built-in esptool, repository and releases links, the font
    notice; copyright and build time (§12 item 22: "development build" from source)."""
    app = embr(language=language)
    window = app.window
    seen: list[str] = []
    with kit.expect_dialog(ui.record_and_ok(seen, joined=True), title="About Embr"):
        _window_menu(window, "About Embr")
    text = seen[0]
    problems: list[str] = []
    for part in (
        __version__,
        esptool.__version__,
        "github.com/LuoIsHere/Embr",
        "github.com/LuoIsHere/Embr/releases",
        "HarmonyOS Sans",
    ):
        if part not in text:
            problems.append(f"About lacks {part!r}")
    if "Copyright © 2026 @LuoIsHere" not in text:
        problems.append(
            "About lacks the copyright line 'Copyright © 2026 @LuoIsHere' (§12 item 22)"
        )
    if "development build" not in text and tr("development build") not in text:
        problems.append("About lacks the build time ('development build' from source, §12 item 22)")
    if ui.in_chinese():
        known = [
            __version__,
            esptool.__version__,
            "github.com/LuoIsHere/Embr",
            "HarmonyOS Sans",
            "Copyright 2021 Huawei Device Co., Ltd.",
            "HarmonyOS Sans Fonts License Agreement",
            "GPL-3.0-or-later",
            "THIRD_PARTY_NOTICES",
        ]
        problems += ui.english_leftovers(text.split(" | "), known)
    ui.no_problems(problems)


@ui.LANGUAGES
def test_sidebar_context_menu_is_the_dots_menu(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """§12.5: right-clicking a firmware selects it and opens the same menu as ⋯; Delete… is
    drawn in the warning colour in both."""
    app = embr(language=language)
    window = app.window
    ui.fixed_firmware(
        window, file_dialogs, "One", kit.hello_files("esp32s3_hello", tmp_path / "one")
    )
    ui.new_firmware(window, "Two", ui.STANDALONE)
    ui.click_row(window, "Two")
    context: list[ui.MenuSeen] = []
    ui.context_menu_on_firmware(window, "One", seen=context)
    assert "One" in ui.header_texts(window)  # the right-clicked firmware is selected
    dots: list[ui.MenuSeen] = []
    ui.firmware_menu(window, seen=dots)
    assert context[0].items == dots[0].items
    expected = [
        ui.plain(tr(t))
        for t in (
            "Rename…\tF2",
            "Duplicate",
            "Move to project…",
            "Archive this version…",
            "Export…",
            "Delete…",
        )
    ]
    assert dots[0].items == expected, dots[0].items
    assert context[0].danger == dots[0].danger == [ui.plain(tr("Delete…"))]
    menu = window.header.menu
    danger = [
        a.defaultWidget()
        for a in menu.actions()
        if hasattr(a, "defaultWidget") and a.defaultWidget() is not None
    ]
    assert isinstance(danger[0], QLabel)
    danger[0].ensurePolished()
    red = danger[0].palette().color(QPalette.ColorRole.WindowText)
    normal = menu.palette().color(QPalette.ColorRole.WindowText)
    assert red != normal and red.red() > red.green(), (red.name(), normal.name())
    _ = QMenu


def _drag_handle(window: Any, index: int, dx: int) -> None:
    """Drag the column splitter's handle `index` by dx pixels with the mouse."""
    handle = window.columns.handle(index)
    start = handle.rect().center()
    QTest.mousePress(handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    for step in range(1, 11):
        QTest.mouseMove(handle, start + QPoint(dx * step // 10, 0))
        kit.pump(10)
    QTest.mouseRelease(
        handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start + QPoint(dx, 0)
    )
    kit.pump(100)


def _layout_problems(window: Any) -> list[str]:
    problems = (
        [f"sidebar: {p}" for p in ui.clipped(window.sidebar_panel)]
        if window.sidebar_panel.isVisible()
        else []
    )
    problems += [f"header: {p}" for p in ui.clipped(window.header)]
    problems += [f"workspace: {p}" for p in ui.clipped(window.firmware_view)]
    problems += [f"right column: {p}" for p in ui.clipped(window.connection)]
    problems += [f"log: {p}" for p in ui.clipped(window.log_panel)]
    problems += [f"top bar: {p}" for p in ui.clipped(window.topbar)]
    table = window.firmware_view.entries.table
    header = table.horizontalHeader()
    used = sum(
        header.sectionSize(c) for c in range(table.columnCount()) if not header.isSectionHidden(c)
    )
    if used > table.viewport().width() + 1:
        problems.append(f"entries table wider than its view: {used} > {table.viewport().width()}")
    if header.sectionSize(7) < 120:
        problems.append(f"Note column {header.sectionSize(7)} px (< 120)")
    return problems


@ui.LANGUAGES
def test_narrow_window_and_sidebar(
    embr: Start, file_dialogs: kit.FileDialogs, language: Language, tmp_path: Path
) -> None:
    """At the minimum window size (1100 x 700) the sidebar folds away by itself (below
    1180 px), Ctrl+B brings it back; with the sidebar dragged to its narrowest the New
    button reads "+ New"; nothing is cut off in any column, the entries table does not
    scroll sideways and Note keeps 120 px (§6.1, §12 item 7, §12.5)."""
    app = embr(language=language)
    window = app.window
    ui.new_project(window, "A project with a fairly long name for the sidebar")
    ui.add_group(window, "Main board with a long group name")
    files = kit.hello_files("esp32s3_hello", tmp_path / "bins")
    ui.fixed_firmware(
        window,
        file_dialogs,
        "S3-main-v1.2 release candidate firmware",
        files,
        "A project with a fairly long name for the sidebar",
        "Main board with a long group name",
    )
    ui.set_note(window, "hello_esp32s3.bin", "main application")
    problems: list[str] = []
    window.resize(1600, 900)
    kit.pump(200)
    problems += [f"1600 px: {p}" for p in _layout_problems(window)]
    window.resize(1100, 700)
    kit.pump(300)
    if window.sidebar_panel.isVisible():
        problems.append("the sidebar does not fold away at 1100 px")
    problems += [f"1100 px, no sidebar: {p}" for p in _layout_problems(window)]
    QTest.keyClick(window, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    kit.pump(200)
    if not window.sidebar_panel.isVisible():
        problems.append("Ctrl+B does not show the sidebar")
    _drag_handle(window, 1, -400)  # the sidebar to its narrowest
    kit.pump(200)
    new = window.sidebar_panel.new_button
    full = tr(f"{PLUS} New   firmware · project")
    if new.fontMetrics().horizontalAdvance(full) > new.width() - 26 and new.text() != tr(
        f"{PLUS} New"
    ):
        problems.append(
            f"narrow sidebar: New button reads {new.text()!r} (sidebar {window.sidebar_panel.width()} px)"  # noqa: E501
        )
    problems += [
        f"1100 px, narrow sidebar ({window.sidebar_panel.width()} px): {p}"
        for p in _layout_problems(window)
    ]
    _drag_handle(window, 2, 400)  # the right column to its narrowest
    problems += [
        f"narrow right column ({window.connection.width()} px): {p}"
        for p in _layout_problems(window)
    ]
    ui.no_problems(problems)


def test_chinese_interface_has_no_untranslated_english(
    embr: Start,
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
    pio_env: Path,  # noqa: F811
) -> None:
    """§12.5 翻译范围: every message of the interface is translated (menus, buttons,
    dialogs, checks, banners, errors); kept in English only what the glossary keeps
    (internal_docs/m7_glossary.md), the test's own data, file names, paths and commands."""
    project_dir = tmp_path / "proj"
    build = kit.idf_build_copy("esp32s3_hello", project_dir)
    app = embr(language=Language.ZH_CN)
    window = app.window
    data = [
        "工程",
        "分组",
        "固件",
        "散件",
        "hello_esp32s3",
        "bins",
        "proj",
        "(archived ",
    ]
    found: list[str] = []

    def sweep(where: str) -> None:
        found.extend(
            f"{where}: {line}" for line in ui.english_leftovers(ui.window_texts(window), data)
        )

    def dialog_sweep(where: str) -> Callable[[kit.DialogDriver], None]:
        def handler(driver: kit.DialogDriver) -> None:
            kit.pump(100)
            found.extend(
                f"{where}: {line}" for line in ui.english_leftovers(ui.dialog_texts(driver), data)
            )
            driver.cancel()

        return handler

    def menu_sweep(where: str, opener: Callable[[], None]) -> None:
        seen: list[ui.MenuSeen] = []
        with ui.menu(seen=seen):
            opener()
        found.extend(f"{where}: {line}" for line in ui.english_leftovers(seen[0].items, data))

    sweep("empty window")
    menu_sweep("≡ menu", lambda: kit.click(window.topbar.menu_button))
    menu_sweep("New menu", lambda: kit.click(window.sidebar_panel.new_button))
    ui.new_project(window, "工程")
    ui.add_group(window, "分组")
    sweep("project overview")
    with kit.expect_dialog(dialog_sweep("New firmware dialog"), title="New firmware"):
        kit.new_menu(window, "New firmware")
    ui.fixed_firmware(
        window,
        file_dialogs,
        "固件",
        kit.hello_files("esp32s3_hello", tmp_path / "bins"),
        "工程",
        "分组",
    )
    for tab in ("Entries", "Parameters", "Records"):
        ui.open_tab(window, tab)
        sweep(f"fixed firmware, {tab}")
    ui.open_tab(window, "Entries")
    menu_sweep("⋯ menu", lambda: kit.click(window.header.menu_button))
    menu_sweep("Add entry menu", lambda: kit.click(window.firmware_view.entries.add_button))
    table = window.firmware_view.entries.table
    menu_sweep(
        "entry menu",
        lambda: kit.context_menu(
            table.viewport(), table.visualRect(table.model().index(0, 4)).center()
        ),
    )
    for title, opener in (
        ("Rename firmware", lambda: ui.firmware_menu(window, "Rename…\tF2")),
        ("Move firmware", lambda: ui.firmware_menu(window, "Move to project…")),
        ("Delete firmware", lambda: ui.firmware_menu(window, "Delete…")),
        ("Export firmware", lambda: ui.firmware_menu(window, "Export…")),
        ("Merge to .bin", lambda: kit.click(window.firmware_view.entries.merge_button)),
        ("Settings", lambda: _window_menu(window, "Settings…")),
        ("Clean archive", lambda: _window_menu(window, "Clean archive…")),
        ("Import ESP-IDF build", lambda: kit.new_menu(window, "Import ESP-IDF build…")),
    ):
        with ui.collect(found, title), kit.expect_dialog(dialog_sweep(title), title=title):
            opener()
    ui.import_idf(window, file_dialogs, build, place="工程", group="分组")
    for tab in ("Entries", "Parameters"):
        ui.open_tab(window, tab)
        sweep(f"IDF firmware, {tab}")
    ui.open_tab(window, "Entries")
    ui.click_row(window, "固件")
    ui.flip_byte(build / "hello_esp32s3.bin", (build / "hello_esp32s3.bin").stat().st_size - 8)
    ui.make_old(build / "hello_esp32s3.bin", 1800)
    ui.click_row(window, "hello_esp32s3")
    sweep("IDF firmware, build updated")
    data_file = build / "flasher_args.json"
    data_file.write_text(
        data_file.read_text(encoding="utf-8").replace(
            '"0x10000" : "hello_esp32s3.bin"', '"0x20000" : "hello_esp32s3.bin"'
        ),
        encoding="utf-8",
    )
    ui.make_old(data_file, 1800)
    ui.reselect(window, "hello_esp32s3", "固件")
    sweep("IDF firmware, needs confirmation")
    ui.new_firmware(window, "散件", "工程", ui.UNGROUPED)
    sweep("ungrouped firmware")
    build.rename(project_dir / "gone")
    ui.reselect(window, "hello_esp32s3", "固件")
    sweep("IDF firmware, build missing")
    with kit.expect_dialog(ui.ok, title="Archive this version"):
        ui.click_row(window, "固件")
        ui.firmware_menu(window, "Archive this version…")
    sweep("archived firmware")
    ui.click_row(window, "工程", "project")
    with kit.expect_dialog(dialog_sweep("Delete project"), title="Delete project"):
        kit.click_button(window.project_view, "Delete project…")
    with kit.expect_dialog(dialog_sweep("Export project"), title="Export project"):
        kit.click_button(window.project_view, "Export project…")
    ui.no_problems(found)
