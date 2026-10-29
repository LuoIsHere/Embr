"""Fixtures for the app tests: a fresh portable data folder under tmp_path."""

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from embr.app.context import AppContext, open_context
from embr.app.library import Library
from embr.core.storage import paths as storage_paths
from embr.core.storage.paths import AppPaths


@pytest.fixture
def paths(tmp_path: Path) -> Iterator[AppPaths]:
    """Prepared data folder; prepare() repoints tempfile, which is restored afterwards."""
    saved = tempfile.tempdir
    app_paths = AppPaths(tmp_path / "embr")
    storage_paths.prepare(app_paths)
    yield app_paths
    tempfile.tempdir = saved


@pytest.fixture
def context(paths: AppPaths) -> Iterator[AppContext]:
    ctx = open_context(paths)
    yield ctx
    ctx.close()


@pytest.fixture
def library(context: AppContext) -> Library:
    return Library(context)
