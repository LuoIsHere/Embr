"""app/context: opening the portable data folder."""

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
