"""The Quick flash switch in the connection panel (design D7): stored in settings.ini, Flash
without Connect & identify for a firmware with one MCU, the hints above Flash, Ctrl+Enter,
the switch while a job runs and its row in a narrow column. Driven offscreen on a real
window; no job is ever started."""

import configparser
import datetime
import shutil
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QApplication

from embr.app.checklist import can_flash, preflight
from embr.app.context import open_context
from embr.app.operations import JobKind, JobOutcome
from embr.core.firmware.entries import entry_from_file
from embr.core.firmware.kinds import EntryKind
from embr.core.flasher.checks import SecurityState
from embr.core.flasher.protocol import InfoEvent
from embr.core.history import RecordResult
from embr.core.serial import ConnectionType, PortInfo
from embr.core.storage.paths import AppPaths, prepare
from embr.i18n import Language, current, set_language, tr
from embr.ui.connection import QUICK_TIP
from embr.ui.window import (
    HINT_QUICK_JTAG,
    HINT_QUICK_NEEDS_ID,
    HINT_QUICK_OTG,
    QUICK_KEY,
    EmbrWindow,
)

# pyright: reportPrivateUsage=false

IDF = Path(__file__).resolve().parents[1] / "fixtures" / "idf"
S3 = IDF / "esp32s3_hello" / "build"
S3_FILES = (
    S3 / "bootloader" / "bootloader.bin",
    S3 / "partition_table" / "partition-table.bin",
    S3 / "hello_esp32s3.bin",
)
P4 = IDF / "esp32p4_rev1_hello" / "build"
P4_FILES = (
    P4 / "bootloader" / "bootloader.bin",
    P4 / "partition_table" / "partition-table.bin",
    P4 / "hello_esp32p4_rev1.bin",
)

UART = PortInfo(
    device="COM7",
    vid=0x10C4,
    pid=0xEA60,
    serial_number="0001",
    description="USB to UART bridge",
    connection=ConnectionType.UART_BRIDGE,
    otg_chip=None,
)
JTAG = PortInfo(
    device="COM8",
    vid=0x303A,
    pid=0x1001,
    serial_number="SN0001",
    description="USB JTAG/serial debug unit",
    connection=ConnectionType.USB_SERIAL_JTAG,
    otg_chip=None,
)
OTG = PortInfo(
    device="COM9",
    vid=0x303A,
    pid=0x0002,
    serial_number="SN0002",
    description="USB OTG",
    connection=ConnectionType.USB_OTG,
    otg_chip="esp32s3",
)

# the texts as the requirements state them
TIP = (
    "Skip Connect & identify and flash directly. The chip is still identified and checked "
    "during the flash; writing is refused if the chip does not match or a security feature "
    "is on."
)
NEEDS_ID = (
    "This firmware is for several chips or has no chip selected, so the board must be "
    "identified first."
)
QUICK_OTG = (
    "Quick flash over USB-OTG: put the board into download mode as described by its vendor "
    "before flashing; Cancel is available while waiting."
)
QUICK_JTAG = (
    "Quick flash over USB-Serial/JTAG: automatic reset is tried; if the running firmware does "
    "not keep this peripheral, the connect step times out, then enter download mode as "
    "described by the board's vendor."
)
ERASE = (
    "Quick flash with erase on: the whole flash of the board on {} will be erased without "
    "identifying it first."
)
OLD_IDENTIFY = "Connect & identify this board first; Flash becomes available when no check refuses."
OLD_OTG = (
    "Connect & identify this board first. USB-OTG: put the board into download mode as "
    "described by its vendor before connecting; Cancel is available while waiting."
)
OLD_JTAG = (
    "Connect & identify this board first. USB-Serial/JTAG: automatic reset is tried; if the "
    "running firmware does not keep this peripheral, the connect step times out, then enter "
    "download mode as described by the board's vendor."
)
MANUAL_RESET = (
    "Flashed and verified. If the device did not restart on its own (usually when download "
    "mode was entered by hand), reset it manually."
)
ZH_QUICK = "快速烧录"
ZH_TIP = (
    "跳过“连接并识别”，直接烧录。烧录时仍会识别芯片并进行安全检查；"  # noqa: RUF001
    "芯片不符或已启用安全功能时拒绝写入。"
)


# ---- helpers ---------------------------------------------------------------------------


def _quiet(window: EmbrWindow) -> EmbrWindow:
    """No port polling while a test runs: the ports are the fake ones put in by hand."""
    window.ports._fallback.stop()
    return window


