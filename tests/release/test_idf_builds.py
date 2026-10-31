"""Release tests with real ESP-IDF builds of the test project (EMBR_TEST_IDF_PROJECT, built
with idf.py through EMBR_TEST_IDF_EXPORT and, for the version switch, EMBR_TEST_IDF_EXPORT_2)
and the real boards. The project is changed (a marker string in the countdown line, the
target, the ESP-IDF version) and restored after the session by the idf_project fixture.

Design rules checked (design doc §3.4, §4.3, §5 stage two, §8.2 "集成 (需真机)", §12.7):
a rebuild that only changes file contents is followed without confirmation and a click on
Flash flashes the new product; a target switched in sdkconfig but not rebuilt blocks
flashing; after the rebuild the chip change needs confirmation, then the P4 build flashes
the Tab5 and is refused on the S3; an ESP-IDF version change needs confirmation; old and
new products mixed (bootloader or app from the other version) are "build incomplete".
"""

import shutil
import time
from collections.abc import Callable
from pathlib import Path

import builds_kit as bk
import pytest
import release_kit as kit

from embr.core.firmware.tracking import ChangeKind
from embr.core.history import RecordResult

APP = 0x10000


def _import(app: kit.EmbrApp, dialogs: kit.FileDialogs, project: bk.IdfProject) -> str:
    """Import the project's build folder through the dialog; returns the firmware name."""
    name = project.name
    bk.import_idf(app.window, dialogs, project.build)
    assert name in kit.sidebar_names(app.window), kit.log_text(app.window)
    bk.wait_state(app, name, "ok", "updated")
    return name


def _build(project: bk.IdfProject, idf: bk.Idf | None = None) -> None:
    (idf or project.idf).run(project.root, "build")
    time.sleep(bk.SETTLE_S)  # older than 2 s: no longer "being built" (design doc §3.4)


def _marker(text: str) -> str:
    return f"{bk.MARK} {text}"


def test_rebuilt_firmware_is_followed_and_the_new_product_flashed(
    idf_project: bk.IdfProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
) -> None:
    uart = kit.board_port(bk.UART)
    idf_project.reset()
    idf_project.mark("build-one")
    _build(idf_project)
    app = embr()
    window = app.window
    name = _import(app, file_dialogs, idf_project)
    bk.identify(window, uart)
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    assert _marker("build-one") in bk.listen(uart, 4, reset=True)

    idf_project.mark("build-two")  # rebuilt while Embr shows the firmware
    _build(idf_project)
    records = len(bk.records(app))
    result = bk.flash(window)  # a click that finds the build changed is repeated once
    assert kit.same_text(result, "Done"), app.log()
    assert len(bk.records(app)) == records + 1  # a turned-down click writes no record
    status = bk.status_of(app, name)
    assert str(status.state.value) in ("updated", "ok"), status.state
    record = bk.last_record(app)
    written = {e.name: not e.skipped for e in record.entries}
    assert written[f"{name}.bin"], written  # the new app went to the board
    assert not written["bootloader.bin"] and not written["partition-table.bin"], written
    out = bk.listen(uart, 4, reset=True)
    assert _marker("build-two") in out and _marker("build-one") not in out, out[-1500:]


