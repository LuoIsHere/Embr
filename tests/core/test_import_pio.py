"""core/firmware/import_pio: reading a PlatformIO env build (design doc §5 stage three,
§8.2, §12.5), and how core tracking takes what it hands over. Projects are assembled in
tmp_path from tests/fixtures/pio; expected values come from the samples' idedata.json,
flasher_args.json, SOURCE.toml and the PIO merge command, never from Embr."""

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest
from embr_testkit import (
    PIO,
    age,
    drop_partition,
    edit_json,
    make_package,
    move_file,
    pio_build,
    pio_project,
    pio_source,
    write_newer,
)

from embr.core.firmware.filecache import FileCache
from embr.core.firmware.import_pio import (
    BOOT_APP0_IN_PACKAGE,
    BuildMissingError,
    EnvConfig,
    EnvDirError,
    EnvRef,
    Framework,
    ImageSource,
    PackageChoice,
    PioBuild,
    PioFile,
    PioUnavailableError,
    board_mcu,
    built_envs_below,
    configured_target,
    core_dir,
    env_ref,
    find_pio,
    ini_envs,
    is_built,
    locate_package,
    parse_config,
    query_config,
    read_env,
    read_idedata,
)
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import BuildRef, Firmware, FlashParams, Source
from embr.core.firmware.tracking import (
    DerivedBuild,
    DerivedFile,
    EntryMark,
    ResolveState,
    accept,
    entry_key,
    resolve,
)

ARDUINO = ("arduino_esp32_16mb", "esp32dev")
OTADATA = ("arduino_esp32s3_otadata", "esp32-s3-devkitc-1")
ESPIDF = ("espidf_esp32s3", "esp32-s3-devkitc-1")
MULTI_ESP32 = ("multi_env", "esp32dev")
MULTI_S3 = ("multi_env", "esp32-s3-devkitc-1")
SAMPLES = [ARDUINO, OTADATA, ESPIDF, MULTI_ESP32, MULTI_S3]
NOT_MOVED = [ARDUINO, ESPIDF, MULTI_ESP32, MULTI_S3]
"""Samples whose otadata partition is where PIO writes boot_app0.bin (or espidf)."""
S3_PACKAGE = "platformio/framework-arduinoespressif32 @ 3.20011.0"


def _project(tmp_path: Path, sample: str, env: str) -> EnvRef:
    """The sample copied to tmp_path/project, its PIO core at _core(tmp_path)."""
    root = tmp_path / "project"
    pio_project(sample, root, _core(tmp_path))
    return EnvRef(root, env)


def _core(tmp_path: Path) -> Path:
    return tmp_path / "core"


def _offset(value: str | int) -> int:
    return value if isinstance(value, int) else int(value, 16)


def _listed(ref: EnvRef) -> list[tuple[int, str]]:
    """(address, file name) of idedata.json's images plus firmware.bin, by address."""
    extra = json.loads((ref.build_dir / "idedata.json").read_text(encoding="utf-8"))["extra"]
    images = [
        (_offset(i["offset"]), PureWindowsPath(i["path"]).name) for i in extra["flash_images"]
    ]
    return sorted([*images, (_offset(extra["application_offset"]), "firmware.bin")])


def _chip(sample: str, env: str) -> str:
    """[expect] chip, or the chip of the env in [expect] envs ("<env> (<chip>, board ...)")."""
    expect = pio_source(sample)["expect"]
    if "chip" in expect:
        return expect["chip"]
    line = next(e for e in expect["envs"] if e.startswith(f"{env} ("))
    return line.split("(")[1].split(",")[0]


def _placed(build: PioBuild) -> list[tuple[int, Path | None, EntryKind | None, int]]:
    return [(f.address, f.path, f.kind, f.blank_size) for f in build.files]


def _named(build: PioBuild, name: str) -> PioFile:
    return next(f for f in build.files if f.name == name)


