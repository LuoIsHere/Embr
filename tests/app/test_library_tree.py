"""app/library: the sidebar tree and its search (design doc §6.1)."""

from pathlib import Path

import pytest
from app_testkit import build_files, copy_files, find_row

from embr.app.library import Library, Tree


def shape(tree: Tree) -> list[tuple[str, list[tuple[str, list[str]]]]]:
    return [
        (p.name, [(g.name, [f.name for f in g.firmware]) for g in p.groups]) for p in tree.projects
    ]


def test_tree_orders_projects_groups_and_firmware(library: Library) -> None:
    library.create_project("beta")
    alpha = library.create_project("alpha")
    for group in ("sensor", "main", "power"):
        library.add_group(alpha.id, group)
    library.create_firmware("b-fw", alpha.id, "sensor")
    library.create_firmware("a-fw", alpha.id, "sensor")
    library.create_firmware("m-fw", alpha.id, "main")
    library.create_firmware("solo")

    tree = library.tree()
    assert shape(tree) == [
        ("alpha", [("sensor", ["a-fw", "b-fw"]), ("main", ["m-fw"]), ("power", [])]),
        ("beta", []),
    ]
    assert [f.name for f in tree.standalone] == ["solo"]
    assert tree.projects[0].firmware_count == 3

    library.create_firmware("u-fw", alpha.id)
    groups = library.tree().projects[0].groups
    assert [(g.name, [f.name for f in g.firmware]) for g in groups][-1] == ("", ["u-fw"])


@pytest.mark.parametrize(
    ("query", "projects", "standalone"),
    [
        ("ALPHA", {"alpha": ["boot-test", "main-app"]}, []),
        ("main", {"alpha": ["main-app"]}, ["main-tool"]),
    ],
    ids=["project-name-keeps-all", "firmware-name-keeps-matches"],
)
def test_search(
    library: Library, query: str, projects: dict[str, list[str]], standalone: list[str]
) -> None:
    alpha = library.create_project("alpha")
    beta = library.create_project("beta")
    library.create_firmware("boot-test", alpha.id)
    library.create_firmware("main-app", alpha.id)
    library.create_firmware("sensor", beta.id)
    library.create_firmware("main-tool")

    tree = library.tree(query)
    found = {p.name: sorted(f.name for g in p.groups for f in g.firmware) for p in tree.projects}
    assert found == projects
    assert [f.name for f in tree.standalone] == standalone
    assert all(p.firmware_count == 2 for p in tree.projects)


def test_origin_is_the_folder_of_the_first_enabled_file(library: Library, tmp_path: Path) -> None:
    _, table, app = build_files("esp32s3_hello")
    (app_copy,) = copy_files([app], tmp_path / "a")
    (table_copy,) = copy_files([table], tmp_path / "b")
    firmware = library.create_firmware("fw")
    library.add_files(firmware.id, [app_copy, table_copy])
    app_entry = next(e for e in library.firmware(firmware.id).entries if e.name == app.name)
    library.set_entry_enabled(firmware.id, app_entry.id, False)

    row = find_row(library.tree(), firmware.id)
    assert Path(row.origin) == (tmp_path / "b").resolve()
