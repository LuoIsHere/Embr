"""Read a PlatformIO build (design doc §5 stage three, §12.5).

The user picks `<project>/.pio/build/<env>`. Images come from the env's idedata.json
(written by the PlatformIO IDE; used only when it is not older than platformio.ini and
every listed file exists), else from a scan of the build folder classified by
image_sniff. Embr never runs `pio project metadata` (it rewrites idedata.json) and never
writes into the project; pio is only used for the read-only `pio project config`.
Offline except run_pio(). May use image_sniff, never the reverse.
"""

import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from embr.core.firmware.image_header import ImageHeader
from embr.core.firmware.image_sniff import (
    SUBTYPE_OTADATA,
    TYPE_DATA,
    parse_partition_table,
    sniff,
)
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import FlashParams
from embr.i18n import N_

PLATFORMIO_INI = "platformio.ini"
IDEDATA = "idedata.json"
FLASHER_ARGS = "flasher_args.json"
"""Written by the espidf framework with idf.py file names (remapped, design doc §5)."""
APP_FILE = "firmware.bin"
BOOT_APP0 = "boot_app0.bin"
BOOT_APP0_PIO_ADDRESS = 0xE000
"""Where PlatformIO writes boot_app0.bin for the Arduino framework, whatever the table."""
ARDUINO_PACKAGE = "framework-arduinoespressif32"
BOOT_APP0_IN_PACKAGE = Path("tools") / "partitions" / BOOT_APP0
CONFIG_TIMEOUT = 30.0
"""Seconds `pio project config` may take before it counts as unavailable."""


class PioBuildError(ValueError):
    """Not a usable PlatformIO build."""


class EnvDirError(PioBuildError):
    """The folder is not `<project>/.pio/build/<env>` of a project whose platformio.ini
    declares that env. The message says which part is wrong."""


class BuildMissingError(PioBuildError):
    """The env's build folder does not exist or holds no .bin file (never built, or after
    `pio run -t clean`)."""


class PioUnavailableError(OSError):
    """pio was not found, failed, timed out or printed something unparsable."""


class Framework(StrEnum):
    ARDUINO = "arduino"
    ESPIDF = "espidf"


class ImageSource(StrEnum):
    IDEDATA = "idedata"
    SCAN = "scan"


@dataclass(frozen=True)
class EnvRef:
    project_root: Path
    """Absolute, resolved."""
    env: str

    @property
    def build_dir(self) -> Path:
        """project_root / ".pio" / "build" / env."""
        return self.project_root / ".pio" / "build" / self.env


@dataclass(frozen=True)
class EnvConfig:
    """One env of `pio project config --json-output` (inheritance already applied)."""

    board: str | None
    mcu: str | None
    """board_build.mcu, lower case; None when the env does not set it."""
    frameworks: tuple[str, ...]
    platform_packages: tuple[str, ...]


@dataclass(frozen=True)
class FlashImage:
    address: int
    path: Path


@dataclass(frozen=True)
class PioImages:
    """extra.flash_images and extra.application_offset of idedata.json."""

    images: tuple[FlashImage, ...]
    """Everything but the app, as listed (absolute paths)."""
    application_offset: int


@dataclass(frozen=True)
class PackageChoice:
    """The Arduino framework package boot_app0.bin comes from (design doc §5 "框架包定位")."""

    path: Path | None
    """Package root; None when nothing was found or the manual path is invalid."""
    auto: bool
    """Found by auto-detection (False: the manual path)."""
    problem: str | None = None
    """Set when a manual path is given but is not a framework package; no fallback."""


@dataclass(frozen=True)
class PioFile:
    address: int
    path: Path | None
    """Absolute; None for a blank otadata entry of `blank_size` bytes."""
    kind: EntryKind | None
    """What the build says the file is (bootloader, app, partition table, otadata); None
    for other files."""
    note: str = ""
    """Shown with the entry, e.g. that boot_app0.bin was moved to the otadata partition, or
    that the listed address and the image disagree (the listed address is used)."""
    blank_size: int = 0

    @property
    def name(self) -> str:
        """File name; "otadata (blank)" for a blank entry."""
        return self.path.name if self.path is not None else "otadata (blank)"


