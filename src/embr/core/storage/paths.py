"""Portable data layout: everything Embr writes lives next to Embr.exe (design doc §7.6)."""

import contextlib
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


def prepare(paths: AppPaths) -> None:
    """Create data/ (projects, firmwares, archive, backup), logs/ and temp/ if missing,
    and point Python's tempfile module at temp/."""
    for folder in (
        paths.projects,
        paths.firmwares,
        paths.archive,
        paths.backup,
        paths.logs,
        paths.temp,
    ):
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