@contextmanager
def _open(paths: AppPaths, language: Language = Language.ENGLISH) -> Generator[EmbrWindow]:
    """Another window on `paths` (prepared here when new), built in `language`."""
    before = current()
    set_language(language)
    prepare(paths)
    context = open_context(paths)
    shown = EmbrWindow(context)
    try:
        yield _quiet(shown)
    finally:
        shown.close()
        shown.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        context.close()
        set_language(before)


def _single(window: EmbrWindow, tmp_path: Path) -> str:
    """The esp32s3 hello build as a fixed firmware (one MCU), selected."""
    folder = tmp_path / "s3"
    folder.mkdir()
    files = [Path(shutil.copy(f, folder / f.name)) for f in S3_FILES]
    firmware = window.library.create_firmware("s3")
    window.library.add_files(firmware.id, files)
    window.select_firmware(firmware.id)
    return firmware.id


def _no_chip(window: EmbrWindow, tmp_path: Path) -> str:
    """A data file at 0x10000, no chip: nothing refuses before identification."""
    folder = tmp_path / "data"
    folder.mkdir()
    file = folder / "data.bin"
    file.write_bytes(b"\x00" * 64)
    firmware = window.library.create_firmware("data")
    window.library.add_files(firmware.id, [file])
    entry = window.library.firmware(firmware.id).entries[0]
    window.library.set_entry_address(firmware.id, entry.id, "0x10000")
    window.select_firmware(firmware.id)
    return firmware.id


def _multi(window: EmbrWindow, tmp_path: Path) -> str:
    """The hello builds for esp32s3 (folder) and esp32p4 v0-v1 (folder/p4), stored directly
    with each partition table's MCU chosen by hand; selected."""
    store = window.context.firmware
    folder = tmp_path / "multi"
    firmware = store.create("multi")
    firmware.folder = str(folder.resolve())
    for files, target, chip, line in (
        (S3_FILES, folder, "esp32s3", ""),
        (P4_FILES, folder / "p4", "esp32p4", "v0-v1"),
    ):
        target.mkdir(parents=True)
        for source in files:
            entry = entry_from_file(Path(shutil.copy(source, target / source.name)).resolve())
            if entry.kind == EntryKind.PARTITION_TABLE:
                entry.chip, entry.chip_line = chip, line
            firmware.entries.append(entry)
    store.save(firmware)
    window.select_firmware(firmware.id)
    return firmware.id


def _port(window: EmbrWindow, port: PortInfo | None) -> None:
    """`port` is the only one (None: no serial port), and the user picked it."""
    window.connection.set_ports(() if port is None else (port,))
    combo = window.connection.port
    picked = [i for i in range(combo.count()) if isinstance(combo.itemData(i), int)]
    if picked:  # past the placeholder of a port chosen before
        combo.setCurrentIndex(picked[0])
    window._port_changed()


def _identify(window: EmbrWindow, port: PortInfo) -> None:
    """A finished Connect & identify of an esp32s3 v0.2 on `port`."""
    security = SecurityState(secure_download_mode=False, flash_encryption=False, secure_boot=False)
    event = InfoEvent(
        chip="esp32s3",
        revision="v0.2",
        major=0,
        minor=2,
        mac="aa:bb:cc:00:00:01",
        flash_size="2MB",
        security=security,
        in_download_mode=False,
    )
    window._memory.record(port, event, datetime.datetime(2026, 1, 1, 12, 0))
    window._refresh_right()


def _quick(window: EmbrWindow, on: bool) -> None:
    """Set the switch as a click would."""
    if window.connection.quick.isChecked() != on:
        window.connection.quick.click()
    assert window.connection.quick.isChecked() is on


