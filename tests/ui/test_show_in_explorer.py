"""Show in Explorer: the two menus and what is started. Nothing is really opened."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from embr.app.locations import Location
from embr.ui.adapters import explorer
from embr.ui.window import EmbrWindow

SHOW = "Show in Explorer"


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    started = MagicMock()
    monkeypatch.setattr(explorer.subprocess, "Popen", started)
    return started


@pytest.fixture
def open_url(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    opened = MagicMock()
    monkeypatch.setattr(explorer.QDesktopServices, "openUrl", opened)
    return opened


def _fixed(window: EmbrWindow, tmp_path: Path) -> tuple[str, Path]:
    file = tmp_path / "app 固件.bin"
    file.write_bytes(b"\x00" * 64)
    firmware = window.library.create_firmware("fw")
    window.library.add_files(firmware.id, [file])
    return firmware.id, file


def _header_action(window: EmbrWindow) -> bool:
    window.header.menu.aboutToShow.emit()
    return window.header.reveal_action.isEnabled()


def test_a_file_is_selected_with_only_its_path_quoted(popen: MagicMock, tmp_path: Path) -> None:
    file = tmp_path / "a b" / "固件.bin"
    explorer.reveal(Location(file, selects_file=True))
    popen.assert_called_once_with(f'explorer /select,"{file}"')


def test_a_folder_is_opened(popen: MagicMock, open_url: MagicMock, tmp_path: Path) -> None:
    explorer.reveal(Location(tmp_path, selects_file=False))
    open_url.assert_called_once_with(QUrl.fromLocalFile(str(tmp_path)))
    popen.assert_not_called()


def test_the_firmware_menu_opens_its_folder(
    window: EmbrWindow, open_url: MagicMock, tmp_path: Path
) -> None:
    firmware_id, _ = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    assert _header_action(window)

    window.header.reveal_action.trigger()

    open_url.assert_called_once_with(QUrl.fromLocalFile(str(tmp_path.resolve())))


def test_the_firmware_menu_is_grey_without_a_folder(window: EmbrWindow) -> None:
    firmware = window.library.create_firmware("empty")
    window.select_firmware(firmware.id)
    assert not _header_action(window)


def test_a_folder_gone_after_opening_the_menu_is_only_noted(
    window: EmbrWindow, open_url: MagicMock
) -> None:
    window.show_in_explorer(None)
    open_url.assert_not_called()


def test_explorer_failing_to_start_is_only_noted(
    window: EmbrWindow, popen: MagicMock, tmp_path: Path
) -> None:
    popen.side_effect = OSError("no explorer")
    window.show_in_explorer(Location(tmp_path / "x.bin", selects_file=True))


def _choose_in_entry_menu(window: EmbrWindow) -> None:
    """Right-click the first entry row and choose Show in Explorer in the menu that opens."""

    def choose() -> None:
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        action = next(a for a in menu.actions() if a.text() == SHOW)
        assert action.isEnabled()
        menu.setActiveAction(action)
        QTest.keyClick(menu, Qt.Key.Key_Return)

    QTimer.singleShot(0, choose)
    entries = window.firmware_view.entries
    entries._context_menu(QPoint(5, entries.table.rowViewportPosition(0) + 5))  # pyright: ignore[reportPrivateUsage]


def test_the_entry_menu_selects_the_file(
    window: EmbrWindow, popen: MagicMock, tmp_path: Path
) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    window.select_firmware(firmware_id)
    window.show()
    QApplication.processEvents()

    _choose_in_entry_menu(window)

    popen.assert_called_once_with(f'explorer /select,"{file.resolve()}"')
