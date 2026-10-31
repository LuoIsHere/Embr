"""Shared harness for the release suite (tests/release, "发布前全量测试").

The release suite is REAL end to end: the real Windows Qt platform (never offscreen), the
real EmbrWindow driven through its visible controls with QTest, real portable data
folders, real worker processes with the bundled esptool and real boards. The only
substitution is the Windows native file dialog: QFileDialog's static functions are replaced
by FileDialogs, which hands out paths the test queued (and checks they were all used).

Running (always as its own pytest invocation, never together with tests/ui, whose
conftest forces the offscreen platform):

    EMBR_RELEASE=1 uv run pytest tests/release

Environment (local machine configuration lives only here, never in tracked files):

    EMBR_RELEASE=1                 enables the suite (otherwise every test is skipped)
    EMBR_RELEASE_INTERACTIVE=0     operator steps skip at once (default: prompts are shown)
    EMBR_TEST_PORT_UART            ESP32-S3 through its CH343 UART bridge
    EMBR_TEST_PORT_USB             the same S3's native USB-Serial/JTAG (re-enumerates
                                   for about 1 s whenever the board is reset via UART)
    EMBR_TEST_PORT_P4              M5Stack Tab5 (ESP32-P4 v1.0, USB-Serial/JTAG)
    EMBR_TEST_IDF_PROJECT, EMBR_TEST_IDF_EXPORT, EMBR_TEST_PIO_PROJECT
                                   real-build tests (see env_path())

A missing variable skips only the tests that need it (env_value(), env_path(),
board_port()).

Overview of the API (details in each docstring):

* environment: env_value, env_path, board_port, interactive, REPO, SRC, FIXTURES, IDF, PIO
* Qt: qt_app (the one QApplication, real platform), pump, wait_until, slot errors are
  collected by SlotErrors (installed for every test by conftest.py)
* the real window: start_embr(root, language) replicates embr.main.run_gui() (writable
  check, instance lock, clear temp/, prepare, TMP/TEMP, QApplication, open_context,
  set_language, EmbrWindow shown on screen) and returns an EmbrApp; EmbrApp.close() closes
  it through its own close button and restores the process (TMP/TEMP, tempfile, language)
* finding by visible text: find_button, find_checkbox, find_label, all_texts, text_of
  (texts are given as the English source text; they are matched in English and in the
  current language through embr.i18n.tr, so one test body serves both languages)
* acting: click, set_checked, type_text, pick_combo, expect_menu / choose_menu (popup
  menus), expect_dialog (modal dialogs; DialogDriver fills fields by caption and presses
  OK / Cancel), sidebar_row / click_sidebar / sidebar_menu, job helpers
  (wait_job_finished, flash_button_text), log_text
* file dialogs: FileDialogs (queue_open, queue_open_many, queue_save, queue_directory,
  queue_cancel; assert_consumed)
* operator steps: operator_step(ready, action, ...) shows a topmost "get ready" question
  (Ready / Skip), then runs `start` and shows the action prompt (Done / Skip, optional
  timeout or automatic completion); EMBR_RELEASE_INTERACTIVE=0 skips without a dialog
* boards: board_port, port_present, wait_port, serial_after_reset
* processes (Win32): verified_tree / process_tree (only real descendants: the recorded
  parent, created after that parent, image python / pythonw / Embr; never a process
  whose recorded parent PID was reused), source_copy (a copy of src/ whose
  `python -m embr` uses <copy>/.run as its portable root, as a source run does),
  EmbrProcess (start, find its windows, read their texts through UI Automation, press a
  message box button, close with WM_CLOSE, terminate exactly its verified processes;
  never `taskkill /T`), Sentinels (empty TEMP / TMP / APPDATA / LOCALAPPDATA /
  USERPROFILE / working folders for a child), file snapshots, deny_access (icacls deny
  ACE on one folder, removed afterwards)
* fixture builds: idf_build_copy, pio_project_copy, hello_files
* screenshot: screenshot_all (conftest saves one for every failed test)
"""

from __future__ import annotations

import base64
import configparser
import contextlib
import ctypes
import ctypes.wintypes
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QCursor, QGuiApplication, QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
FIXTURES = REPO / "tests" / "fixtures"
IDF = FIXTURES / "idf"
PIO = FIXTURES / "pio"
DB_V1 = FIXTURES / "db" / "embr_v1.sqlite"

RELEASE_VAR = "EMBR_RELEASE"
INTERACTIVE_VAR = "EMBR_RELEASE_INTERACTIVE"
REAL_PLATFORM = "windows"
JOB_TIMEOUT = 300.0
"""Generous limit for a flash / read / erase job."""


# ---- environment ------------------------------------------------------------------------


def release_enabled() -> bool:
    return os.environ.get(RELEASE_VAR, "").strip() == "1"


def interactive() -> bool:
    """Operator steps are shown unless EMBR_RELEASE_INTERACTIVE=0."""
    return os.environ.get(INTERACTIVE_VAR, "1").strip() != "0"


def env_value(name: str) -> str:
    """The variable's value; pytest.skip with the reason when it is unset or empty."""
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} not set")
    return value


def env_path(name: str, *, kind: str = "dir") -> Path:
    """A path from the environment. kind "dir" or "file": skip when unset, fail when it
    does not exist (a configured but wrong path is a setup error, not a reason to skip)."""
    path = Path(env_value(name))
    ok = path.is_dir() if kind == "dir" else path.is_file()
    if not ok:
        pytest.fail(f"{name}={path} is not an existing {kind}")
    return path


def board_port(name: str) -> str:
    """The COM port from `name` (EMBR_TEST_PORT_UART / _USB / _P4): skip when unset, fail
    when the port is not present now."""
    device = env_value(name)
    if not port_present(device):
        pytest.fail(f"{name}={device} is not connected (ports: {', '.join(port_names())})")
    return device


# ---- Qt ---------------------------------------------------------------------------------


def qt_app() -> QApplication:
    """The process's QApplication on the real Windows platform (created on first use).
    Fails when an offscreen application already exists (tests/release must run as its own
    pytest invocation)."""
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        del os.environ["QT_QPA_PLATFORM"]
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([sys.argv[0]])
    if QGuiApplication.platformName() != REAL_PLATFORM:
        pytest.fail(
            f"Qt platform is {QGuiApplication.platformName()!r}, not {REAL_PLATFORM!r}: run "
            "tests/release as its own invocation (EMBR_RELEASE=1 uv run pytest tests/release)"
        )
    return app


def pump(ms: int = 0) -> None:
    """Run the event loop for `ms` milliseconds (at least one pass), including deferred
    deletes (widgets the window replaced must not linger as "visible")."""
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    if ms > 0:
        QTest.qWait(ms)


def wait_until[T](
    condition: Callable[[], T | bool | None],
    timeout: float = 10.0,
    *,
    message: str | Callable[[], str] = "condition not met",
    interval_ms: int = 30,
) -> T:
    """Keep the event loop running (job results arrive as Qt signals) until `condition()`
    returns something truthy, and return it. pytest.fail after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return cast(T, value)
        if time.monotonic() > deadline:
            text = message() if callable(message) else message
            pytest.fail(f"timed out after {timeout:.0f} s: {text}")
        QTest.qWait(interval_ms)


class SlotErrors:
    """Collects exceptions that reach sys.excepthook (PySide reports exceptions raised in
    slots and timers there); conftest.py fails a test that leaves any."""

    def __init__(self) -> None:
        self.errors: list[BaseException] = []
        self._saved: Callable[..., Any] | None = None

    def install(self) -> None:
        self._saved = sys.excepthook

        def hook(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
            self.errors.append(value)
            if self._saved is not None:
                self._saved(kind, value, tb)

        sys.excepthook = hook

    def uninstall(self) -> None:
        if self._saved is not None:
            sys.excepthook = self._saved
            self._saved = None


# ---- texts ------------------------------------------------------------------------------


def _plain(text: str) -> str:
    """Display form: mnemonic "&" removed ("&&" stays "&"), a menu's "\\tF2" dropped, rich
    text tags removed, whitespace collapsed."""
    text = text.split("\t", 1)[0]
    text = text.replace("&&", "\0").replace("&", "").replace("\0", "&")
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ")
    return " ".join(text.split())


def variants(text: str) -> set[str]:
    """`text` as given and in the current language (embr.i18n.tr), in display form."""
    from embr.i18n import tr

    return {_plain(text), _plain(tr(text))}


def text_of(widget: QWidget) -> str:
    """The visible text of a widget: a button's text (or its accessible name when its
    face is drawn by child labels, as on the Flash button), a label's text (full text of
    an elided label), a line edit's text."""
    from embr.ui.widgets import ElidedLabel

    if isinstance(widget, ElidedLabel):
        return widget.full_text()
    if isinstance(widget, QAbstractButton):
        return widget.text() or widget.accessibleName()
    if isinstance(widget, QLabel):
        return widget.text()
    if isinstance(widget, QLineEdit):
        return widget.text()
    if isinstance(widget, QPlainTextEdit):
        return widget.toPlainText()
    return widget.accessibleName()


def matches(widget: QWidget, text: str, *, contains: bool = False) -> bool:
    shown = _plain(text_of(widget))
    wanted = variants(text)
    if contains:
        return any(w and w in shown for w in wanted)
    if isinstance(widget, QAbstractButton) and not widget.text():
        shown = shown.split(" (", 1)[0]  # the Flash button: "Flash (Ctrl+Enter)"
    return shown in wanted


