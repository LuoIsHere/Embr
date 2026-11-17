"""Exporting and importing .embr packages."""

import html
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QLabel,
    QLineEdit,
    QWidget,
)

from embr.app.library import Library
from embr.app.packages import ExportCheck, default_group, default_package_name
from embr.core.firmware.model import Firmware
from embr.core.package import PACKAGE_SUFFIX, Package
from embr.i18n import N_, tr
from embr.ui.dialogs.base import Dialog, Placement, PlacementFields, notice, replace_ok
from embr.ui.widgets import browse_row, label, warning_label

PACKAGE_FILTER = N_("Embr packages (*.embr)")


def export_firmware(
    parent: QWidget,
    firmware: Sequence[Firmware],
    chosen_id: str,
    check: ExportCheck,
    folder: Path,
) -> tuple[list[str], Path] | None:
    """Export… of a firmware: the firmware of its scope with check
    boxes, the chosen one checked; blocked ones greyed with the reason. Returns the ids to
    export (in list order) and the package path."""
    dialog = Dialog(parent, tr("Export firmware"), tr("Export"), danger=False)
    chosen = next((f for f in firmware if f.id == chosen_id), firmware[0])
    scope = tr("Firmware in this project") if chosen.project_id else tr("Standalone firmware")
    dialog.text(
        tr(
            f"{scope}; check the ones to put into the package. Tracking firmware is exported as "
            "fixed firmware with its current entries."
        )
    )
    blocked = {b.firmware_id: b for b in check.blocked}
    boxes: list[tuple[QCheckBox, str]] = []
    for item in firmware:
        reason = blocked.get(item.id)
        box = QCheckBox(item.name if reason is None else f"{item.name} — {tr(reason.reason)}")
        box.setChecked(reason is None and item.id == chosen.id)
        box.setEnabled(reason is None)
        if reason is not None:
            box.setToolTip("\n".join(tr(d) for d in reason.details) or tr(reason.reason))
        dialog.body.addWidget(box)
        boxes.append((box, item.id))
    dump = _warning(dialog, "")
    output = _save_field(dialog, folder / default_package_name(chosen.name))
    dumps = {f.id for f in firmware if f.name in check.with_dump}

    def picked() -> list[str]:
        return [fid for box, fid in boxes if box.isChecked()]

    def sync() -> None:
        ids = picked()
        names = [f.name for f in firmware if f.id in ids and f.id in dumps]
        dump.setText(_dump_warning(names))
        dump.setVisible(bool(names))
        dialog.ok.setEnabled(bool(ids))
        if dialog.isVisible():
            dialog.fit()

    for box, _ in boxes:
        box.toggled.connect(sync)
    sync()
    dialog.check = lambda: bool(picked()) and _target_ok(dialog, output)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return picked(), _package_path(output)


def export_project(
    parent: QWidget, project_name: str, count: int, check: ExportCheck, folder: Path
) -> Path | None:
    """Export project…: refused with the reasons when any firmware cannot be exported, else the
    package path."""
    if check.blocked:
        lines = "".join(
            f"<br>•&nbsp;{html.escape(b.name)} — {html.escape(tr(b.reason))}" for b in check.blocked
        )
        notice(
            parent,
            tr("Export project"),
            [
                tr(
                    f"<b>{html.escape(project_name)}</b> cannot be exported now; every firmware "
                    f"in it must be exportable:{lines}"
                ),
                tr("Fix these first, or use Export… in a firmware menu to export only some."),
            ],
        )
        return None
    dialog = Dialog(parent, tr("Export project"), tr("Export"), danger=False)
    dialog.text(
        tr(
            f"Exports <b>{html.escape(project_name)}</b> with its groups and all {count} "
            "firmware, and every file they use, into one .embr package. Tracking firmware is "
            "exported as fixed firmware with its current entries."
        )
    )
    if check.with_dump:
        _warning(dialog, _dump_warning(list(check.with_dump)))
    output = _save_field(dialog, folder / default_package_name(project_name))
    dialog.check = lambda: _target_ok(dialog, output)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return _package_path(output)


