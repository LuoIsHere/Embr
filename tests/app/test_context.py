"""app/context: opening the portable data folder."""

import sqlite3

import pytest

from embr.app.context import open_context
from embr.core.storage.paths import AppPaths
from embr.core.storage.settings import Settings


def test_fresh_context_has_working_stores_and_close_saves_settings(paths: AppPaths) -> None:
    context = open_context(paths)
    project = context.projects.create("demo")
    firmware = context.firmware.create("fw", project_id=project.id)
    assert [f.id for f in context.firmware.in_scope(project.id)] == [firmware.id]
    assert context.history.query() == []

    context.settings.set("window", "sidebar_width", "240")
    context.close()

    assert Settings(paths.settings, paths.backup).get("window", "sidebar_width") == "240"


def test_close_closes_the_database_when_saving_settings_fails(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = open_context(paths)

    def fail() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(context.settings, "save", fail)
    with pytest.raises(OSError, match="disk full"):
        context.close()
    with pytest.raises(sqlite3.ProgrammingError):  # closed
        context.database.execute("SELECT 1")
