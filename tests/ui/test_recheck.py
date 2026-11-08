"""Checks of the selected firmware on a click, on activation and while a build settles: one
timer, at most one check per second, no extra checks while a build is still being written,
and no refresh under a half-typed parameter (design D15). The clock is replaced and the
timer fired by hand, so nothing here waits."""

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMenu, QWidget

from embr.app.tracking import TrackStatus
from embr.core.firmware.tracking import ResolveState
from embr.ui.window import RECHECK_GAP_S, SETTLE_RECHECK_MS, EmbrWindow

# pyright: reportPrivateUsage=false


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(window: EmbrWindow) -> Clock:
    fake = Clock()
    window._clock = fake
    return fake


def _counted(window: EmbrWindow, state: ResolveState | None = None) -> list[str]:
    """Count Tracker.check calls; with `state`, report that state (same digest each time)."""
    tracker = window.library.tracker
    real: Callable[..., TrackStatus] = tracker.check
    calls: list[str] = []

    def check(firmware_id: str, **kwargs: bool) -> TrackStatus:
        calls.append(firmware_id)
        status = real(firmware_id, **kwargs)
        if counted_state[0] is not None:
            status = dataclasses.replace(status, state=counted_state[0])
        return status

    counted_state = [state]
    tracker.check = check  # type: ignore[method-assign]
    window._counted_state = counted_state  # type: ignore[attr-defined]
    return calls


def _fixed(window: EmbrWindow, tmp_path: Path) -> tuple[str, Path]:
    file = tmp_path / "app.bin"
    file.write_bytes(b"\x00" * 64)
    firmware = window.library.create_firmware("fw")
    window.library.add_files(firmware.id, [file])
    window.select_firmware(firmware.id)
    window._recheck_timer.stop()
    return firmware.id, file


def _fire(window: EmbrWindow) -> None:
    timer = window._recheck_timer
    assert timer.isActive()
    timer.stop()
    window._run_recheck()


def test_fifty_requests_in_a_second_check_once(
    window: EmbrWindow, tmp_path: Path, clock: Clock
) -> None:
    _fixed(window, tmp_path)
    calls = _counted(window)
    for _ in range(50):
        window._request_recheck()
        clock.now += 0.02
    assert calls == []

    _fire(window)
    assert len(calls) == 1
    assert not window._recheck_timer.isActive()


def test_a_request_inside_the_gap_waits_for_it(
    window: EmbrWindow, tmp_path: Path, clock: Clock
) -> None:
    _fixed(window, tmp_path)
    calls = _counted(window)
    window._request_recheck()
    _fire(window)
    clock.now += 0.25

    window._request_recheck()
    assert window._recheck_timer.interval() == round((RECHECK_GAP_S - 0.25) * 1000)
    window._request_recheck()  # a second one in the same gap is not another check
    _fire(window)
    assert len(calls) == 2


def test_an_earlier_request_stands_and_a_later_one_moves_up(
    window: EmbrWindow, tmp_path: Path, clock: Clock
) -> None:
    _fixed(window, tmp_path)
    timer = window._recheck_timer
    window._request_recheck()
    window._request_recheck(SETTLE_RECHECK_MS, settle=True)
    assert timer.interval() == 0

    timer.stop()
    window._request_recheck(SETTLE_RECHECK_MS, settle=True)
    window._request_recheck()
    assert timer.interval() == 0
    assert window._recheck_settle  # the settling build is still looked at, sooner


