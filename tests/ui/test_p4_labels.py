"""A tracking ESP32-P4 v3 firmware reads the same everywhere: the sidebar, the project page
and the header. Driven offscreen on a real window."""

import shutil
from pathlib import Path

from PySide6.QtWidgets import QLabel

from embr.ui.window import EmbrWindow

SAMPLE = Path(__file__).parents[1] / "fixtures/idf/esp32p4_rev3_hello"
LABEL = "ESP32-P4 v3"


def _import(window: EmbrWindow, tmp_path: Path, project_id: str | None = None) -> str:
    shutil.copytree(SAMPLE, tmp_path / "hello")
    return window.library.import_idf(tmp_path / "hello/build", project_id=project_id).id


def _sidebar_texts(window: EmbrWindow, firmware_id: str) -> list[str]:
    tree = window.sidebar_panel.tree
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        assert item is not None
        node = tree.node(item)
        if node is not None and node.firmware_id == firmware_id:
            row = tree.itemWidget(item, 0)
            assert row is not None
            return [label.text() for label in row.findChildren(QLabel)]
    raise AssertionError(f"no row for {firmware_id}")


def test_the_sidebar_and_header_agree(window: EmbrWindow, tmp_path: Path) -> None:
    firmware_id = _import(window, tmp_path)
    window.select_firmware(firmware_id)

    assert LABEL in _sidebar_texts(window, firmware_id)
    assert LABEL in [w.text() for w in window.header.findChildren(QLabel)]


def test_the_project_page_agrees(window: EmbrWindow, tmp_path: Path) -> None:
    project = window.library.create_project("Station")
    _import(window, tmp_path, project.id)
    window.select_project(project.id)

    assert any(LABEL in w.text() for w in window.project_view.findChildren(QLabel))
