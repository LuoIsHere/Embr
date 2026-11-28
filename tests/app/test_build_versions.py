"""app/versions: backing up a tracked build as a version, deleting versions and reclaiming the
archive files only they used. git is never run: vcs.read_state is replaced where it matters."""

import datetime
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from app_testkit import (
    Clock,
    FakePio,
    build_files,
    copy_files,
    edit_json,
    fixed_firmware,
    flip_last_byte,
    idf_project,
    move_app,
    pio_build,
    pio_configs,
    pio_project,
    set_target,
    write_newer,
)

from embr.app import versions as app_versions
from embr.app.archiving import Archiver
from embr.app.checklist import CheckState, preflight
from embr.app.chips import firmware_chips
from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.library import Library
from embr.app.tracking import SETTLE_SECONDS, BuildChangedError, Tracker
from embr.app.versions import BuildVersions, Prepared, VersionBusyError
from embr.core.archive import ArchiveFile
from embr.core.firmware.files import archive_ref
from embr.core.firmware.image_header import read_image_header
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Source
from embr.core.firmware.tracking import BLANK_NAME, ResolveState, digest
from embr.core.vcs import GitState
from embr.core.versions import Origin, Version, minute

GIT = GitState(commit="c" * 40, describe="v1.0-2-gccccccc", dirty=False)


@dataclass
class Rig:
    tracker: Tracker
    clock: Clock
    library: Library
    versions: BuildVersions
    firmware_id: str
    build: Path

    @property
    def app(self) -> Path:
        return self.build / "hello_esp32s3.bin"


