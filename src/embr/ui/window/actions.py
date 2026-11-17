"""What the window does to firmware and projects: the firmware menu, new and moved firmware,
imports and exports, archive, merge and files dropped on the sidebar."""

import contextlib
import html
from collections.abc import Generator
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
)

from embr.app.archiving import Archiver
from embr.app.flashplan import PlanError
from embr.app.library import AlreadyTrackedError, TrackingEntriesError
from embr.app.locations import firmware_location
from embr.app.merging import Merger
from embr.app.packages import Packager, Placement
from embr.core.firmware.entries import FolderError
from embr.core.firmware.files import local_file
from embr.core.firmware.import_idf import IdfBuildError, build_dir_below
from embr.core.firmware.import_pio import (
    PLATFORMIO_INI,
    EnvDirError,
    EnvRef,
    PioBuildError,
    built_envs_below,
    env_ref,
)
from embr.core.firmware.store import ReadOnlyError, default_name_archived
from embr.core.flasher.merge import MergeError
from embr.core.package import PACKAGE_SUFFIX, PackageError
from embr.i18n import N_, tr
from embr.ui import dialogs
from embr.ui.adapters import settings as ui_settings
from embr.ui.sidebar import Node
from embr.ui.workspace import kept_notes

if TYPE_CHECKING:
    from embr.ui.window.main import EmbrWindow

CHANGES_FIRMWARE = ("rename", "move", "delete", "directory", "follow_env")
"""Firmware menu actions that would change a firmware's own file."""


PACKAGE_FOLDER = "package_folder"
"""settings.ini [ui]: the folder of the last .embr package exported or imported."""


RENAME_IN_PROJECT = N_(
    "Unique within the project; a duplicate gets a numeric suffix. Flash records keep the name "
    "used at the time."
)


RENAME_STANDALONE = N_(
    "Unique among standalone firmware; a duplicate gets a numeric suffix. Flash records keep "
    "the name used at the time."
)


def sort_drop(paths: list[Path]) -> tuple[list[Path], Path | None, list[str]]:
    """(.bin files to add, a build folder, problems) of what was dropped. A build folder is
    an ESP-IDF build folder or a PlatformIO env build folder (checked first: an espidf env
    folder also has flasher_args.json); it counts only when dropped on its own. A project
    folder stands for its build folder (PlatformIO: the first built env, else the project
    folder itself, which the import dialog explains). is_pio_folder() tells the kinds
    apart."""
    files: list[Path] = []
    builds: list[Path] = []
    problems: list[str] = []
    for path in paths:
        name = html.escape(path.name or str(path))
        if path.is_dir():
            build = _pio_folder(path)
            if build is None:
                idf = (path / "flasher_args.json").is_file()
                build = path if idf else build_dir_below(path)
            if build is None:
                problems.append(
                    tr(f"<b>{name}</b>: a folder without an ESP-IDF or PlatformIO build in it")
                )
            else:
                builds.append(build)
        elif not path.exists():
            problems.append(tr(f"<b>{name}</b>: not found"))
        elif path.suffix.lower() == ".lnk":
            problems.append(tr(f"<b>{name}</b>: a shortcut; drop the file it points to"))
        elif path.suffix.lower() != ".bin":
            problems.append(
                tr(
                    f"<b>{name}</b>: not a .bin file. To flash another kind of file, open a "
                    "firmware and use Add entry, then Attachment, on its Entries tab."
                )
            )
        else:
            try:
                with path.open("rb") as handle:
                    empty = not handle.read(1)
            except OSError as exc:
                problems.append(tr(f"<b>{name}</b>: cannot be read ({html.escape(_text(exc))})"))
                continue
            if empty:
                problems.append(tr(f"<b>{name}</b>: empty file (0 bytes)"))
            else:
                files.append(path)
    if builds and (files or len(builds) > 1 or problems):
        problems.append(tr("Drop a build folder on its own."))
    build = builds[0] if len(builds) == 1 else None
    return files, build, problems


