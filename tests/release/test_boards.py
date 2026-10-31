"""Release tests on the real boards with the fixture builds (no compiler needed): the real
window driven through its controls, the bundled esptool in real worker processes, and an
independent read-back with esptool's command line.

Boards (design doc §8.1): EMBR_TEST_PORT_UART (ESP32-S3 through a CH343 bridge),
EMBR_TEST_PORT_USB (the same S3's USB-Serial/JTAG), EMBR_TEST_PORT_P4 (M5Stack Tab5,
ESP32-P4 v1.0). Every test leaves a fixture hello_world on the boards it used.

Design rules checked: §4.1 (one connection per job, the port released afterwards,
identification snapshot cleared on a port switch), §4.3 (chip and revision refusals with
nothing written), §4.4 (baud not applicable on native USB, reset policy per connection,
baud remembered per VID/PID, manual reset hint), §5 stage one (attachments by partition),
§8.2 "集成 (需真机)", §12.7 (multi-device switching, damaged firmware, operator steps).
"""

import random
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import builds_kit as bk
import pytest
import release_kit as kit

from embr.core.history import Operation, RecordResult
from embr.i18n import Language

S3_HELLO = "esp32s3_hello"
P4_REV1 = "esp32p4_rev1_hello"
P4_REV3 = "esp32p4_rev3_hello"
HELLO = "Hello world!"
APP = 0x10000


def _sample_for(var: str) -> str:
    return P4_REV1 if var == bk.P4 else S3_HELLO


def _pairs(sample: str) -> list[tuple[int, Path]]:
    return bk.fixture_pairs(sample)


def _hello(
    window: Any,
    dialogs: kit.FileDialogs,
    tmp_path: Path,
    sample: str,
    name: str,
    *,
    place: str = "Standalone (no project)",
) -> list[Path]:
    """A fixed firmware `name` with copies of the sample's bootloader, table and app."""
    files = kit.hello_files(sample, tmp_path / name)
    bk.new_fixed_firmware(window, dialogs, name, files, place=place)
    return files


def _same_on_board(device: str, pairs: Sequence[tuple[int, Path]], folder: Path) -> list[str]:
    """Regions whose content differs from the files (empty: all equal)."""
    wrong: list[str] = []
    for address, path in pairs:
        data = path.read_bytes()
        if bk.read_back(device, address, len(data), folder) != data:
            wrong.append(f"0x{address:X} {path.name}")
    return wrong


def _restore_hello(device: str, sample: str) -> None:
    bk.write_back(device, _pairs(sample))


# ---- §8.2: the port is released after identify and flash --------------------------------