def import_package(
    parent: QWidget,
    library: Library,
    package: Package,
    file_name: str,
    project_id: str | None,
    device: str | None = None,
) -> Placement | None:
    """What a checked package holds; where its loose firmware go. Default: `project_id`
    and `device` (a package dropped on the sidebar: the drop point); with
    `device` None, the group they all had when that project has it, else ungrouped (also
    after the project is changed in the dialog). Returns the placement (None project for a
    package without loose firmware)."""
    dialog = Dialog(parent, tr("Import .embr package"), tr("Import"), danger=False)
    manifest = package.manifest
    dialog.text(
        tr(
            f"<b>{html.escape(file_name)}</b> · exported {manifest.exported_at.replace('T', ' ')} "
            f"with Embr {manifest.embr_version}, esptool {manifest.esptool_version}"
        )
    )
    lines: list[str] = []
    for project in package.projects:
        count = sum(1 for f in package.firmwares if f.project_id == project.id)
        lines.append(tr(f"Project <b>{html.escape(project.name)}</b> with {count} firmware"))
    loose = [f for f in package.firmwares if f.project_id is None]
    for item in loose:
        group = f" ({html.escape(item.device)})" if item.device else ""
        lines.append(f"{html.escape(item.name)}{group}")
    size = sum(f.size for f in manifest.files)
    dialog.text("<br>".join(f"•&nbsp;{line}" for line in lines))
    dialog.text(
        tr(f"1 file ({size:,} bytes); it matches the package's list.")
        if len(manifest.files) == 1
        else tr(
            f"{len(manifest.files)} files ({size:,} bytes); every file matches the package's list."
        )
    )
    placement: PlacementFields | None = None
    if loose:
        if device is None:
            groups = library.project(project_id).devices if project_id is not None else []
            device = default_group(package, groups)
        fields = PlacementFields(dialog, library, project_id, device)

        def regroup() -> None:
            chosen = fields.value().project_id
            if chosen is not None:
                group = default_group(package, library.project(chosen).devices)
                fields.group.setCurrentIndex(max(fields.group.findData(group), 0))

        fields.project.currentIndexChanged.connect(regroup)
        placement = fields
    dialog.text(
        tr(
            "Everything is created anew as archived firmware; the files go into the archive. "
            "Names that already exist get a numeric suffix."
        )
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return placement.value() if placement is not None else Placement(None, "")


def _warning(dialog: Dialog, text: str) -> QLabel:
    """Rich text in the warning hint style (as above the Flash button)."""
    widget = warning_label(text)
    widget.setTextFormat(Qt.TextFormat.RichText)
    dialog.body.addWidget(widget)
    return widget


def _dump_warning(names: list[str]) -> str:
    listed = ", ".join(html.escape(n) for n in names)
    return tr(
        f"<b>{listed}</b>: data read from a device. The package may hold Wi-Fi credentials "
        "and other secrets stored in NVS."
    )


def _save_field(dialog: Dialog, default: Path) -> QLineEdit:
    output = QLineEdit(str(default))
    row, browse = browse_row(output)
    dialog.body.addWidget(label(tr("Save as"), tone="3"))
    dialog.body.addLayout(row)

    def pick() -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            dialog, tr("Save package"), output.text(), tr(PACKAGE_FILTER)
        )
        if chosen:
            output.setText(chosen)

    browse.clicked.connect(pick)
    return output


def _package_path(output: QLineEdit) -> Path:
    path = Path(output.text().strip())
    if path.suffix.lower() == PACKAGE_SUFFIX:
        return path
    return path.with_name(path.name + PACKAGE_SUFFIX)


def _target_ok(dialog: Dialog, output: QLineEdit) -> bool:
    if not output.text().strip():
        return False
    path = _package_path(output)
    if path.is_dir() or not path.parent.is_dir():
        return False
    return replace_ok(dialog, path)