@dataclass(frozen=True)
class PioBuild:
    ref: EnvRef
    framework: Framework
    source: ImageSource
    files: tuple[PioFile, ...]
    """Sorted by address."""
    flash: FlashParams
    """From the bootloader image header ("keep" for an unreadable value)."""
    chip: str | None
    """From the firmware.bin image header."""
    package: PackageChoice | None
    """Arduino only."""
    notes: tuple[str, ...]
    """Build-wide remarks: why idedata.json was not used, boot_app0.bin dropped (no otadata
    partition) or replaced by a blank otadata, flash settings that differ between the
    bootloader and app headers, files the scan could not place."""
    problems: tuple[str, ...]
    """Reasons the build cannot be flashed that are not about a file (an invalid manual
    framework package path)."""
    inputs: tuple[Path, ...]
    """Files whose stat decides whether to read again: platformio.ini, idedata.json,
    flasher_args.json (espidf), the build folder itself, every file in `files`."""


Runner = Callable[[Sequence[str], Path, float], str]
"""(argv, working directory, timeout seconds) -> stdout; raises PioUnavailableError."""


def env_ref(path: Path) -> EnvRef:
    """Check that `path` is `<root>/.pio/build/<env>` (compared ignoring case), that
    `<root>/platformio.ini` exists and that it declares `[env:<env>]`. The folder itself
    need not exist yet. Raises EnvDirError."""
    path = path.resolve()
    build = path.parent
    if build.name.lower() != "build" or build.parent.name.lower() != ".pio":
        raise EnvDirError(N_(f"{path} is not a .pio/build/<env> folder of a PlatformIO project"))
    root = build.parent.parent
    if not (root / PLATFORMIO_INI).is_file():
        raise EnvDirError(N_(f"{root} has no {PLATFORMIO_INI}"))
    declared = {e.lower(): e for e in ini_envs(root)}
    env = declared.get(path.name.lower())
    if env is None:
        raise EnvDirError(N_(f"{PLATFORMIO_INI} in {root} does not declare [env:{path.name}]"))
    return EnvRef(root, env)


def built_envs_below(path: Path) -> tuple[EnvRef, ...]:
    """When `path` is a project root (has platformio.ini): its declared envs that are
    built (is_built()), for the "pick .pio/build/<env>" hint. () otherwise. Never searches
    further down."""
    if not (path / PLATFORMIO_INI).is_file():
        return ()
    root = path.resolve()
    return tuple(ref for e in ini_envs(root) if is_built(ref := EnvRef(root, e)))


def ini_envs(project_root: Path) -> tuple[str, ...]:
    """Names of the `[env:<name>]` sections of platformio.ini in file order, read
    offline (no interpolation). () when the file is missing or unreadable."""
    try:
        text = (project_root / PLATFORMIO_INI).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(m[1].strip() for m in _ENV_SECTION.finditer(text))


def is_built(ref: EnvRef) -> bool:
    """The build folder exists and holds at least one .bin file."""
    try:
        return any(p.is_file() for p in ref.build_dir.glob("*.bin"))
    except OSError:
        return False


def read_idedata(ref: EnvRef) -> tuple[PioImages | None, str]:
    """idedata.json of the env, or (None, why) when it is missing, unparsable, lacks
    extra.flash_images or extra.application_offset, is older than platformio.ini, or lists
    a file that does not exist. Offsets are read as integers whatever their spelling
    ("0x1000", "0x0000", 0)."""
    file = ref.build_dir / IDEDATA
    try:
        stamp = file.stat().st_mtime_ns
    except OSError:
        return None, N_(f"{IDEDATA} not found")
    try:
        if stamp < (ref.project_root / PLATFORMIO_INI).stat().st_mtime_ns:
            return None, N_(f"{IDEDATA} is older than {PLATFORMIO_INI}")
        data: object = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, N_(f"{IDEDATA} cannot be read ({exc})")
    table = _table(data)
    if "extra" not in table:  # metadata-style {env: {...}}
        table = _table(table.get(ref.env))
    extra = _table(table.get("extra"))
    listed, offset = extra.get("flash_images"), _int(extra.get("application_offset"))
    if not isinstance(listed, list) or offset is None:
        return None, N_(f"{IDEDATA} lacks extra.flash_images or extra.application_offset")
    images: list[FlashImage] = []
    for item in cast(list[object], listed):
        image = _table(item)
        address, path = _int(image.get("offset")), image.get("path")
        if address is None or not isinstance(path, str) or not path:
            return None, N_(f"{IDEDATA} has a malformed flash_images entry")
        images.append(FlashImage(address, Path(path)))
    missing = [i.path.name for i in images if not i.path.is_file()]
    if missing:
        return None, N_(f"{IDEDATA} lists missing files: {', '.join(missing)}")
    return PioImages(tuple(images), offset), ""


