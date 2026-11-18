"""One event handled in several steps builds each part of the window once: startup, where
the first port list refreshes too, and the end of a job during which the ports changed."""

from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from embr.app.context import open_context
from embr.app.library import Library
from embr.app.operations import JobKind, JobOutcome
from embr.core.history import RecordResult
from embr.core.serial import ConnectionType, PortInfo, PortWatcher
from embr.core.storage.paths import AppPaths, prepare
from embr.ui.adapters import ports as port_adapter
from embr.ui.connection import ConnectionPanel
from embr.ui.sidebar import FirmwareTree
from embr.ui.window import EmbrWindow
from embr.ui.workspace import FirmwareView

# pyright: reportPrivateUsage=false

UART = PortInfo(
    device="COM7",
    vid=0x10C4,
    pid=0xEA60,
    serial_number="0001",
    description="USB to UART bridge",
    connection=ConnectionType.UART_BRIDGE,
    otg_chip=None,
)


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """How often each part of the window was built."""
    counts: Counter[str] = Counter()
    parts: list[tuple[type, str, str]] = [
        (FirmwareView, "show_firmware", "workspace"),
        (FirmwareTree, "rows_rebuilt", "sidebar"),
        (ConnectionPanel, "show_checks", "right panel"),
    ]
    for owner, name, part in parts:
        original: Callable[..., Any] = getattr(owner, name)

        def spy(self: object, *args: Any, _original: Any = original, _part: str = part) -> Any:
            counts[_part] += 1
            return _original(self, *args)

        monkeypatch.setattr(owner, name, spy)
    return counts


def _firmware(library: Library, tmp_path: Path) -> str:
    file = tmp_path / "app.bin"
    file.write_bytes(b"\x00" * 64)
    return library.create_from_files([file], name="fw").id


@pytest.fixture
def present() -> list[PortInfo]:
    return [UART]


@pytest.fixture
def ported_window(
    qapp: QCoreApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    present: list[PortInfo],
) -> Iterator[Callable[[], EmbrWindow]]:
    """Opens a window on a library with one firmware; the serial ports present are those
    in `present` (UART at first)."""
    monkeypatch.setattr(port_adapter, "PortWatcher", lambda: PortWatcher(lambda: list(present)))
    paths = AppPaths(tmp_path / "Embr")
    prepare(paths)
    context = open_context(paths)
    _firmware(Library(context), tmp_path)
    opened: list[EmbrWindow] = []

    def open_window() -> EmbrWindow:
        opened.append(EmbrWindow(context))
        return opened[-1]

    yield open_window
    for window in opened:
        window.close()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    context.close()


def test_startup_builds_each_part_once(
    ported_window: Callable[[], EmbrWindow], built: Counter[str]
) -> None:
    window = ported_window()

    assert window.connection.selected_port() == UART  # the first port list was applied
    assert built == {"workspace": 1, "sidebar": 1, "right panel": 1}


def test_the_end_of_a_job_with_ports_changed_meanwhile_builds_each_part_once(
    ported_window: Callable[[], EmbrWindow], built: Counter[str], present: list[PortInfo]
) -> None:
    present.clear()
    window = ported_window()
    assert window.connection.selected_port() is None
    present.append(UART)  # plugged in while a job runs: the list follows once it ends
    with pytest.MonkeyPatch.context() as running:
        running.setattr(type(window.ops), "busy", property(lambda _self: True))
        window.ports.poll()
    assert window.right_panel.ports_pending
    assert window.connection.selected_port() is None
    built.clear()

    window.jobs._finished(
        JobOutcome(JobKind.IDENTIFY, RecordResult.OK, "", None, "", "", native_usb=False)
    )

    assert window.connection.selected_port() == UART
    assert not window.right_panel.ports_pending
    assert built["workspace"] == built["right panel"] == 1
    assert built["sidebar"] <= 1  # its rows stay when nothing in them changed
