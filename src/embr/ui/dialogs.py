"""In-app dialogs (design doc §6.1 "对话框"): Esc closes, destructive buttons are red and
say what they affect."""

import contextlib
import html
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QShowEvent
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
from embr.app.archiving import ArchiveUsage
from embr.app.flashplan import PlanError
from embr.app.library import Library, PioPreview
from embr.app.merging import MergeOptions, Merger
from embr.core.firmware.import_idf import (
    IdfBuild,
    IdfBuildError,
    build_dir_below,
    default_firmware_name,
)
from embr.core.firmware.import_pio import PioBuild, PioBuildError, built_envs_below
from embr.core.firmware.model import Entry, Firmware
from embr.core.flasher.protocol import InfoEvent
from embr.core.storage.paths import AppPaths
from embr.ui.adapters.jobs import MainThreadDispatcher
from embr.ui.widgets import button, label

IMPORT_LABEL = "Import into Embr as a firmware"
"""Read flash and merge: keep the result in the firmware list too (checked by default)."""
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
    dialog = Dialog(parent, title, "Close", danger=False)
    dialog.cancel.hide()
    for line in lines:
        dialog.text(line)
    dialog.exec()


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
        dialog.row(("Place in", self.project), ("Group", self.group))
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


def settings(parent: QWidget, paths: AppPaths, library: Library) -> None:
    """Program folder (read-only) and the global framework package path for PlatformIO
    Arduino firmware (saved on Close; empty means auto-detect)."""
    dialog = Dialog(parent, "Settings", "Close", danger=False)
    dialog.cancel.hide()
    package = QLineEdit(library.framework_pkg_default())
    detected = library.detected_framework_pkg()
    package.setPlaceholderText(
        f"auto-detect (now: {detected})" if detected else "auto-detect (nothing found)"
    )
    browse = button("Browse…")
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(package, 1)
    row.addWidget(browse)

    def pick() -> None:
        chosen = QFileDialog.getExistingDirectory(dialog, "Framework package", package.text())
        if chosen:
            package.setText(chosen)

    browse.clicked.connect(pick)
    dialog.body.addWidget(
        label(
            "PlatformIO framework package (the framework-arduinoespressif32 folder that "
            "boot_app0.bin of Arduino firmware comes from). Empty: auto-detect. A firmware "
            "can override it on its Parameters tab.",
            tone="3",
            wrap=True,
        )
    )
    dialog.body.addLayout(row)
    folder = label(str(paths.root), mono=True, wrap=True)
    folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    dialog.field("Program folder", folder)
    dialog.field(
        "Data and log folders (portable, inside the program folder; fixed, not configurable)",
        label("data\\ · logs\\ · temp\\ · settings.ini", mono=True),
    )
    dialog.exec()
    if package.text().strip() != library.framework_pkg_default():
        library.set_framework_pkg_default(package.text())


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
    parent: QWidget,
    library: Library,
    project_id: str | None,
    device: str,
    show_firmware: Callable[[str], None],
    build_dir: Path | None = None,
) -> IdfImport | None:
    """`build_dir` fills in the folder (a folder dropped on the sidebar). Only a folder with
    flasher_args.json is accepted. A project folder gets a button to
    use its build folder (never replaced silently); a build folder another firmware already
    follows gets a button that closes the dialog and shows that firmware."""
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
    use_build = button("Use the build folder")
    show_existing = button("Show that firmware")
    redirect = QHBoxLayout()
    redirect.addWidget(use_build)
    redirect.addWidget(show_existing)
    redirect.addStretch(1)
    dialog.body.addLayout(redirect)
    below: list[Path] = []
    existing: list[str] = []
    chosen: list[str] = []
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
        below.clear()
        existing.clear()
        use_build.hide()
        show_existing.hide()
        text = folder.text().strip()
        if not text:
            preview.setText("")
            dialog.fit()
            return
        try:
            build = library.preview_idf(Path(text))
        except (IdfBuildError, OSError) as exc:
            child = build_dir_below(Path(text))
            if child is None:
                preview.setText(f"<b>{html.escape(str(exc))}</b>")
            else:
                below.append(child)
                preview.setText(
                    "<b>This is the project folder.</b> Pick its build folder, which contains "
                    f"flasher_args.json: {html.escape(str(child))}"
                )
                use_build.show()
            dialog.fit()
            return
        tracked = library.tracking_firmware_for(build.build_dir)
        if tracked is not None:
            existing.append(tracked.id)
            preview.setText(
                f"<b>'{html.escape(tracked.name)}' already follows this build folder.</b> "
                "A build folder is followed by one firmware only."
            )
            show_existing.show()
            dialog.fit()
            return
        found.append(build)
        files = ", ".join(f"{f.name} @ 0x{f.address:X}" for f in build.files)
        preview.setText(
            f"{html.escape(build.project_name or '?')} · {build.chip or '?'} · ESP-IDF "
            f"{html.escape(build.idf_version or '?')} · {build.flash_mode} {build.flash_freq} "
            f"{build.flash_size}<br>{html.escape(files)}"
        )
        name.setPlaceholderText(default_firmware_name(build))
        dialog.fit()

    def use_below() -> None:
        if below:
            folder.setText(str(below[0]))
            refresh()

    def show_it() -> None:
        if existing:
            chosen.append(existing[0])
            dialog.reject()

    use_build.clicked.connect(use_below)
    show_existing.clicked.connect(show_it)
    use_build.hide()
    show_existing.hide()

    def pick() -> None:
        chosen = QFileDialog.getExistingDirectory(dialog, "Build directory", folder.text())
        if chosen:
            folder.setText(chosen)
            refresh()

    browse.clicked.connect(pick)
    folder.editingFinished.connect(refresh)
    if build_dir is not None:
        folder.setText(str(build_dir))
        refresh()
    dialog.check = lambda: bool(found)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    if chosen:
        show_firmware(chosen[0])
    if not accepted or not found:
        return None
    return IdfImport(found[0].build_dir, name.text().strip(), placement.value())


