"""Board operations that ask first: merge, erase, read flash."""

import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QLineEdit,
    QWidget,
)

from embr.app.flashplan import PlanError
from embr.app.library import Library
from embr.app.merging import MergeOptions, Merger
from embr.core import chipinfo
from embr.core.firmware.entries import AddressError, parse_address
from embr.core.firmware.model import Firmware
from embr.core.flasher.protocol import InfoEvent
from embr.i18n import N_, tr
from embr.ui.dialogs.base import Dialog, Placement, PlacementFields, confirm, replace_ok
from embr.ui.widgets import browse_row, label

IMPORT_LABEL = N_("Import into Embr as a firmware")
"""Read flash and merge: keep the result in the firmware list too (checked by default)."""


def merge(
    parent: QWidget, merger: Merger, firmware: Firmware, default_output: Path
) -> tuple[Path, MergeOptions] | None:
    """Asks before replacing an existing file (naming the firmware that use it); a folder
    as the output keeps Merge disabled."""
    dialog = Dialog(parent, tr("Merge to .bin"), tr("Merge"), danger=False)
    offset = QLineEdit("0x0")
    dialog.field(tr("Start offset (hex)"), offset)
    pad = QCheckBox(tr("pad to the whole flash size"))
    dialog.body.addWidget(pad)
    output = QLineEdit(str(default_output))
    row, browse = browse_row(output)
    dialog.body.addWidget(label(tr("Save as"), tone="3"))
    dialog.body.addLayout(row)
    add = QCheckBox(tr(IMPORT_LABEL))
    add.setChecked(True)
    dialog.body.addWidget(add)
    dialog.text(
        tr(
            "Gaps are filled with 0xFF, so every partition inside the written range is overwritten "
            "(for example, the NVS partition of the default layout is cleared). A padded image "
            "overwrites the whole flash."
        )
    )
    size = label("", tone="2")
    dialog.body.addWidget(size)
    command = label("", tone="3", wrap=True, mono=True)
    dialog.body.addWidget(command)

    def options() -> MergeOptions | None:
        try:
            start = parse_address(offset.text())
        except AddressError:
            return None
        return MergeOptions(start, pad.isChecked(), add.isChecked())

    def refresh() -> None:
        chosen = options()
        dialog.ok.setEnabled(False)
        size.hide()
        if chosen is None:
            command.setText(tr("The start offset is not a hex number."))
            return
        if output.text().strip() and Path(output.text().strip()).is_dir():
            command.setText(tr("Save as is a folder; enter a file name."))
            return
        try:
            command.setText(merger.command(firmware.id, Path(output.text()), chosen))
        except (PlanError, ValueError) as exc:  # e.g. checked entries for two chips
            command.setText(tr(str(exc)))
            return
        dialog.ok.setEnabled(True)
        try:
            merged = merger.size(firmware.id, chosen)
        except OSError:  # a file gone meanwhile: merging reports it
            merged = None
        if merged is not None:
            size.setText(tr(f"Merged image size: {size_text(merged)} ({merged:,} bytes)"))
            size.show()

    def pick() -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            dialog, tr("Save merged image"), output.text(), tr("Binary files (*.bin)")
        )
        if chosen:
            output.setText(chosen)
            refresh()

    browse.clicked.connect(pick)
    for edit in (offset, output):
        edit.editingFinished.connect(refresh)
    for box in (pad, add):
        box.toggled.connect(refresh)
    refresh()

    def target_ok() -> bool:
        text = output.text().strip()
        if not text or Path(text).is_dir():
            return False
        return replace_ok(dialog, Path(text), merger.users(Path(text)))

    dialog.check = lambda: options() is not None and target_ok()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    chosen = options()
    return (Path(output.text().strip()), chosen) if chosen else None


def size_text(size: int) -> str:
    """1310720 -> "1.25 MB", 8192 -> "8 KB" (binary units, as flash sizes are written);
    below 1 KB in bytes ("512 B")."""
    for unit, factor in (("MB", 1024 * 1024), ("KB", 1024)):
        if size >= factor:
            value = f"{size / factor:.2f}".rstrip("0").rstrip(".")
            return f"{value} {unit}"
    return f"{size} B"


def erase_flash(parent: QWidget, port: str, info: InfoEvent) -> bool:
    accepted, _ = confirm(
        parent,
        tr("Erase flash"),
        [
            tr(
                f"Erase the whole flash of the {chipinfo.display_name(info.chip)} on "
                f"<b>{html.escape(port)}"
                f"</b> (MAC {html.escape(info.mac or '?')})?"
            ),
            tr(
                "Everything is cleared, including NVS (Wi-Fi credentials and other stored data); "
                "the firmware on the board will no longer start. A record is kept."
            ),
        ],
        tr("Erase flash"),
    )
    return accepted


@dataclass(frozen=True)
class ReadRequest:
    address: int
    size: int | None
    """None: to the end of the flash."""
    placement: Placement
    output: Path
    import_firmware: bool


def read_flash(
    parent: QWidget,
    library: Library,
    info: InfoEvent,
    project_id: str | None,
    device: str,
    folder: Path,
) -> ReadRequest | None:
    """`folder`: where the file goes by default (the last one used)."""
    dialog = Dialog(parent, tr("Read flash"), tr("Read"), danger=False)
    whole = QCheckBox(
        tr(f"whole flash ({info.flash_size})")
        if info.flash_size
        else tr("whole flash (size not detected)")
    )
    whole.setChecked(True)
    dialog.body.addWidget(whole)
    start = QLineEdit("0x0")
    size = QLineEdit("0x100000")
    range_row = dialog.row((tr("Start address (hex)"), start), (tr("Size (hex)"), size))
    mac = (info.mac or "unknown").replace(":", "")
    output = QLineEdit(str(folder / f"{info.chip}_{mac}_{datetime.now():%Y%m%d-%H%M%S}.bin"))
    row, browse = browse_row(output)
    dialog.body.addWidget(label(tr("Save to"), tone="3"))
    dialog.body.addLayout(row)
    add = QCheckBox(tr(IMPORT_LABEL))
    add.setChecked(True)
    dialog.body.addWidget(add)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        tr(
            "When imported, the content is also kept in the archive as a new fixed firmware with "
            "one "
            "merged entry. A dump includes NVS and may contain Wi-Fi credentials and other secrets."
        )
    )

    def sync() -> None:
        range_row.setVisible(not whole.isChecked())
        placement.project.setEnabled(add.isChecked())
        placement.group.setEnabled(add.isChecked() and placement.value().project_id is not None)
        if dialog.isVisible():
            dialog.fit()

    def pick() -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            dialog, tr("Save flash content"), output.text(), tr("Binary files (*.bin)")
        )
        if chosen:
            output.setText(chosen)

    whole.toggled.connect(sync)
    add.toggled.connect(sync)
    browse.clicked.connect(pick)
    sync()

    def parsed() -> tuple[int, int | None] | None:
        if whole.isChecked():
            return 0, None
        try:
            address, length = int(start.text().strip(), 16), int(size.text().strip(), 16)
        except ValueError:
            return None
        return (address, length) if address >= 0 and length > 0 else None

    def target_ok() -> bool:
        path = Path(output.text().strip())
        if not output.text().strip() or path.is_dir():
            return False
        return replace_ok(dialog, path)

    dialog.check = lambda: parsed() is not None and target_ok()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    found = parsed()
    if found is None:
        return None
    return ReadRequest(
        found[0], found[1], placement.value(), Path(output.text().strip()), add.isChecked()
    )