def test_idf_version_switch_needs_confirmation_and_mixed_products_are_incomplete(
    idf_project: bk.IdfProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
) -> None:
    uart = kit.board_port(bk.UART)
    second = bk.idf_second()
    idf_project.reset()
    build = idf_project.build
    app = embr()
    window = app.window
    name = _import(app, file_dialogs, idf_project)
    old = bk.status_of(app, name).idf_version
    keep = tmp_path / "v-old"
    keep.mkdir()
    old_boot = Path(shutil.copy(build / "bootloader" / "bootloader.bin", keep))
    old_app = Path(shutil.copy(build / f"{name}.bin", keep))

    # the same project built from scratch with the other ESP-IDF
    shutil.rmtree(build)
    idf_project.mark("idf-two")
    _build(idf_project, second)
    status = bk.wait_state(app, name, "needs_confirmation", timeout=60)
    new = status.idf_version
    assert new and new != old, (old, new)
    kinds = [c.kind for c in status.changes]
    assert ChangeKind.IDF_VERSION in kinds, [(c.kind, c.message) for c in status.changes]
    assert ChangeKind.RESET not in kinds  # v5 "hard_reset" vs v6 "hard-reset" is no change
    banner = bk.banner_text(window)
    assert old in banner and new in banner, banner
    bk.identify(window, uart)
    kit.click_sidebar(window, name)
    assert not window.connection.flash.isEnabled(), bk.checks(window)
    bk.confirm_banner(window)
    bk.wait_state(app, name, "ok", "updated")
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    out = bk.listen(uart, 5, reset=True)
    assert _marker("idf-two") in out and new in out, out[-2000:]

    # old and new products mixed: the bootloader, then the app, of the other version
    for target, older in (
        (build / "bootloader" / "bootloader.bin", old_boot),
        (build / f"{name}.bin", old_app),
    ):
        current = target.read_bytes()
        shutil.copyfile(older, target)
        time.sleep(bk.SETTLE_S)
        mixed = bk.wait_state(app, name, "incomplete")
        assert any(old in p and new in p for p in mixed.problems), mixed.problems
        assert not window.connection.flash.isEnabled(), bk.checks(window)
        assert bk.has_check(window, "Build incomplete"), bk.checks(window)
        target.write_bytes(current)
        time.sleep(bk.SETTLE_S)
        bk.wait_state(app, name, "ok", "updated")

    # and back to the first ESP-IDF: needs confirmation again
    shutil.rmtree(build)
    idf_project.reset()
    back = bk.wait_state(app, name, "needs_confirmation", timeout=60)
    assert back.idf_version == old
    assert ChangeKind.IDF_VERSION in [c.kind for c in back.changes]
    bk.confirm_banner(window)
    bk.wait_state(app, name, "ok", "updated")


def test_set_target_s3_to_p4_blocks_then_confirms_then_flashes_tab5_only(
    idf_project: bk.IdfProject,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
) -> None:
    uart = kit.board_port(bk.UART)
    tab5 = kit.board_port(bk.P4)
    idf_project.reset()
    root = idf_project.root
    app = embr()
    window = app.window
    name = _import(app, file_dialogs, idf_project)
    assert bk.status_of(app, name).firmware.chip == "esp32s3"

    # sdkconfig switched to the P4 (configured from another build folder): build/ is
    # still the S3 build -> target switched, not flashable
    (root / "sdkconfig.defaults").write_text(
        "CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y\n", encoding="utf-8"
    )
    idf_project.idf.run(root, "-B", str(root / "build_p4_config"), "set-target", "esp32p4")
    time.sleep(bk.SETTLE_S)
    switched = bk.wait_state(app, name, "target_switched")
    assert switched.configured_target == "esp32p4"
    assert "esp32p4" in bk.banner_text(window) and "esp32s3" in bk.banner_text(window)
    bk.identify(window, uart)
    kit.click_sidebar(window, name)
    assert not window.connection.flash.isEnabled()
    assert bk.has_check(window, "Target switched: rebuild"), bk.checks(window)

    # idf.py set-target on build/ itself: the old output is gone, still not flashable
    idf_project.idf.run(root, "set-target", "esp32p4")
    time.sleep(bk.SETTLE_S)
    gone = bk.wait_state(app, name, "missing", "incomplete")
    assert not gone.state.flashable
    assert not window.connection.flash.isEnabled()

    # built for the P4: the chip change is listed first and needs confirmation
    idf_project.mark("p4-build")
    _build(idf_project)
    changed = bk.wait_state(app, name, "needs_confirmation", timeout=60)
    assert changed.changes[0].kind == ChangeKind.CHIP, [c.message for c in changed.changes]
    assert "esp32s3" in changed.changes[0].message and "esp32p4" in changed.changes[0].message
    bk.identify(window, tab5)
    kit.click_sidebar(window, name)
    assert not window.connection.flash.isEnabled()
    bk.confirm_banner(window)
    confirmed = bk.wait_state(app, name, "ok", "updated")
    assert confirmed.firmware.chip == "esp32p4"
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    assert bk.last_record(app).chip_detected == "esp32p4"
    out = bk.listen(tab5, 13)
    assert _marker("p4-build") in out, out[-1500:]

    # the P4 build on the S3: refused, nothing written
    spans = [(0x0, 0x1000), (0x2000, 0x1000), (APP, 0x1000)]
    before = bk.regions(uart, spans, tmp_path / "rb")
    count = len(bk.records(app))
    bk.identify(window, uart)
    kit.click_sidebar(window, name)
    assert bk.sidebar_dimmed(window, name)
    assert not window.connection.flash.isEnabled() and bk.has_check(window, "Chip")
    assert len(bk.records(app)) == count
    assert bk.regions(uart, spans, tmp_path / "rb") == before
    assert all(r.result == RecordResult.OK for r in bk.records(app)[:count])


_ = pytest
