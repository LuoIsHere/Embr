"""app/library and app/tracking for PlatformIO firmware (design doc §5 stage three, §8.2,
§12.5): importing envs, the import preview, following rebuilds, a switched board and the
framework package path. pio is never run: PioConfigs gets a FakePio and runs its query
synchronously; tests call load() where the app would wait for the background query."""

import hashlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from app_testkit import (
    Clock,
    FakePio,
    flip_last_byte,
    idedata_images,
    make_package,
    pio_build,
    pio_configs,
    pio_project,
    write_ini,
    write_newer,
)

from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.library import FRAMEWORK_PKG_KEY, PIO_SECTION, AlreadyTrackedError, Library
from embr.app.pio import PioConfigs
from embr.app.tracking import Tracker
from embr.core.firmware.import_pio import BOOT_APP0_IN_PACKAGE, EnvDirError, EnvRef
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Source
from embr.core.firmware.tracking import ChangeKind, EntryMark, ResolveState, entry_key
from embr.core.project import ProjectDefaults

ESP32, S3 = "esp32dev", "esp32-s3-devkitc-1"
ENVS = [ESP32, S3]
CHIPS = {ESP32: "esp32", S3: "esp32s3"}
PACKAGE = Path("packages") / "framework-arduinoespressif32"


@dataclass
class Rig:
    library: Library
    tracker: Tracker
    configs: PioConfigs
    runner: FakePio
    root: Path
    core: Path

    def build(self, env: str) -> Path:
        return pio_build(self.root, env)

    def set_board(self, env_board: str, board: str) -> None:
        """Change `board = <env_board>` in platformio.ini (a user edit) and load the config."""
        text = (self.root / "platformio.ini").read_text()
        write_ini(self.root, text.replace(f"board = {env_board}", f"board = {board}"))
        self.configs.load(self.root)