def parse_config(text: str) -> dict[str, EnvConfig]:
    """`pio project config --json-output` -> EnvConfig per env name (sections
    `env:<name>`; the plain `env` section and others are skipped). Raises
    PioUnavailableError for output that is not that JSON."""
    try:
        data: object = json.loads(text)
    except ValueError as exc:
        raise PioUnavailableError(N_(f"pio project config printed no JSON ({exc})")) from exc
    if not isinstance(data, list):
        raise PioUnavailableError(N_("pio project config printed unexpected JSON"))
    envs: dict[str, EnvConfig] = {}
    for section in cast(list[object], data):
        if not isinstance(section, list) or len(cast(list[object], section)) != 2:
            raise PioUnavailableError(N_("pio project config printed unexpected JSON"))
        name, pairs = cast(list[object], section)
        if not isinstance(name, str) or not isinstance(pairs, list):
            raise PioUnavailableError(N_("pio project config printed unexpected JSON"))
        if not name.startswith("env:"):
            continue
        options: dict[str, object] = {}
        for pair in cast(list[object], pairs):
            if isinstance(pair, list) and len(cast(list[object], pair)) == 2:
                key, value = cast(list[object], pair)
                if isinstance(key, str):
                    options[key] = value
        board, mcu = options.get("board"), options.get("board_build.mcu")
        envs[name[4:]] = EnvConfig(
            board=board if isinstance(board, str) and board else None,
            mcu=mcu.lower() if isinstance(mcu, str) and mcu else None,
            frameworks=_strings(options.get("framework")),
            platform_packages=_strings(options.get("platform_packages")),
        )
    return envs


def board_mcu(board: str, project_root: Path, core: Path) -> str | None:
    """build.mcu (lower case) of the board definition `<board>.json`, looked up offline in
    `<project_root>/boards/`, then in `<core>/platforms/*/boards/`. None when not
    found or unreadable."""
    candidates = [project_root / "boards" / f"{board}.json"]
    candidates += sorted((core / "platforms").glob(f"*/boards/{board}.json"))
    for path in candidates:
        try:
            data: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mcu = _table(_table(data).get("build")).get("mcu")
        if isinstance(mcu, str) and mcu:
            return mcu.lower()
    return None


def configured_target(config: EnvConfig | None, project_root: Path, core: Path) -> str | None:
    """The env's target chip: board_build.mcu, else board_mcu() of its board. None when
    unknown (the configured-target check is then skipped)."""
    if config is None:
        return None
    if config.mcu:
        return config.mcu
    return board_mcu(config.board, project_root, core) if config.board else None


def core_dir(environ: Mapping[str, str]) -> Path:
    """PLATFORMIO_CORE_DIR, else <USERPROFILE>/.platformio."""
    configured = environ.get("PLATFORMIO_CORE_DIR")
    if configured:
        return Path(configured)
    home = environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / ".platformio"


def find_pio(environ: Mapping[str, str], which: Callable[[str], str | None]) -> Path | None:
    """pio on PATH (`which("pio")`), else <core_dir>/penv/Scripts/pio.exe when it exists,
    else None."""
    found = which("pio")
    if found:
        return Path(found)
    bundled = core_dir(environ) / "penv" / "Scripts" / "pio.exe"
    return bundled if bundled.is_file() else None


