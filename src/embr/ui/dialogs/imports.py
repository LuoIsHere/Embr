"""Importing ESP-IDF and PlatformIO builds."""

import contextlib
import html
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from embr.app.library import Library, PioPreview
from embr.core.firmware.import_idf import (
    IdfBuild,
    IdfBuildError,
    build_dir_below,
    default_firmware_name,
)
from embr.core.firmware.import_pio import PioBuild, PioBuildError, built_envs_below
from embr.i18n import tr
from embr.ui.adapters.jobs import MainThreadDispatcher
from embr.ui.dialogs.base import Dialog, Placement, PlacementFields
from embr.ui.widgets import browse_row, button, label

# ---- IDF import, archive, merge, erase, read flash, board records ---------------------


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
    dialog = Dialog(parent, tr("Import ESP-IDF build"), tr("Import"), danger=False)
    folder = QLineEdit()
    folder.setPlaceholderText(tr(r"e.g. D:\work\my_project\build"))
    row, browse = browse_row(folder)
    dialog.body.addWidget(label(tr("Build directory (contains flasher_args.json)"), tone="3"))
    dialog.body.addLayout(row)
    preview = label("", tone="2", wrap=True)
    preview.setTextFormat(Qt.TextFormat.RichText)
    dialog.body.addWidget(preview)
    use_build = button(tr("Use the build folder"))
    show_existing = button(tr("Show that firmware"))
    redirect = QHBoxLayout()
    redirect.addWidget(use_build)
    redirect.addWidget(show_existing)
    redirect.addStretch(1)
    dialog.body.addLayout(redirect)
    below: list[Path] = []
    existing: list[str] = []
    chosen: list[str] = []
    name = QLineEdit()
    dialog.field(tr("Name"), name)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        tr(
            "The firmware follows the build: entries, chip, flash settings and reset modes are "
            "read from it each time. Rebuilds that only change file contents are followed "
            "automatically; other changes need confirming."
        )
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
                preview.setText(f"<b>{html.escape(tr(str(exc)))}</b>")
            else:
                below.append(child)
                preview.setText(
                    tr(
                        "<b>This is the project folder.</b> Pick its build folder, which contains "
                        f"flasher_args.json: {html.escape(str(child))}"
                    )
                )
                use_build.show()
            dialog.fit()
            return
        tracked = library.tracking_firmware_for(build.build_dir)
        if tracked is not None:
            existing.append(tracked.id)
            preview.setText(
                tr(
                    f"<b>'{html.escape(tracked.name)}' already follows this build folder.</b> "
                    "A build folder is followed by one firmware only."
                )
            )
            show_existing.show()
            dialog.fit()
            return
        found.append(build)
        files = ", ".join(f"{f.name} @ 0x{f.address:X}" for f in build.files)
        preview.setText(
            tr(
                f"{html.escape(build.project_name or '?')} · {build.chip or '?'} · ESP-IDF "
                f"{html.escape(build.idf_version or '?')} · {build.flash_mode} {build.flash_freq} "
                f"{build.flash_size}<br>{html.escape(files)}"
            )
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
        chosen = QFileDialog.getExistingDirectory(dialog, tr("Build directory"), folder.text())
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
    checked envs go to the same project and group."""
    dialog = Dialog(parent, tr("Import PlatformIO build"), tr("Import"), danger=False)
    folder = QLineEdit()
    folder.setPlaceholderText(tr(r"e.g. D:\work\my_project\.pio\build\esp32dev"))
    row, browse = browse_row(folder)
    dialog.body.addWidget(label(tr(r"Env build folder (.pio\build\<env>)"), tone="3"))
    dialog.body.addLayout(row)
    status = label("", tone="2", wrap=True)
    status.setTextFormat(Qt.TextFormat.RichText)
    dialog.body.addWidget(status)
    use_env = button(tr("Use this env"))
    show_existing = button(tr("Show that firmware"))
    redirect = QHBoxLayout()
    redirect.addWidget(use_env)
    redirect.addWidget(show_existing)
    redirect.addStretch(1)
    dialog.body.addLayout(redirect)
    envs_box = QWidget()
    envs_layout = QVBoxLayout(envs_box)
    envs_layout.setContentsMargins(0, 0, 0, 0)
    envs_layout.setSpacing(2)
    dialog.field(tr("Envs (each checked env becomes a firmware named after it)"), envs_box)
    placement = PlacementFields(dialog, library, project_id, device)
    dialog.text(
        tr(
            "Each firmware follows its env's build output: entries and flash settings are read "
            "from the files PlatformIO wrote. Embr never builds; when the board or the files "
            "change, it tells you. Rebuilds that only change file contents are followed "
            "automatically; other changes need confirming."
        )
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
                tr(
                    "<b>This is the project folder.</b> Pick an env build folder under "
                    rf".pio\build: {names}"
                )
            )
            use_env.setText(tr(f"Use {built[0].env}"))
            use_env.show()
            sync_ok()
            dialog.fit()
            return
        status.setText(tr("Reading the PlatformIO project…"))
        sync_ok()
        dialog.fit()
        followers = library.pio_followers()  # the firmware files are read here, not in work()

        def work() -> None:
            try:
                result: PioPreview | str = library.preview_pio(path, followers)
            except (PioBuildError, OSError) as exc:
                result = tr(str(exc))
            with contextlib.suppress(RuntimeError):  # the dialog is gone
                dispatcher.dispatch(lambda: shown(ticket, result))

        threading.Thread(target=work, name="pio-preview", daemon=True).start()

    def shown(ticket: int, result: PioPreview | str) -> None:
        if ticket != generation[0]:  # the folder changed or the dialog closed meanwhile
            return
        if isinstance(result, str):
            status.setText(f"<b>{html.escape(tr(result))}</b>")
            dialog.fit()
            return
        found.append(result)
        lines = [tr(f"Project {html.escape(str(result.picked.project_root))}")]
        if result.config_error:
            lines.append(
                tr(
                    f"pio project config failed: {html.escape(tr(result.config_error))}. "
                    "Boards are unknown and the board check is skipped."
                )
            )
        for choice in result.envs:
            box = QCheckBox(_env_text(choice.name, choice.board, choice.build))
            picked = choice.name == result.picked.env
            usable = choice.built and choice.tracked_by is None and choice.build is not None
            box.setEnabled(usable)
            box.setChecked(usable and picked)
            if not choice.built:
                box.setText(tr(f"{choice.name} · not built"))
            elif choice.tracked_by is not None:
                follower = library.firmware(choice.tracked_by).name
                box.setText(tr(f"{choice.name} · already followed by '{follower}'"))
                if picked:
                    existing.append(choice.tracked_by)
                    show_existing.show()
            elif choice.problem:
                box.setText(f"{choice.name} · {tr(choice.problem)}")
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
        picked = QFileDialog.getExistingDirectory(dialog, tr("Env build folder"), folder.text())
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
    parts = [name, board or tr("board unknown")]
    if build is not None:
        parts.append(build.chip or tr("chip unknown"))
        flash = build.flash
        parts.append(f"{flash.mode} {flash.freq} {flash.size}")
    return " · ".join(parts)


def _build_text(build: PioBuild) -> str:
    lines = [
        f"0x{f.address:X}  {f.name}" + (f"  ({tr(f.note)})" if f.note else "") for f in build.files
    ]
    lines += [tr(n) for n in build.notes]
    return "\n".join(lines)
