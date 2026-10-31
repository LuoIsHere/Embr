"""Command-line entry point: the GUI, or `--worker` for a flashing job."""

import argparse
import io
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from embr import __version__
from embr.i18n import N_, tr

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

    from embr.core.storage.paths import AppPaths

NOT_WRITABLE = N_(
    "Embr keeps its data, logs and settings next to Embr.exe, but this folder is not "
    "writable:\n\n{root}\n\nMove the Embr folder to a writable location (for example under "
    "your Documents, not Program Files) and start it again."
)
ALREADY_RUNNING = N_(
    "Embr is already running from this folder:\n\n{root}\n\nSwitch to the open window. "
    "Two copies in different folders do not conflict."
)
NEWER_DATA = N_(
    "The data in this folder was written by a newer version of Embr and cannot be opened by "
    "{version}:\n\n{detail}\n\nUse the newer Embr, or a copy of Embr in another folder."
)
FOLDERS_UNUSABLE = N_(
    "Embr cannot use these folders:\n\n{lines}\n\nRename and continue: what is in the way "
    "is renamed (nothing is deleted) and new folders are created. Exit: nothing is changed."
)
OCCUPIED = N_("{path}: a file has this name")
UNREADABLE = N_("{path}: cannot be read ({detail})")
NOT_RENAMED = N_("{path} could not be renamed ({detail}). Nothing else was changed.")
DAMAGED_DATABASE = N_(
    "The records database cannot be read:\n\n{detail}\n\nBack up and start anew: the "
    "damaged file is copied to a place you choose, then Embr starts with an empty records "
    "database (flash records and the names of archived files stay in the copy; the archived "
    "files themselves are kept). Exit: nothing is changed."
)
NOT_BACKED_UP = N_("The database could not be copied to {target} ({detail}). Nothing was changed.")
RENAME_AND_CONTINUE = N_("Rename and continue")
BACK_UP_AND_START = N_("Back up and start anew")
EXIT = N_("Exit")
BACKUP_TITLE = N_("Back up the damaged database")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--worker"]:
        # esptool.loader reads esptool.cfg once, when it is first imported, and the worker
        # package imports esptool; so the bundled config must be set before that import
        os.environ["ESPTOOL_CFGFILE"] = esptool_config_path()
        from embr.core.flasher.worker import run

        # pipes default to block buffering and the ANSI code page; events need neither
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if isinstance(stream, io.TextIOWrapper):
                stream.reconfigure(encoding="utf-8", line_buffering=True)
        return run(sys.stdin, sys.stdout)
    parser = argparse.ArgumentParser(prog="Embr")
    parser.add_argument("--version", action="version", version=f"Embr {__version__}")
    parser.parse_args(args)
    return run_gui()


def esptool_config_path() -> str:
    """Bundled esptool.cfg, so user-level esptool configs cannot change behaviour."""
    return str(files("embr.resources").joinpath("esptool.cfg"))


def run_gui() -> int:
    """Startup order (design doc §7.6): language for the startup messages, writable check,
    the folders Embr needs (design doc §12.8), instance lock, clear temp/, data folders,
    TMP/TEMP to temp/ before Qt starts, open the data (migrations; a damaged database is
    offered a backup), window."""
    from embr.core.storage.lock import InstanceLock
    from embr.core.storage.paths import AppPaths, clear_temp, default_root, is_writable, prepare

    restart = False
    paths = AppPaths(default_root())
    _startup_language(paths.settings)
    if not is_writable(paths.root):
        return _fatal(tr(NOT_WRITABLE).format(root=paths.root))
    if not _usable_folders(paths):
        return 1
    paths.temp.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(paths.lock_file)
    if not lock.acquire():
        return _fatal(tr(ALREADY_RUNNING).format(root=paths.root))
    try:
        clear_temp(paths)
        prepare(paths)
        # Qt's temporary folder and every child process (the worker) follow these
        os.environ["TMP"] = os.environ["TEMP"] = str(paths.temp)
        from PySide6.QtCore import QLocale

        from embr.app.context import AppContext, open_context
        from embr.core.storage.database import DamagedDatabaseError, NewerSchemaError
        from embr.i18n import effective, set_language
        from embr.ui.adapters.settings import stored_language
        from embr.ui.window import EmbrWindow

        app = _application()
        app.setApplicationName("Embr")
        context: AppContext | None = None
        while context is None:
            try:
                context = open_context(paths)
            except NewerSchemaError as exc:
                return _fatal(tr(NEWER_DATA).format(version=__version__, detail=exc))
            except DamagedDatabaseError as exc:
                if not _replace_damaged_database(paths.database, str(exc)):
                    return 1
        set_language(effective(stored_language(context.settings), QLocale.system().uiLanguages()))
        try:
            window = EmbrWindow(context)
            window.show()
            code = app.exec()
            restart = window.restart_requested
            return code
        finally:
            context.close()
    finally:
        lock.release()
        if restart:
            _relaunch()


