"""core/firmware model and store: TOML files, names, moves and editable parameters."""

import datetime
import hashlib
import tomllib
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import tomli_w
from embr_testkit import FIXTURES, IDF

from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import (
    FIRMWARE_SCHEMA_VERSION,
    PARAMS,
    AddressSource,
    Entry,
    Firmware,
    ReadOnlyFieldError,
    Source,
    editable_params,
    set_param,
)
from embr.core.firmware.store import (
    FirmwareStore,
    ReadOnlyError,
    default_name_archived,
    default_name_from_files,
)
from embr.core.storage.ids import new_id

APP = IDF / "esp32s3_ota_custom/build/ota_custom.bin"
ASSETS = FIXTURES / "synthetic/attachments/assets_fits.bin"


@pytest.fixture
def store(tmp_path: Path) -> FirmwareStore:
    (tmp_path / "firmwares").mkdir()
    return FirmwareStore(tmp_path / "firmwares", tmp_path / "backup")


def _reopen(store: FirmwareStore) -> FirmwareStore:
    return FirmwareStore(store.directory, store.backup_root)


def _comparable(fw: Firmware, *ignore: str) -> dict[str, object]:
    data = asdict(replace(fw, updated_at=""))
    return {k: v for k, v in data.items() if k not in ignore}


def _path_entry(path: Path, kind: EntryKind, address: int) -> Entry:
    data = path.read_bytes()
    return Entry(
        id=new_id(),
        name=path.name,
        file=f"path:{path}",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        kind=kind,
        address=address,
        auto_address=address,
        address_source=AddressSource.AUTO,
    )


def test_firmware_round_trip_with_entries(store: FirmwareStore) -> None:
    fw = store.create("Custom OTA", project_id=new_id(), device="Board A")
    fw.folder = str(APP.parent)
    fw.notes = "notes\nwith two lines"
    fw.tags = ["release"]
    fw.flash.mode, fw.flash.freq, fw.flash.size = "dio", "80m", "4MB"
    fw.erase_all = True
    app = _path_entry(APP, EntryKind.APP, 0x20000)
    app.note = "main app"
    attachment = replace(
        _path_entry(ASSETS, EntryKind.ATTACHMENT, 0x1A000),
        auto_address=None,
        address_source=AddressSource.PARTITION,
        partition="assets",
    )
    otadata = Entry(
        id=new_id(),
        name="otadata",
        file="blank:",
        sha256=hashlib.sha256(b"\xff" * 0x2000).hexdigest(),
        size=0x2000,
        kind=EntryKind.OTADATA,
        address=0x17000,
        auto_address=0x17000,
        address_source=AddressSource.PARTITION,
        partition="otadata",
        auto_added=True,
    )
    attachment.enabled = False
    attachment.chip = "esp32s3"  # MCU chosen by hand (design doc §12 item 13)
    fw.entries = [app, attachment, otadata]
    store.save(fw)

    reloaded = _reopen(store).get(fw.id)
    assert _comparable(reloaded) == _comparable(fw)
    assert not reloaded.read_only


def test_name_scope_and_suffixes(store: FirmwareStore) -> None:
    p, q = new_id(), new_id()
    assert store.create("App", project_id=p).name == "App"
    assert store.create("App", project_id=p).name == "App (2)"
    in_q = store.create("App", project_id=q)
    assert in_q.name == "App"
    assert store.create("App").name == "App"
    assert store.create("App").name == "App (2)"
    other = store.create("Other", project_id=p)
    assert store.rename(other.id, "App").name == "App (3)"
    # moving into a scope where the name is taken suffixes it too
    assert store.move(in_q.id, None).name == "App (3)"


def test_move_changes_only_project_and_device(store: FirmwareStore) -> None:
    p, q = new_id(), new_id()
    fw = store.create("App", project_id=p, device="A", erase_all=True)
    fw.notes = "kept"
    fw.entries = [_path_entry(APP, EntryKind.APP, 0x10000)]
    store.save(fw)
    file = store.directory / f"{fw.id}.toml"
    assert file.exists()
    unchanged = _comparable(fw, "project_id", "device")

    to_group = store.move(fw.id, q, "B")
    assert (to_group.project_id, to_group.device) == (q, "B")
    to_project = store.move(fw.id, p)
    assert (to_project.project_id, to_project.device) == (p, "")
    standalone = store.move(fw.id, None, "ignored")
    assert (standalone.project_id, standalone.device) == (None, "")

    reloaded = _reopen(store).get(fw.id)
    assert _comparable(reloaded, "project_id", "device") == unchanged
    assert file.exists()


def test_default_names() -> None:
    assert default_name_from_files(["hello_esp32s3.bin", "bootloader.bin"]) == "hello_esp32s3"
    archived = default_name_archived("Sensor", datetime.date(2026, 10, 28))
    assert archived == "Sensor (archived 2026-10-28)"


