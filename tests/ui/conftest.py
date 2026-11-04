"""Fixtures for the Qt tests: an offscreen QApplication shared by a module, and a real
window on a throwaway data folder."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # before any Qt GUI import

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from embr.app.context import open_context
from embr.core.storage.paths import AppPaths, prepare
from embr.i18n import Language, current, set_language
from embr.ui.window import EmbrWindow


@pytest.fixture(scope="module")
def qapp() -> Iterator[QCoreApplication]:
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp: QCoreApplication, tmp_path: Path) -> Iterator[EmbrWindow]:
    language = current()
    set_language(Language.ENGLISH)
    paths = AppPaths(tmp_path / "Embr")
    prepare(paths)
    context = open_context(paths)
    shown = EmbrWindow(context)
    yield shown
    shown.close()
    shown.deleteLater()  # a closed window kept alive slows every later style sheet change
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    context.close()
    set_language(language)
