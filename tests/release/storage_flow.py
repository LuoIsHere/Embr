"""Child-process driver for test_storage_sentinel.py: one complete flow in the real window
(new project, ESP-IDF / PlatformIO / file imports, move, archive, merge, identify, flash,
read flash, .embr export and import), with the old database migrated at start.

    python tests/release/storage_flow.py --root R --user U --port COMx --report report.json

R is the portable root (Embr started on it as embr.main.run_gui() does, see
release_kit.start_embr; R/data/embr.sqlite may hold an old database to migrate). U holds
the user's files prepared by the test (U/idf_s3/build, U/pio_s3, U/bins) and receives the
files the user saves (U/out/merged.bin, U/out/dump.bin, U/out/release.embr) through the
file dialog replacement. The report (JSON) gets ok / error / skipped, the log panel text
and the sidebar contents; failure screenshots go next to it. Not a test module itself.
"""

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import pytest
import release_kit as kit

from embr.i18n import Language

PROJECT = "Release flow"
IDF_NAME = "S3 hello"
PIO_ENV = "esp32-s3-devkitc-1"
LOOSE = "Loose bins"


def flow(root: Path, user: Path, port: str, notes: list[str], shots: Path) -> dict[str, Any]:
    dialogs = kit.FileDialogs()
    dialogs.install()
    app = kit.start_embr(root, Language.ENGLISH)
    window = app.window
    try:
        # new project
        def new_project(d: kit.DialogDriver) -> None:
            d.type("Name (unique)", PROJECT)
            d.type("Notes", "storage sentinel flow")
            d.ok()

        with kit.expect_dialog(new_project, title="New project"):
            kit.new_menu(window, "New project")
        kit.wait_log(window, f"[project] created '{PROJECT}'")
        notes.append("project created")

        # ESP-IDF build
        dialogs.queue_directory(user / "idf_s3" / "build")

        def import_idf(d: kit.DialogDriver) -> None:
            d.press("Browse…")
            d.wait_text("esp32s3")
            d.type("Name", IDF_NAME)
            d.pick("Place in", PROJECT)
            d.ok()

        with kit.expect_dialog(import_idf, title="Import ESP-IDF build"):
            kit.new_menu(window, "Import ESP-IDF build…")
        kit.wait_log(window, f"[import] '{IDF_NAME}' follows")
        notes.append("IDF build imported")

        # PlatformIO build (read in the background)
        dialogs.queue_directory(user / "pio_s3" / ".pio" / "build" / PIO_ENV)

        def import_pio(d: kit.DialogDriver) -> None:
            d.press("Browse…")
            kit.wait_until(
                lambda: any(b.isEnabled() and b.isChecked() for b in d.checkboxes()),
                60,
                message=lambda: f"no env to import: {d.texts()}",
            )
            d.pick("Place in", PROJECT)
            d.ok()

        with kit.expect_dialog(import_pio, title="Import PlatformIO build", timeout=90):
            kit.new_menu(window, "Import PlatformIO build…")
        kit.wait_log(window, f"[import] '{PIO_ENV}' follows")
        notes.append("PIO build imported")

        # a fixed firmware from files, standalone
        def new_firmware(d: kit.DialogDriver) -> None:
            d.type("Name", LOOSE)
            d.pick("Place in", "Standalone (no project)")
            d.ok()

        with kit.expect_dialog(new_firmware, title="New firmware"):
            kit.new_menu(window, "New firmware")
        bins = sorted((user / "bins").iterdir(), key=lambda p: p.name)
        dialogs.queue_open_many(bins)
        kit.choose_menu_from(window.firmware_view.entries.add_button, "Files…")
        kit.wait_log(window, "[entry] added")
        notes.append("fixed firmware with files")

        # move it into the project
        def move(d: kit.DialogDriver) -> None:
            d.pick("Place in", PROJECT)
            d.ok()

        with kit.expect_dialog(move, title="Move firmware"):
            kit.firmware_menu(window, "Move to project…")
        kit.wait_log(window, f"[move] '{LOOSE}' → {PROJECT}")
        notes.append("moved")

        # archive the IDF firmware's version
        kit.click_sidebar(window, IDF_NAME)
        with kit.expect_dialog(lambda d: d.ok(), title="Archive this version"):
            kit.firmware_menu(window, "Archive this version…")
        kit.wait_log(window, "[archive] '")
        notes.append("archived")

        # merge the fixed firmware
        kit.click_sidebar(window, LOOSE)
        dialogs.queue_save(user / "out" / "merged.bin")

        def merge(d: kit.DialogDriver) -> None:
            d.press("Browse…")
            kit.wait_until(d.ok_enabled, 10, message=lambda: f"merge not ready: {d.texts()}")
            d.ok()

        (user / "out").mkdir(exist_ok=True)
        with kit.expect_dialog(merge, title="Merge to .bin"):
            kit.click(window.firmware_view.entries.merge_button)
        kit.wait_log(window, "[merge] written to")
        notes.append("merged")

        # identify and flash the IDF firmware on the board
        kit.click_sidebar(window, IDF_NAME)
        identified = kit.identify(window, port)
        notes.append(f"identify: {identified}")
        if not kit.same_text(identified, "Identified"):
            pytest.fail(f"identify ended with {identified!r}:\n{kit.log_text(window)}")
        kit.wait_until(window.connection.flash.isEnabled, 15, message="Flash stays disabled")
        kit.click(window.connection.flash)
        flashed = kit.wait_job_finished(window)
        notes.append(f"flash: {flashed}")
        kit.wait_log(window, "Done. Record saved: result=ok")

        # read 64 KiB of flash, imported as a firmware
        dialogs.queue_save(user / "out" / "dump.bin")

        def read(d: kit.DialogDriver) -> None:
            d.check("whole flash", False, contains=True)
            d.type("Start address (hex)", "0x0")
            d.type("Size (hex)", "0x10000")
            d.press("Browse…")
            d.ok()

        with kit.expect_dialog(read, title="Read flash"):
            kit.click(window.connection.read)
        read_result = kit.wait_job_finished(window)
        notes.append(f"read: {read_result}")
        kit.wait_log(window, "[read] saved to")

        # export the project, import the package
        kit.click_sidebar(window, PROJECT, "project")
        dialogs.queue_save(user / "out" / "release.embr")

        def export(d: kit.DialogDriver) -> None:
            d.press("Browse…")
            d.ok()

        with kit.expect_dialog(export, title="Export project"):
            kit.click_button(window, "Export project…")
        kit.wait_log(window, f"[export] '{PROJECT}' with")
        notes.append("exported")
        dialogs.queue_open(user / "out" / "release.embr")
        with kit.expect_dialog(lambda d: d.ok(), title="Import .embr package"):
            kit.new_menu(window, "Import .embr package…")
        kit.wait_log(window, "[import] release.embr: 1 project(s)")
        notes.append("imported")
        result: dict[str, Any] = {
            "projects": kit.sidebar_names(window, "project"),
            "firmware": kit.sidebar_names(window),
            "log": kit.log_text(window),
        }
    except BaseException:
        kit.screenshot_all(shots, "storage-flow-failure")
        notes.append(kit.log_text(window)[-4000:])
        raise
    finally:
        try:
            app.close()
        finally:
            dialogs.uninstall()
    dialogs.assert_consumed()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("--root", "--user", "--port", "--report"):
        parser.add_argument(name, required=True)
    args = parser.parse_args()
    report = Path(args.report)
    errors = kit.SlotErrors()
    errors.install()
    notes: list[str] = []
    result: dict[str, Any] = {"ok": False, "skipped": False, "notes": notes}
    try:
        result.update(flow(Path(args.root), Path(args.user), args.port, notes, report.parent))
        result["ok"] = not errors.errors
    except BaseException as exc:
        result["skipped"] = isinstance(exc, pytest.skip.Exception)
        result["error"] = traceback.format_exc()
    result["slot_errors"] = [repr(e) for e in errors.errors]
    report.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
