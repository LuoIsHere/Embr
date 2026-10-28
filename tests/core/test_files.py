"""core/firmware/files: file consistency of path, archive and blank entries."""

import hashlib
import shutil
from pathlib import Path

import pytest
from embr_testkit import IDF

from embr.core.firmware.entries import entry_from_file
from embr.core.firmware.files import (
    FileState,
    confirm_entry,
    entry_state,
    firmware_states,
    read_entry,
    relocate,
)
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, Entry, Firmware, Source
from embr.core.storage.ids import new_id

APP = IDF / "esp32s3_hello/build/hello_esp32s3.bin"
OTA_APP = IDF / "esp32s3_ota_custom/build/ota_custom.bin"


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    folder = tmp_path / "archive"
    folder.mkdir()
    return folder


def _copy(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copy(src, folder / src.name))


def _flip_last_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))


def _stored_entry(file: str, data: bytes, name: str = "app.bin") -> Entry:
    return Entry(
        id=new_id(),
        name=name,
        file=file,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        kind=EntryKind.APP,
        address=0x10000,
        auto_address=0x10000,
        address_source=AddressSource.AUTO,
    )


def test_path_entry_ok_changed_missing(tmp_path: Path, archive: Path) -> None:
    path = _copy(APP, tmp_path / "build")
    entry = entry_from_file(path)
    assert entry_state(entry, archive) == FileState.OK
    _flip_last_byte(path)  # same name and size, different content
    assert entry_state(entry, archive) == FileState.CHANGED
    path.unlink()
    assert entry_state(entry, archive) == FileState.MISSING

    # the recorded name must match the file name as well
    renamed = entry_from_file(_copy(APP, tmp_path / "other"))
    renamed.name = "other.bin"
    assert entry_state(renamed, archive) != FileState.OK


def test_archive_entries_checked_by_sha256(archive: Path) -> None:
    data = APP.read_bytes()
    entry = _stored_entry(f"archive:{hashlib.sha256(data).hexdigest()}", data, APP.name)
    assert entry_state(entry, archive) == FileState.MISSING
    stored = archive / f"{entry.sha256}.bin"
    stored.write_bytes(data)
    assert entry_state(entry, archive) == FileState.OK
    assert read_entry(entry, archive) == data
    _flip_last_byte(stored)
    assert entry_state(entry, archive) != FileState.OK


def test_blank_entry_and_firmware_states(tmp_path: Path, archive: Path) -> None:
    blank = _stored_entry("blank:", b"\xff" * 0x2000, "otadata")
    assert entry_state(blank, archive) == FileState.OK
    assert read_entry(blank, archive) == b"\xff" * 0x2000

    present = entry_from_file(_copy(APP, tmp_path / "a"))
    gone_path = _copy(OTA_APP, tmp_path / "b")
    gone = entry_from_file(gone_path)
    gone.enabled = False
    gone_path.unlink()
    fw = Firmware(
        id=new_id(),
        name="F",
        project_id=None,
        device="",
        source=Source.MANUAL,
        entries=[blank, present, gone],
    )
    assert firmware_states(fw, archive) == {
        blank.id: FileState.OK,
        present.id: FileState.OK,
        gone.id: FileState.MISSING,
    }
    with pytest.raises(FileNotFoundError):
        read_entry(gone, archive)


def test_confirm_entry_accepts_new_content(tmp_path: Path, archive: Path) -> None:
    path = _copy(APP, tmp_path / "build")
    entry = entry_from_file(path)
    path.write_bytes(APP.read_bytes() + b"\x00" * 16)
    assert entry_state(entry, archive) == FileState.CHANGED
    confirm_entry(entry, archive)
    assert entry_state(entry, archive) == FileState.OK
    assert entry.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert entry.size == len(APP.read_bytes()) + 16


def test_relocate_follows_file_and_keeps_settings(tmp_path: Path, archive: Path) -> None:
    old = _copy(APP, tmp_path / "old")
    entry = entry_from_file(old)
    entry.address = 0x20000
    entry.address_source = AddressSource.MANUAL
    entry.enabled = False
    entry.note = "keep"
    old.unlink()
    assert entry_state(entry, archive) == FileState.MISSING

    new = _copy(OTA_APP, tmp_path / "new")
    relocate(entry, new)
    data = new.read_bytes()
    assert (entry.name, entry.file) == (new.name, f"path:{new}")
    assert (entry.sha256, entry.size) == (hashlib.sha256(data).hexdigest(), len(data))
    assert (entry.address, entry.address_source) == (0x20000, AddressSource.MANUAL)
    assert (entry.enabled, entry.note) == (False, "keep")
    assert entry_state(entry, archive) == FileState.OK