@dataclass(frozen=True)
class PioImport:
    project_root: Path
    envs: tuple[str, ...]
    placement: Placement


def import_pio(
    parent: QWidget,
    library: Library,
    project_id: str | None,
    device: str,
    show_firmware: Callable[[str], None],
    build_dir: Path | None = None,
) -> PioImport | None:
    """Pick `<project>/.pio/build/<env>`; the project is read in the background (about two
    seconds for `pio project config`). Every env of platformio.ini is listed: built ones can
    be checked (the picked one is), unbuilt and already followed ones are greyed. All
    checked envs go to the same project and group (design doc §12.5)."""
    dialog = Dialog(parent, "Import PlatformIO build", "Import", danger=False)
    folder = QLineEdit()
    folder.setPlaceholderText(r"e.g. D:\work\my_project\.pio\build\esp32dev")
    browse = button("Browse…")
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(folder, 1)
    row.addWidget(browse)
    dialog.body.addWidget(label(r"Env build folder (.pio\build\<env>)", tone="3"))
    dialog.body.addLayout(row)
    status = label("", tone="2", wrap=True)
    status.setTextFormat(Qt.TextFormat.RichText)
    dialog.body.addWidget(status)
    use_env = button("Use this env")
    show_existing = button("Show that firmware")
    redirect = QHBoxLayout()
    redirect.addWidget(use_env)
    redirect.addWidget(show_existing)
    redirect.addStretch(1)
    dialog.body.addLayout(redirect)
    envs_box = QWidget()
    envs_layout = QVBoxLayout(envs_box)
    envs_layout.setContentsMargins(0, 0, 0, 0)
    envs_layout.setSpacing(2)
    dialog.field("Envs (each checked env becomes a firmware named after it)", envs_box)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        "Each firmware follows its env's build output: entries and flash settings are read "
        "from the files PlatformIO wrote. Embr never builds; when the board or the files "
        "change, it tells you. Rebuilds that only change file contents are followed "
        "automatically; other changes need confirming."
    )
    dispatcher = MainThreadDispatcher(dialog)
    found: list[PioPreview] = []
    boxes: list[tuple[QCheckBox, str]] = []
    below: list[Path] = []
    existing: list[str] = []
    chosen: list[str] = []
    generation = [0]

    def usable_checked() -> bool:
        return any(b.isChecked() and b.isEnabled() for b, _ in boxes)

    def sync_ok() -> None:
        dialog.ok.setEnabled(bool(found) and usable_checked())

    def clear() -> None:
        found.clear()
        below.clear()
        existing.clear()
        use_env.hide()
        show_existing.hide()
        for box, _ in boxes:
            box.deleteLater()
        boxes.clear()

    def refresh() -> None:
        generation[0] += 1
        ticket = generation[0]
        clear()
        text = folder.text().strip()
        if not text:
            status.setText("")
            sync_ok()
            dialog.fit()
            return
        path = Path(text)
        built = built_envs_below(path)
        if built:
            below.append(built[0].build_dir)
            names = ", ".join(html.escape(r.env) for r in built)
            status.setText(
                "<b>This is the project folder.</b> Pick an env build folder under "
                rf".pio\build: {names}"
            )
            use_env.setText(f"Use {built[0].env}")
            use_env.show()
            sync_ok()
            dialog.fit()
            return
        status.setText("Reading the PlatformIO project…")
        sync_ok()
        dialog.fit()

        def work() -> None:
            try:
                result: PioPreview | str = library.preview_pio(path)
            except (PioBuildError, OSError) as exc:
                result = str(exc)
            with contextlib.suppress(RuntimeError):  # the dialog is gone
                dispatcher.dispatch(lambda: shown(ticket, result))

        threading.Thread(target=work, name="pio-preview", daemon=True).start()

    def shown(ticket: int, result: PioPreview | str) -> None:
        if ticket != generation[0]:  # the folder changed or the dialog closed meanwhile
            return
        if isinstance(result, str):
            status.setText(f"<b>{html.escape(result)}</b>")
            dialog.fit()
            return
        found.append(result)
        lines = [f"Project {html.escape(str(result.picked.project_root))}"]
        if result.config_error:
            lines.append(
                f"pio project config gave nothing ({html.escape(result.config_error)}); boards "
                "are unknown and the board check is skipped until it works."
            )
        for choice in result.envs:
            box = QCheckBox(_env_text(choice.name, choice.board, choice.build))
            picked = choice.name == result.picked.env
            usable = choice.built and choice.tracked_by is None and choice.build is not None
            box.setEnabled(usable)
            box.setChecked(usable and picked)
            if not choice.built:
                box.setText(f"{choice.name} · not built")
            elif choice.tracked_by is not None:
                follower = library.firmware(choice.tracked_by).name
                box.setText(f"{choice.name} · already followed by '{follower}'")
                if picked:
                    existing.append(choice.tracked_by)
                    show_existing.show()
            elif choice.problem:
                box.setText(f"{choice.name} · {choice.problem}")
            if choice.build is not None:
                box.setToolTip(_build_text(choice.build))
            box.toggled.connect(sync_ok)
            envs_layout.addWidget(box)
            boxes.append((box, choice.name))
        status.setText("<br>".join(lines))
        sync_ok()
        dialog.fit()
        QTimer.singleShot(0, dialog.fit)  # the new check boxes are laid out on the next pass

    def use_below() -> None:
        if below:
            folder.setText(str(below[0]))
            refresh()

    def show_it() -> None:
        if existing:
            chosen.append(existing[0])
            dialog.reject()

    def pick() -> None:
        picked = QFileDialog.getExistingDirectory(dialog, "Env build folder", folder.text())
        if picked:
            folder.setText(picked)
            refresh()

    use_env.clicked.connect(use_below)
    show_existing.clicked.connect(show_it)
    use_env.hide()
    show_existing.hide()
    browse.clicked.connect(pick)
    folder.editingFinished.connect(refresh)
    if build_dir is not None:
        folder.setText(str(build_dir))
        refresh()
    sync_ok()
    dialog.check = lambda: bool(found) and usable_checked()
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    generation[0] += 1  # drop a preview that arrives after the dialog closed
    if chosen:
        show_firmware(chosen[0])
    if not accepted or not found:
        return None
    envs = tuple(name for box, name in boxes if box.isChecked() and box.isEnabled())
    return PioImport(found[0].picked.project_root, envs, placement.value())