def find_all[W: QWidget](
    root: QWidget, kind: type[W], text: str | None = None, **opts: bool
) -> list[W]:
    """Visible widgets of `kind` under `root` (root included), optionally matching `text`
    (see matches(); contains=True for a substring)."""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    found: list[W] = [root] if isinstance(root, kind) else []
    found += root.findChildren(kind)
    shown = [w for w in found if w.isVisible()]
    if text is None:
        return shown
    return [w for w in shown if matches(w, text, contains=opts.get("contains", False))]


def find_one[W: QWidget](root: QWidget, kind: type[W], text: str, **opts: bool) -> W:
    found = find_all(root, kind, text, **opts)
    if len(found) != 1:
        visible = sorted({_plain(text_of(w)) for w in find_all(root, kind)})
        pytest.fail(f"{len(found)} visible {kind.__name__} for {text!r}; visible: {visible}")
    return found[0]


def find_button(root: QWidget, text: str, **opts: bool) -> QAbstractButton:
    return find_one(root, QAbstractButton, text, **opts)


def find_checkbox(root: QWidget, text: str, **opts: bool) -> QCheckBox:
    return find_one(root, QCheckBox, text, **opts)


def find_label(root: QWidget, text: str, **opts: bool) -> QLabel:
    return find_one(root, QLabel, text, **opts)


def all_texts(root: QWidget) -> list[str]:
    """Display texts of every visible label, button and check box under `root`."""
    texts: list[str] = []
    for kind in (QLabel, QAbstractButton):
        texts += [_plain(text_of(w)) for w in find_all(root, kind)]
    return [t for t in texts if t]


# ---- acting -----------------------------------------------------------------------------


def _usable(widget: QWidget) -> None:
    if not widget.isVisible():
        pytest.fail(f"{type(widget).__name__} {text_of(widget)!r} is not visible")
    if not widget.isEnabled():
        pytest.fail(f"{type(widget).__name__} {text_of(widget)!r} is disabled")


def click(widget: QWidget, pos: QPoint | None = None, *, button: str = "left") -> None:
    """Mouse click (press and release) on a visible, enabled widget, at its centre or
    `pos`. A click that opens a modal dialog returns once that dialog is closed: register
    expect_dialog() first."""
    _usable(widget)
    mouse = Qt.MouseButton.LeftButton if button == "left" else Qt.MouseButton.RightButton
    at = pos if pos is not None else widget.rect().center()
    QTest.mouseClick(widget, mouse, Qt.KeyboardModifier.NoModifier, at)
    pump()


def click_button(root: QWidget, text: str, **opts: bool) -> None:
    click(find_button(root, text, **opts))


def set_checked(box: QCheckBox, on: bool) -> None:
    """Click the check box's indicator when its state differs from `on`."""
    if box.isChecked() == on:
        return
    option = QStyleOptionButton()
    option.initFrom(box)
    indicator = box.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, box)
    click(box, indicator.center())
    if box.isChecked() != on:
        pytest.fail(f"check box {text_of(box)!r} did not toggle")


def type_text(edit: QLineEdit, text: str, *, replace: bool = True, finish: str = "") -> None:
    """Type into a line edit: ASCII as key events, other text (Chinese) as an input-method
    commit, as a Windows IME delivers it (QTest.keyClicks hangs on such characters).
    replace: select all first (Ctrl+A). finish: "" (nothing), "return" (Enter:
    editingFinished, and in a dialog also its default button) or "tab"."""
    _usable(edit)
    if replace:
        QTest.keyClick(edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(edit, Qt.Key.Key_Delete)
    for chunk in re.findall(r"[\x20-\x7e]+|[^\x20-\x7e]+", text):
        if chunk.isascii():
            QTest.keyClicks(edit, chunk)
        else:
            commit = QInputMethodEvent("", [])
            commit.setCommitString(chunk)
            QCoreApplication.sendEvent(edit, commit)
    if finish == "return":
        QTest.keyClick(edit, Qt.Key.Key_Return)
    elif finish == "tab":
        QTest.keyClick(edit, Qt.Key.Key_Tab)
    pump()
    if replace and finish != "return" and edit.text() != text:
        pytest.fail(f"typed {text!r}, line edit shows {edit.text()!r}")


def pick_combo(combo: QComboBox, text: str, *, contains: bool = False) -> None:
    """Select the item showing `text`: a click opens the combo box's list, then the
    keyboard (Up / Down, Home when stuck, Return) chooses it, so the combo box emits
    `activated` once. The keyboard is used in the list because the real mouse moving over
    an open list (the operator using the machine) changes its current item and can swallow
    a synthetic click; the real cursor is never moved. Returns once the list is closed and
    the combo box shows the item."""
    _usable(combo)
    wanted = variants(text)
    texts = [_plain(combo.itemText(i)) for i in range(combo.count())]
    hits = [
        i for i, t in enumerate(texts) if (any(w in t for w in wanted) if contains else t in wanted)
    ]
    if len(hits) != 1:
        pytest.fail(f"{len(hits)} items for {text!r} in combo box; items: {texts}")
    index = hits[0]
    if combo.currentIndex() == index:
        return
    view = combo.view()

    def selected() -> bool:
        return not view.isVisible() and combo.currentIndex() == index

    def shows() -> str:
        return f"combo box shows {combo.currentText()!r}, not {text!r}; items: {texts}"

    click(combo)
    wait_until(lambda: view.isVisible() or selected(), 5, message="combo box list did not open")
    for _ in range(2 * combo.count() + 2):
        if not view.isVisible():
            break
        row = view.currentIndex().row()
        if row == index:
            QTest.keyClick(view, Qt.Key.Key_Return)
            break
        QTest.keyClick(view, Qt.Key.Key_Down if row < index else Qt.Key.Key_Up)
        if view.currentIndex().row() == row:  # stuck (no current item, or a separator)
            QTest.keyClick(view, Qt.Key.Key_Home)
    else:
        QTest.keyClick(view, Qt.Key.Key_Escape)
        pytest.fail(f"item {text!r} cannot be reached with the keyboard; items: {texts}")
    wait_until(
        selected,
        5,
        message=lambda: "combo box list did not close" if view.isVisible() else shows(),
    )
    pump()


# ---- watchers: things that appear while a click is being handled -----------------------


class _Watch:
    """Polls `find` every 30 ms (also inside nested event loops, e.g. a modal dialog's
    exec()) and runs `act` once on what it finds. Errors in `act` are kept and raised by
    close()."""

    def __init__(
        self,
        find: Callable[[], QWidget | None],
        act: Callable[[QWidget], None],
        on_error: Callable[[QWidget], None],
        what: str,
        timeout: float,
    ) -> None:
        self._find, self._act, self._on_error, self.what = find, act, on_error, what
        self.timeout = timeout
        self.done = False
        self.error: BaseException | None = None
        self._timer = QTimer()
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll)
        self._started = time.monotonic()
        self._busy = False
        self._timer.start()

    def _poll(self) -> None:
        if self.done or self._busy:
            return
        if time.monotonic() - self._started > self.timeout:
            self.done = True
            self._timer.stop()
            self.error = AssertionError(f"{self.what} did not appear within {self.timeout:.0f} s")
            return
        target = self._find()
        if target is None:
            return
        self._busy = True
        self._timer.stop()
        try:
            self._act(target)
        except BaseException as exc:  # pytest.fail / skip inside a nested event loop
            self.error = exc
            with contextlib.suppress(RuntimeError):
                popup = QApplication.activePopupWidget()
                if popup is not None:
                    popup.close()
                self._on_error(target)
        finally:
            self.done = True
            self._busy = False

    def close(self) -> None:
        wait_until(lambda: self.done, self.timeout + 5, message=f"{self.what} not handled")
        self._timer.stop()
        if self.error is not None:
            raise self.error


@contextlib.contextmanager
def expect_dialog(
    handler: Callable[[DialogDriver], None],
    *,
    title: str | None = None,
    name: str | None = None,
    timeout: float = 30.0,
) -> Generator[None]:
    """Run `handler` on the modal dialog (title `title`, English source text, and / or
    object name `name`) that the code inside the `with` block opens:

        with expect_dialog(lambda d: (d.type("Name", "demo"), d.ok()), title="New project"):
            choose_menu_from(window.sidebar_panel.new_button, "New project")

    The handler must close the dialog (ok(), cancel(), press()); if it raises, the dialog
    is rejected so the test does not hang, and the error is raised at the end of the
    block. Fails when no such dialog appeared."""
    handled: set[int] = set()

    def find() -> QWidget | None:
        for widget in reversed(_dialog_stack()):
            if id(widget) in handled:
                continue
            if title is not None and _plain(widget.windowTitle()) not in variants(title):
                continue
            if name is None or widget.objectName() == name:
                handled.add(id(widget))
                return widget
        return None

    def act(widget: QWidget) -> None:
        handler(DialogDriver(cast(QDialog, widget)))

    def on_error(widget: QWidget) -> None:
        if widget.isVisible():
            cast(QDialog, widget).reject()

    watch = _Watch(find, act, on_error, f"dialog {title or name or '(any)'}", timeout)
    try:
        yield
    except BaseException:
        watch.done = True
        raise
    watch.close()


def _dialog_stack() -> list[QDialog]:
    dialogs = [
        w for w in QApplication.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()
    ]
    modal = QApplication.activeModalWidget()
    if isinstance(modal, QDialog) and modal in dialogs:
        dialogs.remove(modal)
        dialogs.append(modal)
    return dialogs


