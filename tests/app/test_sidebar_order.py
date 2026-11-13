"""The sidebar order of projects and groups: projects by name until
the first drag, then as dragged (settings.ini [ui] project_order) with new projects after
them; groups in the project's own order."""

import tomllib
from pathlib import Path

import pytest
import tomli_w

from embr.app.context import AppContext, open_context
from embr.app.library import PROJECT_ORDER_KEY, UI_SECTION, Library
from embr.core.firmware.store import ReadOnlyError
from embr.core.project import PROJECT_SCHEMA_VERSION
from embr.core.storage.paths import AppPaths


def _names(library: Library) -> list[str]:
    return [p.name for p in library.tree().projects]


def _groups(library: Library, name: str) -> list[str]:
    project = next(p for p in library.tree().projects if p.name == name)
    return [g.name for g in project.groups]


def _projects(library: Library, *names: str) -> dict[str, str]:
    return {name: library.create_project(name).id for name in names}


def test_projects_are_by_name_until_dragged(context: AppContext) -> None:
    library = Library(context)
    _projects(library, "Charlie", "Alpha", "Bravo")
    assert library.project_order() == []
    assert _names(library) == ["Alpha", "Bravo", "Charlie"]


def test_the_dragged_order_is_kept_across_a_restart(paths: AppPaths, context: AppContext) -> None:
    library = Library(context)
    ids = _projects(library, "Alpha", "Bravo", "Charlie")
    library.set_project_order([ids["Charlie"], ids["Alpha"], ids["Bravo"]])
    assert _names(library) == ["Charlie", "Alpha", "Bravo"]

    reopened = open_context(paths)
    try:
        assert _names(Library(reopened)) == ["Charlie", "Alpha", "Bravo"]
    finally:
        reopened.close()


def test_new_projects_come_after_the_dragged_ones(context: AppContext) -> None:
    library = Library(context)
    ids = _projects(library, "Bravo", "Delta")
    library.set_project_order([ids["Delta"], ids["Bravo"]])
    _projects(library, "Charlie", "Alpha")
    assert _names(library) == ["Delta", "Bravo", "Alpha", "Charlie"]


def test_rename_and_delete_keep_the_order(context: AppContext) -> None:
    library = Library(context)
    ids = _projects(library, "Alpha", "Bravo", "Charlie")
    library.set_project_order([ids["Charlie"], ids["Bravo"], ids["Alpha"]])
    library.rename_project(ids["Bravo"], "Zulu")
    library.delete_project(ids["Charlie"])
    assert _names(library) == ["Zulu", "Alpha"]


@pytest.mark.parametrize("text", ["", "garbage", ",,,", "  ,x,", "ÄÖÜ;;;\t"])
def test_a_damaged_order_falls_back_to_names(context: AppContext, text: str) -> None:
    library = Library(context)
    _projects(library, "Charlie", "Alpha", "Bravo")
    context.settings.set(UI_SECTION, PROJECT_ORDER_KEY, text)
    assert _names(library) == ["Alpha", "Bravo", "Charlie"]


def test_unknown_and_repeated_ids_are_skipped(context: AppContext) -> None:
    library = Library(context)
    ids = _projects(library, "Alpha", "Bravo", "Charlie")
    order = ["GONE", ids["Bravo"], ids["Bravo"], "x", ids["Alpha"]]
    context.settings.set(UI_SECTION, PROJECT_ORDER_KEY, ",".join(order))
    assert _names(library) == ["Bravo", "Alpha", "Charlie"]


def test_the_search_keeps_the_order(context: AppContext) -> None:
    library = Library(context)
    ids = _projects(library, "Alpha one", "Bravo", "Charlie one")
    library.set_project_order([ids["Charlie one"], ids["Bravo"], ids["Alpha one"]])
    assert [p.name for p in library.tree("one").projects] == ["Charlie one", "Alpha one"]


def test_reordered_groups_show_in_the_tree(context: AppContext) -> None:
    library = Library(context)
    project = library.create_project("Station")
    for name in ("Main", "Display", "Radio"):
        library.add_group(project.id, name)
    library.create_firmware("loose", project.id)  # the Ungrouped bucket, always last
    library.reorder_groups(project.id, ["Radio", "Main", "Display"])
    assert _groups(library, "Station") == ["Radio", "Main", "Display", ""]


def test_a_newer_project_is_marked_read_only(context: AppContext, tmp_path: Path) -> None:
    library = Library(context)
    project = library.create_project("Future")
    library.add_group(project.id, "A")
    library.add_group(project.id, "B")
    file = context.paths.projects / f"{project.id}.toml"
    doc = tomllib.loads(file.read_text(encoding="utf-8"))
    doc["schema_version"] = PROJECT_SCHEMA_VERSION + 1
    file.write_text(tomli_w.dumps(doc), encoding="utf-8")

    [row] = library.tree().projects
    assert row.read_only
    with pytest.raises(ReadOnlyError):
        library.reorder_groups(project.id, ["B", "A"])
    library.set_project_order([project.id])  # its place in the sidebar is not in its file
    assert library.project_order() == [project.id]