@pytest.mark.parametrize("var", [bk.UART, bk.USB, bk.P4])
def test_port_released_after_identify_and_flash(
    var: str, embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    device = kit.board_port(var)
    app = embr()
    window = app.window
    sample = _sample_for(var)
    _hello(window, file_dialogs, tmp_path, sample, "hello")
    grace = 10.0 if bk.native(device) else 0.0
    bk.identify(window, device)
    bk.open_port_now(device, grace)  # Embr keeps running, another program opens the port
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    record = bk.last_record(app)
    assert record.result == RecordResult.OK and record.port == device
    bk.open_port_now(device, grace)
    assert app.window.isVisible()


# ---- §8.2: S3 over the UART bridge, flash and read back ---------------------------------


def test_s3_uart_flash_and_read_back(
    embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    device = kit.board_port(bk.UART)
    app = embr()
    window = app.window
    files = _hello(window, file_dialogs, tmp_path, S3_HELLO, "s3 uart")
    bk.identify(window, device)
    assert window.connection.baud.isEnabled()
    kit.pick_combo(window.connection.baud, "921600")
    kit.set_checked(window.connection.erase_all, True)  # really writes every entry
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    record = bk.last_record(app)
    assert (record.result, record.baud, record.connection_type) == (
        RecordResult.OK,
        921600,
        "uart_bridge",
    )
    assert (record.reset_before, record.reset_after) == ("default-reset", "hard-reset")
    assert [e.skipped for e in record.entries] == [False, False, False]
    by_name = {p.name: p for p in files}
    pairs = [(e.address, by_name[e.name]) for e in record.entries]
    assert sorted(a for a, _ in pairs) == [0x0, 0x8000, APP]
    assert _same_on_board(device, pairs, tmp_path / "rb") == []
    assert HELLO in bk.listen(device, 6, reset=True)


# ---- §8.2: S3 over USB-Serial/JTAG ------------------------------------------------------


def test_s3_usb_serial_jtag_auto_reset_and_watchdog_boot(
    embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    device = kit.board_port(bk.USB)
    app = embr()
    window = app.window
    files = _hello(window, file_dialogs, tmp_path, S3_HELLO, "s3 usb")
    bk.pick_port(window, device)
    baud = window.connection.baud
    assert not baud.isEnabled() and kit.same_text(baud.currentText(), "not applicable")
    bk.identify(window, device)  # USB-JTAG reset sequence: into download mode by itself
    identity = " ".join(kit.all_texts(window.connection))
    assert any(v in identity for v in kit.variants("in download mode")), identity
    assert bk.has_check(window, "Reset policy") or bk.has_check(
        window, "Reset policy adjusted to connection"
    ), bk.checks(window)
    kit.set_checked(window.connection.erase_all, True)
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    record = bk.last_record(app)
    assert record.result == RecordResult.OK
    assert (record.connection_type, record.baud) == ("usb_serial_jtag", None)
    assert record.reset_after == "watchdog-reset"
    hint = kit.text_of(window.connection.hint)
    assert kit.same_text(hint, HINT_MANUAL), hint
    out = bk.listen(device, 13)  # hello_world restarts every ~11 s: it runs, not in ROM
    assert HELLO in out or "Restarting in" in out, out[-1500:]
    by_name = {p.name: p for p in files}
    pairs = [(e.address, by_name[e.name]) for e in record.entries]
    assert _same_on_board(device, pairs, tmp_path / "rb") == []


HINT_MANUAL = (
    "Flashed and verified. If the device did not restart on its own (usually when download "
    "mode was entered by hand), reset it manually."
)


# ---- §8.2: Tab5 -------------------------------------------------------------------------


def test_tab5_flash_ok_and_rev3_image_refused_without_writing(
    embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    device = kit.board_port(bk.P4)
    app = embr()
    window = app.window
    rev1 = _hello(window, file_dialogs, tmp_path, P4_REV1, "tab5 v1")
    _hello(window, file_dialogs, tmp_path, P4_REV3, "tab5 v3")
    kit.click_sidebar(window, "tab5 v1")
    bk.identify(window, device)
    kit.set_checked(window.connection.erase_all, True)
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    assert bk.last_record(app).result == RecordResult.OK
    pairs = sorted(zip((0x2000, 0x8000, APP), rev1, strict=True))
    assert _same_on_board(device, pairs, tmp_path / "rb") == []
    out = bk.listen(device, 13)
    assert HELLO in out or "Restarting in" in out, out[-1500:]

    # the v3.x-only image: refused before anything is written
    spans = [(0x2000, 0x1000), (APP, 0x1000)]
    before = bk.regions(device, spans, tmp_path / "rb")
    count = len(bk.records(app))
    bk.identify(window, device)
    kit.click_sidebar(window, "tab5 v3")
    kit.pump(300)
    assert not window.connection.flash.isEnabled(), bk.checks(window)
    shown = bk.checks(window)
    assert bk.has_check(window, "No entries enabled") or bk.has_check(window, "Chip revision"), (
        shown
    )
    assert bk.sidebar_dimmed(window, "tab5 v3") and not bk.sidebar_dimmed(window, "tab5 v1")
    assert len(bk.records(app)) == count  # no job, no record
    assert bk.regions(device, spans, tmp_path / "rb") == before


# ---- §8.2: a project with S3 and P4 firmware --------------------------------------------


def test_multichip_project_highlight_dim_and_refusal(
    embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:

    uart = kit.board_port(bk.UART)
    tab5 = kit.board_port(bk.P4)
    app = embr()
    window = app.window
    bk.new_project(window, "Multi-chip")
    _hello(window, file_dialogs, tmp_path, S3_HELLO, "s3 fw", place="Multi-chip")
    _hello(window, file_dialogs, tmp_path, P4_REV1, "p4 fw", place="Multi-chip")
    assert {f.project_id for f in app.context.firmware.all()} == {app.context.projects.all()[0].id}

    bk.identify(window, uart)
    assert not bk.sidebar_dimmed(window, "s3 fw") and bk.sidebar_dimmed(window, "p4 fw")
    kit.click_sidebar(window, "p4 fw")
    assert not window.connection.flash.isEnabled()
    assert bk.has_check(window, "Chip"), bk.checks(window)
    count = len(bk.records(app))
    bk.identify(window, tab5)
    assert bk.sidebar_dimmed(window, "s3 fw") and not bk.sidebar_dimmed(window, "p4 fw")
    kit.click_sidebar(window, "s3 fw")
    assert not window.connection.flash.isEnabled() and bk.has_check(window, "Chip")
    kit.click_sidebar(window, "p4 fw")
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    assert bk.last_record(app).firmware_name == "p4 fw"
    assert len(bk.records(app)) == count + 1


def test_p4_firmware_flash_job_on_s3_refused_behind_the_disabled_button(
    embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    """The window keeps Flash disabled (test above). Behind it, the job must refuse too
    (design doc §4.1: the job checks everything in its own connection; §4.3: identified
    chip, firmware chip and image headers compared, a mismatch is refused), with nothing
    written and no "ok" record for the P4 firmware on the S3."""
    from embr.app.flashplan import PlanError

    uart = kit.board_port(bk.UART)
    app = embr()
    window = app.window
    _hello(window, file_dialogs, tmp_path, P4_REV1, "p4 fw")
    bk.identify(window, uart)
    kit.click_sidebar(window, "p4 fw")
    assert not window.connection.flash.isEnabled()
    spans = [(0x0, 0x1000), (0x2000, 0x1000), (0x8000, 0x1000), (APP, 0x2000)]
    count = len(bk.records(app))
    before = bk.regions(uart, spans, tmp_path / "rb")
    bk.identify(window, uart)  # a fresh snapshot after the read-back's reset
    kit.click_sidebar(window, "p4 fw")
    # behind the disabled button the job must refuse as well (design doc §4.1, §4.3: the
    # job checks the board against the firmware's chip in its own connection)
    port = window.connection.selected_port()
    fw = bk.firmware_by_name(app, "p4 fw")
    ops = window._ops
    try:
        ops.flash(fw.id, port, None, window._signals)
    except PlanError:
        pass  # refused before any job: no record either
    else:
        kit.wait_until(lambda: not ops.busy, 120, message="the bypassed flash job did not end")
        kit.pump(300)
    new = bk.records(app)[: len(bk.records(app)) - count]
    assert bk.regions(uart, spans, tmp_path / "rb") == before
    assert all(r.result == RecordResult.REFUSED for r in new), [
        (r.result.value, r.firmware_name, r.chip_detected, len(r.entries)) for r in new
    ]


# ---- §12.7: switching between the boards ------------------------------------------------


def test_switch_ports_and_boards_with_firmware_selected(
    embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    uart = kit.board_port(bk.UART)
    usb = kit.board_port(bk.USB)
    tab5 = kit.board_port(bk.P4)
    app = embr()
    window = app.window
    _hello(window, file_dialogs, tmp_path, P4_REV1, "p4 sw")
    _hello(window, file_dialogs, tmp_path, S3_HELLO, "s3 sw")
    kit.click_sidebar(window, "s3 sw")

    bk.identify(window, uart)
    assert window.connection.flash.isEnabled(), bk.checks(window)
    bk.pick_port(window, tab5)  # another board: the snapshot is gone
    kit.pump(200)
    assert window.identified_chip() is None
    assert not window.connection.flash.isEnabled()
    assert bk.has_check(window, "Chip / revision / size / security"), bk.checks(window)
    assert not bk.sidebar_dimmed(window, "p4 sw") and not bk.sidebar_dimmed(window, "s3 sw")

    bk.identify(window, tab5)  # the S3 firmware is refused on the P4
    assert window.identified_chip() == "esp32p4"
    assert bk.sidebar_dimmed(window, "s3 sw") and not window.connection.flash.isEnabled()
    kit.click_sidebar(window, "p4 sw")
    assert kit.same_text(bk.flash(window), "Done"), app.log()

    bk.identify(window, usb)  # the same S3 through its other port
    kit.click_sidebar(window, "s3 sw")
    assert window.identified_chip() == "esp32s3"
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    assert bk.last_record(app).connection_type == "usb_serial_jtag"

    # a job on the UART port while the USB port of the same board drops out and returns
    bk.identify(window, uart)
    kit.set_checked(window.connection.erase_all, True)
    kit.click(window.connection.flash)
    kit.wait_until(lambda: kit.job_running(window), 10)
    assert not window.connection.port.isEnabled() and not window.connection.identify.isEnabled()
    result = kit.wait_job_finished(window)
    assert kit.same_text(result, "Done"), app.log()
    kit.set_checked(window.connection.erase_all, False)
    bk.wait_combo_port(window, usb)
    assert window.connection.selected_port().device == uart
    assert bk.last_record(app).port == uart


# ---- §5 stage one: an attachment placed by partition ------------------------------------


@pytest.mark.parametrize("language", [Language.ENGLISH, Language.ZH_CN])
def test_attachment_by_partition_is_flashed_there(
    language: Language,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
) -> None:
    from PySide6.QtWidgets import QComboBox, QLineEdit

    device = kit.board_port(bk.UART)
    app = embr(language=language)
    window = app.window
    files = _hello(window, file_dialogs, tmp_path, S3_HELLO, "with data")
    blob = files[0].parent / "calibration.dat"  # fixed firmware: files below its folder
    blob.write_bytes(random.Random(7).randbytes(0x3000))
    file_dialogs.queue_open(blob)

    def place(driver: kit.DialogDriver) -> None:
        combo = kit.find_all(driver.dialog, QComboBox)[0]
        kit.pick_combo(combo, "Partition: nvs")
        driver.ok()

    def address_prompt(dialog: Any) -> bool:  # the second QInputDialog: a text field only
        return not [c for c in kit.find_all(dialog, QComboBox)] and bool(
            kit.find_all(dialog, QLineEdit)
        )

    with (
        bk.dismissing(address_prompt) as unexpected,
        kit.expect_dialog(place, title="Place attachment"),
    ):
        kit.choose_menu_from(window.firmware_view.entries.add_button, "Attachment…")
    kit.pump(200)
    assert unexpected == [], f"asked for a hex address after choosing a partition: {unexpected}"
    entries = bk.entries_of(app, "with data")
    attached = [e for e in entries if e[1] == blob.name]
    assert attached and attached[0][0] == 0x9000, entries  # nvs of the default table
    with bk.restoring_board(device, _pairs(S3_HELLO)):
        bk.identify(window, device)
        assert kit.same_text(bk.flash(window), "Done"), app.log()
        assert bk.read_back(device, 0x9000, 0x3000, tmp_path / "rb") == blob.read_bytes()
        assert _same_on_board(device, [(APP, files[2])], tmp_path / "rb") == []
    _restore_hello(device, S3_HELLO)  # clear the attachment out of nvs


# ---- §12.7: erase the whole flash, then flash a test program again ----------------------


@pytest.mark.parametrize("var", [bk.UART, bk.P4])
def test_erase_whole_flash_then_reflash(
    var: str, embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    device = kit.board_port(var)
    sample = _sample_for(var)
    app = embr()
    window = app.window
    _hello(window, file_dialogs, tmp_path, sample, "after erase")
    with bk.restoring_board(device, _pairs(sample)):
        bk.identify(window, device)
        assert window.connection.erase.isEnabled()

        def erase(driver: kit.DialogDriver) -> None:
            assert device in driver.text()
            driver.ok()

        with kit.expect_dialog(erase, title="Erase flash"):
            kit.click(window.connection.erase)
        assert kit.same_text(kit.wait_job_finished(window), "Done"), app.log()
        record = bk.last_record(app)
        assert (record.operation, record.result, record.entries) == (
            Operation.ERASE,
            RecordResult.OK,
            (),
        )
        blank = b"\xff" * 0x1000
        for address in (0x0, 0x2000, 0x8000, APP, 0x100000):
            assert bk.read_back(device, address, 0x1000, tmp_path / "rb") == blank, hex(address)
        assert HELLO not in bk.listen(device, 4, reset=not bk.native(device))

        bk.identify(window, device)
        kit.click_sidebar(window, "after erase")
        assert kit.same_text(bk.flash(window), "Done"), app.log()
        assert [e.skipped for e in bk.last_record(app).entries] == [False, False, False]
        out = bk.listen(device, 13)
        assert HELLO in out or "Restarting in" in out, out[-1500:]


# ---- §12.7: read flash, save it and import it -------------------------------------------


def test_read_flash_region_saved_and_imported(
    embr: Callable[..., kit.EmbrApp], tmp_path: Path
) -> None:
    device = kit.board_port(bk.UART)
    app = embr()
    window = app.window
    out = tmp_path / "dumps" / "s3_first64k.bin"
    out.parent.mkdir()
    bk.identify(window, device)
    size = _flash_size(window)

    def read(driver: kit.DialogDriver) -> None:
        driver.check(f"whole flash ({size})", False)
        driver.type("Start address (hex)", "0x0")
        driver.type("Size (hex)", "0x10000")
        driver.type("Save to", str(out))
        assert driver.checkbox("Import into Embr as a firmware").isChecked()
        driver.pick("Place in", "Standalone (no project)")
        driver.ok()

    with kit.expect_dialog(read, title="Read flash"):
        kit.click(window.connection.read)
    assert kit.same_text(kit.wait_job_finished(window), "Done"), app.log()
    data = out.read_bytes()
    assert len(data) == 0x10000
    assert data == bk.read_back(device, 0x0, 0x10000, tmp_path / "rb")
    record = bk.last_record(app)
    assert (record.operation, record.result) == (Operation.READ_FLASH, RecordResult.OK)
    dump = [f for f in app.context.firmware.all() if f.id == record.firmware_id]
    assert len(dump) == 1 and len(dump[0].entries) == 1
    entry = dump[0].entries[0]
    assert (entry.address, entry.file.split(":", 1)[0]) == (0x0, "archive")
    archived = app.paths.archive / f"{entry.file.split(':', 1)[1]}.bin"
    assert archived.read_bytes() == data
    assert window._selection == ("firmware", dump[0].id)  # shown right away


def _flash_size(window: Any) -> str:
    snapshot = window._memory.snapshot
    assert snapshot is not None
    return str(snapshot.info.flash_size)


# ---- §4.4: the baud rate is remembered per VID/PID --------------------------------------


def test_baud_remembered_per_vid_pid(
    embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    uart = kit.board_port(bk.UART)
    tab5 = kit.board_port(bk.P4)
    root = tmp_path / "embr"
    app = embr(root)
    window = app.window
    _hello(window, file_dialogs, tmp_path, S3_HELLO, "baud")
    bk.identify(window, uart)
    assert kit.same_text(window.connection.baud.currentText(), "460800")
    kit.pick_combo(window.connection.baud, "921600")
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    assert bk.last_record(app).baud == 921600
    settings = (root / "settings.ini").read_text(encoding="utf-8")
    assert "[baud]" in settings and "1a86:55d3 = 921600" in settings.replace("=", " = ").replace(
        "  ", " "
    ), settings
    bk.pick_port(window, tab5)
    assert kit.same_text(window.connection.baud.currentText(), "not applicable")
    bk.pick_port(window, uart)
    assert window.connection.baud.currentText() == "921600"
    app.close()
    again = embr(root)
    bk.pick_port(again.window, tab5)
    bk.pick_port(again.window, uart)
    assert again.window.connection.baud.currentText() == "921600"
    assert "303a:1001" not in (root / "settings.ini").read_text(encoding="utf-8")


# ---- §12.7: damaged firmware is refused before anything is written ----------------------


def _damage(kind: str, files: list[Path], folder: Path) -> list[Path]:
    """The hello files with one of them damaged (bootloader, table, app order)."""
    boot, table, app = files
    folder.mkdir(parents=True, exist_ok=True)
    data = bytearray(app.read_bytes())
    if kind == "app content":  # bytes in the middle of a segment: SHA-256 no longer matches
        middle = len(data) // 2
        data[middle : middle + 16] = bytes(b ^ 0x5A for b in data[middle : middle + 16])
    elif kind == "app checksum":  # the checksum byte before the appended SHA-256
        data[-33] ^= 0xFF
    elif kind == "app truncated":
        data = data[: len(data) // 2]
    elif kind == "app all 0xFF":
        data = bytearray(b"\xff" * 0x10000)
    elif kind == "app not an image":
        data = bytearray(random.Random(3).randbytes(0x10000))
    elif kind == "partition table md5":  # a name byte of the first entry: the MD5 entry fails
        table_data = bytearray(table.read_bytes())
        table_data[12] ^= 0x01
        damaged = folder / table.name
        damaged.write_bytes(bytes(table_data))
        return [boot, damaged, app]
    else:
        raise AssertionError(kind)
    damaged = folder / app.name
    damaged.write_bytes(bytes(data))
    return [boot, table, damaged]


@pytest.mark.parametrize(
    "kind",
    [
        "app content",
        "app checksum",
        "app truncated",
        "app all 0xFF",
        "app not an image",
        "partition table md5",
        "wrong chip",
    ],
)
def test_damaged_firmware_refused_before_writing(
    kind: str,
    embr: Callable[..., kit.EmbrApp],
    file_dialogs: kit.FileDialogs,
    tmp_path: Path,
) -> None:
    device = kit.board_port(bk.UART)
    good = _pairs(S3_HELLO)
    bk.write_back(device, good)  # a known state on the board
    app = embr()
    window = app.window
    if kind == "wrong chip":
        files = kit.hello_files("esp32_hello", tmp_path / "esp32")
    else:
        files = _damage(kind, kit.hello_files(S3_HELLO, tmp_path / "good"), tmp_path / "bad")
    bk.new_fixed_firmware(window, file_dialogs, "damaged", files)
    spans = [(0x0, 0x8000), (0x8000, 0x1000), (APP, good[-1][1].stat().st_size + 0x1000)]
    before = bk.regions(device, spans, tmp_path / "rb")
    count = len(bk.records(app))
    bk.identify(window, device)
    kit.click_sidebar(window, "damaged")
    kit.pump(300)
    shown = bk.checks(window)
    flashed = ""
    if window.connection.flash.isEnabled():  # the user may press it: it must not write
        kit.click(window.connection.flash)
        kit.pump(200)
        if kit.job_running(window):
            flashed = kit.wait_job_finished(window)
    after = bk.regions(device, spans, tmp_path / "rb")
    new = bk.records(app)[: len(bk.records(app)) - count]
    if after != before:
        bk.write_back(device, good)
    assert after == before, (
        f"{kind}: written to the board (result {flashed!r}, records "
        f"{[(r.result.value, r.refuse_reason) for r in new]}); checks before: {shown}"
    )
    assert all(r.result != RecordResult.OK for r in new), [r.result for r in new]


# ---- operator steps (EMBR_RELEASE_INTERACTIVE=0 skips them) -----------------------------


def test_operator_unplug_during_flash_records_failed_and_releases_port(
    embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    device = kit.board_port(bk.UART)
    app = embr()
    window = app.window
    _hello(window, file_dialogs, tmp_path, S3_HELLO, "unplug")
    bk.identify(window, device)
    kit.set_checked(window.connection.erase_all, True)  # erases 16 MB first: time to unplug

    def start() -> None:
        kit.click(window.connection.flash)
        kit.wait_until(lambda: kit.job_running(window), 10)

    kit.operator_step(
        "接下来会开始烧录 ESP32-S3 (COM 口为 UART 一路), 烧录进行中请拔掉 S3 的 USB 线。"
        "请准备好 S3 的 USB 线。",
        "现在拔掉 S3 的 USB 线 (烧录正在进行)。拔掉后本提示会自动关闭。",
        start=start,
        done_when=lambda: not kit.job_running(window),
        timeout=180,
    )
    record = bk.last_record(app)
    assert record.result == RecordResult.FAILED, (record.result, app.log()[-2000:])
    assert window.isVisible() and not kit.job_running(window)
    kit.operator_step(
        "接下来请把 S3 的 USB 线插回。",
        "现在把 S3 的 USB 线插回电脑。端口出现后本提示会自动关闭。",
        done_when=lambda: kit.port_present(device),
        timeout=120,
    )
    bk.wait_combo_port(window, device)
    bk.open_port_now(device, 5)
    with bk.restoring_board(device, _pairs(S3_HELLO)):
        bk.identify(window, device)
        kit.click_sidebar(window, "unplug")
        assert kit.same_text(bk.flash(window), "Done"), app.log()
    kit.set_checked(window.connection.erase_all, False)


def test_operator_hotplug_updates_port_list_within_a_second(
    embr: Callable[..., kit.EmbrApp],
) -> None:
    import time

    uart = kit.board_port(bk.UART)
    usb = kit.board_port(bk.USB)
    app = embr()
    window = app.window
    seen: dict[str, float] = {}

    def watch(present: bool) -> Callable[[], bool]:
        def done() -> bool:
            now = time.monotonic()
            if kit.port_present(uart) == present:
                seen.setdefault("system", now)
            if (uart in bk.combo_devices(window)) == present:
                seen.setdefault("embr", now)
            return "system" in seen and "embr" in seen

        return done

    kit.operator_step(
        "接下来请拔掉 ESP32-S3 的 USB 线 (UART 与原生 USB 两个端口会一起消失), 然后再插回。",
        "现在拔掉 S3 的 USB 线。端口列表更新后本提示会自动关闭。",
        done_when=watch(False),
        timeout=120,
    )
    gone = seen["embr"] - seen["system"]
    assert usb not in bk.combo_devices(window) or not kit.port_present(usb)
    seen.clear()
    kit.operator_step(
        "接下来请把 S3 的 USB 线插回。",
        "现在把 S3 的 USB 线插回。端口列表更新后本提示会自动关闭。",
        done_when=watch(True),
        timeout=120,
    )
    back = seen["embr"] - seen["system"]
    assert gone <= 1.0 and back <= 1.0, (gone, back)
    bk.wait_combo_port(window, usb)


def test_operator_tab5_manual_download_mode_flash_and_manual_reset(
    embr: Callable[..., kit.EmbrApp], file_dialogs: kit.FileDialogs, tmp_path: Path
) -> None:
    device = kit.board_port(bk.P4)
    app = embr()
    window = app.window
    _hello(window, file_dialogs, tmp_path, P4_REV1, "tab5 manual")
    left: list[float] = []

    def gone_and_back() -> bool:
        if not kit.port_present(device):
            left.append(1)
        return bool(left) and kit.port_present(device) and device in bk.combo_devices(window)

    kit.operator_step(
        "接下来请让 Tab5 手动进入下载模式: 长按 Tab5 的复位键约 2 秒, 直到绿灯快闪。",
        "现在长按 Tab5 复位键约 2 秒至绿灯快闪。端口重新出现后本提示会自动关闭。",
        done_when=gone_and_back,
        timeout=120,
    )
    assert left, "the port list did not follow the board into download mode"
    bk.identify(window, device)
    kit.set_checked(window.connection.erase_all, True)
    assert kit.same_text(bk.flash(window), "Done"), app.log()
    kit.set_checked(window.connection.erase_all, False)
    assert bk.last_record(app).result == RecordResult.OK
    assert kit.same_text(kit.text_of(window.connection.hint), HINT_MANUAL)
    still = bk.listen(device, 5)
    assert HELLO not in still, "left download mode by itself (design doc §4.4 says it cannot)"
    kit.operator_step(
        "接下来请手动复位 Tab5 (短按复位键), 让它运行刚烧录的程序。",
        "现在短按 Tab5 的复位键。按完后点击“完成”。",
        timeout=120,
    )
    out = bk.listen(device, 13)
    assert HELLO in out or "Restarting in" in out, out[-1500:]


def test_operator_tab5_not_in_download_mode_prompt_and_cancel(
    embr: Callable[..., kit.EmbrApp],
) -> None:
    """Connect & identify (required before Flash, design doc §12 item 5) on a Tab5 whose
    firmware does not keep USB-Serial/JTAG: the automatic reset cannot work, the hint says
    to enter download mode as the vendor describes, the wait can be cancelled at once and
    the port is released."""
    import time

    device = kit.board_port(bk.P4)
    app = embr()
    window = app.window
    kit.operator_step(
        "接下来需要 Tab5 处于“无法自动进入下载模式”的状态: 让它运行一个不保留 USB-Serial/JTAG "
        "外设的固件 (例如把 USB 口交给 TinyUSB 的程序), 或按住复位键不放。手头没有此类固件、"
        "也不便按住复位键时请点“跳过”。",
        "Tab5 已处于无法自动复位的状态后点击“完成” (若选择按住复位键, "
        "请在测试取消作业前一直按住)。",
        timeout=600,
    )
    if not kit.port_present(device):
        pytest.skip(f"{device} is gone in that state (no USB-Serial/JTAG): nothing to connect to")
    bk.pick_port(window, device)
    assert kit.same_text(kit.text_of(window.connection.hint), HINT_JTAG)
    kit.click(window.connection.identify)
    kit.wait_until(lambda: kit.job_running(window), 10)
    kit.pump(8000)
    assert kit.job_running(window), f"the connect step did not wait: {app.log()[-2000:]}"
    assert kit.same_text(kit.text_of(window.connection.hint), HINT_JTAG)
    started = time.monotonic()
    kit.click(window.connection.flash)  # the button says Cancel while a job runs
    result = kit.wait_job_finished(window, 30)
    assert time.monotonic() - started < 5
    assert kit.same_text(result, "Cancelled"), result
    bk.open_port_now(device, 10)


HINT_JTAG = (
    "Connect & identify this board first. USB-Serial/JTAG: automatic reset is tried; if the "
    "running firmware does not keep this peripheral, the connect step times out, then enter "
    "download mode as described by the board's vendor."
)