def _startup_language(settings_file: Path) -> None:
    """Language of the messages shown before the data is open: the stored choice when
    settings.ini can be read, else the system's (design doc §12.8)."""
    import configparser

    from PySide6.QtCore import QLocale

    from embr.i18n import SETTINGS_KEY, Language, effective, set_language

    choice = Language.SYSTEM
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(settings_file.read_text(encoding="utf-8"))
        text = parser.get("ui", SETTINGS_KEY, fallback="")
        choice = Language(text) if text in {m.value for m in Language} else Language.SYSTEM
    except (OSError, UnicodeDecodeError, configparser.Error):
        pass
    set_language(effective(choice, QLocale.system().uiLanguages()))


def _usable_folders(paths: "AppPaths") -> bool:
    """True when every folder Embr needs is usable, or was made usable by renaming what is
    in the way (the user's choice); False to exit (design doc §12.8)."""
    from embr.core.storage.paths import folder_problems, set_aside

    problems = folder_problems(paths)
    if not problems:
        return True
    lines = "\n".join(
        tr(OCCUPIED).format(path=p.path)
        if p.occupied
        else tr(UNREADABLE).format(path=p.path, detail=p.detail)
        for p in problems
    )
    if _choose(tr(FOLDERS_UNUSABLE).format(lines=lines), tr(RENAME_AND_CONTINUE)) is False:
        return False
    for problem in problems:
        try:
            set_aside(problem.path)
        except OSError as exc:
            _fatal(tr(NOT_RENAMED).format(path=problem.path, detail=exc.strerror or exc))
            return False
    if folder_problems(paths):
        return _usable_folders(paths)
    return True


def _replace_damaged_database(database: Path, detail: str) -> bool:
    """Ask what to do with a damaged embr.sqlite: back it up to a place the user picks and
    start with a new one (True), or exit (False). The file is never changed before the copy
    succeeded (design doc §12.8)."""
    import datetime
    import shutil

    from PySide6.QtWidgets import QFileDialog

    while True:
        if not _choose(tr(DAMAGED_DATABASE).format(detail=detail), tr(BACK_UP_AND_START)):
            return False
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        suggested = Path.home() / f"embr-damaged-{stamp}.sqlite"
        chosen, _ = QFileDialog.getSaveFileName(
            None, tr(BACKUP_TITLE), str(suggested), "SQLite (*.sqlite);;* (*)"
        )
        if not chosen:
            continue
        target = Path(chosen)
        try:
            shutil.copy2(database, target)
            for suffix in ("-journal", "-wal"):
                extra = database.with_name(database.name + suffix)
                if extra.is_file():
                    shutil.copy2(extra, target.with_name(target.name + suffix))
        except OSError as exc:
            _fatal(tr(NOT_BACKED_UP).format(target=target, detail=exc.strerror or exc))
            continue
        for suffix in ("", "-journal", "-wal", "-shm"):
            extra = database.with_name(database.name + suffix)
            if extra.is_file():
                extra.unlink()
        return True


def _choose(message: str, proceed: str) -> bool:
    """A warning with `proceed` and Exit; True for `proceed`."""
    from PySide6.QtWidgets import QMessageBox

    _application()
    box = QMessageBox(QMessageBox.Icon.Warning, "Embr", message)
    go = box.addButton(proceed, QMessageBox.ButtonRole.AcceptRole)
    leave = box.addButton(tr(EXIT), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(leave)
    box.setEscapeButton(leave)
    box.exec()
    return box.clickedButton() is go


def _application() -> "QApplication":
    """The QApplication, created on first use (startup messages may need it earlier)."""
    from PySide6.QtWidgets import QApplication

    existing = QApplication.instance()
    return existing if isinstance(existing, QApplication) else QApplication(sys.argv)


def _relaunch() -> None:
    """Start Embr again (language change, design doc §12.5): the packaged exe, or this
    interpreter with -m embr when running from source. Called after the instance lock is
    released."""
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "embr"]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([*command, *sys.argv[1:]], creationflags=flags, close_fds=True)


def _fatal(message: str) -> int:
    from PySide6.QtWidgets import QMessageBox

    _application()
    QMessageBox.critical(None, "Embr", message)
    return 1