def run_pio(argv: Sequence[str], cwd: Path, timeout: float) -> str:
    """Run pio without a console window and return its stdout. PioUnavailableError when
    it cannot start, exits non-zero or exceeds `timeout`."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        done = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PioUnavailableError(N_(f"pio did not answer within {timeout:.0f} s")) from exc
    except OSError as exc:
        raise PioUnavailableError(N_(f"pio could not be started: {exc}")) from exc
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise PioUnavailableError(N_(f"pio exited with code {done.returncode}: {last}"))
    return done.stdout


def query_config(pio: Path, project_root: Path, run: Runner = run_pio) -> dict[str, EnvConfig]:
    """`pio project config --json-output` in `project_root` (read-only), parsed.
    PioUnavailableError propagates."""
    argv = [str(pio), "project", "config", "--json-output"]
    return parse_config(run(argv, project_root, CONFIG_TIMEOUT))


def locate_package(
    *,
    manual: str,
    listed: Path | None,
    config: EnvConfig | None,
    core: Path,
) -> PackageChoice:
    """The Arduino framework package. A non-empty `manual` path wins and is never replaced:
    a folder holding tools/partitions/boot_app0.bin, else a problem. Otherwise auto-detect:
    the package of `listed` (the boot_app0.bin path idedata.json gives), else the
    `framework-arduinoespressif32*` folders under `<core>/packages/`: the one whose
    package.json version a `platform_packages` entry names, else the highest version."""
    if manual.strip():
        path = Path(manual.strip())
        if (path / BOOT_APP0_IN_PACKAGE).is_file():
            return PackageChoice(path, auto=False)
        return PackageChoice(
            None,
            auto=False,
            problem=N_(
                f"framework package not found: {path} has no {BOOT_APP0_IN_PACKAGE.as_posix()}"
            ),
        )
    if listed is not None and listed.is_file() and len(listed.parents) > 2:
        return PackageChoice(listed.parents[2], auto=True)
    try:
        found = [
            d
            for d in (core / "packages").glob(f"{ARDUINO_PACKAGE}*")
            if (d / BOOT_APP0_IN_PACKAGE).is_file()
        ]
    except OSError:
        found = []
    if not found:
        return PackageChoice(None, auto=True)
    versions = {d: _package_version(d) for d in found}
    for spec in config.platform_packages if config is not None else ():
        if ARDUINO_PACKAGE not in spec or "@" not in spec:
            continue
        wanted = spec.rsplit("@", 1)[1].strip().lstrip("~^=")
        for d in found:
            if d.name.endswith(f"@{wanted}") or versions[d][1].startswith(wanted):
                return PackageChoice(d, auto=True)
    return PackageChoice(max(found, key=lambda d: versions[d][0]), auto=True)


def read_env(
    ref: EnvRef,
    *,
    core: Path,
    manual_package: str = "",
    config: EnvConfig | None = None,
) -> PioBuild:
    """Read the env's build.

    Framework: from `config` when it names arduino or espidf, else espidf when the build
    folder has flasher_args.json, else arduino.
    Images: read_idedata(); when it gives None the build folder is scanned and the reason
    goes into notes.
    - idedata: the listed images plus firmware.bin at application_offset.
    - scan, arduino: every .bin, classified with image_sniff against partitions.bin;
      merged images and unrecognised files are left out (noted).
    - scan, espidf: flasher_args.json's flash_files with idf.py names mapped to PlatformIO
      outputs (bootloader -> bootloader.bin, partition table -> partitions.bin, app ->
      firmware.bin, otadata -> ota_data_initial.bin, others by file name).
    Arduino boot_app0.bin (from the idedata path or locate_package()): placed at the
    table's otadata partition with a note when that is not 0xE000; dropped (noted) when the
    table has no otadata partition; a blank otadata of the partition's size when no package
    has the file (noted); an invalid manual path is a problem.
    Each listed address is cross-checked with image_sniff; a disagreement is a note on the
    file and the listed address is kept.
    Raises BuildMissingError, EnvDirError (env no longer declared), PioBuildError."""
    build_dir = ref.build_dir
    if not is_built(ref):
        raise BuildMissingError(N_(f"{build_dir}: no build output"))
    if ref.env not in ini_envs(ref.project_root):
        raise EnvDirError(N_(f"{PLATFORMIO_INI} no longer declares [env:{ref.env}]"))
    framework = _framework(build_dir, config)
    notes: list[str] = []
    problems: list[str] = []
    inputs = [ref.project_root / PLATFORMIO_INI, build_dir / IDEDATA, build_dir]
    images, why = read_idedata(ref)
    listed_boot: Path | None = None
    if images is not None:
        source = ImageSource.IDEDATA
        raw: list[tuple[int, Path, EntryKind | None, bool]] = []
        for image in images.images:
            if framework == Framework.ARDUINO and image.path.name.lower() == BOOT_APP0:
                listed_boot = image.path
                continue
            raw.append((image.address, image.path, _kind_by_name(image.path.name), True))
        app = build_dir / APP_FILE
        raw.append((images.application_offset, app, EntryKind.APP, True))
    else:
        source = ImageSource.SCAN
        notes.append(N_(f"{why}; the build folder was scanned instead"))
        if framework == Framework.ESPIDF and (build_dir / FLASHER_ARGS).is_file():
            inputs.append(build_dir / FLASHER_ARGS)
            raw = _espidf_files(build_dir)
        else:
            raw = []
    contents = {path: _read(path) for _, path, _, _ in raw}
    if source == ImageSource.SCAN and not raw:
        for path in sorted(build_dir.glob("*.bin")):
            contents[path] = _read(path)
            raw.append((-1, path, _kind_by_name(path.name), False))
    table = next(
        (
            t
            for data in contents.values()
            if data is not None and (t := parse_partition_table(data)) is not None
        ),
        None,
    )
    files: list[PioFile] = []
    for address, path, kind, listed in raw:
        data = contents.get(path)
        if data is None:  # listed but unreadable: the resolver reports it missing
            files.append(PioFile(max(address, 0), path, kind))
            continue
        found = sniff(data, path.name, table)
        if not listed:
            if found.kind == EntryKind.MERGED:
                notes.append(N_(f"{path.name}: a merged image; left out"))
                continue
            if found.kind == EntryKind.UNKNOWN or found.address is None:
                notes.append(N_(f"{path.name}: not recognised; left out"))
                continue
            files.append(PioFile(found.address, path, found.kind))
            continue
        note = ""
        if (
            found.address is not None
            and not found.confirm
            and found.address != address
            and found.kind in (EntryKind.BOOTLOADER, EntryKind.APP, EntryKind.PARTITION_TABLE)
        ):
            note = (
                f"listed at 0x{address:X}; the {found.kind.value} image belongs at "
                f"0x{found.address:X} (the listed address is used)"
            )
        files.append(PioFile(address, path, kind, note))
    package: PackageChoice | None = None
    if framework == Framework.ARDUINO:
        package = locate_package(
            manual=manual_package, listed=listed_boot, config=config, core=core
        )
        if package.problem:
            problems.append(package.problem)
        boot = package.path / BOOT_APP0_IN_PACKAGE if package.path is not None else None
        otadata = table.first(TYPE_DATA, SUBTYPE_OTADATA) if table is not None else None
        if any(f.kind == EntryKind.OTADATA for f in files):
            pass  # the build has its own otadata image
        elif otadata is None:
            if table is not None:
                notes.append(
                    N_(f"{BOOT_APP0} left out: the partition table has no otadata partition")
                )
        elif boot is not None:
            note = ""
            if otadata.offset != BOOT_APP0_PIO_ADDRESS:
                note = (
                    f"PlatformIO writes {BOOT_APP0} at 0x{BOOT_APP0_PIO_ADDRESS:X}; placed at "
                    f"the otadata partition (0x{otadata.offset:X})"
                )
            files.append(PioFile(otadata.offset, boot, EntryKind.OTADATA, note))
        elif package.problem is None:
            files.append(PioFile(otadata.offset, None, EntryKind.OTADATA, blank_size=otadata.size))
            notes.append(
                N_(f"{BOOT_APP0} was not found in any framework package; a blank otadata is used")
            )
    unique: dict[object, PioFile] = {}
    for f in files:
        unique.setdefault(f.path if f.path is not None else ("blank", f.address), f)
    ordered = sorted(unique.values(), key=lambda f: f.address)
    headers = {
        f.kind: header
        for f in ordered
        if f.path is not None
        and f.kind in (EntryKind.BOOTLOADER, EntryKind.APP)
        and (data := contents.get(f.path)) is not None
        and (header := sniff(data, f.path.name, table).header) is not None
    }
    flash = _flash(headers.get(EntryKind.BOOTLOADER))
    app_header = headers.get(EntryKind.APP)
    if app_header is not None and EntryKind.BOOTLOADER in headers:
        app_flash = _flash(app_header)
        if app_flash != flash:
            notes.append(
                N_(
                    f"flash settings differ: bootloader {_flash_text(flash)}, "
                    f"app {_flash_text(app_flash)} (the bootloader's are used)"
                )
            )
    inputs += [f.path for f in ordered if f.path is not None]
    return PioBuild(
        ref=ref,
        framework=framework,
        source=source,
        files=tuple(ordered),
        flash=flash,
        chip=app_header.chip if app_header is not None else None,
        package=package,
        notes=tuple(notes),
        problems=tuple(problems),
        inputs=tuple(inputs),
    )


_ENV_SECTION = re.compile(r"^[ \t]*\[env:([^\]]+)\]", re.MULTILINE)

_PIO_NAMES = {
    EntryKind.BOOTLOADER: "bootloader.bin",
    EntryKind.PARTITION_TABLE: "partitions.bin",
    EntryKind.APP: APP_FILE,
    EntryKind.OTADATA: "ota_data_initial.bin",
}
"""PlatformIO output names for the idf.py files flasher_args.json lists (espidf)."""

_SECTIONS = {
    "bootloader": EntryKind.BOOTLOADER,
    "app": EntryKind.APP,
    "partition-table": EntryKind.PARTITION_TABLE,
    "otadata": EntryKind.OTADATA,
}


def _table(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return None
    return None


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    if isinstance(value, list):
        return tuple(v for v in cast(list[object], value) if isinstance(v, str))
    return ()


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _framework(build_dir: Path, config: EnvConfig | None) -> Framework:
    names = set(config.frameworks) if config is not None else set[str]()
    if Framework.ESPIDF.value in names:
        return Framework.ESPIDF
    if Framework.ARDUINO.value in names:
        return Framework.ARDUINO
    return Framework.ESPIDF if (build_dir / FLASHER_ARGS).is_file() else Framework.ARDUINO


def _kind_by_name(name: str) -> EntryKind | None:
    lowered = name.lower()
    by_name = {v.lower(): k for k, v in _PIO_NAMES.items()}
    if lowered == BOOT_APP0:
        return EntryKind.OTADATA
    return by_name.get(lowered)


def _espidf_files(build_dir: Path) -> list[tuple[int, Path, EntryKind | None, bool]]:
    try:
        args = _table(json.loads((build_dir / FLASHER_ARGS).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise PioBuildError(f"{FLASHER_ARGS}: {exc}") from exc
    kinds: dict[str, EntryKind] = {}
    for section, kind in _SECTIONS.items():
        name = _table(args.get(section)).get("file")
        if isinstance(name, str):
            kinds[name.replace("\\", "/")] = kind
    files: list[tuple[int, Path, EntryKind | None, bool]] = []
    for offset, name in _table(args.get("flash_files")).items():
        address = _int(offset)
        if address is None or not isinstance(name, str):
            raise PioBuildError(N_(f"{FLASHER_ARGS}: bad flash_files entry {offset!r}"))
        kind = kinds.get(name.replace("\\", "/"))
        mapped = _PIO_NAMES.get(kind) if kind is not None else None
        path = build_dir / (mapped or Path(name).name)
        if not path.is_file() and (build_dir / name).is_file():
            path = build_dir / name
        files.append((address, path, kind, True))
    if not files:
        raise PioBuildError(N_(f"{FLASHER_ARGS} lists no flash files"))
    return files


def _package_version(folder: Path) -> tuple[tuple[int, ...], str]:
    try:
        data = _table(json.loads((folder / "package.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        data = {}
    version = data.get("version")
    text = version if isinstance(version, str) else ""
    return tuple(int(n) for n in re.findall(r"\d+", text)), text


def _flash(header: ImageHeader | None) -> FlashParams:
    if header is None:
        return FlashParams()
    return FlashParams(
        header.flash_mode or "keep", header.flash_freq or "keep", header.flash_size or "keep"
    )


def _flash_text(flash: FlashParams) -> str:
    return f"{flash.mode} {flash.freq} {flash.size}"