@contextlib.contextmanager
def expect_menu(*path: str, timeout: float = 10.0) -> Generator[None]:
    """Choose `path` (an action, or submenu titles then an action; English source texts)
    in the popup menu that the code inside the block opens, whether the menu blocks
    (QMenu.exec) or not (QMenu.popup)."""

    def find() -> QWidget | None:
        popup = QApplication.activePopupWidget()
        return popup if isinstance(popup, QMenu) and popup.isVisible() else None

    def act(widget: QWidget) -> None:
        menu = cast(QMenu, widget)
        for depth, text in enumerate(path):
            action = _menu_action(menu, text)
            last = depth == len(path) - 1
            if not action.isEnabled():
                pytest.fail(f"menu item {text!r} is disabled")
            QTest.mouseClick(
                menu,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                menu.actionGeometry(action).center(),
            )
            if not last:
                sub = action.menu()
                if not isinstance(sub, QMenu):
                    pytest.fail(f"menu item {text!r} has no submenu")
                wait_until(sub.isVisible, 5, message=f"submenu {text!r} did not open")
                menu = sub

    def on_error(widget: QWidget) -> None:
        cast(QMenu, widget).close()

    watch = _Watch(find, act, on_error, f"menu {' > '.join(path)}", timeout)
    try:
        yield
    except BaseException:
        watch.done = True
        raise
    watch.close()


def menu_texts(menu: QMenu) -> list[str]:
    return [_plain(a.text()) for a in menu.actions() if not a.isSeparator() and a.isVisible()]


def _menu_action(menu: QMenu, text: str) -> Any:
    wanted = variants(text)
    hits = [a for a in menu.actions() if a.isVisible() and _plain(a.text()) in wanted]
    if len(hits) != 1:
        pytest.fail(f"{len(hits)} menu items for {text!r}; menu: {menu_texts(menu)}")
    return hits[0]


def choose_menu_from(button: QWidget, *path: str) -> None:
    """Click `button`, which pops up a menu, and choose `path` in it."""
    with expect_menu(*path):
        click(button)


class DialogDriver:
    """Acts on one dialog (embr.ui.dialogs.Dialog or any QDialog) through its widgets.
    Fields are found by the caption above them (Dialog.field / Dialog.row), buttons and
    check boxes by their text; all texts are English source texts (see variants())."""

    def __init__(self, dialog: QDialog) -> None:
        self.dialog = dialog

    @property
    def title(self) -> str:
        return _plain(self.dialog.windowTitle())

    def texts(self) -> list[str]:
        return all_texts(self.dialog)

    def text(self) -> str:
        """Every visible text of the dialog, one per line."""
        return "\n".join(self.texts())

    def wait_text(self, text: str, timeout: float = 15.0) -> None:
        """Wait until some visible text contains `text` (e.g. a preview read in the
        background)."""
        wanted = variants(text)
        wait_until(
            lambda: any(w in t for t in self.texts() for w in wanted),
            timeout,
            message=lambda: f"{text!r} not shown in dialog {self.title!r}: {self.texts()}",
        )

    def field(self, caption: str) -> QWidget:
        """The widget below (or right of) the caption label `caption`."""
        layout = self.dialog.layout()
        found = _captioned(layout, variants(caption)) if layout is not None else None
        if found is None:
            pytest.fail(f"no field {caption!r} in dialog {self.title!r}: {self.texts()}")
        return found

    def line_edit(self, caption: str) -> QLineEdit:
        widget = self.field(caption)
        if not isinstance(widget, QLineEdit):
            pytest.fail(f"field {caption!r} is a {type(widget).__name__}, not a line edit")
        return widget

    def combo(self, caption: str) -> QComboBox:
        widget = self.field(caption)
        if not isinstance(widget, QComboBox):
            pytest.fail(f"field {caption!r} is a {type(widget).__name__}, not a combo box")
        return widget

    def type(self, caption: str, text: str, *, finish: str = "") -> None:
        type_text(self.line_edit(caption), text, finish=finish)

    def pick(self, caption: str, item: str, *, contains: bool = False) -> None:
        pick_combo(self.combo(caption), item, contains=contains)

    def checkbox(self, text: str, *, contains: bool = False) -> QCheckBox:
        return find_checkbox(self.dialog, text, contains=contains)

    def checkboxes(self) -> list[QCheckBox]:
        return find_all(self.dialog, QCheckBox)

    def check(self, text: str, on: bool = True, *, contains: bool = False) -> None:
        set_checked(self.checkbox(text, contains=contains), on)

    def button(self, text: str) -> QAbstractButton:
        return find_button(self.dialog, text)

    def press(self, text: str) -> None:
        """Click a button by its text (closing the dialog or not)."""
        click(self.button(text))

    def ok(self) -> None:
        """Click the accept button (Dialog.ok, or the button with the accept role)."""
        click(self._ok_button())

    def cancel(self) -> None:
        cancel = getattr(self.dialog, "cancel", None)
        if isinstance(cancel, QPushButton) and cancel.isVisible():
            click(cancel)
        else:
            self.dialog.reject()

    def ok_enabled(self) -> bool:
        return self._ok_button().isEnabled()

    def _ok_button(self) -> QPushButton:
        ok = getattr(self.dialog, "ok", None)
        if isinstance(ok, QPushButton):
            return ok
        for candidate in find_all(self.dialog, QPushButton):
            if candidate.isDefault() or _plain(candidate.text()) in variants("OK"):
                return candidate
        pytest.fail(f"no OK button in dialog {self.title!r}")


def _captioned(layout: QLayout, captions: set[str]) -> QWidget | None:
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item is None:
            continue
        widget = item.widget()
        if isinstance(widget, QLabel) and _plain(widget.text()) in captions:
            following = layout.itemAt(index + 1) if index + 1 < layout.count() else None
            target = _first_input(following) if following is not None else None
            if target is not None:
                return target
        inner = item.layout()
        if inner is None and widget is not None:
            inner = widget.layout()
        if inner is not None:
            found = _captioned(inner, captions)
            if found is not None:
                return found
    return None


def _first_input(item: QLayoutItem) -> QWidget | None:
    widget = item.widget()
    if widget is not None:
        return widget  # an input, or a label as a read-only field (Settings > folder)
    inner = item.layout()
    if inner is None:
        return None
    for index in range(inner.count()):
        child = inner.itemAt(index)
        found = _first_input(child) if child is not None else None
        if found is not None:
            return found
    return None


# ---- the real window --------------------------------------------------------------------


@dataclass
class EmbrApp:
    """A running EmbrWindow on a portable data folder, started by start_embr()."""

    paths: Any
    """embr.core.storage.paths.AppPaths"""
    context: Any
    """embr.app.context.AppContext"""
    window: Any
    """embr.ui.window.EmbrWindow"""
    lock: Any
    """embr.core.storage.lock.InstanceLock"""
    _saved_env: dict[str, str | None] = field(default_factory=lambda: dict[str, str | None]())
    _saved_tempdir: str | None = None
    closed: bool = False

    def close(self) -> None:
        """Close the window with the close button in its title bar, as a user does;
        then what run_gui() does after app.exec(): context.close() and the lock released.
        The process is restored (TMP / TEMP, tempfile.tempdir, English)."""
        if self.closed:
            return
        self.closed = True
        try:
            if self.window.isVisible():
                click(self.window.topbar.close_button)
                wait_until(lambda: not self.window.isVisible(), 10, message="window not closed")
        finally:
            try:
                self.context.close()
            finally:
                self.lock.release()
                self.window.deleteLater()
                pump(50)
                _restore_process(self._saved_env, self._saved_tempdir)

    def log(self) -> str:
        return log_text(self.window)


def start_embr(root: Path, language: Any = None, *, show: bool = True) -> EmbrApp:
    """Start Embr on the portable root `root` in this process, as embr.main.run_gui()
    does: writable check, instance lock, clear temp/, prepare(), TMP and TEMP pointed at
    temp/, the QApplication (real platform), open_context() (migrations), the language
    (embr.i18n.set_language(effective(stored, system))), EmbrWindow shown. `language`
    (embr.i18n.Language) is stored in settings.ini first, as the Language menu does, so
    the stored choice and the shown language agree; None keeps what is stored.
    Fonts and the theme are applied by EmbrWindow itself (apply_theme)."""
    from PySide6.QtCore import QLocale

    from embr.app.context import open_context
    from embr.core.storage.lock import InstanceLock
    from embr.core.storage.paths import AppPaths, clear_temp, is_writable, prepare
    from embr.core.storage.settings import Settings
    from embr.i18n import SETTINGS_KEY, effective, set_language
    from embr.ui.adapters import settings as ui_settings
    from embr.ui.adapters.settings import stored_language
    from embr.ui.window import EmbrWindow

    paths = AppPaths(root)
    if not is_writable(paths.root):
        pytest.fail(f"{root} is not writable")
    paths.temp.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(paths.lock_file)
    if not lock.acquire():
        pytest.fail(f"{root} is in use by another Embr")
    saved_env = {k: os.environ.get(k) for k in ("TMP", "TEMP")}
    saved_tempdir = tempfile.tempdir
    try:
        clear_temp(paths)
        prepare(paths)
        os.environ["TMP"] = os.environ["TEMP"] = str(paths.temp)
        app = qt_app()
        app.setApplicationName("Embr")
        if language is not None:
            settings = Settings(paths.settings, paths.backup)
            ui_settings.save_choice(settings, SETTINGS_KEY, str(language.value))
            settings.save()
        context = open_context(paths)
    except BaseException:
        lock.release()
        _restore_process(saved_env, saved_tempdir)
        raise
    try:
        set_language(effective(stored_language(context.settings), QLocale.system().uiLanguages()))
        window = EmbrWindow(context)
    except BaseException:
        context.close()
        lock.release()
        _restore_process(saved_env, saved_tempdir)
        raise
    running = EmbrApp(paths, context, window, lock, saved_env, saved_tempdir)
    if show:
        window.show()
        window.raise_()
        window.activateWindow()
        QTest.qWaitForWindowExposed(window, 10000)
        pump(100)
    return running