def _stored(paths: AppPaths) -> str | None:
    """settings.ini [ui] quick_flash as written on disk."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(paths.settings, encoding="utf-8")
    return parser.get("ui", QUICK_KEY, fallback=None)


def _nothing_refuses(window: EmbrWindow, firmware_id: str, port: PortInfo) -> bool:
    """Before identification: so a disabled Flash comes from the missing identification."""
    firmware = window.library.firmware(firmware_id)
    return can_flash(preflight(window.context, firmware, port, None))


def _flash(window: EmbrWindow) -> bool:
    return window.connection.flash.isEnabled()


def _hint(window: EmbrWindow) -> tuple[str, str]:
    hint = window.connection.hint
    return hint.text(), str(hint.property("hint"))


def _settle(window: EmbrWindow, right: int) -> None:
    window.show()
    window.resize(1360, 760)
    QApplication.processEvents()
    sizes = window.columns.sizes()
    window.columns.setSizes([sizes[0], sum(sizes) - sizes[0] - right, right])
    QApplication.processEvents()
    QApplication.processEvents()
    assert window.connection.width() == right


# ---- the setting -----------------------------------------------------------------------


def test_off_by_default(window: EmbrWindow) -> None:
    _quiet(window)
    assert QUICK_KEY == "quick_flash"
    assert not window.connection.quick.isChecked()
    assert _stored(window.context.paths) is None


def test_toggling_saves_at_once(window: EmbrWindow) -> None:
    paths = _quiet(window).context.paths
    _quick(window, True)
    assert _stored(paths) == "1"
    _quick(window, False)
    assert _stored(paths) == "0"


@pytest.mark.parametrize("on", [True, False])
def test_kept_across_restarts(window: EmbrWindow, on: bool) -> None:
    paths = _quiet(window).context.paths
    _quick(window, True)
    _quick(window, on)
    with _open(paths) as again:
        assert again.connection.quick.isChecked() is on


def test_a_stored_value_is_read_at_start(window: EmbrWindow, tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "Other")
    paths.root.mkdir()
    paths.settings.write_text(f"[ui]\n{QUICK_KEY} = 1\n", encoding="utf-8")
    with _open(paths) as shown:
        assert shown.connection.quick.isChecked()


# ---- Flash -----------------------------------------------------------------------------


def test_quick_off_needs_identification(window: EmbrWindow, tmp_path: Path) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, UART)
    assert not _flash(window)
    _identify(window, UART)
    assert _flash(window)


def test_quick_on_flashes_a_single_chip_firmware_unidentified(
    window: EmbrWindow, tmp_path: Path
) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, UART)
    _quick(window, True)
    assert _flash(window)
    _quick(window, False)
    assert not _flash(window)


def test_quick_on_still_needs_a_port(window: EmbrWindow, tmp_path: Path) -> None:
    _single(_quiet(window), tmp_path)
    _quick(window, True)
    _port(window, None)
    assert not _flash(window)
    _port(window, UART)
    assert _flash(window)


def test_quick_on_still_refuses(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id = _single(_quiet(window), tmp_path)
    _port(window, UART)
    _quick(window, True)
    assert _flash(window)
    (tmp_path / "s3" / "hello_esp32s3.bin").unlink()
    window.library.tracker.invalidate(firmware_id)
    window.changed()
    assert not _flash(window)


def test_quick_on_without_a_chip_needs_identification(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id = _no_chip(_quiet(window), tmp_path)
    assert _nothing_refuses(window, firmware_id, UART)
    _port(window, UART)
    _quick(window, True)
    assert not _flash(window)
    assert _hint(window) == (NEEDS_ID, "ok")


def test_quick_on_with_several_chips_needs_identification(
    window: EmbrWindow, tmp_path: Path
) -> None:
    firmware_id = _multi(_quiet(window), tmp_path)
    assert _nothing_refuses(window, firmware_id, UART)
    _port(window, UART)
    _quick(window, True)
    assert not _flash(window)
    assert _hint(window) == (NEEDS_ID, "ok")
    _identify(window, UART)  # the board is an esp32s3: nothing refuses
    assert _flash(window)
    assert _hint(window)[0] == ""


@pytest.mark.parametrize("port", [UART, JTAG, OTG], ids=["uart", "jtag", "otg"])
def test_quick_on_any_link(window: EmbrWindow, tmp_path: Path, port: PortInfo) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, port)
    _quick(window, True)
    assert _flash(window)


def test_no_firmware_no_flash(window: EmbrWindow) -> None:
    _quiet(window)
    _port(window, UART)
    _quick(window, True)
    assert not _flash(window)


# ---- hints -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("port", "text", "kind"),
    [(UART, "", "ok"), (JTAG, QUICK_JTAG, "ok"), (OTG, QUICK_OTG, "warn")],
    ids=["uart", "jtag", "otg"],
)
def test_quick_hint_per_link(
    window: EmbrWindow, tmp_path: Path, port: PortInfo, text: str, kind: str
) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, port)
    _quick(window, True)
    assert window.connection.hint.text() == text
    if text:
        assert _hint(window) == (text, kind)


@pytest.mark.parametrize(
    ("port", "text", "kind"),
    [(UART, OLD_IDENTIFY, "ok"), (JTAG, OLD_JTAG, "ok"), (OTG, OLD_OTG, "warn")],
    ids=["uart", "jtag", "otg"],
)
def test_quick_off_keeps_the_old_hints(
    window: EmbrWindow, tmp_path: Path, port: PortInfo, text: str, kind: str
) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, port)
    _quick(window, True)
    _quick(window, False)
    assert _hint(window) == (text, kind)
    assert not _flash(window)


def test_erase_on_warns(window: EmbrWindow, tmp_path: Path) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, UART)
    _quick(window, True)
    window.connection.erase_all.click()
    assert window.library.firmware(window._selected_firmware_id() or "").erase_all
    assert _hint(window) == (ERASE.format(UART.device), "warn")
    assert _flash(window)
    window.connection.erase_all.click()
    assert window.connection.hint.text() == ""


def test_erase_on_with_quick_off_keeps_the_old_hint(window: EmbrWindow, tmp_path: Path) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, UART)
    window.connection.erase_all.click()
    assert _hint(window) == (OLD_IDENTIFY, "ok")


def test_no_quick_hint_once_identified(window: EmbrWindow, tmp_path: Path) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, OTG)
    _quick(window, True)
    window.connection.erase_all.click()
    _identify(window, OTG)
    assert window.connection.hint.text() == ""


def test_the_last_job_hint_comes_first(window: EmbrWindow, tmp_path: Path) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, OTG)
    _quick(window, True)
    window._last_outcome = JobOutcome(
        kind=JobKind.FLASH,
        result=RecordResult.OK,
        message="",
        record_id="r1",
        log_file="",
        segment_id="r1",
        native_usb=True,
    )
    window._refresh_right()
    assert _hint(window) == (MANUAL_RESET, "warn")


def test_hint_constants_match_the_requirements() -> None:
    assert (HINT_QUICK_NEEDS_ID, HINT_QUICK_OTG, HINT_QUICK_JTAG) == (
        NEEDS_ID,
        QUICK_OTG,
        QUICK_JTAG,
    )
    assert QUICK_TIP == TIP


# ---- Ctrl+Enter ------------------------------------------------------------------------


def _recorded(window: EmbrWindow, monkeypatch: pytest.MonkeyPatch) -> list[None]:
    calls: list[None] = []
    monkeypatch.setattr(window, "_flash_or_cancel", lambda: calls.append(None))
    return calls


def test_shortcut_follows_flash_with_quick_on(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recorded(_quiet(window), monkeypatch)
    _single(window, tmp_path)
    _port(window, UART)
    window._flash_shortcut()
    assert calls == []
    _quick(window, True)
    window._flash_shortcut()
    assert calls == [None]


def test_shortcut_does_nothing_for_a_chipless_firmware(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recorded(_quiet(window), monkeypatch)
    firmware_id = _no_chip(window, tmp_path)
    assert _nothing_refuses(window, firmware_id, UART)
    _port(window, UART)
    _quick(window, True)
    window._flash_shortcut()
    assert calls == []


# ---- the switch itself -----------------------------------------------------------------


def test_disabled_while_a_job_runs(window: EmbrWindow, tmp_path: Path) -> None:
    _single(_quiet(window), tmp_path)
    _port(window, UART)
    quick = window.connection.quick
    assert quick.isEnabled()
    window.connection.set_running(True)
    assert not quick.isEnabled()
    window._refresh_right()
    assert not quick.isEnabled()
    window.connection.set_running(False)
    assert quick.isEnabled()


def test_visible_without_a_firmware(window: EmbrWindow) -> None:
    _quiet(window)
    _settle(window, 330)
    panel = window.connection
    assert window._selected_firmware_id() is None
    assert not panel.erase_all.isVisible()
    assert panel.quick.isVisible()
    assert panel.quick.isEnabled()


@pytest.mark.parametrize(("right", "wrapped"), [(280, True), (330, False)])
def test_own_row_in_a_narrow_english_column(
    window: EmbrWindow, tmp_path: Path, right: int, wrapped: bool
) -> None:
    _single(_quiet(window), tmp_path)
    _settle(window, right)
    panel = window.connection
    assert panel.erase_all.isVisible() and panel.quick.isVisible()
    assert panel.wrapped() is wrapped
    below = (
        panel.quick.mapTo(panel, panel.quick.rect().topLeft()).y()
        > panel.erase_all.mapTo(panel, panel.erase_all.rect().bottomLeft()).y()
    )
    assert below is wrapped


def test_one_row_in_chinese_at_the_narrowest(tmp_path: Path, qapp: QCoreApplication) -> None:
    with _open(AppPaths(tmp_path / "Embr"), Language.ZH_CN) as shown:
        _single(shown, tmp_path)
        _settle(shown, 280)
        assert shown.connection.erase_all.isVisible()
        assert not shown.connection.wrapped()


# ---- Simplified Chinese ----------------------------------------------------------------


@pytest.fixture
def chinese() -> Iterator[None]:
    before = current()
    set_language(Language.ZH_CN)
    yield
    set_language(before)


@pytest.mark.usefixtures("chinese")
def test_translations() -> None:
    assert tr("Quick flash") == ZH_QUICK
    assert tr(TIP) == ZH_TIP
    for text in (NEEDS_ID, QUICK_OTG, QUICK_JTAG):
        assert tr(text) != text
    erase = tr(ERASE.format("COM7"))
    assert erase != ERASE.format("COM7")
    assert "COM7" in erase


def test_chinese_panel(tmp_path: Path, qapp: QCoreApplication) -> None:
    with _open(AppPaths(tmp_path / "Embr"), Language.ZH_CN) as shown:
        _single(shown, tmp_path)
        _port(shown, OTG)
        _quick(shown, True)
        panel = shown.connection
        assert panel.quick_text.text() == ZH_QUICK
        assert panel.quick.toolTip() == ZH_TIP
        assert panel.quick_help.toolTip() == ZH_TIP
        assert panel.quick.accessibleName() == ZH_QUICK
        text, kind = _hint(shown)
        assert (text, kind) == (tr(QUICK_OTG), "warn")
        assert text != QUICK_OTG


def _hotplug(
    window: EmbrWindow, monkeypatch: pytest.MonkeyPatch, ports: tuple[PortInfo, ...]
) -> None:
    """The port monitor now reports `ports`."""
    monkeypatch.setattr(type(window.ports), "ports", property(lambda _self: ports))
    window._apply_ports()


def _pick(window: EmbrWindow, device: str) -> None:
    """The user picks `device` in the port list."""
    combo = window.connection.port
    (index,) = [
        i
        for i in range(combo.count())
        if isinstance(data := combo.itemData(i), int)
        and window.connection._ports[data].device == device
    ]
    combo.setCurrentIndex(index)
    window._port_changed()


def test_a_port_away_for_a_moment_is_kept_and_selected_again(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chosen port is away for a moment (a cable unplugged, a board re-enumerating):
    meanwhile nothing else is selected (Flash on another board, quick flash on)."""
    _single(_quiet(window), tmp_path)
    _quick(window, True)
    _hotplug(window, monkeypatch, (UART, JTAG))
    combo = window.connection.port
    _pick(window, JTAG.device)
    _identify(window, JTAG)
    outcome = JobOutcome(JobKind.FLASH, RecordResult.OK, "", "r1", "", "r1", native_usb=True)
    window._last_outcome = outcome
    assert window.connection.flash.isEnabled()

    _hotplug(window, monkeypatch, (UART,))
    assert combo.currentText() == "COM8 · unavailable"
    assert window.connection.selected_port() is None
    assert not window.connection.flash.isEnabled()
    assert not window.connection.identify.isEnabled()
    assert window._last_outcome is outcome and window._memory.snapshot is not None

    _hotplug(window, monkeypatch, (UART, JTAG))
    assert window.connection.selected_port() == JTAG
    assert window._last_outcome is outcome and window._memory.snapshot is not None
    assert window.connection.flash.isEnabled()


def test_picking_another_port_while_one_is_away_drops_it(
    window: EmbrWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _single(_quiet(window), tmp_path)
    _hotplug(window, monkeypatch, (UART, JTAG))
    combo = window.connection.port
    _pick(window, JTAG.device)
    _hotplug(window, monkeypatch, (UART,))
    model = combo.model()
    assert isinstance(model, QStandardItemModel) and not model.item(0).isEnabled()

    _pick(window, UART.device)
    assert [combo.itemText(i).split(" ")[0] for i in range(combo.count())] == ["COM7"]
    _hotplug(window, monkeypatch, (UART, JTAG))  # back, but no longer the chosen one
    assert window.connection.selected_port() == UART