def _env_text(name: str, board: str | None, build: PioBuild | None) -> str:
    parts = [name, board or "board unknown"]
    if build is not None:
        parts.append(build.chip or "chip unknown")
        flash = build.flash
        parts.append(f"{flash.mode} {flash.freq} {flash.size}")
    return " · ".join(parts)


def _build_text(build: PioBuild) -> str:
    lines = [
        f"0x{f.address:X}  {f.name}" + (f"  ({f.note})" if f.note else "") for f in build.files
    ]
    lines += build.notes
    return "\n".join(lines)


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


def clean_archive(
    parent: QWidget,
    usage: list[ArchiveUsage],
    names: Mapping[str, str],
    show_firmware: Callable[[str], None],
) -> list[str]:
    """What the archive holds and why files are kept; returns the hashes to delete (the
    unreferenced ones, after confirming), else []. `names`: firmware id -> name. A firmware
    name is a link that closes the dialog and shows that firmware."""
    unused = [u.file for u in usage if not u.firmware_ids]
    kept = [u for u in usage if u.firmware_ids]
    dialog = Dialog(
        parent, "Clean archive", "Delete files" if unused else "Close", danger=bool(unused)
    )
    if not unused:
        dialog.cancel.hide()
    total = sum(u.file.size for u in usage)
    dialog.text(f"The archive holds {len(usage)} files ({total:,} bytes).")
    if kept:
        users: dict[str, int] = {}
        for u in kept:
            for firmware_id in u.firmware_ids:
                users[firmware_id] = users.get(firmware_id, 0) + 1
        links = ", ".join(
            f'<a href="{html.escape(fid)}">{html.escape(names.get(fid, fid))}</a> ({count})'
            for fid, count in sorted(users.items(), key=lambda item: names.get(item[0], ""))
        )
        kept_text = dialog.text(
            f"{len(kept)} files are kept because firmware refers to them: {links}. Archived "
            "versions are firmware too; delete one of them first to free its files."
        )
        chosen: list[str] = []

        def show(firmware_id: str) -> None:
            chosen.append(firmware_id)
            dialog.reject()

        kept_text.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        kept_text.linkActivated.connect(show)
    else:
        chosen = []
    if unused:
        size = sum(f.size for f in unused)
        lines = "".join(
            f"<br>•&nbsp;{f.sha256[:12]}… {f.size:,} bytes, archived {f.first_archived[:10]}"
            for f in unused[:12]
        )
        more = f"<br>…and {len(unused) - 12} more" if len(unused) > 12 else ""
        dialog.text(
            f"<b>Delete {len(unused)} files ({size:,} bytes) that no firmware refers to?</b> "
            f"This cannot be undone.{lines}{more}"
        )
    else:
        dialog.text("<b>Nothing to clean:</b> every archived file is in use.")
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    if chosen:
        show_firmware(chosen[0])
    return [f.sha256 for f in unused] if accepted and unused else []


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
    add = QCheckBox(IMPORT_LABEL)
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
        dialog.ok.setEnabled(False)
        if chosen is None:
            command.setText("The start offset is not a hex number.")
            return
        try:
            command.setText(merger.command(firmware.id, Path(output.text()), chosen))
        except (PlanError, ValueError) as exc:  # e.g. checked entries for two chips
            command.setText(str(exc))
            return
        dialog.ok.setEnabled(True)

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
    dialog = Dialog(parent, "Read flash", "Read", danger=False)
    whole = QCheckBox(f"whole flash ({info.flash_size or 'detected size'})")
    whole.setChecked(True)
    dialog.body.addWidget(whole)
    start = QLineEdit("0x0")
    size = QLineEdit("0x100000")
    range_row = dialog.row(("Start address (hex)", start), ("Size (hex)", size))
    mac = (info.mac or "unknown").replace(":", "")
    output = QLineEdit(str(folder / f"{info.chip}_{mac}_{datetime.now():%Y%m%d-%H%M%S}.bin"))
    browse = button("Browse…")
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(output, 1)
    row.addWidget(browse)
    dialog.body.addWidget(label("Save to", tone="3"))
    dialog.body.addLayout(row)
    add = QCheckBox(IMPORT_LABEL)
    add.setChecked(True)
    dialog.body.addWidget(add)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        "Imported, the content is also kept in the archive as a new fixed firmware with one "
        "merged entry. A dump includes NVS and may contain Wi-Fi credentials and other secrets."
    )

    def sync() -> None:
        range_row.setVisible(not whole.isChecked())
        placement.project.setEnabled(add.isChecked())
        placement.group.setEnabled(add.isChecked() and placement.value().project_id is not None)
        if dialog.isVisible():
            dialog.fit()

    def pick() -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            dialog, "Save flash content", output.text(), "Binary files (*.bin)"
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
        if not path.exists():
            return True
        accepted, _ = confirm(
            dialog,
            "Replace file",
            [
                f"<b>{html.escape(path.name)}</b> already exists in "
                f"{html.escape(str(path.parent))}. Replace it?"
            ],
            "Replace",
        )
        return accepted

    dialog.check = lambda: parsed() is not None and target_ok()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    found = parsed()
    if found is None:
        return None
    return ReadRequest(
        found[0], found[1], placement.value(), Path(output.text().strip()), add.isChecked()
    )