def _restore_process(saved_env: dict[str, str | None], saved_tempdir: str | None) -> None:
    from embr.i18n import Language, set_language

    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    tempfile.tempdir = saved_tempdir
    set_language(Language.ENGLISH)


def log_text(window: Any) -> str:
    """The log panel's text."""
    return cast(str, window.log_panel.log.toPlainText())


def wait_log(window: Any, text: str, timeout: float = 10.0) -> None:
    wait_until(
        lambda: text in log_text(window),
        timeout,
        message=lambda: f"{text!r} not in the log:\n{log_text(window)[-3000:]}",
    )


def flash_button_text(window: Any) -> str:
    """ "Flash" or "Cancel" (the button's face is drawn by labels; its accessible name is
    "<text> (<keys>)")."""
    return str(window.connection.flash.accessibleName()).split(" (", 1)[0]


def job_running(window: Any) -> bool:
    from embr.i18n import tr

    return flash_button_text(window) == tr("Cancel")


def wait_job_finished(window: Any, timeout: float = JOB_TIMEOUT) -> str:
    """Wait until the running job ended (the Flash button no longer says Cancel); returns
    the progress text (e.g. "Done", "Identified", "Failed", in the current language)."""
    wait_until(
        lambda: not job_running(window),
        timeout,
        message=lambda: f"job still running:\n{log_text(window)[-3000:]}",
        interval_ms=100,
    )
    pump(100)
    return cast(str, window.connection.progress_text.text())


def check_texts(window: Any) -> list[str]:
    """Texts of the Checks list in the right column."""
    from embr.ui.connection import ConnectionPanel

    panel = cast(ConnectionPanel, window.connection)
    texts: list[str] = []
    for index in range(panel.checks.count()):
        item = panel.checks.itemAt(index)
        if item is None:
            continue
        own = item.widget()
        inner = item.layout() or (own.layout() if own is not None else None)
        widgets: list[QWidget] = []
        if own is not None:
            widgets.append(own)
        if inner is not None:
            widgets += _layout_widgets(inner)
        texts += [_plain(text_of(w)) for w in widgets if isinstance(w, QLabel)]
    return [t for t in texts if t]


def _layout_widgets(layout: QLayout) -> list[QWidget]:
    found: list[QWidget] = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            found.append(widget)
            found += widget.findChildren(QWidget)
        inner = item.layout()
        if inner is not None:
            found += _layout_widgets(inner)
    return found


def same_text(shown: str, english: str) -> bool:
    """`shown` is `english` in English or in the current language."""
    return _plain(shown) in variants(english)


def identify(window: Any, port: str, timeout: float = 120.0) -> str:
    """Pick `port` in the Port list, click Connect & identify, wait for the result (the
    progress text in the current language; compare with same_text(result, "Identified"))."""
    pick_combo(window.connection.port, port, contains=True)
    click(window.connection.identify)
    return wait_job_finished(window, timeout)


# ---- the sidebar ------------------------------------------------------------------------


def sidebar_rows(window: Any) -> list[tuple[QTreeWidgetItem, Any, str]]:
    """(item, embr.ui.sidebar.Node, name shown) of every row of the sidebar tree."""
    from embr.ui.widgets import ElidedLabel

    tree = window.sidebar_panel.tree
    rows: list[tuple[QTreeWidgetItem, Any, str]] = []
    for index in range(tree.topLevelItemCount()):
        item = cast(QTreeWidgetItem, tree.topLevelItem(index))
        node = tree.node(item)
        widget = cast(QWidget | None, tree.itemWidget(item, 0))
        names: list[ElidedLabel] = widget.findChildren(ElidedLabel) if widget is not None else []
        rows.append((item, node, names[0].full_text() if names else ""))
    return rows


def sidebar_row(window: Any, name: str, kind: str = "firmware") -> QTreeWidgetItem:
    """The tree row of kind "firmware", "project" or "group" showing `name`."""
    hits = [i for i, n, shown in sidebar_rows(window) if n and n.kind == kind and shown == name]
    if len(hits) != 1:
        shown = [(n.kind, s) for _, n, s in sidebar_rows(window) if n]
        pytest.fail(f"{len(hits)} sidebar {kind} rows named {name!r}; rows: {shown}")
    return hits[0]


def sidebar_names(window: Any, kind: str = "firmware") -> list[str]:
    return [shown for _, n, shown in sidebar_rows(window) if n and n.kind == kind]


def standalone_names(window: Any) -> list[str]:
    """Firmware rows below the STANDALONE FIRMWARE header."""
    rows = sidebar_rows(window)
    start = next(
        (i for i, (_, n, _) in enumerate(rows) if n and n.kind == "header" and n.drop),
        len(rows),
    )
    return [shown for _, n, shown in rows[start:] if n and n.kind == "firmware"]