def _merge_command() -> tuple[FlashParams, list[tuple[int, str]]]:
    """Flash settings and (address, file name) of arduino_esp32_16mb's PIO merge command."""
    path = PIO / "arduino_esp32_16mb" / "reference_merge_command.txt"
    words = path.read_text(encoding="utf-8").split()

    def arg(name: str) -> str:
        return words[words.index(name) + 1]

    images = [
        (int(w, 16), PureWindowsPath(words[i + 1]).name)
        for i, w in enumerate(words)
        if w.startswith("0x")
    ]
    return FlashParams(arg("--flash-mode"), arg("--flash-freq"), arg("--flash-size")), images


# ---- the env folder ----------------------------------------------------------------------

SAME_ENV: dict[str, Callable[[EnvRef], Path]] = {
    "plain": lambda r: r.build_dir,
    "case": lambda r: r.project_root / ".PIO" / "Build" / r.env.upper(),
    "relative": lambda r: r.build_dir.relative_to(r.project_root.parent),
}


@pytest.mark.parametrize("variant", list(SAME_ENV))
def test_env_ref_accepts_the_env_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    ref = _project(tmp_path, *MULTI_S3)
    monkeypatch.chdir(ref.project_root.parent)
    found = env_ref(SAME_ENV[variant](ref))
    assert (found.project_root, found.env.casefold()) == (ref.project_root, ref.env)
    assert found.project_root.is_absolute()
    shutil.rmtree(ref.build_dir)  # need not exist yet
    assert env_ref(ref.build_dir) == ref


REJECTED: dict[str, Callable[[EnvRef], Path]] = {
    "no_pio_folder": lambda r: r.project_root / "build" / r.env,
    "not_build": lambda r: r.project_root / ".pio" / "libdeps" / r.env,
    "below_the_env": lambda r: r.build_dir / "sub",
    "env_not_declared": lambda r: r.project_root / ".pio" / "build" / "other",
}


@pytest.mark.parametrize("case", [*REJECTED, "no_platformio_ini"])
def test_env_ref_rejects(tmp_path: Path, case: str) -> None:
    ref = _project(tmp_path, *ARDUINO)
    if case == "no_platformio_ini":
        (ref.project_root / "platformio.ini").unlink()
        folder = ref.build_dir
    else:
        folder = REJECTED[case](ref)
    folder.mkdir(parents=True, exist_ok=True)
    with pytest.raises(EnvDirError):
        env_ref(folder)


def test_ini_envs_and_built_envs(tmp_path: Path) -> None:
    ref = _project(tmp_path, *MULTI_ESP32)
    root = ref.project_root
    assert ini_envs(root) == ("esp32dev", "esp32-s3-devkitc-1")
    assert {r.env for r in built_envs_below(root)} == {"esp32dev", "esp32-s3-devkitc-1"}
    assert {r.project_root for r in built_envs_below(root)} == {root}

    for file in ref.build_dir.glob("*.bin"):  # `pio run -t clean`
        file.unlink()
    assert not is_built(ref) and is_built(EnvRef(root, "esp32-s3-devkitc-1"))
    assert [r.env for r in built_envs_below(root)] == ["esp32-s3-devkitc-1"]
    shutil.rmtree(ref.build_dir)
    assert not is_built(ref)
    assert built_envs_below(pio_build(root, "esp32-s3-devkitc-1")) == ()
    assert built_envs_below(root / ".pio") == ()  # never searches further down

    (root / "platformio.ini").unlink()
    assert ini_envs(root) == () and built_envs_below(root) == ()


# ---- idedata.json ------------------------------------------------------------------------


@pytest.mark.parametrize(("sample", "env"), SAMPLES)  # offsets "0x1000", "0x0000" and 0
def test_read_idedata(tmp_path: Path, sample: str, env: str) -> None:
    ref = _project(tmp_path, sample, env)
    images, why = read_idedata(ref)
    assert images is not None, why
    listed = [(i.address, i.path.name) for i in images.images]
    assert sorted([*listed, (images.application_offset, "firmware.bin")]) == _listed(ref)
    assert all(i.path.is_absolute() and i.path.is_file() for i in images.images)


@pytest.mark.parametrize(
    ("spelling", "address"), [("0x1000", 0x1000), ("0x0000", 0), (0, 0), (4096, 0x1000)]
)
def test_idedata_offset_spellings(tmp_path: Path, spelling: str | int, address: int) -> None:
    ref = _project(tmp_path, *OTADATA)
    edit_json(
        ref.build_dir / "idedata.json",
        lambda d: d["extra"]["flash_images"][0].update(offset=spelling),
    )
    images, why = read_idedata(ref)
    assert images is not None, why
    assert images.images[0].address == address