def test_firmware_in_one_project_stay_independent(store: FirmwareStore) -> None:
    p = new_id()
    a = store.create("A", project_id=p)
    b = store.create("B", project_id=p)
    set_param(a, "flash_size", "16MB")
    a.entries = [_path_entry(APP, EntryKind.APP, 0x10000)]
    set_param(b, "flash_size", "4MB")
    b.entries = [_path_entry(APP, EntryKind.APP, 0x20000)]
    store.save(a)
    store.save(b)

    reopened = _reopen(store)
    ra, rb = reopened.get(a.id), reopened.get(b.id)
    assert (ra.flash.size, [e.address for e in ra.entries]) == ("16MB", [0x10000])
    assert (rb.flash.size, [e.address for e in rb.entries]) == ("4MB", [0x20000])


# --- editable parameters -----------------------------------------------------------------


def test_editable_params() -> None:
    expected = {
        # the chip is derived for every firmware (design doc §12 item 13)
        Source.MANUAL: set(PARAMS) - {"chip"},
        Source.ARCHIVED: set(PARAMS) - {"chip"},
        Source.IDF_BUILD: {"erase_all", "tags", "notes"},
        Source.PIO_BUILD: {"before", "after", "erase_all", "tags", "notes"},
    }
    for source, params in expected.items():
        fw = Firmware(id=new_id(), name="F", project_id=None, device="", source=source)
        assert editable_params(fw) == params, source


def test_set_param_on_fixed_firmware_round_trips(store: FirmwareStore) -> None:
    fw = store.create("Fixed")
    values: dict[str, object] = {
        "flash_mode": "qio",
        "flash_freq": "40m",
        "flash_size": "8MB",
        "before": "no-reset",
        "after": "no-reset",
        "erase_all": True,
        "tags": ["a", "b"],
        "notes": "n",
    }
    for name, value in values.items():
        set_param(fw, name, value)
    store.save(fw)
    r = _reopen(store).get(fw.id)
    assert (r.flash.mode, r.flash.freq, r.flash.size) == ("qio", "40m", "8MB")
    assert (r.before, r.after, r.erase_all, r.tags, r.notes) == (
        "no-reset",
        "no-reset",
        True,
        ["a", "b"],
        "n",
    )


def test_set_param_rejects_invalid_values() -> None:
    fw = Firmware(id=new_id(), name="F", project_id=None, device="", source=Source.MANUAL)
    before = asdict(fw)
    invalid: list[tuple[str, object]] = [
        ("flash_size", "3MB"),
        ("flash_mode", "fast"),
        ("before", "bogus-reset"),
        ("erase_all", "yes"),
    ]
    for name, value in invalid:
        with pytest.raises(ValueError):
            set_param(fw, name, value)
        assert asdict(fw) == before, name


def test_set_param_refuses_read_only_params() -> None:
    idf = Firmware(id=new_id(), name="I", project_id=None, device="", source=Source.IDF_BUILD)
    pio = Firmware(id=new_id(), name="P", project_id=None, device="", source=Source.PIO_BUILD)
    manual = Firmware(id=new_id(), name="M", project_id=None, device="", source=Source.MANUAL)
    for fw, name, value in [
        (manual, "chip", "esp32"),
        (idf, "chip", "esp32"),
        (idf, "flash_size", "4MB"),
        (idf, "before", "no-reset"),
        (pio, "chip", "esp32"),
        (pio, "flash_mode", "dio"),
    ]:
        with pytest.raises(ReadOnlyFieldError):
            set_param(fw, name, value)
    set_param(pio, "before", "no-reset")
    set_param(idf, "erase_all", True)
    assert (pio.before, idf.erase_all, idf.chip, manual.chip) == ("no-reset", True, None, None)


def test_file_without_the_new_fields_loads_with_defaults(store: FirmwareStore) -> None:
    # no migration (design doc §12 item 13): files without folder and entry chips load with
    # the defaults
    fw = store.create("Old")
    fw.entries = [_path_entry(APP, EntryKind.APP, 0x20000)]
    store.save(fw)
    doc = tomllib.loads((store.directory / f"{fw.id}.toml").read_text(encoding="utf-8"))
    assert "folder" not in doc and "chip" not in doc["entries"][0]

    loaded = _reopen(store).get(fw.id)
    assert (loaded.folder, loaded.entries[0].chip) == ("", None)
    assert not loaded.read_only


def test_newer_firmware_is_read_only(store: FirmwareStore) -> None:
    fw = store.create("Future")
    file = store.directory / f"{fw.id}.toml"
    doc = tomllib.loads(file.read_text(encoding="utf-8"))
    doc["schema_version"] = FIRMWARE_SCHEMA_VERSION + 1
    file.write_text(tomli_w.dumps(doc), encoding="utf-8")
    original = file.read_bytes()

    loaded = _reopen(store).get(fw.id)
    assert loaded.read_only
    with pytest.raises(ReadOnlyError):
        _reopen(store).save(loaded)
    assert file.read_bytes() == original
