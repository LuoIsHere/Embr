"""Portable data layout: everything Embr writes lives next to Embr.exe (design doc §7.6)."""

import contextlib
import datetime
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def projects(self) -> Path:
        return self.data / "projects"

    @property
    def firmwares(self) -> Path:
        return self.data / "firmwares"

    @property
    def archive(self) -> Path:
        return self.data / "archive"

    @property
    def backup(self) -> Path:
        return self.data / "backup"

    @property
    def database(self) -> Path:
        return self.data / "embr.sqlite"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def temp(self) -> Path:
        return self.root / "temp"

    @property
    def lock_file(self) -> Path:
        return self.temp / "embr.lock"

    @property
    def settings(self) -> Path:
        return self.root / "settings.ini"


def default_root() -> Path:
    """Folder of Embr.exe when frozen; `<repo>/.run` when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # src/embr/core/storage/paths.py -> repository root
    return Path(__file__).resolve().parents[4] / ".run"


def is_writable(root: Path) -> bool:
    """True if files can be created and deleted in `root` (created if missing)."""
    probe = root / f".embr-write-test-{os.getpid()}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return False
    return True


def folders(paths: AppPaths) -> tuple[Path, ...]:
    """The folders Embr needs, parents first."""
    return (
        paths.data,
        paths.projects,
        paths.firmwares,
        paths.archive,
        paths.backup,
        paths.logs,
        paths.temp,
    )


@dataclass(frozen=True)
class FolderProblem:
    """A folder Embr needs that it cannot use (design doc §12.8)."""

    path: Path
    occupied: bool
    """True: a file (not a folder) has the folder's name. False: the folder cannot be
    listed."""
    detail: str = ""


def folder_problems(paths: AppPaths) -> list[FolderProblem]:
    """Needed folders that are a file, or a folder that cannot be listed. Missing folders
    are not problems (prepare() creates them)."""
    problems: list[FolderProblem] = []
    for folder in folders(paths):
        if folder.exists() and not folder.is_dir():
            problems.append(FolderProblem(folder, occupied=True))
        elif folder.is_dir():
            try:
                next(folder.iterdir(), None)
            except OSError as exc:
                problems.append(FolderProblem(folder, False, exc.strerror or str(exc)))
    return problems


def set_aside(path: Path) -> Path:
    """Rename `path` (a file or folder in the way) to "<name>.<timestamp>" (with a number
    added if that exists) and return the new path. Raises OSError when it cannot be
    renamed."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.{stamp}")
    count = 2
    while target.exists():
        target = path.with_name(f"{path.name}.{stamp}-{count}")
        count += 1
    path.rename(target)
    return target


def prepare(paths: AppPaths) -> None:
    """Create data/ (projects, firmwares, archive, backup), logs/ and temp/ if missing,
    and point Python's tempfile module at temp/."""
    for folder in folders(paths):
        folder.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(paths.temp)


def clear_temp(paths: AppPaths) -> None:
    """Delete everything in temp/ except the instance lock file. Never touches data/ or
    logs/."""
    if not paths.temp.is_dir():
        return
    for child in paths.temp.iterdir():
        if child == paths.lock_file:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                child.unlink()
