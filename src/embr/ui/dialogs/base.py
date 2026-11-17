"""The dialog frame and the small questions every part of the window asks."""

import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from embr.app.library import Library
from embr.i18n import tr
from embr.ui.widgets import button, label


class Dialog(QDialog):
    """Title, body widgets, Cancel / OK buttons on the right."""

    def __init__(
        self, parent: QWidget | None, title: str, ok: str = "OK", *, danger: bool = False
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(480)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.check: Callable[[], bool] = lambda: True
        """Input check run by validate()."""
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.ok = button(ok, "danger-small" if danger else "accent-small")
        self.cancel = button(tr("Cancel"))
        self.ok.setDefault(True)
        self.ok.clicked.connect(self._accept)
        self.cancel.clicked.connect(self.reject)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.cancel)
        actions.addWidget(self.ok)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(heading)
        layout.addLayout(self.body)
        layout.addLayout(actions)

    def text(self, text: str) -> QLabel:
        widget = label(text, tone="2", wrap=True)
        widget.setTextFormat(Qt.TextFormat.RichText)
        self.body.addWidget(widget)
        return widget

    def field(self, caption: str, widget: QWidget) -> QWidget:
        box = QVBoxLayout()
        box.setSpacing(4)
        box.addWidget(label(caption, tone="3"))
        box.addWidget(widget)
        self.body.addLayout(box)
        return widget

    def row(self, *fields: tuple[str, QWidget]) -> QWidget:
        """Captioned fields side by side on one line; returns the line (to hide it)."""
        line = QWidget()
        layout = QHBoxLayout(line)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        for caption, widget in fields:
            box = QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(label(caption, tone="3"))
            box.addWidget(widget)
            layout.addLayout(box, 1)
        self.body.addWidget(line)
        return line

    def fit(self) -> None:
        """Height for the current width. Wrapped text makes the default size hint assume a
        narrower dialog, and the spare height used to open gaps between the parts."""
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        height = layout.totalHeightForWidth(self.width())
        self.resize(self.width(), height if height > 0 else layout.sizeHint().height())

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.fit()

    def validate(self) -> bool:
        """Override (or set `check`) to keep the dialog open on bad input."""
        return self.check()

    def _accept(self) -> None:
        if self.validate():
            self.accept()


def confirm(
    parent: QWidget,
    title: str,
    lines: list[str],
    ok: str,
    *,
    danger: bool = True,
    checkbox: str | None = None,
) -> tuple[bool, bool]:
    """(accepted, checkbox checked). Lines are rich text."""
    dialog = Dialog(parent, title, ok, danger=danger)
    for line in lines:
        dialog.text(line)
    box = QCheckBox(checkbox) if checkbox else None
    if box is not None:
        dialog.body.addWidget(box)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return accepted, bool(box and box.isChecked())


def notice(parent: QWidget, title: str, lines: list[str]) -> None:
    """A message with a Close button only. Lines are rich text."""
    dialog = Dialog(parent, title, tr("Close"), danger=False)
    dialog.cancel.hide()
    for line in lines:
        dialog.text(line)
    dialog.exec()


def internal_error(parent: QWidget, crash_log: str) -> bool:
    """An exception no code handled (shown once per run). True: open the log folder."""
    dialog = Dialog(parent, tr("Unexpected error"), tr("Open log folder"), danger=False)
    dialog.cancel.setText(tr("Close"))
    dialog.text(tr("Embr ran into an unexpected error; the action may not have finished."))
    dialog.text(tr(f"Details were written to logs/{html.escape(crash_log)}."))
    return dialog.exec() == QDialog.DialogCode.Accepted


def ask_choice(
    parent: QWidget, title: str, caption: str, choices: list[str], ok: str
) -> int | None:
    """Index of the chosen item, or None when cancelled."""
    dialog = Dialog(parent, title, ok, danger=False)
    combo = QComboBox()
    combo.addItems(choices)
    dialog.field(caption, combo)
    combo.setFocus()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return combo.currentIndex()


def ask_text(
    parent: QWidget, title: str, caption: str, value: str, note: str, ok: str
) -> str | None:
    dialog = Dialog(parent, title, ok, danger=False)
    edit = QLineEdit(value)
    edit.selectAll()
    dialog.field(caption, edit)
    if note:
        dialog.text(note)
    edit.setFocus()
    if dialog.exec() != QDialog.DialogCode.Accepted or not edit.text().strip():
        return None
    return edit.text().strip()


@dataclass(frozen=True)
class Placement:
    project_id: str | None
    device: str


class PlacementFields:
    """Linked Project and Group selectors; the group list follows the project."""

    def __init__(
        self, dialog: Dialog, library: Library, project_id: str | None, device: str
    ) -> None:
        self._library = library
        self.project = QComboBox()
        for project in library.tree().projects:
            self.project.addItem(project.name, project.id)
        self.project.addItem(tr("Standalone (no project)"), None)
        index = self.project.findData(project_id)
        self.project.setCurrentIndex(index if index >= 0 else self.project.count() - 1)
        self.group = QComboBox()
        dialog.row((tr("Place in"), self.project), (tr("Group"), self.group))
        self.project.currentIndexChanged.connect(self._sync)
        self._sync()
        if device:
            found = self.group.findData(device)
            if found >= 0:
                self.group.setCurrentIndex(found)

    def _sync(self) -> None:
        self.group.clear()
        self.group.addItem(tr("(ungrouped)"), "")
        project_id = self.project.currentData()
        if isinstance(project_id, str):
            for name in self._library.project(project_id).devices:
                self.group.addItem(name, name)
        self.group.setEnabled(isinstance(project_id, str))

    def value(self) -> Placement:
        project_id = self.project.currentData()
        if not isinstance(project_id, str):
            return Placement(None, "")
        device = self.group.currentData()
        return Placement(project_id, device if isinstance(device, str) else "")


def replace_ok(parent: QWidget, path: Path, users: Sequence[str] = ()) -> bool:
    """True when `path` does not exist yet or the user agrees to replace it; `users` are
    the firmware referring to the file, named in the question."""
    if not path.exists():
        return True
    lines = [
        tr(
            f"<b>{html.escape(path.name)}</b> already exists in "
            f"{html.escape(str(path.parent))}. Replace it?"
        )
    ]
    if users:
        names = ", ".join(f"<b>{html.escape(name)}</b>" for name in users)
        lines.append(
            tr(
                f"Used by firmware {names}, which will show 'content changed' after the "
                "replacement."
            )
        )
    accepted, _ = confirm(parent, tr("Replace file"), lines, tr("Replace"))
    return accepted
