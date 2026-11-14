"""core/firmware/import_idf: reading an ESP-IDF build directory.
Expected values come from the fixtures' flasher_args.json and SOURCE.toml."""

import json
import shutil
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from embr_testkit import IDF, edit_json, idf_expect, idf_project, link_build, make_newer

from embr.core.firmware.import_idf import (
    BuildMissingError,
    EncryptedBuildError,
    IdfBuildError,
    build_dir_below,
    configured_target,
    default_firmware_name,
    normalize_arg,
    read_build,
    unbuilt_image,
)


@pytest.mark.parametrize("sample", ["esp32s3_hello", "esp32s3_hello_v61"])  # 5.x _, 6.x -
def test_read_build(sample: str) -> None:
    build_dir = IDF / sample / "build"
    args = json.loads((build_dir / "flasher_args.json").read_text(encoding="utf-8"))
    files = cast(dict[str, str], args["flash_files"])
    source = tomllib.loads((IDF / sample / "SOURCE.toml").read_text(encoding="utf-8"))
    expect = idf_expect(sample)
    settings = json.loads(expect["flash_settings"])

    build = read_build(build_dir)

    assert [(f.address, f.path) for f in build.files] == sorted(
        (int(address, 16), (build_dir / rel).resolve()) for address, rel in files.items()
    )
    assert all(f.path.is_absolute() for f in build.files)
    assert (build.flash_mode, build.flash_freq, build.flash_size) == (
        settings["flash_mode"],
        settings["flash_freq"],
        settings["flash_size"],
    )
    assert (build.chip, build.target) == (expect["chip"], expect["chip"])
    assert (build.before, build.after, build.stub) == ("default-reset", "hard-reset", True)
    assert build.project_name == source["project_name"]
    assert build.idf_version == cast(str, source["toolchain"]).removeprefix("ESP-IDF ")
    assert set(build.inputs) >= {
        build_dir / "flasher_args.json",
        build_dir / "project_description.json",
        *(f.path for f in build.files),
    }
    assert default_firmware_name(build) == source["project_name"]


def test_unbuilt_image_follows_ninja_order(tmp_path: Path) -> None:
    """An image older than its ELF, or an ELF older than a library linked into it, is not
    finished; the ELFs and libraries are inputs, so a re-check notices them change."""
    build = idf_project("esp32s3_hello", tmp_path)
    assert unbuilt_image(read_build(build)) is None  # no ELF or library: nothing to tell
    app = link_build(build)
    boot = link_build(build, "bootloader")
    found = read_build(build)
    assert [link.image for link in found.links] == [app.image, boot.image]
    assert {app.elf, app.library, boot.elf, boot.library} <= set(found.inputs)
    assert unbuilt_image(found) is None

    make_newer(app.library, than=app.image)  # compiled, not linked yet
    assert unbuilt_image(found) == app.image.name
    make_newer(app.elf, than=app.library)  # linked, image not generated yet
    assert unbuilt_image(found) == app.image.name
    make_newer(app.image, than=app.elf)
    assert unbuilt_image(found) is None
    make_newer(boot.library, than=boot.image)
    assert unbuilt_image(found) == "bootloader.bin"
    boot.elf.unlink()  # a file that is gone tells nothing
    assert unbuilt_image(found) is None


def test_normalize_arg() -> None:
    values = ["default_reset", "hard_reset", "--flash_mode", "no-reset", "dio"]
    assert [normalize_arg(v) for v in values] == [
        "default-reset",
        "hard-reset",
        "--flash-mode",
        "no-reset",
        "dio",
    ]


def _encrypt_app(args: dict[str, Any]) -> None:
    args["app"]["encrypted"] = "true"


BROKEN: dict[str, tuple[Callable[[Path], object], type[IdfBuildError]]] = {
    "no_directory": (shutil.rmtree, BuildMissingError),
    "no_flasher_args": (lambda b: (b / "flasher_args.json").unlink(), BuildMissingError),
    "encrypted": (lambda b: edit_json(b / "flasher_args.json", _encrypt_app), EncryptedBuildError),
    "malformed": (lambda b: (b / "flasher_args.json").write_bytes(b"{"), IdfBuildError),
}


@pytest.mark.parametrize("case", list(BROKEN))
def test_unusable_build_raises(tmp_path: Path, case: str) -> None:
    change, error = BROKEN[case]
    build = idf_project("esp32s3_hello", tmp_path / "hello")
    change(build)
    with pytest.raises(IdfBuildError) as raised:
        read_build(build)
    assert type(raised.value) is error


def test_optional_fields_missing(tmp_path: Path) -> None:
    build_dir = idf_project("esp32s3_hello", tmp_path / "my_project")
    (build_dir / "project_description.json").unlink()

    def strip(args: dict[str, Any]) -> None:
        del args["extra_esptool_args"]["stub"]
        del args["flash_settings"]["flash_freq"]

    edit_json(build_dir / "flasher_args.json", strip)
    build = read_build(build_dir)
    assert (build.project_name, build.target, build.idf_version, build.config_file) == (
        None,
        None,
        None,
        None,
    )
    assert (build.chip, build.stub, build.flash_freq) == ("esp32s3", True, "keep")
    assert default_firmware_name(build) == "my_project"

    def no_stub(args: dict[str, Any]) -> None:
        args["extra_esptool_args"]["stub"] = False

    edit_json(build_dir / "flasher_args.json", no_stub)
    assert read_build(build_dir).stub is False


def test_configured_target(tmp_path: Path) -> None:
    config = tmp_path / "sdkconfig"
    config.write_bytes(
        b"#\n# Espressif IoT Development Framework\n#\n"
        b'CONFIG_IDF_TARGET_ARCH="xtensa"\nCONFIG_IDF_TARGET="esp32p4"\n'
        b"CONFIG_IDF_TARGET_ESP32P4=y\n"
    )
    other = tmp_path / "other"
    other.write_bytes(b"CONFIG_FOO=y\n")
    assert configured_target(config) == "esp32p4"
    assert configured_target(other) is None
    assert configured_target(tmp_path / "missing") is None
    assert configured_target(None) is None


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        (["build/flasher_args.json"], "build"),
        (["flasher_args.json", "build/flasher_args.json"], None),
        ([], None),
        (["app/build/flasher_args.json"], None),  # never searches two levels down
    ],
    ids=["project_folder", "build_folder", "neither", "two_levels"],
)
def test_build_dir_below(tmp_path: Path, layout: list[str], expected: str | None) -> None:
    for rel in layout:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("{}", encoding="utf-8")
    assert build_dir_below(tmp_path) == (tmp_path / expected if expected else None)
