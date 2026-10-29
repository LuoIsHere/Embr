"""app/tracking: when sources are read again, auto-follow, BUILDING and the flash-time check
(design doc §3.4 "检测时机"). The tracker clock is injected; build files are written an
hour in the past, so only files touched on purpose look recent."""

import datetime
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from app_testkit import (
    Clock,
    build_files,
    copy_files,
    fixed_firmware,
    flip_last_byte,
    idf_project,
    move_app,
)

from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.library import Library
from embr.app.tracking import SETTLE_SECONDS, BuildChangedError, Tracker
from embr.core.firmware.filecache import FileCache
from embr.core.firmware.tracking import EntryMark, ResolveState, digest

APP = "app:hello_esp32s3.bin"


@dataclass
class Rig:
    tracker: Tracker
    cache: FileCache
    clock: Clock
    library: Library
    firmware_id: str
    build: Path

    @property
    def app(self) -> Path:
        return self.build / "hello_esp32s3.bin"


def _rig(context: AppContext, tmp_path: Path) -> Rig:
    clock, cache = Clock(), FileCache()
    tracker = Tracker(context, cache=cache, clock=clock)
    library = Library(context, tracker)
    build = idf_project("esp32s3_hello", tmp_path / "hello")
    firmware = library.import_idf(build)
    return Rig(tracker, cache, clock, library, firmware.id, build)


def test_unchanged_sources_return_the_cached_status(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    first = rig.tracker.check(rig.firmware_id)
    assert first.state is ResolveState.OK
    hashed = rig.cache.hashed

    rig.clock.advance(10)
    assert rig.tracker.check(rig.firmware_id) == first  # same checked_at: nothing resolved
    assert rig.tracker.cached(rig.firmware_id) == first
    assert rig.cache.hashed == hashed

    rig.tracker.invalidate(rig.firmware_id)
    again = rig.tracker.check(rig.firmware_id)
    assert (again.state, again.checked_at) == (ResolveState.OK, rig.clock.now)
    assert rig.cache.hashed == hashed  # resolved again, but no file was hashed again


def test_content_change_is_followed_and_saved(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    first = rig.tracker.check(rig.firmware_id)
    saved = context.firmware.get(rig.firmware_id).last_resolved

    flip_last_byte(rig.app)
    rig.clock.advance(10)
    status = rig.tracker.check(rig.firmware_id)

    assert (status.state, status.updated_at) == (ResolveState.UPDATED, rig.clock.now)
    assert status.marks[APP] is EntryMark.CHANGED
    assert status.digest != first.digest
    assert context.firmware.get(rig.firmware_id).last_resolved != saved
    rig.tracker.mark_seen(rig.firmware_id)
    cached = rig.tracker.cached(rig.firmware_id)
    assert cached is not None and cached.updated_at is None


def test_recently_written_file_means_building(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    shown = rig.tracker.check(rig.firmware_id).digest
    recent = (rig.clock.now - datetime.timedelta(seconds=SETTLE_SECONDS / 4)).timestamp()
    os.utime(rig.app, (recent, recent))

    assert rig.tracker.check(rig.firmware_id).state is ResolveState.BUILDING
    with pytest.raises(BuildChangedError) as raised:
        rig.tracker.verify_for_flash(rig.firmware_id, shown)
    assert raised.value.building

    rig.clock.advance(SETTLE_SECONDS + 1)  # BUILDING was not cached
    assert rig.tracker.check(rig.firmware_id).state is ResolveState.OK


def test_confirm_accepts_a_structural_change(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    rig.tracker.check(rig.firmware_id)
    with pytest.raises(ValueError):
        rig.tracker.confirm(rig.firmware_id)

    move_app(rig.build, 0x20000)
    assert rig.tracker.check(rig.firmware_id).state is ResolveState.NEEDS_CONFIRMATION
    confirmed = rig.tracker.confirm(rig.firmware_id)
    assert confirmed.state is ResolveState.OK
    app = next(e for e in confirmed.firmware.entries if e.id == APP)
    assert app.address == 0x20000


def test_missing_build_and_change_directory(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    rig.tracker.check(rig.firmware_id)
    shutil.rmtree(rig.build)
    assert rig.tracker.check(rig.firmware_id).state is ResolveState.MISSING

    moved = idf_project("esp32s3_hello", tmp_path / "moved")
    assert rig.tracker.set_build_dir(rig.firmware_id, moved).state is ResolveState.OK
    build_ref = context.firmware.get(rig.firmware_id).build_ref
    assert build_ref is not None and Path(build_ref.path).resolve() == moved.resolve()

    fixed = rig.library.create_firmware("fixed")
    with pytest.raises(ValueError):
        rig.tracker.set_build_dir(fixed.id, moved)


def test_verify_for_flash_refuses_a_changed_build(context: AppContext, tmp_path: Path) -> None:
    rig = _rig(context, tmp_path)
    shown = rig.tracker.check(rig.firmware_id).digest
    assert digest(rig.tracker.verify_for_flash(rig.firmware_id, shown)) == shown

    flip_last_byte(rig.app)  # followed automatically, but not what the UI showed
    with pytest.raises(BuildChangedError) as raised:
        rig.tracker.verify_for_flash(rig.firmware_id, shown)
    assert not raised.value.building
    assert raised.value.status.state is ResolveState.UPDATED
    assert raised.value.status.digest != shown
    assert digest(rig.tracker.verify_for_flash(rig.firmware_id, raised.value.status.digest)) == (
        raised.value.status.digest
    )


def test_verify_for_flash_refuses_a_build_needing_confirmation(
    context: AppContext, tmp_path: Path
) -> None:
    rig = _rig(context, tmp_path)
    move_app(rig.build, 0x20000)
    with pytest.raises(PlanError) as raised:
        rig.tracker.verify_for_flash(rig.firmware_id, None)
    assert not isinstance(raised.value, BuildChangedError)


def test_fixed_firmware_with_a_changed_file_needs_confirmation(
    context: AppContext, tmp_path: Path
) -> None:
    clock = Clock()
    tracker = Tracker(context, clock=clock)
    library = Library(context, tracker)
    files = copy_files(build_files("esp32s3_hello"), tmp_path / "files")
    firmware = fixed_firmware(library, files)
    assert tracker.check(firmware.id).state is ResolveState.OK

    app = files[2]
    flip_last_byte(app)
    clock.advance(10)
    assert tracker.check(firmware.id).state is ResolveState.NEEDS_CONFIRMATION
    assert tracker.confirm(firmware.id).state is ResolveState.OK
    sha = hashlib.sha256(app.read_bytes()).hexdigest()
    assert sha in {e.sha256 for e in context.firmware.get(firmware.id).entries}