def assert_no_dialog(title: str, seconds: float = 1.5) -> None:
    """Keep the event loop running for `seconds`; fail if a dialog titled `title` shows."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for dialog in _dialog_stack():
            if _plain(dialog.windowTitle()) in variants(title):
                dialog.reject()
                pytest.fail(f"dialog {title!r} shown again")
        QTest.qWait(50)


def click_sidebar(window: Any, name: str, kind: str = "firmware") -> None:
    """Click a sidebar row (on its name, away from a project's fold caret). The real mouse
    cursor is moved there as well: the sidebar reads the cursor position to tell a click
    on the caret from a click on the name; it is put back right after the click."""
    tree = window.sidebar_panel.tree
    item = sidebar_row(window, name, kind)
    tree.scrollToItem(item)
    pump()
    rect = tree.visualItemRect(item)
    at = QPoint(rect.left() + min(rect.width() // 2, 60), rect.center().y())
    saved = QCursor.pos()
    QCursor.setPos(tree.viewport().mapToGlobal(at))
    try:
        click(tree.viewport(), at)
    finally:
        QCursor.setPos(saved)


def sidebar_menu(window: Any, name: str, *path: str) -> None:
    """Right-click a firmware row (a context-menu event, as Windows sends for a right
    click) and choose `path` in the menu that opens."""
    tree = window.sidebar_panel.tree
    item = sidebar_row(window, name)
    tree.scrollToItem(item)
    pump()
    at = tree.visualItemRect(item).center()
    with expect_menu(*path):
        context_menu(tree.viewport(), at)


def context_menu(widget: QWidget, at: QPoint) -> None:
    """Deliver a mouse context-menu event at `at` (widget coordinates)."""
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, at, widget.mapToGlobal(at))
    QCoreApplication.sendEvent(widget, event)
    pump()


def firmware_menu(window: Any, *path: str) -> None:
    """The workspace header's ⋯ menu of the selected firmware."""
    choose_menu_from(window.header.menu_button, *path)


def new_menu(window: Any, *path: str) -> None:
    """The sidebar's New menu (its plus button)."""
    choose_menu_from(window.sidebar_panel.new_button, *path)


def window_menu(window: Any, *path: str) -> None:
    """The title bar's ≡ menu (Settings, Clean archive, Theme, Language, About)."""
    choose_menu_from(window.topbar.menu_button, *path)


# ---- file dialogs -----------------------------------------------------------------------


@dataclass
class FileDialogCall:
    kind: str
    caption: str
    directory: str


class FileDialogs:
    """Replaces QFileDialog.getOpenFileName / getOpenFileNames / getSaveFileName /
    getExistingDirectory (the Windows native dialogs) while installed. Each call takes the
    next queued answer, which must be of the same kind; a call with nothing queued fails
    the test (the dialog then answers "cancelled"). assert_consumed() checks every queued
    answer was used. Use as a context manager or install() / uninstall()."""

    KINDS = ("open", "open_many", "save", "directory")

    def __init__(self) -> None:
        self.queue: list[tuple[str, object]] = []
        self.calls: list[FileDialogCall] = []
        self.errors: list[str] = []
        self._saved: dict[str, object] = {}

    def queue_open(self, path: Path) -> None:
        self.queue.append(("open", path))

    def queue_open_many(self, paths: Sequence[Path]) -> None:
        self.queue.append(("open_many", list(paths)))

    def queue_save(self, path: Path) -> None:
        self.queue.append(("save", path))

    def queue_directory(self, path: Path) -> None:
        self.queue.append(("directory", path))

    def queue_cancel(self, kind: str) -> None:
        self.queue.append((kind, None))

    def _next(self, kind: str, caption: str, directory: str) -> object:
        self.calls.append(FileDialogCall(kind, caption, directory))
        if not self.queue:
            self.errors.append(f"unexpected {kind} file dialog {caption!r}")
            return None
        queued_kind, value = self.queue.pop(0)
        if queued_kind != kind:
            self.errors.append(f"{kind} file dialog {caption!r}, but {queued_kind} was queued")
            return None
        return value

    def install(self) -> None:
        if self._saved:
            return
        for name in (
            "getOpenFileName",
            "getOpenFileNames",
            "getSaveFileName",
            "getExistingDirectory",
        ):
            self._saved[name] = QFileDialog.__dict__[name]

        def args(a: tuple[object, ...], k: dict[str, object]) -> tuple[str, str]:
            caption = str(k.get("caption", a[1] if len(a) > 1 else ""))
            directory = str(k.get("dir", a[2] if len(a) > 2 else ""))
            return caption, directory

        def open_one(*a: object, **k: object) -> tuple[str, str]:
            value = self._next("open", *args(a, k))
            return (str(value), "") if isinstance(value, Path) else ("", "")

        def open_many(*a: object, **k: object) -> tuple[list[str], str]:
            value = self._next("open_many", *args(a, k))
            paths = cast(list[Path], value) if isinstance(value, list) else []
            return [str(p) for p in paths], ""

        def save(*a: object, **k: object) -> tuple[str, str]:
            value = self._next("save", *args(a, k))
            return (str(value), "") if isinstance(value, Path) else ("", "")

        def directory(*a: object, **k: object) -> str:
            value = self._next("directory", *args(a, k))
            return str(value) if isinstance(value, Path) else ""

        setattr(QFileDialog, "getOpenFileName", staticmethod(open_one))  # noqa: B010
        setattr(QFileDialog, "getOpenFileNames", staticmethod(open_many))  # noqa: B010
        setattr(QFileDialog, "getSaveFileName", staticmethod(save))  # noqa: B010
        setattr(QFileDialog, "getExistingDirectory", staticmethod(directory))  # noqa: B010

    def uninstall(self) -> None:
        for name, original in self._saved.items():
            setattr(QFileDialog, name, original)
        self._saved.clear()

    def assert_consumed(self) -> None:
        problems = [*self.errors]
        if self.queue:
            problems.append(f"queued file dialog answers not used: {self.queue}")
        if problems:
            pytest.fail("; ".join(problems))

    def __enter__(self) -> FileDialogs:
        self.install()
        return self

    def __exit__(self, *exc: object) -> None:
        self.uninstall()


# ---- operator steps ---------------------------------------------------------------------

READY_TEXT = "准备好了 (Ready)"
SKIP_TEXT = "跳过 (Skip)"
DONE_TEXT = "完成 (Done)"
READY_NAME = "release-operator-ready"
ACTION_NAME = "release-operator-action"


def _operator_dialog(
    name: str, heading: str, body: str, test_name: str, buttons: tuple[str, str]
) -> tuple[QDialog, QPushButton, QPushButton]:
    dialog = QDialog()
    dialog.setObjectName(name)
    dialog.setWindowTitle("Embr 发布测试 · 操作员")
    dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    dialog.setModal(True)
    dialog.setMinimumWidth(520)
    layout = QVBoxLayout(dialog)
    title = QLabel(heading)
    title.setStyleSheet("font-size: 16px; font-weight: 600;")
    text = QLabel(body)
    text.setWordWrap(True)
    text.setStyleSheet("font-size: 14px;")
    test = QLabel(f"测试 (test): {test_name}")
    test.setStyleSheet("color: gray;")
    test.setWordWrap(True)
    accept = QPushButton(buttons[0])
    skip = QPushButton(buttons[1])
    accept.setObjectName("accept")
    skip.setObjectName("skip")
    accept.clicked.connect(dialog.accept)
    skip.clicked.connect(dialog.reject)
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(skip)
    row.addWidget(accept)
    for widget in (title, text, test):
        layout.addWidget(widget)
    layout.addLayout(row)
    return dialog, accept, skip


def _show_operator_dialog(
    dialog: QDialog, timeout: float | None, done_when: Callable[[], bool] | None
) -> str:
    """ "accepted", "skipped" or "timeout"."""
    outcome = ["skipped"]
    started = time.monotonic()
    timer = QTimer()
    timer.setInterval(100)

    def tick() -> None:
        if done_when is not None and done_when():
            outcome[0] = "accepted"
            dialog.done(QDialog.DialogCode.Accepted)
        elif timeout is not None and time.monotonic() - started > timeout:
            outcome[0] = "timeout"
            dialog.done(QDialog.DialogCode.Rejected)

    timer.timeout.connect(tick)
    timer.start()
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    try:
        code = dialog.exec()
    finally:
        timer.stop()
    if code == QDialog.DialogCode.Accepted:
        return "accepted"
    return outcome[0]


def operator_step(
    ready: str,
    action: str,
    *,
    start: Callable[[], None] | None = None,
    done_when: Callable[[], bool] | None = None,
    timeout: float | None = None,
    test_name: str | None = None,
) -> None:
    """A step only a person can do (plug or unplug a cable, hold a button). In order:

    1. a topmost "get ready" question saying what is about to be asked (`ready`: which
       board, cable or button, what will happen), buttons Ready / Skip;
    2. after Ready, `start()` runs (e.g. it starts the flash job);
    3. a topmost action prompt (`action`), buttons Done / Skip; it also closes by itself
       when `done_when()` turns true (polled every 100 ms) and gives up after `timeout`
       seconds.

    The texts are shown as given (write them in Chinese), with the test's name for
    reference. Skip at either dialog, or the timeout, → pytest.skip with the reason;
    EMBR_RELEASE_INTERACTIVE=0 → pytest.skip at once, no dialog. The test verifies the
    result itself afterwards."""
    name = test_name or os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0] or "?"
    if not interactive():
        pytest.skip(f"operator step skipped ({INTERACTIVE_VAR}=0): {action}")
    qt_app()
    dialog, _, _ = _operator_dialog(READY_NAME, "准备", ready, name, (READY_TEXT, SKIP_TEXT))
    try:
        if _show_operator_dialog(dialog, None, None) != "accepted":
            pytest.skip(f"operator skipped at the get-ready question: {ready}")
    finally:
        dialog.deleteLater()
    if start is not None:
        start()
    dialog, _, _ = _operator_dialog(ACTION_NAME, "请操作", action, name, (DONE_TEXT, SKIP_TEXT))
    try:
        outcome = _show_operator_dialog(dialog, timeout, done_when)
    finally:
        dialog.deleteLater()
    if outcome == "timeout":
        pytest.skip(f"operator step timed out after {timeout} s: {action}")
    if outcome != "accepted":
        pytest.skip(f"operator skipped the step: {action}")


# ---- boards -----------------------------------------------------------------------------


def port_names() -> list[str]:
    from embr.core.serial import list_ports

    return [p.device for p in list_ports()]


def port_present(device: str) -> bool:
    return device.upper() in (name.upper() for name in port_names())


def wait_port(device: str, present: bool = True, timeout: float = 15.0) -> None:
    """Wait (event loop running) until `device` is listed (or gone)."""
    wait_until(
        lambda: port_present(device) == present,
        timeout,
        message=f"{device} {'did not appear' if present else 'did not go away'}",
        interval_ms=200,
    )


def serial_after_reset(device: str, seconds: float = 4.0, baud: int = 115200) -> str:
    """Reset the board through a UART bridge's RTS (EN) with DTR (IO0) released, so it
    boots its application, and return what it prints within `seconds`."""
    import serial  # pyserial

    with serial.Serial() as port:
        port.port = device
        port.baudrate = baud
        port.timeout = 0.1
        port.dtr = False
        port.rts = True  # EN low: held in reset
        port.open()
        time.sleep(0.1)
        port.rts = False  # EN high: boots (IO0 high, DTR released)
        chunks: list[bytes] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunks.append(port.read(4096))
    return b"".join(chunks).decode("utf-8", errors="replace")


# ---- fixture builds ---------------------------------------------------------------------


def _old(path: Path, age_s: float = 3600) -> None:
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def idf_build_copy(sample: str, project: Path) -> Path:
    """project/build: a copy of an IDF fixture sample's build (an hour old, as a finished
    build), project_description.json's config_file pointing at project/sdkconfig, which
    names the sample's target. Returns the build directory."""
    source = IDF / sample / "build"
    build = project / "build"
    shutil.copytree(source, build)
    description = build / "project_description.json"
    data = cast(dict[str, Any], json.loads(description.read_text(encoding="utf-8")))
    data["config_file"] = str(project / "sdkconfig")
    description.write_text(json.dumps(data, indent=4), encoding="utf-8")
    expect = tomllib.loads((IDF / sample / "SOURCE.toml").read_text(encoding="utf-8"))
    chip = cast(dict[str, str], expect["expect"])["chip"]
    (project / "sdkconfig").write_text(f'CONFIG_IDF_TARGET="{chip}"\n', encoding="utf-8")
    for file in [*build.rglob("*"), project / "sdkconfig"]:
        if file.is_file():
            _old(file)
    return build


def pio_project_copy(sample: str, root: Path, core: Path) -> Path:
    """A PlatformIO project from a PIO fixture sample: root/platformio.ini (from
    SOURCE.toml), root/.pio/build/ with <PROJECT_DIR>, <PIO_CORE> and <HOME> filled in, a
    fake PIO core folder `core` (the sample's pio_core plus board definitions). Point
    PLATFORMIO_CORE_DIR at `core`. Returns the first env's build folder."""
    source = PIO / sample
    root.mkdir(parents=True, exist_ok=True)
    text = tomllib.loads((source / "SOURCE.toml").read_text(encoding="utf-8"))["platformio_ini"]
    ini = root / "platformio.ini"
    ini.write_text(cast(str, text), encoding="utf-8")
    _old(ini, 7200)
    places = {"<PROJECT_DIR>": root, "<PIO_CORE>": core, "<HOME>": root.parent / "home"}
    for file in (source / ".pio").rglob("*"):
        if not file.is_file():
            continue
        target = root / ".pio" / file.relative_to(source / ".pio")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = file.read_bytes()
        if file.name == "idedata.json":
            content = data.decode("utf-8")
            for key, value in places.items():
                content = content.replace(key, json.dumps(str(value))[1:-1])
            data = content.encode("utf-8")
        target.write_bytes(data)
        _old(target)
    if (source / "pio_core").is_dir():
        shutil.copytree(source / "pio_core", core, dirs_exist_ok=True)
    boards = core / "platforms" / "espressif32" / "boards"
    boards.mkdir(parents=True, exist_ok=True)
    for board, mcu in {"esp32dev": "esp32", "esp32-s3-devkitc-1": "esp32s3"}.items():
        (boards / f"{board}.json").write_text(json.dumps({"build": {"mcu": mcu}}))
    envs = sorted((root / ".pio" / "build").iterdir())
    return envs[0]