UNUSABLE_IDEDATA: dict[str, Callable[[EnvRef, Path], object]] = {
    "missing": lambda r, core: (r.build_dir / "idedata.json").unlink(),
    "unparsable": lambda r, core: write_newer(r.build_dir / "idedata.json", b"{"),
    "no_flash_images": lambda r, core: edit_json(
        r.build_dir / "idedata.json", lambda d: d["extra"].pop("flash_images")
    ),
    "no_application_offset": lambda r, core: edit_json(
        r.build_dir / "idedata.json", lambda d: d["extra"].pop("application_offset")
    ),
    "older_than_platformio_ini": lambda r, core: age(
        r.build_dir / "idedata.json", r.project_root / "platformio.ini"
    ),
    "listed_file_missing": lambda r, core: (
        core / "packages" / "framework-arduinoespressif32" / BOOT_APP0_IN_PACKAGE
    ).unlink(),
}


@pytest.mark.parametrize("case", list(UNUSABLE_IDEDATA))
def test_read_idedata_unusable(tmp_path: Path, case: str) -> None:
    ref = _project(tmp_path, *ARDUINO)
    UNUSABLE_IDEDATA[case](ref, _core(tmp_path))
    images, why = read_idedata(ref)
    assert images is None and why


# ---- read_env ----------------------------------------------------------------------------


@pytest.mark.parametrize(("sample", "env"), NOT_MOVED)
def test_read_env_takes_the_idedata_images(tmp_path: Path, sample: str, env: str) -> None:
    ref = _project(tmp_path, sample, env)
    build = read_env(ref, core=_core(tmp_path))
    assert build.ref == ref and build.source is ImageSource.IDEDATA
    assert [(f.address, f.name) for f in build.files] == _listed(ref)
    assert all(f.note == "" and f.path is not None and f.path.is_file() for f in build.files)
    assert build.chip == _chip(sample, env)
    assert build.problems == ()
    files = {f.path for f in build.files if f.path is not None}
    ini, idedata = ref.project_root / "platformio.ini", ref.build_dir / "idedata.json"
    assert set(build.inputs) >= {ini, idedata, ref.build_dir, *files}


def test_arduino_build_matches_the_pio_merge_command(tmp_path: Path) -> None:
    ref = _project(tmp_path, *ARDUINO)
    flash, images = _merge_command()
    build = read_env(ref, core=_core(tmp_path))
    assert build.framework is Framework.ARDUINO
    assert build.flash == flash  # from the bootloader header: dio / 40m / 16MB
    assert [(f.address, f.name) for f in build.files] == images
    kinds = [EntryKind.BOOTLOADER, EntryKind.PARTITION_TABLE, EntryKind.OTADATA, EntryKind.APP]
    assert [f.kind for f in build.files] == kinds
    assert build.package is not None and build.package.auto and build.package.path is not None


@pytest.mark.parametrize("how", ["deleted", "aged"])
@pytest.mark.parametrize(("sample", "env"), SAMPLES)
def test_scan_agrees_with_idedata(tmp_path: Path, sample: str, env: str, how: str) -> None:
    ref = _project(tmp_path, sample, env)
    listed = read_env(ref, core=_core(tmp_path))
    idedata = ref.build_dir / "idedata.json"
    if how == "deleted":
        idedata.unlink()
    else:
        age(idedata, ref.project_root / "platformio.ini")
    scanned = read_env(ref, core=_core(tmp_path))

    assert (listed.source, scanned.source) == (ImageSource.IDEDATA, ImageSource.SCAN)
    assert any("idedata.json" in n for n in scanned.notes)  # why it was not used
    assert _placed(scanned) == _placed(listed)
    assert (scanned.framework, scanned.flash, scanned.chip) == (
        listed.framework,
        listed.flash,
        listed.chip,
    )


