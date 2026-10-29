"""Command-line entry point: the GUI, or `--worker` for a flashing job."""

import argparse
import io
import os
import sys
from importlib.resources import files

from embr import __version__

NOT_WRITABLE = (
    "Embr keeps its data, logs and settings next to Embr.exe, but this folder is not "
    "writable:\n\n{root}\n\nMove the Embr folder to a writable location (for example under "
    "your Documents, not Program Files) and start it again."
)
ALREADY_RUNNING = (
    "Embr is already running from this folder:\n\n{root}\n\nSwitch to the open window. "
    "Two copies in different folders do not conflict."
)
NEWER_DATA = (
    "The data in this folder was written by a newer version of Embr and cannot be opened by "
    "{version}:\n\n{detail}\n\nUse the newer Embr, or a copy of Embr in another folder."
)


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
    """Startup order (design doc §7.6): writable check, instance lock, clear temp/, data
    folders, TMP/TEMP to temp/ before Qt starts, open the data (migrations), window."""
    from embr.core.storage.lock import InstanceLock
    from embr.core.storage.paths import AppPaths, clear_temp, default_root, is_writable, prepare

    paths = AppPaths(default_root())
    if not is_writable(paths.root):
        return _fatal(NOT_WRITABLE.format(root=paths.root))
    paths.temp.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(paths.lock_file)
    if not lock.acquire():
        return _fatal(ALREADY_RUNNING.format(root=paths.root))
    try:
        clear_temp(paths)
        prepare(paths)
        # Qt's temporary folder and every child process (the worker) follow these
        os.environ["TMP"] = os.environ["TEMP"] = str(paths.temp)
        from PySide6.QtWidgets import QApplication

        from embr.app.context import open_context
        from embr.core.storage.database import NewerSchemaError
        from embr.ui.window import EmbrWindow

        app = QApplication(sys.argv)
        app.setApplicationName("Embr")
        try:
            context = open_context(paths)
        except NewerSchemaError as exc:
            return _fatal(NEWER_DATA.format(version=__version__, detail=exc))
        try:
            window = EmbrWindow(context)
            window.show()
            return app.exec()
        finally:
            context.close()
    finally:
        lock.release()


def _fatal(message: str) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.critical(None, "Embr", message)
    del app
    return 1