def test_a_settling_build_keeps_one_timer_and_ignores_clicks(
    window: EmbrWindow, tmp_path: Path, clock: Clock
) -> None:
    firmware_id, _ = _fixed(window, tmp_path)
    calls = _counted(window, ResolveState.BUILDING)
    window.changed()
    timer = window._recheck_timer
    due = window._recheck_due
    assert timer.isActive() and timer.interval() == SETTLE_RECHECK_MS

    clock.now += 0.5
    window.changed()  # refreshing again does not add or move the timer
    assert window._recheck_due == due
    before = len(calls)
    for _ in range(20):
        window._request_recheck()
        clock.now += 0.05
    assert len(calls) == before
    assert window._recheck_due == due

    _fire(window)
    assert len(calls) == before + 1
    assert timer.isActive() and timer.interval() == SETTLE_RECHECK_MS  # still settling

    window._counted_state[0] = None  # type: ignore[attr-defined]
    _fire(window)  # settled: the workspace shows it and nothing is queued any more
    assert window._shown is not None and window._shown[1] != ResolveState.BUILDING
    assert not timer.isActive()
    assert firmware_id in calls


def test_a_click_while_settling_requeues_a_dropped_look(
    window: EmbrWindow, tmp_path: Path, clock: Clock
) -> None:
    _fixed(window, tmp_path)
    _counted(window, ResolveState.BUILDING)
    window.changed()
    window._recheck_timer.stop()  # dropped, e.g. a dialog was open when it fired

    window._request_recheck()
    assert window._recheck_timer.interval() == SETTLE_RECHECK_MS


def test_a_click_in_the_window_queues_a_check(
    window: EmbrWindow, tmp_path: Path, clock: Clock
) -> None:
    _fixed(window, tmp_path)
    window.show()
    QApplication.processEvents()
    timer = window._recheck_timer
    timer.stop()

    other = QWidget()
    other.resize(100, 100)
    other.show()
    QTest.mouseClick(other, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    assert not timer.isActive()
    other.close()

    QTest.mouseClick(window.log_panel, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
    assert timer.isActive()


def test_nothing_is_queued_during_a_round_or_behind_a_menu(
    window: EmbrWindow, tmp_path: Path, clock: Clock
) -> None:
    _fixed(window, tmp_path)
    window.show()
    timer = window._recheck_timer
    window._round = ["queued"]
    window._request_recheck()
    assert not timer.isActive()
    window._round = []

    menu = QMenu(window)
    menu.addAction("x")
    menu.popup(window.mapToGlobal(QPoint(50, 50)))
    QApplication.processEvents()
    assert QApplication.activePopupWidget() is menu
    window._request_recheck()
    assert not timer.isActive()
    menu.close()


def _typing_a_name(window: EmbrWindow) -> QLineEdit:
    window.show()
    window.activateWindow()
    window.firmware_view.tab_group.button(1).click()
    QApplication.processEvents()
    name = window.firmware_view.params.findChild(QLineEdit)
    assert name is not None
    name.setFocus()
    QApplication.processEvents()
    name.setText("half-typ")
    assert window.firmware_view.params.editing()
    return name


def test_no_refresh_under_a_half_typed_parameter(
    window: EmbrWindow, tmp_path: Path, clock: Clock
) -> None:
    firmware_id, file = _fixed(window, tmp_path)
    name = _typing_a_name(window)
    file.write_bytes(b"\x01" * 96)  # the build changed meanwhile

    window._recheck_selected()
    assert window.firmware_view.params.findChild(QLineEdit) is name
    assert name.text() == "half-typ"
    shown = window._shown

    window._round = [firmware_id]
    window._round_changed = True
    window._next_in_round()
    assert window.firmware_view.params.findChild(QLineEdit) is name
    assert window._shown == shown  # the next check still finds the difference


def test_the_refresh_comes_once_the_field_is_left(
    window: EmbrWindow, tmp_path: Path, clock: Clock
) -> None:
    _, file = _fixed(window, tmp_path)
    name = _typing_a_name(window)
    name.setText("fw")  # nothing to rename when it loses focus
    file.write_bytes(b"\x01" * 96)
    window._recheck_selected()
    shown = window._shown

    window.log_panel.setFocus()
    QApplication.processEvents()
    assert not window.firmware_view.params.editing()
    window._recheck_selected()
    assert window._shown != shown
    assert window.firmware_view.params.findChild(QLineEdit) is not name