def hello_files(sample: str, folder: Path) -> list[Path]:
    """Copies of an IDF sample's [bootloader, partition table, app] in `folder` (flat)."""
    build = IDF / sample / "build"
    args = cast(
        dict[str, dict[str, str]],
        json.loads((build / "flasher_args.json").read_text(encoding="utf-8")),
    )
    folder.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for key in ("bootloader", "partition-table", "app"):
        source = build / args[key]["file"]
        files.append(Path(shutil.copy(source, folder / source.name)))
    return files


# ---- screenshots ------------------------------------------------------------------------


def screenshot_all(folder: Path, stem: str) -> list[Path]:
    """Save every visible top-level window and the whole primary screen as PNG."""
    saved: list[Path] = []
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return saved
    folder.mkdir(parents=True, exist_ok=True)
    for index, widget in enumerate(w for w in QApplication.topLevelWidgets() if w.isVisible()):
        path = folder / f"{stem}-window{index}.png"
        if widget.grab().save(str(path)):
            saved.append(path)
    screen = QGuiApplication.primaryScreen()
    path = folder / f"{stem}-screen.png"
    if screen.grabWindow(0).save(str(path)):
        saved.append(path)
    return saved


# ---- files and folders ------------------------------------------------------------------

Snapshot = dict[str, tuple[int, int]]


def snapshot(folder: Path) -> Snapshot:
    """Relative path -> (size, mtime_ns) of every file below `folder` (folders as -1)."""
    found: Snapshot = {}
    if not folder.exists():
        return found
    for path in folder.rglob("*"):
        try:
            st = path.stat()
        except OSError:
            continue
        rel = path.relative_to(folder).as_posix()
        found[rel] = (-1, 0) if path.is_dir() else (st.st_size, st.st_mtime_ns)
    return found


def new_or_changed(before: Snapshot, after: Snapshot) -> list[str]:
    return sorted(k for k, v in after.items() if before.get(k) != v and v[0] != -1)


def new_entries(before: Snapshot, after: Snapshot) -> list[str]:
    return sorted(k for k in after if k not in before)


class Sentinels:
    """Empty folders that stand in for a child process's TEMP, TMP, APPDATA,
    LOCALAPPDATA, USERPROFILE (HOME, HOMEDRIVE / HOMEPATH) and working folder, so anything
    it writes outside its program folder lands here and is seen."""

    NAMES = ("temp", "tmp", "appdata", "localappdata", "home", "cwd")

    def __init__(self, base: Path) -> None:
        self.base = base
        for name in self.NAMES:
            (base / name).mkdir(parents=True, exist_ok=True)

    def folder(self, name: str) -> Path:
        return self.base / name

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """os.environ with the redirections, QT_QPA_PLATFORM removed (real platform) and
        PYTHONDONTWRITEBYTECODE=1 (no __pycache__ next to the sources), plus `extra`."""
        env = dict(os.environ)
        env.pop("QT_QPA_PLATFORM", None)
        home = str(self.folder("home"))
        drive, rest = os.path.splitdrive(home)
        env.update(
            TEMP=str(self.folder("temp")),
            TMP=str(self.folder("tmp")),
            APPDATA=str(self.folder("appdata")),
            LOCALAPPDATA=str(self.folder("localappdata")),
            USERPROFILE=home,
            HOME=home,
            HOMEDRIVE=drive,
            HOMEPATH=rest,
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.update(extra or {})
        return env

    def leftovers(self, *, shell_dialog: bool = False) -> list[str]:
        """Everything that appeared in any sentinel folder, except the display driver's
        shader caches (DRIVER_CACHES: written by the GPU driver for every window that
        draws through Direct3D / OpenGL, not by Embr). shell_dialog: the run opened a
        native Windows file dialog, whose shell writes its icon caches (SHELL_CACHES)."""
        found = [f"{name}/{rel}" for name in self.NAMES for rel in snapshot(self.folder(name))]
        found = [f for f in found if not DRIVER_CACHES.match(f)]
        if shell_dialog:
            found = [f for f in found if not SHELL_CACHES.match(f)]
        return found

    def driver_caches(self) -> list[str]:
        """What leftovers() left out."""
        found = [f"{name}/{rel}" for name in self.NAMES for rel in snapshot(self.folder(name))]
        return [f for f in found if DRIVER_CACHES.match(f)]


DRIVER_CACHES = re.compile(
    r"^(home/appdata(/locallow(/(nvidia|intel|amd)(/.*)?)?)?"
    r"|home/appdata/local(/(d3dscache|nvidia|amd|intel)(/.*)?)?"
    r"|localappdata/(d3dscache|nvidia|amd|intel)(/.*)?"
    r"|appdata/(nvidia|amd|intel)(/.*)?)$",
    re.IGNORECASE,
)
"""Sentinel paths the GPU driver writes (shader caches) for any program that opens a
window; seen on the release machine as home/AppData/LocalLow/NVIDIA/DXCache."""
SHELL_CACHES = re.compile(
    r"^(home/appdata/local|localappdata)(/microsoft(/windows(/(explorer|caches)(/.*)?)?)?)?$",
    re.IGNORECASE,
)
"""What the Windows shell writes behind a native file dialog (Explorer icon caches,
Windows/Caches); seen on the release machine."""


def real_user_folders() -> list[Path]:
    """This user's real roaming and local AppData and Temp folders (known folders, not
    the environment), for a by-name check that nothing of Embr's appeared there."""
    folders: list[Path] = []
    for guid in (
        "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",  # RoamingAppData
        "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",  # LocalAppData
    ):
        path = _known_folder(guid)
        if path is not None:
            folders.append(path)
    local = _known_folder("{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}")
    if local is not None:
        folders.append(local / "Temp")
    return folders


NAME_HINTS = re.compile(r"embr|esptool|pyside|qt6|qtproject|python", re.IGNORECASE)


def suspicious_new_entries(before: dict[Path, set[str]]) -> list[str]:
    """Top-level entries that appeared in real_user_folders() since `before` and whose
    names look like Embr's, Qt's, Python's or esptool's (other programs write there too,
    so unrelated names are not reported)."""
    found: list[str] = []
    for folder, names in before.items():
        now = {p.name for p in folder.iterdir()} if folder.is_dir() else set[str]()
        found += [str(folder / n) for n in sorted(now - names) if NAME_HINTS.search(n)]
    return found


def user_folder_listing() -> dict[Path, set[str]]:
    return {f: {p.name for p in f.iterdir()} for f in real_user_folders() if f.is_dir()}


def _known_folder(guid: str) -> Path | None:
    class GUID(ctypes.Structure):
        _fields_ = (
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        )

    value = GUID()
    ole32: Any = ctypes.windll.ole32
    shell32: Any = ctypes.windll.shell32
    if ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(value)) != 0:
        return None
    out = ctypes.c_wchar_p()
    if shell32.SHGetKnownFolderPath(ctypes.byref(value), 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return Path(str(out.value))
    finally:
        ole32.CoTaskMemFree(out)


def current_user_sid() -> str:
    output = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return output.strip().split(",")[-1].strip('"')


@contextlib.contextmanager
def deny_access(folder: Path, rights: str = "(OI)(CI)(WD,AD,WEA,WA,DE,DC)") -> Generator[None]:
    """Add a deny ACE for the current user on `folder` only (icacls), removed afterwards.
    The default denies writing and deleting with specific rights; never deny "W" (generic
    write includes SYNCHRONIZE, so reading files would be denied too). "(RD)" denies
    listing the folder."""
    sid = current_user_sid()
    subprocess.run(
        ["icacls", str(folder), "/deny", f"*{sid}:{rights}"],
        capture_output=True,
        check=True,
    )
    try:
        yield
    finally:
        subprocess.run(
            ["icacls", str(folder), "/remove:d", f"*{sid}"],
            capture_output=True,
            check=True,
        )


def source_copy(dest: Path) -> Path:
    """dest/src: a copy of the repository's src/ (no __pycache__). `python -m embr` run
    with PYTHONPATH=dest/src uses dest/.run as its portable root, as a source checkout
    uses <repo>/.run (embr.core.storage.paths.default_root). Returns dest."""
    shutil.copytree(SRC, dest / "src", ignore=shutil.ignore_patterns("__pycache__"))
    return dest


# ---- child processes and their windows (Win32, UI Automation) ---------------------------

WM_CLOSE = 0x0010
TH32CS_SNAPPROCESS = 0x00000002


class _ProcessEntry(ctypes.Structure):
    _fields_ = (
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    )


@dataclass(frozen=True)
class ProcessEntry:
    """One row of a Toolhelp process snapshot."""

    pid: int
    parent: int
    """th32ParentProcessID: recorded when the process started and never updated; after
    the parent ends, that PID may belong to an unrelated process (PIDs are reused)."""
    image: str


@dataclass(frozen=True)
class VerifiedProcess:
    pid: int
    created: int
    """Creation time (FILETIME ticks) when it was verified."""
    depth: int
    """0 for the root, 1 for its children, ..."""


OUR_IMAGES = frozenset({"python.exe", "pythonw.exe", "embr.exe"})
"""Images our processes run as: the venv launcher and the interpreter (python -m embr,
its workers, storage_flow.py), the packaged Embr.exe and its --worker copies."""
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_snapshot() -> list[ProcessEntry]:
    kernel32: Any = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entries: list[ProcessEntry] = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            row = ProcessEntry(
                int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile)
            )
            entries.append(row)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return entries