def _rig(context: AppContext, tmp_path: Path, sample: str = "multi_env") -> Rig:
    root, core = tmp_path / "project", tmp_path / "core"
    pio_project(sample, root, core)
    runner = FakePio()
    configs = pio_configs(core, runner)
    configs.load(root)
    tracker = Tracker(context, clock=Clock(), pio=configs)
    return Rig(Library(context, tracker), tracker, configs, runner, root, core)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_import_pio_creates_one_firmware_per_env(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    project = rig.library.create_project("p")
    defaults = ProjectDefaults(before="no-reset", after="no-reset", erase_all=True)
    rig.library.set_project_defaults(project.id, defaults)

    imported = rig.library.import_pio(rig.root, ENVS, project_id=project.id, device="board")

    assert [f.name for f in imported] == ENVS
    boot_app0 = _sha(rig.core / PACKAGE / BOOT_APP0_IN_PACKAGE)
    for firmware, env in zip(imported, ENVS, strict=True):
        stored = context.firmware.get(firmware.id)
        assert (stored.source, stored.project_id, stored.device) == (
            Source.PIO_BUILD,
            project.id,
            "board",
        )
        assert stored.build_ref is not None
        assert (Path(stored.build_ref.path), stored.build_ref.env) == (rig.root, env)
        # PIO builds give no reset modes: the project's defaults are copied
        assert (stored.erase_all, stored.before, stored.after) == (True, "no-reset", "no-reset")
        assert stored.last_resolved is not None
        status = rig.tracker.check(firmware.id)
        assert (status.state, status.firmware.chip) == (ResolveState.OK, CHIPS[env])
        entries = sorted((e.address, e.name) for e in status.firmware.entries)
        assert entries == idedata_images(rig.build(env))
        assert next(e for e in status.firmware.entries if e.name == "boot_app0.bin").sha256 == (
            boot_app0
        )


SAME_ROOT: dict[str, Callable[[Path], Path]] = {
    "same": lambda r: r,
    "case": lambda r: Path(str(r).upper()),
    "dotdot": lambda r: r / ".." / r.name,
}


@pytest.mark.parametrize("variant", list(SAME_ROOT))
def test_an_env_is_followed_once(context: AppContext, tmp_path: Path, variant: str) -> None:
    rig = _rig(context, tmp_path)
    [first] = rig.library.import_pio(rig.root, [ESP32])  # only the chosen env
    assert [f.id for f in context.firmware.all()] == [first.id]
    root = SAME_ROOT[variant](rig.root)
    found = rig.library.tracking_firmware_for_env(root, ESP32)
    assert found is not None and found.id == first.id
    assert rig.library.tracking_firmware_for_env(root, S3) is None
    assert rig.library.tracking_firmware_for_env(root, ESP32.upper()) is None  # env exactly

    with pytest.raises(AlreadyTrackedError) as raised:
        rig.library.import_pio(root, [S3, ESP32])
    assert raised.value.firmware_id == first.id
    assert [f.id for f in context.firmware.all()] == [first.id]  # all or nothing


def test_import_pio_creates_nothing_for_an_unbuilt_env(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    with pytest.raises(ValueError):
        rig.library.import_pio(rig.root, [])
    shutil.rmtree(rig.build(S3))
    with pytest.raises(PlanError, match=S3):
        rig.library.import_pio(rig.root, ENVS)
    assert context.firmware.all() == []


def test_preview_pio(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    preview = rig.library.preview_pio(rig.build(S3))
    assert (preview.picked, preview.config_error) == (EnvRef(rig.root, S3), None)
    assert [(e.name, e.built, e.board, e.tracked_by, e.problem) for e in preview.envs] == [
        (ESP32, True, ESP32, None, None),
        (S3, True, S3, None, None),
    ]
    assert [e.build.chip if e.build else None for e in preview.envs] == ["esp32", "esp32s3"]
    assert context.firmware.all() == []

    [first] = rig.library.import_pio(rig.root, [ESP32])
    shutil.rmtree(rig.build(S3))
    again = rig.library.preview_pio(rig.build(ESP32))
    assert again.picked == EnvRef(rig.root, ESP32)
    assert [(e.name, e.built, e.tracked_by, e.build is None) for e in again.envs] == [
        (ESP32, True, first.id, False),
        (S3, False, None, True),
    ]

    with pytest.raises(EnvDirError):
        rig.library.preview_pio(rig.root / ".pio" / "libdeps" / ESP32)


def test_preview_pio_without_the_pio_config(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    rig.runner.fail = True
    rig.set_board(ESP32, ESP32)  # same text, new mtime: the config is queried again
    preview = rig.library.preview_pio(rig.build(ESP32))
    assert preview.config_error
    assert [(e.name, e.built, e.board) for e in preview.envs] == [
        (ESP32, True, None),
        (S3, True, None),
    ]
    [firmware] = rig.library.import_pio(rig.root, [ESP32])  # still possible
    assert rig.tracker.check(firmware.id).state is ResolveState.OK


def test_switched_board_blocks_until_rebuilt(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    [firmware] = rig.library.import_pio(rig.root, [ESP32])

    rig.set_board(ESP32, S3)  # .pio/build/esp32dev still holds the esp32 build
    switched = rig.tracker.check(firmware.id)
    assert (switched.state, switched.configured_target) == (ResolveState.TARGET_SWITCHED, "esp32s3")
    assert not switched.state.flashable

    for name in ("bootloader.bin", "partitions.bin", "firmware.bin"):  # `pio run` again
        write_newer(rig.build(ESP32) / name, (rig.build(S3) / name).read_bytes())
    rebuilt = rig.tracker.check(firmware.id)
    assert rebuilt.state is ResolveState.NEEDS_CONFIRMATION
    chip = next(c for c in rebuilt.changes if c.kind is ChangeKind.CHIP)
    assert (chip.before, chip.after) == ("esp32", "esp32s3")
    assert rig.tracker.confirm(firmware.id).firmware.chip == "esp32s3"


def test_unknown_config_skips_the_target_check(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    [firmware] = rig.library.import_pio(rig.root, [ESP32])
    rig.runner.fail = True
    rig.set_board(ESP32, S3)
    status = rig.tracker.check(firmware.id)
    assert (status.state, status.configured_target) == (ResolveState.OK, None)


def test_rebuild_with_new_content_is_followed(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    [firmware] = rig.library.import_pio(rig.root, [ESP32])
    rig.tracker.check(firmware.id)
    flip_last_byte(rig.build(ESP32) / "firmware.bin")
    status = rig.tracker.check(firmware.id)
    assert status.state is ResolveState.UPDATED
    assert status.marks[entry_key(EntryKind.APP, "firmware.bin")] is EntryMark.CHANGED


def test_notes_reach_the_status(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path, "arduino_esp32s3_otadata")
    [firmware] = rig.library.import_pio(rig.root, [S3])
    status = rig.tracker.check(firmware.id)
    boot_app0 = next(e for e in status.firmware.entries if e.name == "boot_app0.bin")
    assert boot_app0.address == 0xD000  # moved from 0xe000 to the otadata partition
    assert status.entry_notes.get(boot_app0.id)

    (rig.build(S3) / "idedata.json").unlink()
    scanned = rig.tracker.check(firmware.id)
    assert scanned.state is ResolveState.OK
    assert any("idedata.json" in n for n in scanned.build_notes)


def test_framework_package_paths(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    [firmware] = rig.library.import_pio(rig.root, [ESP32])
    shared = make_package(tmp_path / "shared", "1.0.0", b"\x01" * 0x2000)
    own = make_package(tmp_path / "own", "2.0.0", b"\x02" * 0x2000)

    def boot_app0() -> str:
        status = rig.tracker.check(firmware.id)
        assert status.state.flashable, status.problems
        return next(e.sha256 for e in status.firmware.entries if e.name == "boot_app0.bin")

    assert boot_app0() == _sha(rig.core / PACKAGE / BOOT_APP0_IN_PACKAGE)
    rig.library.set_framework_pkg_default(f"  {shared}  ")
    assert rig.library.framework_pkg_default() == str(shared)
    assert context.settings.get(PIO_SECTION, FRAMEWORK_PKG_KEY) == str(shared)
    assert boot_app0() == _sha(shared / BOOT_APP0_IN_PACKAGE)

    assert rig.library.set_framework_pkg(firmware.id, str(own)).framework_pkg == str(own)
    assert boot_app0() == _sha(own / BOOT_APP0_IN_PACKAGE)  # the firmware's path wins
    rig.library.set_framework_pkg(firmware.id, "")
    assert boot_app0() == _sha(shared / BOOT_APP0_IN_PACKAGE)
    rig.library.set_framework_pkg_default("")
    assert context.settings.get(PIO_SECTION, FRAMEWORK_PKG_KEY) is None
    assert boot_app0() == _sha(rig.core / PACKAGE / BOOT_APP0_IN_PACKAGE)


def test_invalid_framework_package_blocks(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    [firmware] = rig.library.import_pio(rig.root, [ESP32])
    rig.library.set_framework_pkg(firmware.id, str(tmp_path / "no-such-package"))
    status = rig.tracker.check(firmware.id)
    assert status.state is ResolveState.INCOMPLETE
    assert any("no-such-package" in p for p in status.problems)

    fixed = rig.library.create_firmware("fixed")
    with pytest.raises(ValueError):
        rig.library.set_framework_pkg(fixed.id, str(tmp_path / "own"))


def test_pio_project_metadata_is_never_run(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    before = {p: p.stat().st_mtime_ns for p in rig.root.rglob("*")}
    rig.library.preview_pio(rig.build(ESP32))
    for firmware in rig.library.import_pio(rig.root, ENVS):
        rig.tracker.check(firmware.id)
    assert any("config" in argv for argv in rig.runner.calls)
    assert not any("metadata" in argv for argv in rig.runner.calls)
    assert {p: p.stat().st_mtime_ns for p in rig.root.rglob("*")} == before  # nothing written
