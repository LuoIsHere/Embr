"""New, renamed and moved firmware and projects."""

from PySide6.QtWidgets import (
    QDialog,
    QLineEdit,
    QWidget,
)

from embr.app.library import Library
from embr.i18n import N_, tr
from embr.ui.dialogs.base import Dialog, Placement, PlacementFields

ARCHIVE_KEPT = N_(
    "Archived files are kept (use Clean archive to remove the ones no firmware refers to); "
    "flash records are not affected."
)
"""Deleting firmware or a project: what stays."""


def new_firmware(
    parent: QWidget, library: Library, project_id: str | None, device: str
) -> tuple[str, Placement] | None:
    dialog = Dialog(parent, tr("New firmware"), tr("Create"), danger=False)
    name = QLineEdit()
    name.setPlaceholderText(tr("e.g. S3-main-v1.3 (default: the first file added)"))
    dialog.field(tr("Name"), name)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        tr(
            "Fixed firmware: add entries by hand afterwards. Reset policy is copied from the "
            "project defaults; standalone firmware use default-reset / hard-reset."
        )
    )
    name.setFocus()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return name.text().strip(), placement.value()


def new_project(parent: QWidget) -> tuple[str, str] | None:
    dialog = Dialog(parent, tr("New project"), tr("Create"), danger=False)
    name = QLineEdit()
    name.setPlaceholderText(tr("e.g. 500_roller_hmi"))
    notes = QLineEdit()
    notes.setPlaceholderText(tr("hardware revision, purpose…"))
    dialog.field(tr("Name (unique)"), name)
    dialog.field(tr("Notes"), notes)
    dialog.text(tr("Defaults for new firmware: before=default-reset · after=hard-reset"))
    name.setFocus()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return name.text().strip(), notes.text().strip()


def move_firmware(
    parent: QWidget, library: Library, project_id: str | None, device: str
) -> Placement | None:
    dialog = Dialog(parent, tr("Move firmware"), tr("Move"), danger=False)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        tr(
            "Only the project and the group change; entries, files and records stay as they are. "
            "Or drag the firmware in the sidebar."
        )
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return placement.value()
