"""While a job runs, a project cannot be deleted and its groups cannot be removed: a read
may be saving its content into them."""

import pytest
from PySide6.QtWidgets import QPushButton

from embr.ui import dialogs
from embr.ui.window import EmbrWindow

# pyright: reportPrivateUsage=false


def _buttons(window: EmbrWindow) -> dict[str, QPushButton]:
    return {b.text(): b for b in window.project_view.findChildren(QPushButton)}


def test_delete_and_remove_wait_for_a_running_job(
    window: EmbrWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = window.library.create_project("p")
    window.library.add_group(project.id, "board")
    asked: list[str] = []

    def confirm(_parent: object, title: str, *_args: object, **_kwargs: object) -> None:
        asked.append(title)

    monkeypatch.setattr(dialogs, "confirm", confirm)

    window.select_project(project.id)
    assert _buttons(window)["Delete project…"].isEnabled()
    assert _buttons(window)["Remove"].isEnabled()

    monkeypatch.setattr(window, "busy", lambda: True)
    window.select_project(project.id)
    assert not _buttons(window)["Delete project…"].isEnabled()
    assert not _buttons(window)["Remove"].isEnabled()

    window.project_view._delete()  # a job started after the page was built
    window.project_view._remove_group("board", 0)
    assert asked == []
    assert window.library.project(project.id).devices == ["board"]
