"""In-app dialogs (design doc §6.1 "对话框"): Esc closes, destructive buttons are red and
say what they affect."""

import html
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from embr import __version__
from embr.app.flashplan import PlanError
from embr.app.library import Library
from embr.app.merging import MergeOptions, Merger
from embr.core.archive import ArchiveFile
from embr.core.firmware.import_idf import IdfBuild, IdfBuildError, default_firmware_name
from embr.core.firmware.model import Entry, Firmware
from embr.core.flasher.protocol import InfoEvent
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
        self.check: Callable[[], bool] = lambda: True
        """Input check run by validate()."""
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


# ---- stage two: IDF import, archive, merge, erase, read flash, board records ----------


@dataclass(frozen=True)
class IdfImport:
    build_dir: Path
    name: str
    placement: Placement


def import_idf(
    parent: QWidget, library: Library, project_id: str | None, device: str
) -> IdfImport | None:
    dialog = Dialog(parent, "Import ESP-IDF build", "Import", danger=False)
    folder = QLineEdit()
    folder.setPlaceholderText(r"e.g. D:\work\my_project\build")
    browse = button("Browse…")
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(folder, 1)
    row.addWidget(browse)
    dialog.body.addWidget(label("Build directory (contains flasher_args.json)", tone="3"))
    dialog.body.addLayout(row)
    preview = label("", tone="2", wrap=True)
    preview.setTextFormat(Qt.TextFormat.RichText)
    dialog.body.addWidget(preview)
    name = QLineEdit()
    dialog.field("Name", name)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        "The firmware follows the build: entries, chip, flash settings and reset modes are read "
        "from it each time. Rebuilds that only change file contents are followed automatically; "
        "other changes need confirming."
    )
    found: list[IdfBuild] = []

    def refresh() -> None:
        found.clear()
        text = folder.text().strip()
        if not text:
            preview.setText("")
            return
        try:
            build = library.preview_idf(Path(text))
        except (IdfBuildError, OSError) as exc:
            preview.setText(f"<b>{html.escape(str(exc))}</b>")
            return
        found.append(build)
        files = ", ".join(f"{f.name} @ 0x{f.address:X}" for f in build.files)
        preview.setText(
            f"{html.escape(build.project_name or '?')} · {build.chip or '?'} · ESP-IDF "
            f"{html.escape(build.idf_version or '?')} · {build.flash_mode} {build.flash_freq} "
            f"{build.flash_size}<br>{html.escape(files)}"
        )
        name.setPlaceholderText(default_firmware_name(build))

    def pick() -> None:
        chosen = QFileDialog.getExistingDirectory(dialog, "Build directory", folder.text())
        if chosen:
            folder.setText(chosen)
            refresh()

    browse.clicked.connect(pick)
    folder.editingFinished.connect(refresh)
    dialog.check = lambda: bool(found)
    if dialog.exec() != QDialog.DialogCode.Accepted or not found:
        return None
    return IdfImport(found[0].build_dir, name.text().strip(), placement.value())


