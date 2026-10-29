"""core/firmware/image_header: chip ID, revision range and flash settings of fixtures."""

import json

import pytest
from embr_testkit import FIXTURES, IDF, chip_defs, idf_app_path, idf_expect, parse_rev_range

from embr.core.firmware.image_header import (
    NO_MAX_REVISION,
    format_revision,
    read_image_header,
    read_image_header_file,
)

IDF_SAMPLES = sorted(p.name for p in IDF.iterdir() if p.is_dir())


@pytest.mark.parametrize("sample", IDF_SAMPLES)
def test_idf_app_header_matches_expect(sample: str) -> None:
    expect = idf_expect(sample)
    header = read_image_header_file(idf_app_path(sample))
    assert header is not None
    assert header.chip == expect["chip"]
    assert header.chip_id == chip_defs()[expect["chip"]].IMAGE_CHIP_ID
    rev_range = parse_rev_range(expect["app_chip_revision"])
    assert (header.min_rev_full, header.max_rev_full) == rev_range
    settings = json.loads(expect["flash_settings"])
    assert (header.flash_mode, header.flash_freq, header.flash_size) == (
        settings["flash_mode"],
        settings["flash_freq"],
        settings["flash_size"],
    )
    assert header.has_app_desc and not header.has_bootloader_desc
    # the descriptors carry the same IDF_VER as the build metadata
    build = IDF / sample / "build"
    description = json.loads((build / "project_description.json").read_text(encoding="utf-8"))
    boot = read_image_header_file(build / "bootloader/bootloader.bin")
    assert boot is not None
    assert header.idf_ver == boot.idf_ver == description["git_revision"][:31]


def test_p4_rev1_and_rev3() -> None:
    rev1 = read_image_header_file(idf_app_path("esp32p4_rev1_hello"))
    rev3 = read_image_header_file(IDF / "esp32p4_rev3_hello/build/bootloader/bootloader.bin")
    assert rev1 is not None and rev3 is not None
    assert (rev1.chip_id, rev1.min_rev_full, rev1.max_rev_full) == (18, 0, 199)
    assert (rev3.chip_id, rev3.min_rev_full, rev3.max_rev_full) == (18, 301, 399)
    assert rev3.has_bootloader_desc and not rev3.has_app_desc


def test_pio_image_has_no_upper_limit() -> None:
    path = FIXTURES / "pio/arduino_esp32_16mb/.pio/build/esp32dev/firmware.bin"
    header = read_image_header(path.read_bytes())
    assert header is not None
    assert header.chip == "esp32"
    assert header.max_rev_full == NO_MAX_REVISION


def test_non_images_return_none() -> None:
    files = [
        *sorted((FIXTURES / "synthetic" / "negative").iterdir()),
        IDF / "esp32_hello/build/partition_table/partition-table.bin",
        IDF / "esp32s3_ota_custom/build/ota_data_initial.bin",
        FIXTURES / "synthetic/merged/esp32_offset_0x0.bin",  # starts with padding
    ]
    for path in files:
        assert read_image_header_file(path) is None, path
    app = idf_app_path("esp32s3_hello").read_bytes()
    unknown_chip = app[:12] + (0x7777).to_bytes(2, "little") + app[14:]
    for data in (b"", b"\xe9", app[:16], app[:40], unknown_chip, b"\xff" * 64):
        assert read_image_header(data) is None


def test_format_revision() -> None:
    assert [format_revision(r) for r in (0, 199, 301)] == ["v0.0", "v1.99", "v3.1"]
    assert format_revision(NO_MAX_REVISION) == "any"