@pytest.fixture
def no_git(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """vcs.read_state answers None and records the folders it was asked about."""
    asked: list[Path] = []

    def read_state(folder: Path, **kwargs: Any) -> GitState | None:
        asked.append(folder)
        return None

    monkeypatch.setattr(app_versions.vcs, "read_state", read_state)
    return asked


def _rig(context: AppContext, tmp_path: Path, name: str = "hello") -> Rig:
    clock = Clock()
    tracker = Tracker(context, clock=clock)
    versions = BuildVersions(context, tracker, clock=clock)
    library = Library(context, tracker, versions)
    build = idf_project("esp32s3_hello", tmp_path / name)
    firmware = library.import_idf(build)
    return Rig(tracker, clock, library, versions, firmware.id, build)


def _backup(rig: Rig, note: str = "") -> Version:
    rig.clock.advance(60)  # a new minute: default names stay apart
    return rig.versions.backup(rig.firmware_id, origin=Origin.MANUAL, note=note)


def _names_count(context: AppContext) -> int:
    return context.database.execute("SELECT COUNT(*) FROM archive_names").fetchone()[0]


# --- what can be backed up -----------------------------------------------------------------


def test_backup_keeps_the_resolved_build(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    status = rig.tracker.check(rig.firmware_id)
    version = _backup(rig, note="first")

    assert (version.seq, version.origin, version.note) == (1, Origin.MANUAL, "first")
    assert version.digest == digest(status.firmware, erase_all=False)
    assert version.chip == status.firmware.chip == "esp32s3"
    assert version.params.idf_version == status.idf_version
    assert version.params.use_app_version
    assert version.build_path == str(rig.build.resolve())
    resolved = status.firmware.entries
    assert [(e.name, e.kind, e.address, e.sha256, e.size, e.enabled) for e in version.entries] == [
        (e.name, e.kind.value, e.address, e.sha256, e.size, e.enabled) for e in resolved
    ]
    for entry in version.entries:  # stored and indexed, without a name record
        assert context.archive.has(entry.sha256)
    assert {f.sha256 for f in context.archive.files()} == {e.sha256 for e in version.entries}
    assert _names_count(context) == 0

    header = read_image_header(rig.app.read_bytes())
    assert header is not None and header.app_version is not None
    assert version.app_version == header.app_version == version.name  # no git: app version


def test_unchanged_content_returns_the_latest_version(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    first = _backup(rig)
    assert _backup(rig) == first

    rig.library.set_param(rig.firmware_id, "erase_all", True)  # an option, not content
    rig.tracker.invalidate(rig.firmware_id)
    assert rig.tracker.check(rig.firmware_id).firmware.erase_all
    assert _backup(rig) == first

    flip_last_byte(rig.app)
    assert rig.tracker.check(rig.firmware_id).state is ResolveState.UPDATED
    second = _backup(rig)
    assert second.seq == 2 and second.id != first.id


def test_changed_flash_settings_make_a_new_version(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    first = _backup(rig)
    edit_json(
        rig.build / "flasher_args.json",
        lambda args: args["flash_settings"].update(flash_mode="qio"),
    )
    if rig.tracker.check(rig.firmware_id).state is ResolveState.NEEDS_CONFIRMATION:
        rig.tracker.confirm(rig.firmware_id)
    second = _backup(rig)
    assert second.seq == 2
    assert [e.sha256 for e in second.entries] == [e.sha256 for e in first.entries]
    assert (first.params.flash_mode, second.params.flash_mode) == ("dio", "qio")


def _building(rig: Rig) -> None:
    recent = (rig.clock.now - datetime.timedelta(seconds=SETTLE_SECONDS / 4)).timestamp()
    os.utime(rig.app, (recent, recent))


def _incomplete(rig: Rig) -> None:
    write_newer(rig.build / "flasher_args.json", b"{")


def _switched(rig: Rig) -> None:
    set_target(rig.build, "esp32")


def _unconfirmed(rig: Rig) -> None:
    move_app(rig.build, 0x20000)


@pytest.mark.parametrize(
    ("change", "state"),
    [
        (_building, ResolveState.BUILDING),
        (_incomplete, ResolveState.INCOMPLETE),
        (_switched, ResolveState.TARGET_SWITCHED),
        (_unconfirmed, ResolveState.NEEDS_CONFIRMATION),
    ],
    ids=["building", "incomplete", "target-switched", "needs-confirmation"],
)
def test_builds_that_cannot_be_flashed_are_refused(
    context: AppContext, tmp_path: Path, no_git: list[Path], change: Any, state: ResolveState
) -> None:
    rig = _rig(context, tmp_path)
    change(rig)
    assert rig.tracker.check(rig.firmware_id).state is state
    with pytest.raises(PlanError):
        rig.versions.prepare(rig.firmware_id)
    assert context.versions.for_firmware(rig.firmware_id) == []
    assert not rig.versions.busy(rig.firmware_id)


def test_fixed_firmware_has_no_versions(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    files = copy_files(build_files("esp32s3_hello"), tmp_path / "files")
    fixed = fixed_firmware(rig.library, files)
    with pytest.raises(PlanError):
        rig.versions.prepare(fixed.id)
    with pytest.raises(KeyError):
        rig.versions.prepare("01JC0000000000000000000000")


# --- the background copy -------------------------------------------------------------------


def test_a_file_changed_during_the_copy_writes_nothing(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    prepared = rig.versions.prepare(rig.firmware_id)
    assert isinstance(prepared, Prepared)
    flip_last_byte(rig.app)
    with pytest.raises(BuildChangedError) as raised:
        rig.versions.copy(prepared)
    assert raised.value.building
    rig.versions.abandon(prepared)
    assert not rig.versions.busy(rig.firmware_id)
    assert context.versions.for_firmware(rig.firmware_id) == []
    assert context.archive.files() == []
    stored = {p.stem for p in context.paths.archive.glob("*.bin")}
    assert prepared.firmware.entries[-1].sha256 not in stored  # the changed app is not kept


def test_a_failing_backup_is_no_longer_in_progress(
    context: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(context, tmp_path)

    def broken(folder: Path, **kwargs: Any) -> GitState | None:
        raise RuntimeError("git broke")

    monkeypatch.setattr(app_versions.vcs, "read_state", broken)
    with pytest.raises(RuntimeError):
        _backup(rig)
    assert not rig.versions.busy(rig.firmware_id)
    assert context.versions.for_firmware(rig.firmware_id) == []


def test_one_backup_per_firmware_at_a_time(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    prepared = rig.versions.prepare(rig.firmware_id)
    assert isinstance(prepared, Prepared)
    with pytest.raises(VersionBusyError):
        rig.versions.prepare(rig.firmware_id)
    version = rig.versions.commit(prepared, rig.versions.copy(prepared), origin=Origin.AUTO)
    assert version.origin is Origin.AUTO
    assert not rig.versions.busy(rig.firmware_id)
    assert rig.versions.prepare(rig.firmware_id) == version  # same content again


def test_deleting_the_firmware_during_a_backup_writes_nothing(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    prepared = rig.versions.prepare(rig.firmware_id)
    assert isinstance(prepared, Prepared)
    copied = rig.versions.copy(prepared)
    rig.library.delete_firmware(rig.firmware_id)
    with pytest.raises(KeyError):
        rig.versions.commit(prepared, copied, origin=Origin.MANUAL)
    assert context.versions.for_firmware(rig.firmware_id) == []


# --- git and the app version ---------------------------------------------------------------


def test_idf_git_state_comes_from_the_project_folder(
    context: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(context, tmp_path)
    project = tmp_path / "hello"
    edit_json(
        rig.build / "project_description.json",
        lambda d: d.update(project_path=str(project)),
    )
    asked: list[Path] = []

    def read_state(folder: Path, **kwargs: Any) -> GitState | None:
        asked.append(folder)
        return GIT

    monkeypatch.setattr(app_versions.vcs, "read_state", read_state)
    version = _backup(rig)
    assert asked == [project]
    assert (version.git_commit, version.git_describe, version.git_dirty) == (
        GIT.commit,
        GIT.describe,
        GIT.dirty,
    )
    assert version.name == GIT.describe


def test_idf_without_a_project_path_skips_git(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    edit_json(rig.build / "project_description.json", lambda d: d.pop("project_path"))
    version = _backup(rig)
    assert no_git == []
    assert version.git_commit is None


def _pio(
    context: AppContext, tmp_path: Path, sample: str, *, packages: bool = True
) -> tuple[Rig, Path]:
    root, core = tmp_path / "project", tmp_path / "core"
    pio_project(sample, root, core)
    if not packages:
        shutil.rmtree(core / "packages")
    configs = pio_configs(core, FakePio())
    configs.load(root)
    clock = Clock()
    tracker = Tracker(context, clock=clock, pio=configs)
    versions = BuildVersions(context, tracker, clock=clock)
    library = Library(context, tracker, versions)
    [firmware] = library.import_pio(root, ["esp32-s3-devkitc-1"])
    build = pio_build(root, "esp32-s3-devkitc-1")
    return Rig(tracker, clock, library, versions, firmware.id, build), root


def test_pio_arduino_keeps_but_does_not_use_the_app_version(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig, root = _pio(context, tmp_path, "arduino_esp32s3_otadata")
    version = _backup(rig)
    assert no_git == [root]
    assert not version.params.use_app_version
    assert version.app_version is not None  # the Arduino core's text, kept as read
    assert version.shown_app_version is None
    assert version.name == minute(version.created_at)


def test_pio_espidf_uses_the_app_version(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig, root = _pio(context, tmp_path, "espidf_esp32s3")
    version = _backup(rig)
    assert no_git == [root]
    assert version.params.use_app_version
    assert version.app_version is not None and version.name == version.app_version


def test_a_blank_entry_is_kept_as_its_bytes(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig, _ = _pio(context, tmp_path, "arduino_esp32s3_otadata", packages=False)
    status = rig.tracker.check(rig.firmware_id)
    blank = next(e for e in status.firmware.entries if e.name == BLANK_NAME)
    version = _backup(rig)
    kept = next(e for e in version.entries if e.name == BLANK_NAME)
    assert (kept.kind, kept.address, kept.sha256, kept.size) == (
        EntryKind.OTADATA.value,
        blank.address,
        blank.sha256,
        blank.size,
    )
    assert context.archive.path(kept.sha256).read_bytes() == b"\xff" * blank.size
    assert version.digest == digest(status.firmware, erase_all=False)


# --- deleting ------------------------------------------------------------------------------


def _two_versions(rig: Rig) -> tuple[Version, Version]:
    first = _backup(rig)
    flip_last_byte(rig.app)
    second = _backup(rig)
    return first, second


def _app_sha(version: Version) -> str:
    return next(e.sha256 for e in version.entries if e.kind == EntryKind.APP.value)


def test_deleting_a_version_reclaims_only_its_own_files(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    first, second = _two_versions(rig)
    shared = {e.sha256 for e in first.entries} & {e.sha256 for e in second.entries}
    assert shared and _app_sha(first) not in shared
    unrelated = context.archive.add(b"kept for Clean archive", original_name="other.bin")

    assert rig.versions.exclusive_size(rig.firmware_id) == sum(
        {e.sha256: e.size for v in (first, second) for e in v.entries}.values()
    )
    assert rig.versions.freed_by([first.id]) == next(
        e.size for e in first.entries if e.kind == EntryKind.APP.value
    )
    assert rig.versions.freed_by([first.id, second.id, "unknown"]) == sum(
        {e.sha256: e.size for v in (first, second) for e in v.entries}.values()
    )
    rig.versions.delete([first.id])
    assert context.versions.get(first.id) is None
    assert not context.archive.has(_app_sha(first))
    for sha in shared | {_app_sha(second), unrelated.sha256}:
        assert context.archive.has(sha)
    assert {f.sha256 for f in context.archive.files()} == {
        *(e.sha256 for e in second.entries),
        unrelated.sha256,
    }


def test_files_a_firmware_entry_uses_are_not_reclaimed(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    version = _backup(rig)
    app = _app_sha(version)
    holder = context.firmware.create("holder", source=Source.ARCHIVED)
    entry = next(e for e in rig.tracker.check(rig.firmware_id).firmware.entries)
    holder.entries = [entry]
    entry.file = archive_ref(app)
    context.firmware.save(holder)

    assert rig.versions.exclusive_size(rig.firmware_id) == sum(
        e.size for e in version.entries if e.sha256 != app
    )
    rig.versions.delete([version.id])
    assert context.archive.has(app)
    assert {f.sha256 for f in context.archive.files()} == {app}


def test_files_another_firmwares_version_uses_are_not_reclaimed(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    one = _rig(context, tmp_path, "one")
    other = _rig(context, tmp_path, "other")  # the same sample: the same files
    version = _backup(one)
    kept = _backup(other)
    assert one.versions.exclusive_size(one.firmware_id) == 0
    one.versions.delete([version.id])
    for entry in kept.entries:
        assert context.archive.has(entry.sha256)


def test_clean_archive_keeps_version_files(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    version = _backup(rig)
    unrelated = context.archive.add(b"unused", original_name="other.bin")
    archiver = Archiver(context, rig.tracker)

    assert [f.sha256 for f in archiver.unreferenced()] == [unrelated.sha256]
    usage = {u.file.sha256: u.firmware_ids for u in archiver.usage()}
    assert usage[unrelated.sha256] == ()
    for entry in version.entries:
        assert usage[entry.sha256] == (rig.firmware_id,)
    cleaned = archiver.clean([f.sha256 for f in context.archive.files()])
    assert cleaned.deleted == 1
    assert all(context.archive.has(e.sha256) for e in version.entries)


def test_deleting_the_firmware_drops_its_versions(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    first, second = _two_versions(rig)
    rig.library.delete_firmware(rig.firmware_id)
    assert context.versions.for_firmware(rig.firmware_id) == []
    assert context.database.execute("SELECT COUNT(*) FROM version_counters").fetchone()[0] == 0
    assert context.archive.files() == []
    for version in (first, second):
        assert all(not context.archive.has(e.sha256) for e in version.entries)


def test_deleting_a_project_with_its_firmware_drops_their_versions(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    project = rig.library.create_project("p")
    rig.library.move_firmware(rig.firmware_id, project.id)
    version = _backup(rig)

    rig.library.delete_project(project.id)  # the firmware stays, standalone
    assert context.versions.get(version.id) == version
    project = rig.library.create_project("q")
    rig.library.move_firmware(rig.firmware_id, project.id)
    rig.library.delete_project(project.id, delete_firmware=True)
    assert context.versions.get(version.id) is None
    assert context.archive.files() == list[ArchiveFile]()


# --- using a version -----------------------------------------------------------------------


def test_a_version_as_firmware_is_its_build(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    project = rig.library.create_project("p")
    rig.library.move_firmware(rig.firmware_id, project.id, "board")
    version = _backup(rig)
    flip_last_byte(rig.app)  # the build moves on; the version does not
    rig.library.set_param(rig.firmware_id, "erase_all", True)

    shown = rig.versions.as_firmware(version)
    assert (shown.id, shown.name, shown.project_id, shown.device) == (
        rig.firmware_id,
        context.firmware.get(rig.firmware_id).name,
        project.id,
        "board",
    )
    assert (shown.source, shown.read_only, shown.erase_all) == (Source.ARCHIVED, True, True)
    assert digest(shown, erase_all=False) == version.digest
    assert [e.file for e in shown.entries] == [archive_ref(e.sha256) for e in version.entries]
    assert firmware_chips(context, shown) == ("esp32s3",)
    table = next(e for e in shown.entries if e.kind == EntryKind.PARTITION_TABLE)
    assert table.chip == "esp32s3"  # names no chip itself: the version's chip
    items = preflight(context, shown, None, None)
    assert not [i for i in items if i.state == CheckState.REFUSE]
    assert len(context.firmware.all()) == 1  # nothing saved


def test_a_version_saved_as_firmware(
    context: AppContext, tmp_path: Path, no_git: list[Path]
) -> None:
    rig = _rig(context, tmp_path)
    project = rig.library.create_project("p")
    rig.library.move_firmware(rig.firmware_id, project.id, "board")
    version = _backup(rig)
    names = _names_count(context)

    saved = rig.versions.save_as_firmware(version.id, " demo ")
    again = rig.versions.save_as_firmware(version.id, "demo")
    assert (saved.name, again.name) == ("demo", "demo (2)")
    assert (saved.source, saved.project_id, saved.device) == (Source.ARCHIVED, project.id, "board")
    assert saved.entries == context.firmware.get(saved.id).entries
    assert [e.file for e in saved.entries] == [archive_ref(e.sha256) for e in version.entries]
    shown = rig.versions.as_firmware(version)
    assert (saved.flash, saved.before, saved.after) == (shown.flash, shown.before, shown.after)
    assert saved.entries[0].chip is None  # the bootloader names its chip itself
    assert firmware_chips(context, saved) == ("esp32s3",)
    assert f"version #{version.seq}" in saved.archived_from
    assert version.build_path in saved.archived_from
    assert _names_count(context) == names
    today = rig.clock().date().isoformat()
    source_name = context.firmware.get(rig.firmware_id).name
    assert rig.versions.save_as_firmware(version.id).name == f"{source_name} (archived {today})"

    rig.versions.delete([version.id])  # the saved firmware still uses the files
    assert all(context.archive.has(e.sha256) for e in version.entries)
    with pytest.raises(KeyError):
        rig.versions.save_as_firmware(version.id)
