"""app.locations: what "Show in Explorer" shows for an entry or a firmware."""

from pathlib import Path

from embr.app.locations import Location, entry_location, firmware_location
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, BuildRef, Entry, Firmware, Source


def _entry(file: str) -> Entry:
    return Entry(
        id="e",
        name="app.bin",
        file=file,
        sha256="",
        size=0,
        kind=EntryKind.APP,
        address=0,
        auto_address=0,
        address_source=AddressSource.AUTO,
    )


def _firmware(source: Source = Source.MANUAL, **fields: object) -> Firmware:
    return Firmware(id="f", name="fw", project_id=None, device="", source=source, **fields)  # type: ignore[arg-type]


def test_an_entry_selects_its_file(tmp_path: Path) -> None:
    file = tmp_path / "app 固件.bin"
    file.write_bytes(b"x")
    assert entry_location(_entry(f"path:{file}")) == Location(file, selects_file=True)


def test_a_missing_file_opens_its_folder(tmp_path: Path) -> None:
    file = tmp_path / "gone.bin"
    assert entry_location(_entry(f"path:{file}")) == Location(tmp_path, selects_file=False)


def test_a_missing_folder_has_no_location(tmp_path: Path) -> None:
    assert entry_location(_entry(f"path:{tmp_path / 'gone' / 'app.bin'}")) is None


def test_archived_and_blank_entries_have_no_location() -> None:
    assert entry_location(_entry("archive:abc")) is None
    assert entry_location(_entry("blank:")) is None


def test_an_idf_firmware_opens_its_build_directory(tmp_path: Path) -> None:
    firmware = _firmware(Source.IDF_BUILD, build_ref=BuildRef(str(tmp_path)))
    assert firmware_location(firmware) == Location(tmp_path, selects_file=False)


def test_a_pio_firmware_opens_its_env_build_directory(tmp_path: Path) -> None:
    build = tmp_path / ".pio" / "build" / "esp32dev"
    firmware = _firmware(Source.PIO_BUILD, build_ref=BuildRef(str(tmp_path), "esp32dev"))
    assert firmware_location(firmware) is None  # not built

    build.mkdir(parents=True)
    assert firmware_location(firmware) == Location(build, selects_file=False)


def test_a_fixed_firmware_opens_its_folder(tmp_path: Path) -> None:
    assert firmware_location(_firmware(folder=str(tmp_path))) == Location(
        tmp_path, selects_file=False
    )


def test_a_firmware_without_a_folder_has_no_location(tmp_path: Path) -> None:
    assert firmware_location(_firmware()) is None
    assert firmware_location(_firmware(Source.ARCHIVED)) is None
    assert firmware_location(_firmware(folder=str(tmp_path / "gone"))) is None