def test_boot_app0_moves_to_the_otadata_partition(tmp_path: Path) -> None:
    ref = _project(tmp_path, *OTADATA)
    otadata = int(pio_source(OTADATA[0])["expect"]["otadata_offset"], 16)
    build = read_env(ref, core=_core(tmp_path))
    boot_app0 = _named(build, "boot_app0.bin")
    assert (boot_app0.address, boot_app0.kind) == (otadata, EntryKind.OTADATA)
    assert boot_app0.note  # moved from 0xe000
    others = [(f.address, f.name) for f in build.files if f.name != "boot_app0.bin"]
    assert others == [i for i in _listed(ref) if i[1] != "boot_app0.bin"]
    assert all(f.note == "" for f in build.files if f.name != "boot_app0.bin")


def test_boot_app0_is_dropped_without_an_otadata_partition(tmp_path: Path) -> None:
    ref = _project(tmp_path, *ARDUINO)
    drop_partition(ref.build_dir / "partitions.bin", "otadata")
    build = read_env(ref, core=_core(tmp_path))
    assert build.source is ImageSource.IDEDATA
    assert [f.name for f in build.files] == ["bootloader.bin", "partitions.bin", "firmware.bin"]
    assert any("boot_app0" in n for n in build.notes)


@pytest.mark.parametrize(
    ("sample", "env", "address"), [(*ARDUINO, 0xE000), (*OTADATA, 0xD000)]
)  # otadata partitions are 0x2000 bytes in both tables
def test_blank_otadata_without_a_framework_package(
    tmp_path: Path, sample: str, env: str, address: int
) -> None:
    ref = _project(tmp_path, sample, env)
    shutil.rmtree(_core(tmp_path) / "packages")
    build = read_env(ref, core=_core(tmp_path))
    blank = next(f for f in build.files if f.path is None)
    assert (blank.address, blank.kind, blank.blank_size) == (address, EntryKind.OTADATA, 0x2000)
    assert blank.name == "otadata (blank)"
    assert "boot_app0.bin" not in [f.name for f in build.files]
    assert any("boot_app0" in n for n in (*build.notes, blank.note))
    assert build.package is not None and build.package.path is None
    assert build.problems == ()


def test_espidf_scan_maps_idf_file_names(tmp_path: Path) -> None:
    ref = _project(tmp_path, *ESPIDF)
    (ref.build_dir / "idedata.json").unlink()
    move_file(ref.build_dir, 0x8000, 0x9000)  # the table offset comes from the file
    build = read_env(ref, core=_core(tmp_path))
    assert (build.framework, build.source, build.package) == (
        Framework.ESPIDF,
        ImageSource.SCAN,
        None,
    )
    assert [(f.address, f.name, f.kind) for f in build.files] == [
        (0x0, "bootloader.bin", EntryKind.BOOTLOADER),
        (0x9000, "partitions.bin", EntryKind.PARTITION_TABLE),
        (0x10000, "firmware.bin", EntryKind.APP),
    ]
    assert all(f.path is not None and f.path.parent == ref.build_dir for f in build.files)
    assert ref.build_dir / "flasher_args.json" in build.inputs


@pytest.mark.parametrize(
    ("sample", "configured", "flasher_args", "expected"),
    [
        (ARDUINO, None, True, Framework.ARDUINO),
        (ARDUINO, "arduino", True, Framework.ARDUINO),
        (ESPIDF, None, True, Framework.ESPIDF),  # flasher_args.json present
        (ESPIDF, "espidf", False, Framework.ESPIDF),  # from the config
        (ESPIDF, None, False, Framework.ARDUINO),
    ],
)
def test_framework(
    tmp_path: Path,
    sample: tuple[str, str],
    configured: str | None,
    flasher_args: bool,
    expected: Framework,
) -> None:
    ref = _project(tmp_path, *sample)
    if not flasher_args:
        (ref.build_dir / "flasher_args.json").unlink(missing_ok=True)
    config = None if configured is None else EnvConfig(None, None, (configured,), ())
    assert read_env(ref, core=_core(tmp_path), config=config).framework is expected


