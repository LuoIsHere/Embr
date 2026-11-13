"""core/project: project files, names, deletion, defaults and device groups."""

import tomllib
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import tomli_w

from embr.core.firmware import store as store_module
from embr.core.firmware.model import Source
from embr.core.firmware.store import FirmwareStore, ReadOnlyError
from embr.core.project import (
    PROJECT_SCHEMA_VERSION,
    DuplicateGroupError,
    Project,
    ProjectDefaults,
    ProjectStore,
)

Stores = tuple[ProjectStore, FirmwareStore]


@pytest.fixture
def stores(tmp_path: Path) -> Stores:
    for name in ("projects", "firmwares", "backup"):
        (tmp_path / name).mkdir()
    return (
        ProjectStore(tmp_path / "projects", tmp_path / "backup"),
        FirmwareStore(tmp_path / "firmwares", tmp_path / "backup"),
    )


def _comparable(project: Project) -> dict[str, object]:
    return asdict(replace(project, updated_at=""))


def test_project_round_trip(stores: Stores) -> None:
    projects, _ = stores
    project = projects.create(
        "Sensor", notes="line 1\nline 2", defaults=ProjectDefaults("no-reset", "no-reset", True)
    )
    project.tags = ["lab", "v2"]
    project.devices = ["Board A", "Board B"]
    projects.save(project)

    reloaded = ProjectStore(projects.directory, projects.backup_root).get(project.id)
    assert _comparable(reloaded) == _comparable(project)
    assert not reloaded.read_only


def test_project_names_are_globally_unique(stores: Stores) -> None:
    projects, _ = stores
    first = projects.create("Demo")
    second = projects.create("Demo")
    other = projects.create("Other")
    assert (first.name, second.name) == ("Demo", "Demo (2)")
    assert projects.rename(other.id, "Demo").name == "Demo (3)"
    assert [p.name for p in projects.all()] == ["Demo", "Demo (2)", "Demo (3)"]


def test_rename_keeps_firmware_project_id(stores: Stores) -> None:
    projects, firmware = stores
    project = projects.create("Old")
    fw = projects.create_firmware(firmware, project.id, "App")
    projects.rename(project.id, "New")
    assert projects.get(project.id).name == "New"
    assert firmware.get(fw.id).project_id == project.id


def test_delete_project_makes_firmware_standalone(stores: Stores) -> None:
    projects, firmware = stores
    project = projects.create("P")
    projects.add_group(project.id, "G")
    inside = projects.create_firmware(firmware, project.id, "App", device="G")
    inside.notes = "keep me"
    firmware.save(inside)
    standalone = firmware.create("App")

    projects.delete(project.id, firmware)

    with pytest.raises(KeyError):
        projects.get(project.id)
    moved = firmware.get(inside.id)
    assert (moved.project_id, moved.device) == (None, "")
    assert moved.notes == "keep me"
    assert moved.name == "App (2)"
    assert firmware.get(standalone.id).name == "App"
    assert len(firmware.all()) == 2


