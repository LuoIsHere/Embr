"""app/library: the sidebar tree and its search (design doc §6.1)."""

from pathlib import Path

import pytest
from app_testkit import (
    build_files,
    copy_files,
    find_row,
    fixed_firmware,
    multi_chip_firmware,
    p4_lines_firmware,
)

from embr.app.context import AppContext
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


def test_origin_is_the_folder(library: Library, tmp_path: Path) -> None:
    firmware = fixed_firmware(library, copy_files(build_files("esp32s3_hello"), tmp_path / "a"))
    empty = library.create_firmware("empty")
    tree = library.tree()
    assert Path(find_row(tree, firmware.id).origin) == (tmp_path / "a").resolve()
    assert find_row(tree, empty.id).origin == ""


def test_row_lists_every_chip(library: Library, context: AppContext, tmp_path: Path) -> None:
    multi = multi_chip_firmware(context, tmp_path)
    single = fixed_firmware(library, build_files("esp32s3_hello"), "s3")
    empty = library.create_firmware("empty")
    tree = library.tree()
    assert [find_row(tree, f.id).chips for f in (multi, single, empty)] == [
        ("esp32p4", "esp32s3"),  # shown as "multi"
        ("esp32s3",),
        (),
    ]


def test_row_lists_p4_lines_as_mcus(library: Library, context: AppContext, tmp_path: Path) -> None:
    lines = p4_lines_firmware(context, tmp_path / "lines")
    rev3 = fixed_firmware(library, copy_files(build_files("esp32p4_rev3_hello"), tmp_path / "v3"))
    tree = library.tree()
    assert [find_row(tree, f.id).mcus for f in (lines, rev3)] == [
        ("ESP32-P4 v0\u2013v1", "ESP32-P4 v3"),  # one chip, two MCUs: shown as "multi"
        ("ESP32-P4 v3",),
    ]
    assert find_row(tree, lines.id).chips == ("esp32p4",)