def test_listed_address_disagreeing_with_the_image_is_kept_with_a_note(tmp_path: Path) -> None:
    ref = _project(tmp_path, *OTADATA)  # esp32s3: the bootloader belongs at 0x0
    edit_json(
        ref.build_dir / "idedata.json",
        lambda d: d["extra"]["flash_images"][0].update(offset="0x1000"),
    )
    build = read_env(ref, core=_core(tmp_path))
    assert build.source is ImageSource.IDEDATA
    bootloader = _named(build, "bootloader.bin")
    assert bootloader.address == 0x1000 and bootloader.note
    assert _named(build, "firmware.bin").note == ""


@pytest.mark.parametrize("how", ["folder_gone", "no_bin"])
def test_build_missing(tmp_path: Path, how: str) -> None:
    ref = _project(tmp_path, *ARDUINO)
    if how == "folder_gone":
        shutil.rmtree(ref.build_dir)
    else:
        for file in ref.build_dir.glob("*.bin"):
            file.unlink()
    with pytest.raises(BuildMissingError):
        read_env(ref, core=_core(tmp_path))


def test_env_no_longer_declared(tmp_path: Path) -> None:
    ref = _project(tmp_path, *MULTI_ESP32)
    write_newer(ref.project_root / "platformio.ini", b"[env:esp32-s3-devkitc-1]\n")
    with pytest.raises(EnvDirError):
        read_env(ref, core=_core(tmp_path))


def test_read_env_with_a_manual_package(tmp_path: Path) -> None:
    ref = _project(tmp_path, *ARDUINO)
    mine = make_package(tmp_path / "mine", "9.9.9", b"\x01" * 0x2000)
    build = read_env(ref, core=_core(tmp_path), manual_package=str(mine))
    assert build.package == PackageChoice(mine, auto=False)
    boot_app0 = _named(build, "boot_app0.bin")
    assert (boot_app0.address, boot_app0.path) == (0xE000, mine / BOOT_APP0_IN_PACKAGE)

    bad = read_env(ref, core=_core(tmp_path), manual_package=str(tmp_path / "nowhere"))
    assert bad.problems and bad.package is not None and bad.package.problem
    assert bad.package.path is None


# ---- framework package -------------------------------------------------------------------


def _packages(core: Path) -> dict[str, Path]:
    """Arduino framework packages under core/packages/ by version; the highest version is
    neither first nor last by folder name. framework-espidf must be ignored."""
    make_package(core / "packages" / "framework-espidf", "5.3.0")
    folders = {
        "3.20009.0": "framework-arduinoespressif32",
        "3.20017.241212+sha.dcc1105b": "framework-arduinoespressif32@3.20017.241212",
        "3.20011.0": "framework-arduinoespressif32@src-zzz",
    }
    return {v: make_package(core / "packages" / name, v) for v, name in folders.items()}


def _arduino(*packages: str) -> EnvConfig:
    return EnvConfig("esp32dev", None, ("arduino",), packages)


def test_locate_package_auto(tmp_path: Path) -> None:
    core = _core(tmp_path)
    packages = _packages(core)
    highest = packages["3.20017.241212+sha.dcc1105b"]
    assert locate_package(manual="", listed=None, config=_arduino(), core=core) == PackageChoice(
        highest, auto=True
    )
    assert locate_package(manual="", listed=None, config=None, core=core).path == highest
    named = locate_package(manual="", listed=None, config=_arduino(S3_PACKAGE), core=core)
    assert named == PackageChoice(packages["3.20011.0"], auto=True)

    other = make_package(tmp_path / "elsewhere" / "framework-arduinoespressif32", "1.0.0")
    listed = other / BOOT_APP0_IN_PACKAGE
    from_idedata = locate_package(manual="", listed=listed, config=_arduino(S3_PACKAGE), core=core)
    assert from_idedata == PackageChoice(other, auto=True)

    nothing = locate_package(manual="", listed=None, config=None, core=tmp_path / "empty")
    assert (nothing.path, nothing.problem) == (None, None)