def _open_process(pid: int, access: int) -> int:
    kernel32: Any = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    return int(kernel32.OpenProcess(access, False, pid) or 0)


def _close_handle(handle: int) -> None:
    kernel32: Any = ctypes.windll.kernel32
    kernel32.CloseHandle(ctypes.wintypes.HANDLE(handle))


def _handle_created(handle: int) -> int | None:
    kernel32: Any = ctypes.windll.kernel32
    times = [ctypes.wintypes.FILETIME() for _ in range(4)]
    ok = kernel32.GetProcessTimes(ctypes.wintypes.HANDLE(handle), *(ctypes.byref(t) for t in times))
    if not ok:
        return None
    return (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)


def creation_time(pid: int) -> int | None:
    """The process's creation time; None when it cannot be opened (gone, or not ours to
    query)."""
    handle = _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return None
    try:
        return _handle_created(handle)
    finally:
        _close_handle(handle)


def verified_tree(
    root: int,
    *,
    entries: Sequence[ProcessEntry] | None = None,
    created: Callable[[int], int | None] = creation_time,
    images: frozenset[str] = OUR_IMAGES,
) -> dict[int, VerifiedProcess]:
    """`root` (a process we started and still hold) and its real descendants. A process
    counts as a child of P only when its recorded parent is P, its image is one of
    `images`, and it was created after P's current creation time: a recorded parent PID
    that a later process reused (the real parent ended) is then never followed. Empty
    when `root` is gone or not one of `images`. `entries` and `created` are seams for the
    self-tests."""
    rows = list(entries) if entries is not None else process_snapshot()
    root_row = next((r for r in rows if r.pid == root), None)
    root_time = created(root)
    if root_time is None or root_row is None or root_row.image.lower() not in images:
        return {}
    tree = {root: VerifiedProcess(root, root_time, 0)}
    grew = True
    while grew:
        grew = False
        for row in rows:
            parent = tree.get(row.parent)
            if row.pid in tree or parent is None or row.image.lower() not in images:
                continue
            child_time = created(row.pid)
            if child_time is None or child_time <= parent.created:
                continue  # older than its "parent": that PID was reused
            tree[row.pid] = VerifiedProcess(row.pid, child_time, parent.depth + 1)
            grew = True
    return tree


def process_tree(pid: int) -> set[int]:
    """PIDs of verified_tree(pid) (a venv's python.exe is a launcher that starts the real
    interpreter as a child)."""
    return set(verified_tree(pid))


def still_same(process: VerifiedProcess) -> bool:
    """The PID still belongs to the process that was verified (it may have exited while
    a handle keeps it known; see still_running())."""
    return creation_time(process.pid) == process.created


STILL_ACTIVE = 259


def still_running(process: VerifiedProcess) -> bool:
    """The verified process exists and has not exited."""
    handle = _open_process(process.pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return False
    kernel32: Any = ctypes.windll.kernel32
    try:
        code = ctypes.wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(ctypes.wintypes.HANDLE(handle), ctypes.byref(code))
        return (
            bool(ok) and _handle_created(handle) == process.created and code.value == STILL_ACTIVE
        )
    finally:
        _close_handle(handle)


def terminate(process: VerifiedProcess) -> None:
    """TerminateProcess on exactly this process, after checking on the opened handle that
    it is still the one that was verified."""
    handle = _open_process(process.pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return
    kernel32: Any = ctypes.windll.kernel32
    try:
        if _handle_created(handle) == process.created:
            kernel32.TerminateProcess(ctypes.wintypes.HANDLE(handle), 1)
    finally:
        _close_handle(handle)


def terminate_tree(tree: dict[int, VerifiedProcess], *, keep_root: bool = False) -> None:
    """Terminate the verified processes, deepest first (children before parents)."""
    for process in sorted(tree.values(), key=lambda p: -p.depth):
        if keep_root and process.depth == 0:
            continue
        terminate(process)


@dataclass(frozen=True)
class NativeWindow:
    hwnd: int
    pid: int
    title: str


def top_windows(pids: set[int]) -> list[NativeWindow]:
    """Visible top-level windows owned by `pids`."""
    user32: Any = ctypes.windll.user32
    found: list[NativeWindow] = []
    proto = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def visit(hwnd: int, _: int) -> bool:
        owner = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) in pids and user32.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buffer, 512)
            found.append(NativeWindow(int(hwnd), int(owner.value), buffer.value))
        return True

    user32.EnumWindows(proto(visit), 0)
    return found


def post_close(hwnd: int) -> None:
    user32: Any = ctypes.windll.user32
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


WM_SETTEXT = 0x000C
BM_CLICK = 0x00F5
IDOK = 1
IDCANCEL = 2
FILE_NAME_EDIT = 1001
"""Control id of the file-name box in the Windows file dialogs."""


def _child_controls(hwnd: int, control_id: int, class_name: str) -> list[int]:
    user32: Any = ctypes.windll.user32
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def visit(child: int, _: int) -> bool:
        buffer = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, buffer, 64)
        if user32.GetDlgCtrlID(child) == control_id and buffer.value == class_name:
            found.append(int(child))
        return True

    user32.EnumChildWindows(hwnd, proto(visit), 0)
    return found


def native_file_dialog_answer(dialog: int, path: Path | None) -> None:
    """Answer a Windows Open / Save dialog (hwnd `dialog`): type `path` into its file-name
    box and press its OK button (Save / Open), or press Cancel for None. Its UI Automation
    tree does not expose these controls, so they are reached as Win32 controls by id."""
    user32: Any = ctypes.windll.user32
    if path is None:
        user32.PostMessageW(user32.GetDlgItem(dialog, IDCANCEL), BM_CLICK, 0, 0)
        return
    edits = _child_controls(dialog, FILE_NAME_EDIT, "Edit")
    if not edits:
        pytest.fail("no file-name box in the file dialog")
    text = ctypes.create_unicode_buffer(str(path))
    user32.SendMessageW.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.wintypes.UINT,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM,
    ]
    user32.SendMessageW(edits[0], WM_SETTEXT, 0, ctypes.cast(text, ctypes.c_void_p).value)
    user32.PostMessageW(user32.GetDlgItem(dialog, IDOK), BM_CLICK, 0, 0)


@dataclass
class UiaWindow:
    """A top-level window as UI Automation sees it (Qt exposes its widgets there)."""

    hwnd: int
    class_name: str
    """The Qt class: "EmbrWindow" for the main window, "QMessageBox" for a message."""
    name: str
    texts: list[str]
    buttons: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


_UIA_DUMP = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$pids = @(%(pids)s)
$deep = $%(deep)s
foreach ($p in $pids) {
  $cond = New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, [int]$p)
  $wins = $A::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
  foreach ($w in $wins) {
    "W`t" + $w.Current.NativeWindowHandle + "`t" + $w.Current.ClassName + "`t" + $w.Current.Name
    if (-not $deep -and $w.Current.ClassName -eq "EmbrWindow") { continue }
    $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($e in $all) {
      $kind = $e.Current.ControlType.ProgrammaticName
      $name = $e.Current.Name
      if ($kind -eq "ControlType.Button") { "B`t" + ($name -replace "`r?`n", " ") }
      elseif ($name) { "T`t" + ($name -replace "`r?`n", "\n") }
      $vp = $null
      if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
        $v = $vp.Current.Value
        if ($v) { "T`t" + ($v -replace "`r?`n", "\n") }
      }
    }
  }
}
"""

_UIA_PRESS = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$pids = @(%(pids)s)
$deep = $%(deep)s
foreach ($p in $pids) {
  $cond = New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, [int]$p)
  $wins = $A::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
  foreach ($w in $wins) {
    if (-not $deep -and $w.Current.ClassName -eq "EmbrWindow") { continue }
    $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($e in $all) {
      if ($e.Current.ControlType.ProgrammaticName -eq "ControlType.Button" -and
          $e.Current.Name -eq '%(name)s') {
        $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        "pressed"
        exit 0
      }
    }
  }
}
"""


