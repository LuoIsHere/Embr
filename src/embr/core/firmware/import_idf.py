"""Read an ESP-IDF build directory (design doc §5 stage two): build/flasher_args.json for
the files, addresses, flash settings, chip and reset modes, build/project_description.json
for the project name, target, IDF version and config file, and the project's sdkconfig for
the configured target. Offline; reads files only. May use image_sniff, never the reverse.
"""

from dataclasses import dataclass
from pathlib import Path

FLASHER_ARGS = "flasher_args.json"
PROJECT_DESCRIPTION = "project_description.json"


class IdfBuildError(ValueError):
    """Not a usable ESP-IDF build directory (malformed flasher_args.json, no flash files)."""


class BuildMissingError(IdfBuildError):
    """The directory or its flasher_args.json does not exist (e.g. after idf.py fullclean)."""


class EncryptedBuildError(IdfBuildError):
    """A flash file is marked "encrypted": true; Embr does not support flash encryption."""


@dataclass(frozen=True)
class BuildFile:
    address: int
    path: Path
    """Absolute: flash_files paths are relative to the build directory."""

    @property
    def name(self) -> str:
        """File name, e.g. "bootloader.bin"."""
        return self.path.name


@dataclass(frozen=True)
class IdfBuild:
    build_dir: Path
    files: tuple[BuildFile, ...]
    """flash_files, sorted by address."""
    flash_mode: str
    flash_freq: str
    flash_size: str
    """From flash_settings; "keep" for a missing value."""
    chip: str | None
    """extra_esptool_args.chip."""
    before: str
    after: str
    """extra_esptool_args, in esptool v5 spelling ("default-reset", "hard-reset")."""
    stub: bool
    """extra_esptool_args.stub; True when absent."""
    project_name: str | None
    target: str | None
    """project_description.json "target"."""
    idf_version: str | None
    """project_description.json "git_revision" (the IDF_VER string, e.g. "v6.1")."""
    config_file: Path | None
    """project_description.json "config_file", else <project_path>/sdkconfig, else None."""

    @property
    def inputs(self) -> tuple[Path, ...]:
        """Every file a re-check must stat: flasher_args.json, project_description.json,
        the config file (when known) and all flash files."""
        raise NotImplementedError


def normalize_arg(value: str) -> str:
    """IDF 5.x underscore spelling to esptool v5: "default_reset" -> "default-reset",
    "--flash_mode" -> "--flash-mode". Values without underscores are returned unchanged."""
    raise NotImplementedError


def read_build(build_dir: Path) -> IdfBuild:
    """Parse the build directory. project_description.json is optional (its fields are
    then None). Raises BuildMissingError, EncryptedBuildError, IdfBuildError."""
    raise NotImplementedError


def configured_target(config_file: Path | None) -> str | None:
    """CONFIG_IDF_TARGET from an sdkconfig file; None when the file is missing, unreadable
    or has no such line."""
    raise NotImplementedError


def default_firmware_name(build: IdfBuild) -> str:
    """project_name, else the name of the build directory's parent folder."""
    raise NotImplementedError
