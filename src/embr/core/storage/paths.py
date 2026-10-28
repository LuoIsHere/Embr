"""Portable data layout: everything Embr writes lives next to Embr.exe (design doc §7.6)."""

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
    raise NotImplementedError


def is_writable(root: Path) -> bool:
    """True if files can be created and deleted in `root` (created if missing)."""
    raise NotImplementedError


def prepare(paths: AppPaths) -> None:
    """Create data/ (projects, firmwares, archive, backup), logs/ and temp/ if missing,
    and point Python's tempfile module at temp/."""
    raise NotImplementedError


def clear_temp(paths: AppPaths) -> None:
    """Delete everything in temp/ except the instance lock file. Never touches data/ or
    logs/."""
    raise NotImplementedError
