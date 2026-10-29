"""Fixtures for the Qt adapter tests: an offscreen QApplication shared by a module."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # before any Qt GUI import

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp() -> Iterator[QCoreApplication]:
    app = QApplication.instance() or QApplication([])
    yield app
