"""Read an ESP-IDF build directory (design doc §5 stage two): build/flasher_args.json for
the files, addresses, flash settings, chip and reset modes, build/project_description.json
for the project name, target, IDF version and config file, and the project's sdkconfig for
the configured target. Offline; reads files only. May use image_sniff, never the reverse.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from embr.core.firmware.kinds import EntryKind
from embr.i18n import N_

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
    kind: EntryKind | None = None
    """What flasher_args.json says the file is (its "bootloader", "app", "partition-table"
    or "otadata" section); None for other flash files."""

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
        paths = [self.build_dir / FLASHER_ARGS, self.build_dir / PROJECT_DESCRIPTION]
        if self.config_file is not None:
            paths.append(self.config_file)
        return (*paths, *(f.path for f in self.files))


def normalize_arg(value: str) -> str:
    """IDF 5.x underscore spelling to esptool v5: "default_reset" -> "default-reset",
    "--flash_mode" -> "--flash-mode". Values without underscores are returned unchanged."""
    return value.replace("_", "-")


def read_build(build_dir: Path) -> IdfBuild:
    """Parse the build directory. project_description.json is optional (its fields are
    then None). Raises BuildMissingError, EncryptedBuildError, IdfBuildError."""
    args_file = build_dir / FLASHER_ARGS
    if not args_file.is_file():
        raise BuildMissingError(N_(f"{args_file} not found"))
    args = _load(args_file)
    for name, section in args.items():
        if isinstance(section, dict) and _true(cast(dict[str, object], section).get("encrypted")):
            raise EncryptedBuildError(
                N_(f"{name}: flash encryption is enabled in this project; Embr does not support it")
            )
    build_dir = build_dir.resolve()
    files = _flash_files(build_dir, args.get("flash_files"), _section_kinds(build_dir, args))
    settings = _table(args.get("flash_settings"))
    extra = _table(args.get("extra_esptool_args"))
    desc_file = build_dir / PROJECT_DESCRIPTION
    desc = _load(desc_file) if desc_file.is_file() else {}
    stub = extra.get("stub", True)
    return IdfBuild(
        build_dir=build_dir,
        files=files,
        flash_mode=_str(settings.get("flash_mode")) or "keep",
        flash_freq=_str(settings.get("flash_freq")) or "keep",
        flash_size=_str(settings.get("flash_size")) or "keep",
        chip=_str(extra.get("chip")),
        before=normalize_arg(_str(extra.get("before")) or "default-reset"),
        after=normalize_arg(_str(extra.get("after")) or "hard-reset"),
        stub=stub if isinstance(stub, bool) else True,
        project_name=_str(desc.get("project_name")),
        target=_str(desc.get("target")),
        idf_version=_str(desc.get("git_revision")),
        config_file=_config_file(desc),
    )


def configured_target(config_file: Path | None) -> str | None:
    """CONFIG_IDF_TARGET from an sdkconfig file; None when the file is missing, unreadable
    or has no such line."""
    if config_file is None:
        return None
    try:
        text = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _TARGET_LINE.search(text)
    return match[1] if match else None


def default_firmware_name(build: IdfBuild) -> str:
    """project_name, else the name of the build directory's parent folder."""
    return build.project_name or build.build_dir.resolve().parent.name


def build_dir_below(path: Path) -> Path | None:
    """`path/build` when `path` itself has no flasher_args.json but `path/build` has one
    (the user picked the project folder), else None. Never searches deeper."""
    if (path / FLASHER_ARGS).is_file():
        return None
    child = path / "build"
    return child if (child / FLASHER_ARGS).is_file() else None


_TARGET_LINE = re.compile(r'^CONFIG_IDF_TARGET="([^"]*)"', re.MULTILINE)


def _load(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IdfBuildError(f"{path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise IdfBuildError(N_(f"{path.name}: expected a JSON object"))
    return cast(dict[str, object], value)


def _table(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _true(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


_SECTIONS = {
    "bootloader": EntryKind.BOOTLOADER,
    "app": EntryKind.APP,
    "partition-table": EntryKind.PARTITION_TABLE,
    "otadata": EntryKind.OTADATA,
}


def _section_kinds(build_dir: Path, args: dict[str, object]) -> dict[Path, EntryKind]:
    kinds: dict[Path, EntryKind] = {}
    for section, kind in _SECTIONS.items():
        name = _str(_table(args.get(section)).get("file"))
        if name is not None:
            kinds[(build_dir / name).resolve()] = kind
    return kinds


def _flash_files(
    build_dir: Path, value: object, kinds: dict[Path, EntryKind]
) -> tuple[BuildFile, ...]:
    files: list[BuildFile] = []
    for offset, name in _table(value).items():
        if not isinstance(name, str):
            raise IdfBuildError(N_(f"flash_files: bad entry for {offset}"))
        try:
            address = int(offset, 16)
        except ValueError:
            raise IdfBuildError(N_(f"flash_files: {offset!r} is not an address")) from None
        path = (build_dir / name).resolve()
        files.append(BuildFile(address, path, kinds.get(path)))
    if not files:
        raise IdfBuildError(N_(f"{FLASHER_ARGS} lists no flash files"))
    return tuple(sorted(files, key=lambda f: f.address))


def _config_file(desc: dict[str, object]) -> Path | None:
    config = _str(desc.get("config_file"))
    if config is not None:
        return Path(config)
    project = _str(desc.get("project_path"))
    return Path(project) / "sdkconfig" if project is not None else None