def test_locate_package_manual_path_wins(tmp_path: Path) -> None:
    core = _core(tmp_path)
    packages = _packages(core)
    listed = packages["3.20009.0"] / BOOT_APP0_IN_PACKAGE
    mine = make_package(tmp_path / "mine", "9.9.9")
    chosen = locate_package(manual=str(mine), listed=listed, config=_arduino(), core=core)
    assert chosen == PackageChoice(mine, auto=False)

    bad = tmp_path / "not-a-package"
    bad.mkdir()
    refused = locate_package(manual=str(bad), listed=listed, config=_arduino(), core=core)
    assert (refused.path, refused.auto) == (None, False)  # no fallback
    assert refused.problem is not None and "not-a-package" in refused.problem


# ---- pio project config, boards and the PIO core -----------------------------------------

CONFIG = json.dumps(
    [
        ["platformio", [["default_envs", ["a"]]]],
        ["env", [["platform", "espressif32@6.12.0"], ["framework", ["arduino"]]]],
        [
            "env:a",
            [
                ["board", "esp32dev"],
                ["platform", "espressif32@6.12.0"],
                ["framework", ["arduino"]],
                ["board_build.mcu", "ESP32"],
            ],
        ],
        [
            "env:b",
            [
                ["board", "esp32-s3-devkitc-1"],
                ["platform", "espressif32@6.12.0"],
                ["framework", ["arduino", "espidf"]],
                ["platform_packages", [S3_PACKAGE]],
            ],
        ],
    ]
)


def test_parse_config() -> None:
    assert parse_config(CONFIG) == {
        "a": EnvConfig("esp32dev", "esp32", ("arduino",), ()),
        "b": EnvConfig("esp32-s3-devkitc-1", None, ("arduino", "espidf"), (S3_PACKAGE,)),
    }


@pytest.mark.parametrize("text", ["", "Error: Not a PlatformIO project", "{}", '[["env:a", 1]]'])
def test_parse_config_rejects_other_output(text: str) -> None:
    with pytest.raises(PioUnavailableError):
        parse_config(text)


def test_query_config(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path]] = []

    def run(argv: Sequence[str], cwd: Path, timeout: float) -> str:
        calls.append((list(argv), cwd))
        return CONFIG

    def fail(argv: Sequence[str], cwd: Path, timeout: float) -> str:
        raise PioUnavailableError("pio exited with 1")

    pio = tmp_path / "pio.exe"
    assert set(query_config(pio, tmp_path, run)) == {"a", "b"}
    [(argv, cwd)] = calls
    assert {"project", "config", "--json-output"} <= set(argv) and "metadata" not in argv
    assert cwd == tmp_path
    with pytest.raises(PioUnavailableError):
        query_config(pio, tmp_path, fail)


def _board(folder: Path, name: str, definition: Any) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(definition), encoding="utf-8")


def test_board_mcu(tmp_path: Path) -> None:
    root, core = tmp_path / "project", _core(tmp_path)
    _board(core / "platforms" / "espressif32" / "boards", "custom", {"build": {"mcu": "ESP32S3"}})
    _board(core / "platforms" / "other" / "boards", "elsewhere", {"build": {"mcu": "esp32c3"}})
    assert board_mcu("custom", root, core) == "esp32s3"
    assert board_mcu("elsewhere", root, core) == "esp32c3"
    _board(root / "boards", "custom", {"build": {"mcu": "esp32c6"}})
    assert board_mcu("custom", root, core) == "esp32c6"  # the project's own board wins
    _board(root / "boards", "no_mcu", {"build": {}})
    (root / "boards" / "broken.json").write_text("{", encoding="utf-8")
    assert [board_mcu(b, root, core) for b in ("unknown", "no_mcu", "broken")] == [None] * 3


def test_configured_target(tmp_path: Path) -> None:
    root, core = tmp_path / "project", _core(tmp_path)
    _board(core / "platforms" / "espressif32" / "boards", "esp32dev", {"build": {"mcu": "esp32"}})
    with_mcu = EnvConfig("esp32dev", "esp32s3", ("arduino",), ())
    assert configured_target(with_mcu, root, core) == "esp32s3"  # board_build.mcu wins
    assert configured_target(_arduino(), root, core) == "esp32"
    unknown = EnvConfig("unknown", None, ("arduino",), ())
    assert configured_target(unknown, root, core) is None
    assert configured_target(None, root, core) is None