def _powershell(script: str, timeout: float = 45) -> str:
    """Run a UI Automation script; "" when it does not finish in time (a UIA query can
    stall now and then; callers poll again). The script goes in as -EncodedCommand
    (UTF-16LE), so Chinese texts in it survive any console code page; the scripts set
    their output to UTF-8."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ""
    return done.stdout


def uia_windows(pids: set[int], *, deep: bool = False) -> list[UiaWindow]:
    """Top-level windows of `pids` with their texts and buttons (UI Automation). The main
    window (EmbrWindow, hundreds of elements; walking it can take long on a busy machine)
    is listed without its contents unless `deep`; its own dialogs are inside it."""
    pids_text = ",".join(str(p) for p in sorted(pids))
    output = _powershell(_UIA_DUMP % {"pids": pids_text, "deep": str(deep).lower()})
    windows: list[UiaWindow] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if parts[0] == "W" and len(parts) >= 4:
            hwnd = int(parts[1]) if parts[1].isdigit() else 0
            windows.append(UiaWindow(hwnd, parts[2], "\t".join(parts[3:]), [], []))
        elif windows and parts[0] == "T" and len(parts) >= 2:
            windows[-1].texts.append("\t".join(parts[1:]).replace("\\n", "\n"))
        elif windows and parts[0] == "B" and len(parts) >= 2:
            windows[-1].buttons.append("\t".join(parts[1:]))
    return windows


def uia_press(pids: set[int], name: str, *, in_main: bool = False) -> bool:
    """Invoke the button named `name` in a window of `pids` (inside the main window only
    with `in_main`, e.g. a dialog of the main window)."""
    quoted = name.replace("'", "''")  # a single-quoted PowerShell string
    pids_text = ",".join(str(p) for p in sorted(pids))
    script = _UIA_PRESS % {"pids": pids_text, "name": quoted, "deep": str(in_main).lower()}
    return "pressed" in _powershell(script)


MAIN_CLASS = "EmbrWindow"
NOT_READY = re.compile(r"^Qt\d+QWindow")
"""A Qt window seen by its Win32 class: its accessibility is not set up yet (its class
and texts follow a moment later)."""


class EmbrProcess:
    """A child Embr process (`python -m embr` from a source_copy(), or a packaged
    Embr.exe), its output in log files, its windows via Win32 / UI Automation.
    Only processes that verified_tree() accepts are ever closed or terminated; kill()
    ends them. Use as a context manager so a failing test never leaves one running."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str],
        cwd: Path,
        logs: Path,
        name: str = "embr",
    ) -> None:
        logs.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "-", name)
        self.stdout_path = logs / f"{name}.stdout.txt"
        self.stderr_path = logs / f"{name}.stderr.txt"
        self._out = self.stdout_path.open("wb")
        self._err = self.stderr_path.open("wb")
        self.popen = subprocess.Popen(
            list(command), env=env, cwd=cwd, stdout=self._out, stderr=self._err
        )

    @classmethod
    def source(
        cls, program: Path, *, env: dict[str, str], cwd: Path, logs: Path, name: str = "embr"
    ) -> EmbrProcess:
        """`python -m embr` with the sources of source_copy(program)."""
        env = {**env, "PYTHONPATH": str(program / "src")}
        return cls([sys.executable, "-m", "embr"], env=env, cwd=cwd, logs=logs, name=name)

    @property
    def pid(self) -> int:
        return self.popen.pid

    def tree(self) -> dict[int, VerifiedProcess]:
        """This process and its verified descendants (verified_tree())."""
        return verified_tree(self.popen.pid) if self.running() else {}

    def pids(self) -> set[int]:
        return set(self.tree())

    def _current_pids(self) -> set[int]:
        """Verified PIDs, each re-checked right now (just before acting on its windows)."""
        return {p.pid for p in self.tree().values() if still_same(p)}

    def running(self) -> bool:
        return self.popen.poll() is None

    def windows(self, *, deep: bool = False) -> list[UiaWindow]:
        """Top-level windows of our verified processes whose accessibility is ready (the
        main window's contents only with `deep`, see uia_windows())."""
        if not self.running():
            return []
        found = uia_windows(self.pids(), deep=deep)
        return [w for w in found if not NOT_READY.match(w.class_name)]

    def native_window(self, title: str, timeout: float = 30.0) -> NativeWindow:
        """A visible top-level window titled `title` (e.g. a Windows file dialog) of our
        verified processes."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tree = self.tree()
            for window in top_windows(set(tree)):
                if window.title == title and still_same(tree[window.pid]):
                    return window
            if not self.running():
                break
            time.sleep(0.3)
        pytest.fail(f"no window {title!r} within {timeout:.0f} s; stderr:\n{self.stderr()}")

    def answer_file_dialog(self, title: str, path: Path | None, timeout: float = 30.0) -> None:
        """Answer the native file dialog titled `title` (path, or None to cancel)."""
        native_file_dialog_answer(self.native_window(title, timeout).hwnd, path)

    def wait_for(
        self, predicate: Callable[[UiaWindow], bool], timeout: float = 60.0
    ) -> UiaWindow | None:
        """The first window matching `predicate`; None when the process ended first.
        pytest.fail after `timeout`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                return None
            for window in self.windows():
                if predicate(window):
                    return window
            time.sleep(0.3)
        self.kill()
        pytest.fail(f"no matching window within {timeout:.0f} s; stderr:\n{self.stderr()}")

    def wait_main_window(self, timeout: float = 60.0) -> UiaWindow | None:
        return self.wait_for(lambda w: w.class_name == MAIN_CLASS, timeout)

    def wait_any_window(self, timeout: float = 60.0) -> UiaWindow | None:
        return self.wait_for(lambda w: True, timeout)

    def wait_message(
        self, contains: str, buttons: Sequence[str] = (), timeout: float = 60.0
    ) -> UiaWindow | None:
        """A message box (a window other than the main window) whose text contains
        `contains` and which shows every one of `buttons`, waited for until its texts are
        there (UI Automation fills them in a moment after the window appears, and a
        window that is closing may still be listed). None when the process ended first."""

        def wanted(window: UiaWindow) -> bool:
            if window.class_name == MAIN_CLASS or contains not in window.text:
                return False
            return all(b in window.buttons for b in buttons)

        deadline = time.monotonic() + timeout
        seen: list[UiaWindow] = []
        while time.monotonic() < deadline:
            if not self.running():
                return None
            seen = self.windows()
            found = next((w for w in seen if wanted(w)), None)
            if found is not None:
                return found
            time.sleep(0.3)
        self.kill()
        shown = [(w.class_name, w.texts, w.buttons) for w in seen]
        pytest.fail(f"no message with {contains!r} and {list(buttons)} in {timeout:.0f} s: {shown}")

    def press(self, button: str, *, in_main: bool = False, timeout: float = 30.0) -> bool:
        """Invoke the button named `button`; tried again until it works (a UI Automation
        query can stall on a busy machine) or `timeout`. False when it never did."""
        deadline = time.monotonic() + timeout
        while self.running() and time.monotonic() < deadline:
            pids = self._current_pids()
            if pids and uia_press(pids, button, in_main=in_main):
                return True
            time.sleep(0.5)
        return False

    def close(self, timeout: float = 30.0) -> int:
        """Post WM_CLOSE to every visible window of our verified processes (each window's
        owner re-checked just before), again every 2 s (a modal dialog closes first, then
        the window behind it), and wait for the exit code."""
        deadline = time.monotonic() + timeout
        while self.running() and time.monotonic() < deadline:
            tree = self.tree()
            for window in top_windows(set(tree)):
                if still_same(tree[window.pid]):
                    post_close(window.hwnd)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.popen.wait(2)
        return self.wait(max(1.0, deadline - time.monotonic()))

    def wait(self, timeout: float = 30.0) -> int:
        try:
            return self.popen.wait(timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            pytest.fail(f"process did not exit within {timeout:.0f} s")

    def kill(self) -> None:
        """Terminate exactly our verified processes: the descendants deepest first, then
        the root through our own Popen handle. Never a tree kill by PID relation."""
        if self.running():
            terminate_tree(self.tree(), keep_root=True)
            with contextlib.suppress(OSError):
                self.popen.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.popen.wait(10)
        for handle in (self._out, self._err):
            with contextlib.suppress(OSError):
                handle.flush()

    def stdout(self) -> str:
        if not self._out.closed:
            self._out.flush()
        return _decode(self.stdout_path.read_bytes())

    def stderr(self) -> str:
        if not self._err.closed:
            self._err.flush()
        return _decode(self.stderr_path.read_bytes())

    def __enter__(self) -> EmbrProcess:
        return self

    def __exit__(self, *exc: object) -> None:
        self.kill()
        self._out.close()
        self._err.close()


def store_language(root: Path, language: Any) -> None:
    """Write the Language choice (embr.i18n.Language) into root/settings.ini, as the
    Language menu does."""
    from embr.core.storage.settings import Settings
    from embr.i18n import SETTINGS_KEY
    from embr.ui.adapters import settings as ui_settings

    settings = Settings(root / "settings.ini", root / "data" / "backup")
    ui_settings.save_choice(settings, SETTINGS_KEY, str(language.value))
    settings.save()


def translated(text: str, language: Any) -> str:
    """embr.i18n.tr(text) in `language` (the current language is restored)."""
    from embr.i18n import current, set_language, tr

    saved = current()
    set_language(language)
    try:
        return tr(text)
    finally:
        set_language(saved)


def _decode(data: bytes) -> str:
    """A child's redirected output: UTF-8, else the ANSI code page Python uses for a
    redirected stream (e.g. cp936 on Chinese Windows)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def is_valid_ini(data: bytes) -> bool:
    """settings.ini is read as UTF-8 RawConfigParser with "=" delimiters."""
    parser = configparser.RawConfigParser(interpolation=None, delimiters=("=",))
    try:
        parser.read_string(data.decode("utf-8"))
    except (configparser.Error, UnicodeDecodeError):
        return False
    return True


def poll_until(condition: Callable[[], bool], timeout: float, message: str) -> None:
    """Wait without a Qt event loop (for child processes): poll every 0.2 s."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f} s: {message}")
        time.sleep(0.2)


def wait_file(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"{path} did not appear within {timeout:.0f} s")
        time.sleep(0.2)