def test_delete_project_reads_each_firmware_file_once(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects, firmware = stores
    project = projects.create("P")
    for i in range(6):
        projects.create_firmware(firmware, project.id, f"App {i}")
    firmware.create("Other")
    reopened = FirmwareStore(firmware.directory, firmware.backup_root)
    parsed: list[Path] = []
    load = store_module.load_document

    def counted(path: Path, **kwargs: object) -> object:
        parsed.append(path)
        return load(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "load_document", counted)
    projects.delete(project.id, reopened)
    assert len(parsed) == len(set(parsed)) == 7
    assert sorted(f.name for f in reopened.in_scope(None)) == [
        "App 0",
        "App 1",
        "App 2",
        "App 3",
        "App 4",
        "App 5",
        "Other",
    ]


def test_delete_project_with_firmware(stores: Stores) -> None:
    projects, firmware = stores
    doomed = projects.create("Doomed")
    kept = projects.create("Kept")
    gone = projects.create_firmware(firmware, doomed.id, "App")
    survivor = projects.create_firmware(firmware, kept.id, "App")

    projects.delete(doomed.id, firmware, delete_firmware=True)

    with pytest.raises(KeyError):
        firmware.get(gone.id)
    assert firmware.get(survivor.id).project_id == kept.id


def test_defaults_apply_only_to_new_firmware(stores: Stores) -> None:
    projects, firmware = stores
    project = projects.create("P")
    before = projects.create_firmware(firmware, project.id, "Before")

    project.defaults = ProjectDefaults(before="no-reset", after="no-reset", erase_all=True)
    projects.save(project)
    after = projects.create_firmware(firmware, project.id, "After")

    old = firmware.get(before.id)
    assert (old.before, old.after, old.erase_all) == ("default-reset", "hard-reset", False)
    assert (after.before, after.after, after.erase_all) == ("no-reset", "no-reset", False)
    assert not firmware.get(after.id).erase_all


def test_tracking_firmware_defaults(stores: Stores) -> None:
    projects, firmware = stores
    defaults = ProjectDefaults(before="no-reset", after="no-reset", erase_all=True)
    project = projects.create("P", defaults=defaults)

    idf = projects.create_firmware(firmware, project.id, "IDF", source=Source.IDF_BUILD)
    assert not idf.erase_all
    # the reset strategy of IDF tracking firmware comes from the build, not the project
    assert idf.before != "no-reset"
    assert idf.after != "no-reset"

    pio = projects.create_firmware(firmware, project.id, "PIO", source=Source.PIO_BUILD)
    assert (pio.before, pio.after, pio.erase_all) == ("no-reset", "no-reset", False)


# --- device groups -----------------------------------------------------------------------


def test_empty_group_is_saved_and_duplicates_rejected(stores: Stores) -> None:
    projects, _ = stores
    project = projects.create("P")
    projects.add_group(project.id, "Board A")
    assert ProjectStore(projects.directory, projects.backup_root).get(project.id).devices == [
        "Board A"
    ]
    with pytest.raises(DuplicateGroupError):
        projects.add_group(project.id, "Board A")


def test_rename_group_syncs_firmware_in_project(stores: Stores) -> None:
    projects, firmware = stores
    project = projects.create("P")
    other = projects.create("Q")
    for p in (project, other):
        projects.add_group(p.id, "A")
    projects.add_group(project.id, "B")
    in_a = projects.create_firmware(firmware, project.id, "One", device="A")
    in_b = projects.create_firmware(firmware, project.id, "Two", device="B")
    elsewhere = projects.create_firmware(firmware, other.id, "Three", device="A")

    with pytest.raises(DuplicateGroupError):
        projects.rename_group(project.id, "A", "B", firmware)
    renamed = projects.rename_group(project.id, "A", "C", firmware)

    assert renamed.devices == ["C", "B"]
    assert firmware.get(in_a.id).device == "C"
    assert firmware.get(in_b.id).device == "B"
    assert firmware.get(elsewhere.id).device == "A"


def test_remove_group_keeps_firmware_ungrouped(stores: Stores) -> None:
    projects, firmware = stores
    project = projects.create("P")
    projects.add_group(project.id, "A")
    fw = projects.create_firmware(firmware, project.id, "One", device="A")

    assert projects.remove_group(project.id, "A", firmware).devices == []
    kept = firmware.get(fw.id)
    assert (kept.project_id, kept.device) == (project.id, "")
    assert len(firmware.in_scope(project.id)) == 1


def test_unknown_device_is_added_as_group(stores: Stores) -> None:
    projects, firmware = stores
    project = projects.create("P")
    created = projects.create_firmware(firmware, project.id, "One", device="Rev A")
    standalone = firmware.create("Loose")

    moved = projects.move_firmware(firmware, standalone.id, project.id, "Rev B")

    assert (moved.project_id, moved.device) == (project.id, "Rev B")
    assert created.device == "Rev A"
    assert projects.get(project.id).devices == ["Rev A", "Rev B"]


def test_reorder_groups_keeps_the_new_order(stores: Stores) -> None:
    projects, _ = stores
    project = projects.create("P")
    for name in ("A", "B", "C"):
        projects.add_group(project.id, name)
    projects.reorder_groups(project.id, ["C", "A", "B"])
    reloaded = ProjectStore(projects.directory, projects.backup_root).get(project.id)
    assert reloaded.devices == ["C", "A", "B"]


@pytest.mark.parametrize("names", [["A", "B"], ["A", "B", "C", "D"], ["A", "A", "B"], []])
def test_reorder_groups_needs_exactly_the_groups(stores: Stores, names: list[str]) -> None:
    projects, _ = stores
    project = projects.create("P")
    for name in ("A", "B", "C"):
        projects.add_group(project.id, name)
    with pytest.raises(ValueError, match="are not the groups"):
        projects.reorder_groups(project.id, names)
    assert projects.get(project.id).devices == ["A", "B", "C"]


def test_reorder_groups_of_a_newer_project_is_refused(stores: Stores) -> None:
    projects, _ = stores
    project = projects.create("P")
    for name in ("A", "B"):
        projects.add_group(project.id, name)
    file = projects.directory / f"{project.id}.toml"
    doc = tomllib.loads(file.read_text(encoding="utf-8"))
    doc["schema_version"] = PROJECT_SCHEMA_VERSION + 1
    file.write_text(tomli_w.dumps(doc), encoding="utf-8")
    before = file.read_bytes()
    with pytest.raises(ReadOnlyError):
        projects.reorder_groups(project.id, ["B", "A"])
    assert file.read_bytes() == before
