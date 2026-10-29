"""In-app dialogs (design doc §6.1 "对话框"): Esc closes, destructive buttons are red and
say what they affect."""

from dataclasses import dataclass

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
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

from embr import __version__
from embr.app.library import Library
from embr.core.storage.paths import AppPaths
from embr.ui.widgets import button, label

REPOSITORY = "https://github.com/LuoIsHere/Embr"
RELEASES = "https://github.com/LuoIsHere/Embr/releases"


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
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.ok = button(ok, "danger-small" if danger else "accent-small")
        self.cancel = button("Cancel")
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

    def validate(self) -> bool:
        """Override to keep the dialog open on bad input."""
        return True

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
        self.project.addItem("Standalone (no project)", None)
        index = self.project.findData(project_id)
        self.project.setCurrentIndex(index if index >= 0 else self.project.count() - 1)
        self.group = QComboBox()
        dialog.field("Place in", self.project)
        dialog.field("Group", self.group)
        self.project.currentIndexChanged.connect(self._sync)
        self._sync()
        if device:
            found = self.group.findData(device)
            if found >= 0:
                self.group.setCurrentIndex(found)

    def _sync(self) -> None:
        self.group.clear()
        self.group.addItem("(ungrouped)", "")
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


def new_firmware(
    parent: QWidget, library: Library, project_id: str | None, device: str
) -> tuple[str, Placement] | None:
    dialog = Dialog(parent, "New firmware", "Create", danger=False)
    name = QLineEdit()
    name.setPlaceholderText("e.g. S3-main-v1.3 (default: the first file added)")
    dialog.field("Name", name)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        "Fixed firmware: add entries by hand afterwards. Reset policy and erase_all are copied "
        "from the project defaults."
    )
    name.setFocus()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return name.text().strip(), placement.value()


def new_project(parent: QWidget) -> tuple[str, str] | None:
    dialog = Dialog(parent, "New project", "Create", danger=False)
    name = QLineEdit()
    name.setPlaceholderText("e.g. 500_roller_hmi")
    notes = QLineEdit()
    notes.setPlaceholderText("hardware revision, purpose…")
    dialog.field("Name (unique)", name)
    dialog.field("Notes", notes)
    dialog.text(
        "Defaults for new firmware: before=default-reset · after=hard-reset · erase_all=off"
    )
    name.setFocus()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return name.text().strip(), notes.text().strip()


def move_firmware(
    parent: QWidget, library: Library, project_id: str | None, device: str
) -> Placement | None:
    dialog = Dialog(parent, "Move firmware", "Move", danger=False)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        "Only the project and the group change; entries, files and records stay as they are. "
        "Or drag the firmware in the sidebar."
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return placement.value()


def settings(parent: QWidget, paths: AppPaths) -> None:
    dialog = Dialog(parent, "Settings", "Close", danger=False)
    dialog.cancel.hide()
    folder = label(str(paths.root), mono=True, wrap=True)
    folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    dialog.field("Program folder", folder)
    dialog.field(
        "Data and log folders (portable, inside the program folder; fixed, not configurable)",
        label("data\\ · logs\\ · temp\\ · settings.ini", mono=True),
    )
    dialog.exec()


def about(parent: QWidget, esptool_version: str) -> None:
    dialog = Dialog(parent, "About Embr", "Close", danger=False)
    dialog.cancel.hide()
    dialog.text(f"Embr {__version__} · built-in esptool {esptool_version} · GPL-3.0-or-later")
    for caption, url in (("Repository", REPOSITORY), ("Releases", RELEASES)):
        link = QLabel(f'{caption}: <a href="{url}">{url.removeprefix("https://")}</a>')
        link.setTextFormat(Qt.TextFormat.RichText)
        link.setOpenExternalLinks(False)
        link.linkActivated.connect(_open_url)
        dialog.body.addWidget(link)
    dialog.text("Third-party notices: THIRD_PARTY_NOTICES in the program folder.")
    dialog.exec()


def _open_url(href: str) -> None:
    QDesktopServices.openUrl(QUrl(href))
