"""A group row always shows how many firmware it holds; an empty one says so."""

from PySide6.QtWidgets import QLabel

from embr.ui.window import EmbrWindow


def _group_texts(window: EmbrWindow, name: str) -> list[str]:
    tree = window.sidebar_panel.tree
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        assert item is not None
        node = tree.node(item)
        if node is not None and node.kind == "group" and node.device == name:
            row = tree.itemWidget(item, 0)
            assert row is not None
            return [label.text() for label in row.findChildren(QLabel)]
    raise AssertionError(f"no group row {name}")


def test_an_expanded_group_shows_its_count(window: EmbrWindow) -> None:
    library = window.library
    project = library.create_project("Station")
    library.add_group(project.id, "Main")
    library.add_group(project.id, "Spare")
    for name in ("one", "two"):
        library.create_firmware(name, project.id, "Main")
    window.changed()

    assert "2" in _group_texts(window, "Main")
    assert "empty" in _group_texts(window, "Spare")