def archive_version(
    parent: QWidget, source_name: str, default_name: str, entries: list[Entry]
) -> tuple[str, str] | None:
    dialog = Dialog(parent, "Archive this version", "Archive", danger=False)
    name = QLineEdit(default_name)
    dialog.field("Name", name)
    notes = QLineEdit()
    notes.setPlaceholderText("why this version is kept")
    dialog.field("Notes", notes)
    copied = [e for e in entries if e.file.startswith("path:")]
    lines = "".join(f"<br>•&nbsp;{html.escape(e.name)} ({e.size:,} bytes)" for e in copied)
    dialog.text(
        f"A new archived firmware is created next to <b>{html.escape(source_name)}</b>, which "
        f"stays as it is. Files copied into data/archive/:{lines}"
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return name.text().strip(), notes.text().strip()


def clean_archive(parent: QWidget, files: list[ArchiveFile]) -> bool:
    if not files:
        confirm(parent, "Clean archive", ["No unreferenced archive files."], "Close", danger=False)
        return False
    total = sum(f.size for f in files)
    lines = "".join(
        f"<br>•&nbsp;{f.sha256[:12]}… {f.size:,} bytes, archived {f.first_archived[:10]}"
        for f in files[:12]
    )
    more = f"<br>…and {len(files) - 12} more" if len(files) > 12 else ""
    accepted, _ = confirm(
        parent,
        "Clean archive",
        [
            f"Delete {len(files)} archive files ({total:,} bytes) that no firmware refers to? "
            f"This cannot be undone.{lines}{more}"
        ],
        "Delete files",
    )
    return accepted


def merge(
    parent: QWidget, merger: Merger, firmware: Firmware, default_output: Path
) -> tuple[Path, MergeOptions] | None:
    dialog = Dialog(parent, "Merge to .bin", "Merge", danger=False)
    offset = QLineEdit("0x0")
    dialog.field("Start offset (hex)", offset)
    pad = QCheckBox("pad to the whole flash size")
    dialog.body.addWidget(pad)
    output = QLineEdit(str(default_output))
    browse = button("Browse…")
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(output, 1)
    row.addWidget(browse)
    dialog.body.addWidget(label("Save as", tone="3"))
    dialog.body.addLayout(row)
    add = QCheckBox("add the result to the list as a new firmware")
    add.setChecked(True)
    dialog.body.addWidget(add)
    dialog.text(
        "Gaps are filled with 0xFF, so every partition inside the written range is overwritten "
        "(the NVS between the partition table and otadata is cleared). A padded image overwrites "
        "the whole flash."
    )
    command = label("", tone="3", wrap=True, mono=True)
    dialog.body.addWidget(command)

    def options() -> MergeOptions | None:
        try:
            start = int(offset.text().strip(), 16)
        except ValueError:
            return None
        return MergeOptions(start, pad.isChecked(), add.isChecked())

    def refresh() -> None:
        chosen = options()
        if chosen is None:
            command.setText("The start offset is not a hex number.")
            return
        try:
            command.setText(merger.command(firmware.id, Path(output.text()), chosen))
        except (PlanError, ValueError) as exc:
            command.setText(str(exc))

    def pick() -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            dialog, "Save merged image", output.text(), "Binary files (*.bin)"
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
    dialog.check = lambda: options() is not None and bool(output.text().strip())
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    chosen = options()
    return (Path(output.text().strip()), chosen) if chosen else None


def erase_flash(parent: QWidget, port: str, info: InfoEvent) -> bool:
    accepted, _ = confirm(
        parent,
        "Erase flash",
        [
            f"Erase the whole flash of the {html.escape(info.chip)} on <b>{html.escape(port)}"
            f"</b> (MAC {html.escape(info.mac or '?')})?",
            "Everything is cleared, including NVS (Wi-Fi credentials and other stored data); "
            "the firmware on the device will no longer start. A record is kept.",
        ],
        "Erase flash",
    )
    return accepted


@dataclass(frozen=True)
class ReadRequest:
    address: int
    size: int | None
    """None: to the end of the flash."""
    placement: Placement


def read_flash(
    parent: QWidget, library: Library, info: InfoEvent, project_id: str | None, device: str
) -> ReadRequest | None:
    dialog = Dialog(parent, "Read flash", "Read", danger=False)
    whole = QCheckBox(f"whole flash ({info.flash_size or 'detected size'})")
    whole.setChecked(True)
    dialog.body.addWidget(whole)
    start = QLineEdit("0x0")
    size = QLineEdit("0x100000")
    dialog.field("Start address (hex)", start)
    dialog.field("Size (hex)", size)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        "The content is saved in the archive as a new fixed firmware with one merged entry. A "
        "dump includes NVS and may contain Wi-Fi credentials and other secrets."
    )

    def sync() -> None:
        start.setEnabled(not whole.isChecked())
        size.setEnabled(not whole.isChecked())

    whole.toggled.connect(sync)
    sync()

    def parsed() -> tuple[int, int | None] | None:
        if whole.isChecked():
            return 0, None
        try:
            address, length = int(start.text().strip(), 16), int(size.text().strip(), 16)
        except ValueError:
            return None
        return (address, length) if address >= 0 and length > 0 else None

    dialog.check = lambda: parsed() is not None
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    found = parsed()
    if found is None:
        return None
    return ReadRequest(found[0], found[1], placement.value())