def test_core_dir(tmp_path: Path) -> None:
    home = {"USERPROFILE": str(tmp_path / "home")}
    assert core_dir(home) == tmp_path / "home" / ".platformio"
    assert core_dir({**home, "PLATFORMIO_CORE_DIR": str(tmp_path / "pio")}) == tmp_path / "pio"


def test_find_pio(tmp_path: Path) -> None:
    environ = {"PLATFORMIO_CORE_DIR": str(_core(tmp_path))}
    asked: list[str] = []

    def which(name: str) -> str | None:
        asked.append(name)
        return None

    on_path = tmp_path / "bin" / "pio.exe"
    assert find_pio(environ, lambda _: str(on_path)) == on_path
    assert find_pio(environ, which) is None
    assert asked == ["pio"]
    penv = _core(tmp_path) / "penv" / "Scripts" / "pio.exe"
    penv.parent.mkdir(parents=True)
    penv.write_bytes(b"")
    assert find_pio(environ, which) == penv


# ---- core tracking of what the importer hands over ---------------------------------------

NOW = "2026-01-01T12:00:00"
BLANK_NOTE = "no package has boot_app0.bin; blank otadata"
APP_NOTE = "listed at 0x10000"


def _derived(
    build_dir: Path, *, notes: tuple[str, ...] = (), problems: tuple[str, ...] = ()
) -> DerivedBuild:
    files = (
        DerivedFile(0x1000, build_dir / "bootloader.bin", EntryKind.BOOTLOADER),
        DerivedFile(0x8000, build_dir / "partitions.bin", EntryKind.PARTITION_TABLE),
        DerivedFile(0xE000, None, EntryKind.OTADATA, note=BLANK_NOTE, blank_size=0x2000),
        DerivedFile(0x10000, build_dir / "firmware.bin", EntryKind.APP, note=APP_NOTE),
    )
    return DerivedBuild(
        files=files,
        declared_chips=(),
        configured_target=None,
        idf_version=None,
        flash=FlashParams("dio", "40m", "16MB"),
        before=None,
        after=None,
        stub=True,
        inputs=tuple(f.path for f in files if f.path is not None),
        notes=notes,
        problems=problems,
    )


def _pio_firmware(ref: EnvRef) -> Firmware:
    return Firmware(
        id="fw",
        name=ref.env,
        project_id=None,
        device="",
        source=Source.PIO_BUILD,
        build_ref=BuildRef(str(ref.project_root), ref.env),
    )


def test_resolve_blank_otadata_and_notes(tmp_path: Path) -> None:
    ref = _project(tmp_path, *ARDUINO)
    firmware = _pio_firmware(ref)
    derived = _derived(ref.build_dir, notes=("idedata.json is older than platformio.ini",))
    archive = tmp_path / "archive"
    first = resolve(firmware, derived, FileCache(), archive_dir=archive, now=NOW)

    assert first.state is ResolveState.OK, first.problems
    blank = next(e for e in first.firmware.entries if e.kind is EntryKind.OTADATA)
    assert (blank.name, blank.file, blank.size, blank.address) == (
        "otadata (blank)",
        "blank:",
        0x2000,
        0xE000,
    )
    assert blank.id == entry_key(EntryKind.OTADATA, "otadata (blank)")
    assert first.marks.get(blank.id) is not EntryMark.MISSING
    app = entry_key(EntryKind.APP, "firmware.bin")
    assert dict(first.entry_notes) == {blank.id: BLANK_NOTE, app: APP_NOTE}
    assert first.build_notes == derived.notes
    assert first.firmware.chip == "esp32"

    accept(firmware, first)
    again = resolve(firmware, derived, FileCache(), archive_dir=archive, now=NOW)
    assert (again.state, again.changes) == (ResolveState.OK, ())


def test_resolve_build_problems_make_it_incomplete(tmp_path: Path) -> None:
    ref = _project(tmp_path, *ARDUINO)
    problem = "framework package D:/nowhere has no tools/partitions/boot_app0.bin"
    derived = _derived(ref.build_dir, problems=(problem,))
    resolution = resolve(
        _pio_firmware(ref), derived, FileCache(), archive_dir=tmp_path / "archive", now=NOW
    )
    assert resolution.state is ResolveState.INCOMPLETE
    assert problem in resolution.problems