def is_pio_folder(path: Path) -> bool:
    """A PlatformIO env build folder (`.pio/build/<env>`) or a project folder with
    platformio.ini; sort_drop() gives one of these or an ESP-IDF build folder."""
    parent = path.parent
    in_build = parent.name.lower() == "build" and parent.parent.name.lower() == ".pio"
    return in_build or (path / PLATFORMIO_INI).is_file()


def _pio_folder(path: Path) -> Path | None:
    try:
        return env_ref(path).build_dir
    except EnvDirError:
        pass
    if not (path / PLATFORMIO_INI).is_file():
        return None
    built = built_envs_below(path)
    return built[0].build_dir if built else path


@contextlib.contextmanager
def _busy() -> Generator[None]:
    """Wait cursor while packing or checking a package."""
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


def _text(exc: Exception) -> str:
    return exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)


class LibraryActions:
    """Library operations started from the sidebar, the firmware menu and the dialogs."""

    def __init__(self, window: "EmbrWindow") -> None:
        self._window = window
        context, tracker = window.context, window.library.tracker
        self._archiver = Archiver(context, tracker)
        self._merger = Merger(context, tracker)
        self._packager = Packager(context, tracker)

    def firmware_action(self, name: str) -> None:
        firmware_id = self._window.selected_firmware_id()
        if firmware_id is None or self._window.is_locked(firmware_id):
            return
        firmware = self._window.library.firmware(firmware_id)
        if firmware.read_only and name in CHANGES_FIRMWARE:
            self.read_only_notice(firmware.name)
            return
        if name == "reveal":
            self._window.show_in_explorer(firmware_location(firmware))
            return
        if name == "rename":
            text = dialogs.ask_text(
                self._window,
                tr("Rename firmware"),
                tr("Name"),
                firmware.name,
                tr(RENAME_IN_PROJECT if firmware.project_id else RENAME_STANDALONE),
                tr("Rename"),
            )
            if text:
                renamed = self._window.library.rename_firmware(firmware_id, text)
                self._window.note("dim", f"[firmware] renamed to '{renamed.name}'")
        elif name == "duplicate":
            copy = self._window.library.duplicate_firmware(firmware_id)
            self._window.note("dim", f"[firmware] duplicated as '{copy.name}'")
            self._window.select_firmware(copy.id)  # the usual rules of leaving the selection
            return
        elif name == "move":
            placement = dialogs.move_firmware(
                self._window, self._window.library, firmware.project_id, firmware.device
            )
            if placement is not None:
                self.move(firmware_id, placement.project_id, placement.device)
                return
        elif name == "archive":
            self._archive_version(firmware_id)
            return
        elif name == "export":
            self._export_firmware(firmware_id)
            return
        elif name == "directory":
            self._change_build_dir(firmware_id)
            return
        elif name == "follow_env":
            self._follow_env(firmware_id)
            return
        elif name == "delete":
            accepted, _ = dialogs.confirm(
                self._window,
                tr("Delete firmware"),
                [
                    tr(f"Delete <b>{html.escape(firmware.name)}</b>?"),
                    tr(dialogs.ARCHIVE_KEPT),
                ],
                tr("Delete"),
            )
            if not accepted:
                return
            self._window.library.delete_firmware(firmware_id)
            self._window.note("warning", f"[firmware] '{firmware.name}' deleted")
            first = self._window.first_firmware()
            if first is not None:
                self._window.select_firmware(first)
                return
            self._window.clear_selection()
            return
        self._window.changed()

    def move(self, firmware_id: str, project_id: object, device: str) -> None:
        if self._window.is_locked(firmware_id):
            return
        target = project_id if isinstance(project_id, str) else None
        before = self._window.library.firmware(firmware_id)
        if before.project_id == target and before.device == device:
            return
        if before.read_only:
            self.read_only_notice(before.name)
            return
        moved = self._window.library.move_firmware(firmware_id, target, device)
        if target is None:
            where = "standalone"
        else:
            where = self._window.library.project(target).name + (
                f" / {device}" if device else " (ungrouped)"
            )
        self._window.note("dim", f"[move] '{moved.name}' → {where}")
        self._window.changed()

    def reorder_projects(self, project_ids: list[str]) -> None:
        """Projects dragged into a new order in the sidebar (settings.ini)."""
        self._window.library.set_project_order(project_ids)
        self._window.refresh_tree()

    def reorder_groups(self, project_id: str, names: list[str]) -> None:
        """Groups dragged into a new order within their project (its `devices`)."""
        try:
            self._window.library.reorder_groups(project_id, names)
        except ReadOnlyError:
            self.read_only_notice(self._window.library.project(project_id).name)
        except (KeyError, ValueError):  # the project changed under the drag
            pass
        self._window.refresh_tree()

    def read_only_notice(self, name: str) -> None:
        """Firmware or project written by a newer Embr: shown, never changed."""
        dialogs.notice(
            self._window,
            tr("Read only"),
            [
                tr(
                    f"<b>{html.escape(name)}</b> was created by a newer version of Embr and is "
                    "read-only here."
                )
            ],
        )

    def new_firmware(self) -> None:
        project_id, device = self._window.current_placement()
        result = dialogs.new_firmware(self._window, self._window.library, project_id, device)
        if result is None:
            return
        name, placement = result
        firmware = self._window.library.create_firmware(
            name, placement.project_id, placement.device
        )
        where = (
            f" in {self._window.library.project(placement.project_id).name}"
            if placement.project_id
            else " as standalone"
        )
        self._window.note("dim", f"[new] fixed firmware '{firmware.name}' created{where}")
        self._window.select_firmware(firmware.id)

    def new_project(self) -> None:
        result = dialogs.new_project(self._window)
        if result is None:
            return
        project = self._window.library.create_project(*result)
        self._window.note("dim", f"[project] created '{project.name}'")
        self._window.select_project(project.id)

    def import_idf(
        self, build_dir: Path | None = None, placement: tuple[str | None, str] | None = None
    ) -> None:
        project_id, device = placement or self._window.current_placement()
        request = dialogs.import_idf(
            self._window,
            self._window.library,
            project_id,
            device,
            self._window.select_firmware,
            build_dir,
        )
        if request is None:
            return
        try:
            firmware = self._window.library.import_idf(
                request.build_dir,
                name=request.name,
                project_id=request.placement.project_id,
                device=request.placement.device,
            )
        except AlreadyTrackedError as exc:  # imported meanwhile by another way
            self._window.note("error", f"[import] {exc}")
            self._window.select_firmware(exc.firmware_id)
            return
        except (PlanError, IdfBuildError, OSError) as exc:
            self._window.note("error", f"[import] {exc}")
            return
        self._window.note("ok", f"[import] '{firmware.name}' follows {request.build_dir}")
        self._window.select_firmware(firmware.id)

    def _archive_version(self, firmware_id: str) -> None:
        try:
            entries = self._archiver.preview(firmware_id)
        except PlanError as exc:
            self._window.note("error", f"[archive] {exc}")
            return
        firmware = self._window.library.firmware(firmware_id)
        answer = dialogs.archive_version(
            self._window, firmware.name, default_name_archived(firmware.name, date.today()), entries
        )
        if answer is None:
            return
        try:
            archived = self._archiver.archive_version(firmware_id, name=answer[0], notes=answer[1])
        except (PlanError, OSError) as exc:
            self._window.note("error", f"[archive] {exc}")
            return
        self._window.note(
            "ok", f"[archive] '{archived.name}' created; '{firmware.name}' is unchanged"
        )
        self._window.select_firmware(archived.id)

    def _package_folder(self) -> Path:
        stored = self._window.context.settings.get(ui_settings.UI_SECTION, PACKAGE_FOLDER)
        folder = Path(stored) if stored else Path.home()
        return folder if folder.is_dir() else Path.home()

    def _remember_package_folder(self, path: Path) -> None:
        settings = self._window.context.settings
        settings.set(ui_settings.UI_SECTION, PACKAGE_FOLDER, str(path.parent))
        settings.save()

    def _export_firmware(self, firmware_id: str) -> None:
        """Export…: the firmware of the same scope, the chosen one checked."""
        firmware = self._window.library.firmware(firmware_id)
        if firmware.project_id is not None:
            scope = self._window.library.project_firmware(firmware.project_id)
        else:
            scope = [
                self._window.library.firmware(r.id) for r in self._window.library.tree().standalone
            ]
        check = self._packager.check_export([f.id for f in scope])
        request = dialogs.export_firmware(
            self._window, scope, firmware_id, check, self._package_folder()
        )
        if request is None:
            return
        ids, path = request
        try:
            with _busy():
                self._packager.export_firmware(ids, path)
        except (PlanError, OSError, ValueError) as exc:
            self._window.note("error", f"[export] {_text(exc)}")
            return
        self._remember_package_folder(path)
        self._window.note("ok", f"[export] {len(ids)} firmware written to {path}")

    def export_project(self, project_id: str) -> None:
        project = self._window.library.project(project_id)
        ids = [f.id for f in self._window.library.project_firmware(project_id)]
        check = self._packager.check_export(ids)
        path = dialogs.export_project(
            self._window, project.name, len(ids), check, self._package_folder()
        )
        if path is None:
            return
        try:
            with _busy():
                self._packager.export_project(project_id, path)
        except (PlanError, OSError, ValueError) as exc:
            self._window.note("error", f"[export] {_text(exc)}")
            return
        self._remember_package_folder(path)
        self._window.note(
            "ok", f"[export] '{project.name}' with {len(ids)} firmware written to {path}"
        )

    def import_package(
        self,
        path: Path | None = None,
        project_id: str | None = None,
        device: str | None = None,
    ) -> None:
        """Import .embr package… (New menu, or a package dropped on the sidebar, where the
        drop point gives the default project and group): the package is checked in full
        first; nothing is written unless all of it is valid."""
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(
                self._window,
                tr("Import .embr package"),
                str(self._package_folder()),
                tr(dialogs.PACKAGE_FILTER),
            )
            if not chosen:
                return
            path = Path(chosen)
            project_id = self._window.current_placement()[0]
        try:
            with _busy():
                package = self._packager.open(path)
        except (PackageError, OSError) as exc:
            dialogs.notice(
                self._window,
                tr("Not imported"),
                [
                    tr(f"<b>{html.escape(path.name)}</b> cannot be imported."),
                    html.escape(tr(_text(exc))),
                ],
            )
            return
        try:
            placement = dialogs.import_package(
                self._window, self._window.library, package, path.name, project_id, device
            )
            if placement is None:
                return
            with _busy():
                result = self._packager.import_package(
                    package, path.name, Placement(placement.project_id, placement.device)
                )
        except (KeyError, OSError, ValueError) as exc:
            self._window.note("error", f"[import] {path.name}: {_text(exc)}")
            return
        finally:
            package.discard()
        self._remember_package_folder(path)
        self._window.note(
            "ok",
            f"[import] {path.name}: {len(result.project_ids)} project(s), "
            f"{len(result.firmware_ids)} firmware",
        )
        if result.firmware_ids:
            self._window.select_firmware(result.firmware_ids[0])
        elif result.project_ids:
            self._window.select_project(result.project_ids[0])
        else:
            self._window.changed()

    def archive_entry(self, entry_id: str) -> None:
        firmware_id = self._window.selected_firmware_id()
        if firmware_id is None:
            return
        try:
            entry = self._archiver.archive_entry(firmware_id, entry_id)
        except (PlanError, ValueError, OSError) as exc:
            self._window.note("error", f"[archive] {exc}")
            return
        self._window.note("ok", f"[archive] {entry.name} copied into the archive")
        self._window.changed()

    def import_pio(
        self, build_dir: Path | None = None, placement: tuple[str | None, str] | None = None
    ) -> None:
        project_id, device = placement or self._window.current_placement()
        request = dialogs.import_pio(
            self._window,
            self._window.library,
            project_id,
            device,
            self._window.select_firmware,
            build_dir,
        )
        if request is None:
            return
        try:
            created = self._window.library.import_pio(
                request.project_root,
                request.envs,
                project_id=request.placement.project_id,
                device=request.placement.device,
            )
        except AlreadyTrackedError as exc:  # imported meanwhile by another way
            self._window.note("error", f"[import] {exc}")
            self._window.select_firmware(exc.firmware_id)
            return
        except (PlanError, PioBuildError, OSError, ValueError) as exc:
            self._window.note("error", f"[import] {exc}")
            return
        for firmware in created:
            ref = firmware.build_ref
            env = ref.env if ref is not None else "?"
            self._window.note(
                "ok", f"[import] '{firmware.name}' follows {request.project_root} env {env}"
            )
        self._window.select_firmware(created[0].id)

    def _change_build_dir(self, firmware_id: str) -> None:
        firmware = self._window.library.firmware(firmware_id)
        start = firmware.build_ref.path if firmware.build_ref else ""
        title = (
            tr("Env build folder (.pio\\build\\<env>)")
            if firmware.build_ref and firmware.build_ref.env
            else tr("Build directory")
        )
        chosen = QFileDialog.getExistingDirectory(self._window, title, start)
        if not chosen:
            return
        try:
            status = self._window.library.tracker.set_build_dir(firmware_id, Path(chosen))
        except ValueError as exc:  # not an env build folder of a PlatformIO project
            self._window.note("error", f"[build] {exc}")
            return
        self._window.note(
            "dim", f"[build] {firmware.name} now follows {chosen} ({status.state.value})"
        )
        self._window.changed()

    def _follow_env(self, firmware_id: str) -> None:
        """The banner's "Follow env": follow the one built env platformio.ini
        still declares and no other firmware follows. The firmware keeps its name."""
        firmware = self._window.library.firmware(firmware_id)
        candidates = self._window.library.tracker.env_candidates(firmware_id)
        if len(candidates) != 1 or firmware.build_ref is None:
            self._window.changed()  # the project changed since the banner was shown
            return
        (env,) = candidates
        build_dir = EnvRef(Path(firmware.build_ref.path), env).build_dir
        try:
            status = self._window.library.tracker.set_build_dir(firmware_id, build_dir)
        except ValueError as exc:
            self._window.note("error", f"[build] {exc}")
            return
        self._window.note(
            "dim", f"[build] {firmware.name} now follows env {env} ({status.state.value})"
        )
        self._window.changed()

    def clean_archive(self) -> None:
        names = {f.id: f.name for f in self._window.context.firmware.all()}
        doomed = dialogs.clean_archive(
            self._window, self._archiver.usage(), names, self._window.select_firmware
        )
        if doomed:
            cleaned = self._archiver.clean(doomed)
            self._window.note("warning", f"[archive] {cleaned.deleted} unreferenced files deleted")
            if cleaned.in_use:
                self._window.note(
                    "warning",
                    f"[archive] {cleaned.in_use} files in use were kept;"
                    " Clean archive tries them again next time",
                )

    def merge(self) -> None:
        firmware_id = self._window.selected_firmware_id()
        if firmware_id is None:
            return
        status = self._window.library.tracker.check(firmware_id)
        firmware = status.firmware
        paths = [p for e in firmware.entries if (p := local_file(e)) is not None]
        folder = paths[0].parent if paths else self._window.context.paths.root
        request = dialogs.merge(
            self._window, self._merger, firmware, self._merger.default_output(firmware, folder)
        )
        if request is None:
            return
        try:
            created = self._merger.merge(firmware_id, *request)
        except (PlanError, MergeError, OSError) as exc:
            self._window.note("error", f"[merge] {exc}")
            return
        self._window.note("ok", f"[merge] written to {request[0]}")
        if created is not None:
            self._window.select_firmware(created.id)

    def files_dropped(self, names: list[str], node: object) -> None:
        """Files or a folder dragged from Explorer onto the sidebar:
        on a firmware they are added; on a project, group or the standalone header a fixed
        firmware is created after its name is confirmed, or an ESP-IDF build folder opens the
        import dialog, and a PlatformIO env build folder or project folder opens the
        PlatformIO import dialog. Anything else is refused as a whole, with a dialog saying
        why."""
        if not isinstance(node, Node):
            return
        if node.firmware_id is not None and self._window.is_locked(node.firmware_id):
            dialogs.notice(
                self._window, tr("Nothing added"), [tr("This firmware is being flashed.")]
            )
            return
        paths = [Path(n) for n in names]
        packages = [p for p in paths if p.suffix.lower() == PACKAGE_SUFFIX]
        if packages:
            if len(paths) > 1:
                dialogs.notice(
                    self._window, tr("Nothing added"), [tr("Drop a .embr package on its own.")]
                )
                return
            # the drop point, as for dragged firmware: a group, a project (ungrouped), the
            # standalone header, or a firmware's own project and group
            project_id, device = node.project_id, node.device
            if node.firmware_id is not None:
                dropped_on = self._window.library.firmware(node.firmware_id)
                project_id, device = dropped_on.project_id, dropped_on.device
            self.import_package(packages[0], project_id, device)
            return
        files, build, problems = sort_drop(paths)
        if node.firmware_id is not None:
            self._drop_on_firmware(node.firmware_id, files, build, problems)
            return
        if build is not None and not problems:
            if is_pio_folder(build):
                self.import_pio(build, (node.project_id, node.device))
            else:
                self.import_idf(build, (node.project_id, node.device))
            return
        if problems or not files:
            dialogs.notice(
                self._window, tr("Nothing added"), problems or [tr("No files were dropped.")]
            )
            return
        name = dialogs.ask_text(
            self._window,
            tr("New firmware"),
            tr("Name shown in the sidebar"),
            files[0].stem,
            tr(f"{len(files)} file(s) from {files[0].parent}"),
            tr("Create"),
        )
        if name is None:
            return
        try:
            firmware = self._window.library.create_from_files(
                files, node.project_id, node.device, name
            )
        except (FolderError, OSError, ValueError) as exc:
            dialogs.notice(
                self._window, tr("Nothing added"), [html.escape(tr(_text(exc)), quote=False)]
            )
            return
        self._window.note(
            "dim", f"[new] fixed firmware '{firmware.name}' from {len(files)} file(s)"
        )
        self._window.select_firmware(firmware.id)

    def _drop_on_firmware(
        self, firmware_id: str, files: list[Path], build: Path | None, problems: list[str]
    ) -> None:
        if build is not None:
            problems = [
                *problems,
                tr(f"{html.escape(build.name)}: drop a build folder on a project or group"),
            ]
        if problems or not files:
            dialogs.notice(
                self._window, tr("Nothing added"), problems or [tr("No files were dropped.")]
            )
            return
        try:
            result = self._window.library.add_files(firmware_id, files)
        except (TrackingEntriesError, FolderError, OSError, ValueError) as exc:
            dialogs.notice(
                self._window, tr("Nothing added"), [html.escape(tr(_text(exc)), quote=False)]
            )
            return
        name = self._window.library.firmware(firmware_id).name
        if result.added:
            self._window.note("dim", f"[entry] {len(result.added)} entries added to '{name}'")
        for line in kept_notes(result):
            self._window.note("dim", line)
        self._window.select_firmware(firmware_id)
        if not result.added and not result.updated:
            names = html.escape(", ".join(e.name for e in result.unchanged))
            dialogs.notice(
                self._window,
                tr("Nothing added"),
                [tr(f"Already in this firmware, content unchanged: {names}")],
            )
