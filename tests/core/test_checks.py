"""core/flasher/checks: pre-write checks fed with injected chip state."""

import shutil
from pathlib import Path

import pytest
from embr_testkit import FIXTURES, IDF, idf_app_path, idf_entries, make_entry

from embr.core.flasher.checks import (
    CAPACITY,
    FILES,
    FLASH_SIZE,
    CheckLevel,
    ChipState,
    SecurityState,
    check_capacity,
    check_chip,
    check_files,
    check_revisions,
    check_security,
    refuses,
    revision_incompatibility,
)
from embr.core.flasher.plan import PlanEntry

MB = 1024 * 1024
NO_SECURITY = SecurityState(secure_download_mode=False, flash_encryption=False, secure_boot=False)


def _state(
    chip: str = "esp32s3", major: int = 0, minor: int = 2, size: str | None = "4MB"
) -> ChipState:
    return ChipState(chip=chip, major=major, minor=minor, flash_size=size, security=NO_SECURITY)


def _entry(
    address: int = 0x10000,
    size: int = 0x1000,
    min_rev_full: int | None = None,
    max_rev_full: int | None = None,
    legacy_min_rev: int | None = None,
) -> PlanEntry:
    return PlanEntry(
        address,
        "C:\\fw\\a.bin",
        "a.bin",
        "0" * 64,
        size,
        min_rev_full=min_rev_full,
        max_rev_full=max_rev_full,
        legacy_min_rev=legacy_min_rev,
    )


# --- security ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        SecurityState(True, False, False),
        SecurityState(False, True, False),
        SecurityState(False, False, True),
    ],
    ids=["secure-download-mode", "flash-encryption", "secure-boot"],
)
def test_each_security_feature_refuses(state: SecurityState) -> None:
    checks = check_security(state)
    assert [c.level for c in checks] == [CheckLevel.REFUSE]
    assert checks[0].message.strip()


def test_security_all_or_nothing() -> None:
    assert [c.level for c in check_security(NO_SECURITY)] == [CheckLevel.PASS]
    all_on = check_security(SecurityState(True, True, True))
    assert [c.level for c in all_on] == [CheckLevel.REFUSE] * 3
    assert all_on[0].message == check_security(SecurityState(True, False, False))[0].message


# --- chip and revision ------------------------------------------------------------------


def test_chip_mismatch_refuses() -> None:
    assert check_chip("esp32s3", "esp32s3", idf_entries("esp32s3_hello")).level is CheckLevel.PASS
    check = check_chip("esp32s3", None, idf_entries("esp32p4_rev1_hello"))
    assert check.level is CheckLevel.REFUSE
    message = check.message.lower().replace("-", "")
    assert "esp32s3" in message and "esp32p4" in message


def test_p4_rev3_app_refused_on_v1_chip() -> None:
    v1 = _state("esp32p4", 1, 0)
    assert check_revisions(v1, idf_entries("esp32p4_rev1_hello")).level is CheckLevel.PASS
    # Compatible bootloader, incompatible app (not at the bootloader offset): still refused.
    boot = make_entry(0x2000, IDF / "esp32p4_rev1_hello/build/bootloader/bootloader.bin")
    app = make_entry(0x10000, idf_app_path("esp32p4_rev3_hello"), "app_rev3.bin")
    check = check_revisions(v1, [boot, app])
    assert check.level is CheckLevel.REFUSE
    assert "app_rev3.bin" in check.message
    assert check_revisions(_state("esp32p4", 3, 1), [app]).level is CheckLevel.PASS


def test_max_0xffff_means_no_upper_limit() -> None:
    entry = _entry(min_rev_full=100, max_rev_full=0xFFFF, legacy_min_rev=1)
    assert revision_incompatibility(_state(major=99, minor=99), entry) is None
    assert revision_incompatibility(_state(major=0, minor=99), entry) is not None
    bounded = _entry(min_rev_full=0, max_rev_full=199, legacy_min_rev=0)
    assert revision_incompatibility(_state(major=1, minor=99), bounded) is None
    assert revision_incompatibility(_state(major=2, minor=0), bounded) is not None


@pytest.mark.parametrize(
    ("chip", "major", "minor", "compatible"),
    [
        ("esp32", 3, 0, True),  # major 3 >= 1
        ("esp32", 0, 5, False),  # major 0 < 1 although minor 5 >= 1
        ("esp32c3", 0, 4, True),  # ESP32-C3 compares the minor revision
        ("esp32c3", 5, 0, False),
    ],
)
def test_legacy_min_rev(chip: str, major: int, minor: int, compatible: bool) -> None:
    # max_rev_full == 0: the image predates the *_full fields; legacy min_rev is 1 (3 on C3).
    entry = _entry(min_rev_full=0, max_rev_full=0, legacy_min_rev=3 if chip == "esp32c3" else 1)
    state = _state(chip, major, minor)
    assert (revision_incompatibility(state, entry) is None) is compatible
    assert refuses([check_revisions(state, [entry])]) is not compatible


# --- capacity ---------------------------------------------------------------------------


def test_capacity_overflow_refuses() -> None:
    end = 0x1A0000 + 0x180000  # idf/esp32s3_ota_custom's last partition (ota_1)
    checks = check_capacity(_state(size="2MB"), None, [], end)
    assert [c.name for c in checks if c.level is CheckLevel.REFUSE] == [CAPACITY]
    assert not refuses(check_capacity(_state(size="4MB"), None, [], end))
    calib = make_entry(0x3FF000, FIXTURES / "synthetic/attachments/calib_at_address.bin")
    assert not refuses(check_capacity(_state(size="4MB"), None, [calib], None))  # ends at 4MB
    assert refuses(
        check_capacity(_state(size="4MB"), None, [_entry(4 * MB - 0x1000, 0x1001)], None)
    )


def test_declared_size_mismatch_only_warns() -> None:
    checks = check_capacity(_state(size="16MB"), "4MB", idf_entries("esp32s3_hello"), 0x110000)
    assert [c.name for c in checks if c.level is CheckLevel.WARN] == [FLASH_SIZE]
    assert not refuses(checks)
    same = check_capacity(_state(size="4MB"), "4MB", idf_entries("esp32s3_hello"), 0x110000)
    assert all(c.level is CheckLevel.PASS for c in same)


# --- files ------------------------------------------------------------------------------


@pytest.fixture
def copied(tmp_path: Path) -> list[PlanEntry]:
    entries: list[PlanEntry] = []
    for entry in idf_entries("esp32s3_hello"):
        target = tmp_path / Path(entry.path).name
        shutil.copyfile(entry.path, target)
        entries.append(make_entry(entry.address, target))
    assert check_files(entries).level is CheckLevel.PASS
    return entries


def test_changed_file_refuses(copied: list[PlanEntry]) -> None:
    data = bytearray(Path(copied[1].path).read_bytes())
    data[100] ^= 0xFF  # same size, different content
    Path(copied[1].path).write_bytes(bytes(data))
    check = check_files(copied)
    assert (check.name, check.level) == (FILES, CheckLevel.REFUSE)
    assert copied[1].name in check.message or Path(copied[1].path).name in check.message


def test_missing_file_refuses(copied: list[PlanEntry]) -> None:
    Path(copied[2].path).unlink()
    check = check_files(copied)
    assert (check.name, check.level) == (FILES, CheckLevel.REFUSE)
    assert copied[2].name in check.message or Path(copied[2].path).name in check.message
