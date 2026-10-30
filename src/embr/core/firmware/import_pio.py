"""Read a PlatformIO build (design doc §5 stage three, §12.5).

The user picks `<project>/.pio/build/<env>`. Images come from the env's idedata.json
(written by the PlatformIO IDE; used only when it is not older than platformio.ini and
every listed file exists), else from a scan of the build folder classified by
image_sniff. Embr never runs `pio project metadata` (it rewrites idedata.json) and never
writes into the project; pio is only used for the read-only `pio project config`.
Offline except run_pio(). May use image_sniff, never the reverse.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import FlashParams

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
    raise NotImplementedError


def built_envs_below(path: Path) -> tuple[EnvRef, ...]:
    """When `path` is a project root (has platformio.ini): its declared envs that are
    built (is_built()), for the "pick .pio/build/<env>" hint. () otherwise. Never searches
    further down."""
    raise NotImplementedError


def ini_envs(project_root: Path) -> tuple[str, ...]:
    """Names of the `[env:<name>]` sections of platformio.ini in file order, read
    offline (no interpolation). () when the file is missing or unreadable."""
    raise NotImplementedError


def is_built(ref: EnvRef) -> bool:
    """The build folder exists and holds at least one .bin file."""
    raise NotImplementedError


def read_idedata(ref: EnvRef) -> tuple[PioImages | None, str]:
    """idedata.json of the env, or (None, why) when it is missing, unparsable, lacks
    extra.flash_images or extra.application_offset, is older than platformio.ini, or lists
    a file that does not exist. Offsets are read as integers whatever their spelling
    ("0x1000", "0x0000", 0)."""
    raise NotImplementedError


def parse_config(text: str) -> dict[str, EnvConfig]:
    """`pio project config --json-output` -> EnvConfig per env name (sections
    `env:<name>`; the plain `env` section and others are skipped). Raises
    PioUnavailableError for output that is not that JSON."""
    raise NotImplementedError


def board_mcu(board: str, project_root: Path, core: Path) -> str | None:
    """build.mcu (lower case) of the board definition `<board>.json`, looked up offline in
    `<project_root>/boards/`, then in `<core>/platforms/*/boards/`. None when not
    found or unreadable."""
    raise NotImplementedError


def configured_target(config: EnvConfig | None, project_root: Path, core: Path) -> str | None:
    """The env's target chip: board_build.mcu, else board_mcu() of its board. None when
    unknown (the configured-target check is then skipped)."""
    raise NotImplementedError


def core_dir(environ: Mapping[str, str]) -> Path:
    """PLATFORMIO_CORE_DIR, else <USERPROFILE>/.platformio."""
    raise NotImplementedError


def find_pio(environ: Mapping[str, str], which: Callable[[str], str | None]) -> Path | None:
    """pio on PATH (`which("pio")`), else <core_dir>/penv/Scripts/pio.exe when it exists,
    else None."""
    raise NotImplementedError


def run_pio(argv: Sequence[str], cwd: Path, timeout: float) -> str:
    """Run pio without a console window and return its stdout. PioUnavailableError when
    it cannot start, exits non-zero or exceeds `timeout`."""
    raise NotImplementedError


def query_config(pio: Path, project_root: Path, run: Runner = run_pio) -> dict[str, EnvConfig]:
    """`pio project config --json-output` in `project_root` (read-only), parsed.
    PioUnavailableError propagates."""
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError
