"""Keep the shown state of firmware in step with their sources (design doc §3.4
"检测时机"). One Tracker serves the sidebar flags, the workspace and the flash button.

check() first compares a fingerprint (size and modification time of every source file)
with the previous check and returns the cached status when nothing changed; only then is
the source resolved again, hashing just the files whose stat changed. Tracking firmware
resolve through core.firmware.tracking (content-only changes are accepted and saved at
once); fixed firmware check their "path:" and "archive:" entries by name, path and hash.
"""

import copy
import dataclasses
import datetime
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.pio import FRAMEWORK_PKG_KEY, PIO_SECTION, PioConfigs
from embr.core.firmware.filecache import FileCache, Fingerprint, fingerprint
from embr.core.firmware.files import FileState, confirm_entry
from embr.core.firmware.import_idf import (
    FLASHER_ARGS,
    PROJECT_DESCRIPTION,
    BuildMissingError,
    IdfBuildError,
    configured_target,
    read_build,
)
from embr.core.firmware.import_pio import (
    PLATFORMIO_INI,
    EnvRef,
    PioBuildError,
    env_ref,
    read_env,
)
from embr.core.firmware.import_pio import BuildMissingError as PioBuildMissingError
from embr.core.firmware.model import Entry, Firmware, FlashParams, Source
from embr.core.firmware.tracking import (
    Change,
    DerivedBuild,
    DerivedFile,
    EntryMark,
    Resolution,
    ResolveState,
    accept,
    digest,
    missing,
    resolve,
)
from embr.i18n import N_

SETTLE_SECONDS = 2.0
"""A source file modified less than this long ago means a build is still writing."""


@dataclass(frozen=True)
class TrackStatus:
    firmware_id: str
    state: ResolveState
    """Fixed firmware use OK, NEEDS_CONFIRMATION (a file's content changed) and MISSING."""
    firmware: Firmware
    """What the workspace shows and a flash writes: the resolved copy for tracking
    firmware, the stored firmware for fixed ones."""
    changes: tuple[Change, ...]
    problems: tuple[str, ...]
    marks: Mapping[str, EntryMark]
    digest: str
    """tracking.digest() of `firmware`; the UI keeps the digest it displayed."""
    checked_at: datetime.datetime
    updated_at: datetime.datetime | None
    """When content changes were last followed automatically ("Build updated HH:MM:SS");
    None once mark_seen() was called or when nothing was followed."""
    idf_version: str | None = None
    configured_target: str | None = None
    entry_notes: Mapping[str, str] = dataclasses.field(default_factory=lambda: dict[str, str]())
    """Resolution.entry_notes (e.g. boot_app0.bin moved to the otadata partition)."""
    build_notes: tuple[str, ...] = ()
    """Resolution.build_notes (e.g. idedata.json not used and why)."""
    removed: tuple[Entry, ...] = ()
    """Resolution.removed: entries whose file left the build, shown marked missing."""


class BuildChangedError(PlanError):
    """Flash was pressed but the firmware is not what the UI showed: its digest changed,
    or a source file is still being written. No job is started and no record written."""

    def __init__(self, message: str, status: TrackStatus, *, building: bool) -> None:
        super().__init__(message)
        self.status = status
        self.building = building


class Tracker:
    def __init__(
        self,
        context: AppContext,
        *,
        cache: FileCache | None = None,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
        pio: PioConfigs | None = None,
    ) -> None:
        """`pio` gives PIO firmware their configured target; None creates one without an
        on_ready callback."""
        self._ctx = context
        self._pio = pio or PioConfigs()
        self._cache = cache or FileCache()
        self._clock = clock
        self._status: dict[str, TrackStatus] = {}
        self._prints: dict[str, Fingerprint] = {}
        self._inputs: dict[str, tuple[Path, ...]] = {}
        self._resolutions: dict[str, Resolution] = {}
        self._updated: dict[str, datetime.datetime] = {}

    def check(self, firmware_id: str, *, settle: bool = True) -> TrackStatus:
        """Stat the firmware's source files. Fingerprint unchanged since the last check
        (and no invalidate() since) -> the cached status, nothing read. Otherwise resolve
        again. A newest file younger than SETTLE_SECONDS -> BUILDING (not cached, so the
        next check looks again; tracking firmware only, a fixed firmware's changed file
        needs confirmation anyway); settle=False skips that rule (importing a build the
        user just picked). UPDATED resolutions are accepted and saved; updated_at is set.
        Raises KeyError for an unknown firmware."""
        cached = self._status.get(firmware_id)
        inputs = self._inputs.get(firmware_id)
        if (
            cached is not None
            and inputs is not None
            and fingerprint(inputs) == self._prints.get(firmware_id)
        ):
            return cached
        firmware = self._ctx.firmware.get(firmware_id)
        now = self._clock()
        status, inputs, resolution = self._evaluate(firmware, now)
        print_ = fingerprint(inputs)
        newest = print_.newest_mtime_ns
        settling = settle and firmware.source.tracking and newest is not None
        if settling and now.timestamp() - (newest or 0) / 1e9 < SETTLE_SECONDS:
            building = dataclasses.replace(
                status,
                state=ResolveState.BUILDING,
                problems=("The build output changed moments ago; waiting for it to settle.",),
            )
            self._status[firmware_id] = building
            self._inputs[firmware_id] = inputs
            self._prints.pop(firmware_id, None)  # look again next time
            return building
        if resolution is not None:
            self._resolutions[firmware_id] = resolution
            if resolution.state == ResolveState.UPDATED or (
                resolution.state == ResolveState.OK and firmware.last_resolved is None
            ):
                accept(firmware, resolution)
                self._ctx.firmware.save(firmware)
                if resolution.state == ResolveState.UPDATED:
                    self._updated[firmware_id] = now
                print_ = fingerprint(inputs)  # the TOML is not an input; files unchanged
        if not status.state.flashable:  # "Build updated" only describes a flashable build
            self._updated.pop(firmware_id, None)
        status = dataclasses.replace(status, updated_at=self._updated.get(firmware_id))
        self._status[firmware_id] = status
        self._inputs[firmware_id] = inputs
        self._prints[firmware_id] = print_
        return status

    @property
    def pio(self) -> PioConfigs:
        return self._pio

    def cached(self, firmware_id: str) -> TrackStatus | None:
        """The last status, without any file access; None before the first check."""
        return self._status.get(firmware_id)

    def invalidate(self, firmware_id: str) -> None:
        """The firmware itself changed (overrides, user entries, build directory, deleted):
        the next check() resolves again even with an unchanged fingerprint."""
        self._prints.pop(firmware_id, None)
        self._inputs.pop(firmware_id, None)

    def invalidate_project(self, project_root: Path) -> list[str]:
        """A `pio project config` result for `project_root` arrived (PioConfigs on_ready):
        invalidate every PIO firmware following an env of it and return their ids."""
        key = os.path.normcase(str(project_root.resolve()))
        found = [
            f.id
            for f in self._ctx.firmware.all()
            if f.source == Source.PIO_BUILD
            and f.build_ref is not None
            and os.path.normcase(str(Path(f.build_ref.path).resolve())) == key
        ]
        for firmware_id in found:
            self.invalidate(firmware_id)
        return found

    def mark_seen(self, firmware_id: str) -> None:
        """The user left the firmware: clear updated_at and, after a followed content
        change, the "content changed" marks (design doc §3.4: they last until the next
        change of the build output or switching firmware)."""
        self._updated.pop(firmware_id, None)
        status = self._status.get(firmware_id)
        if status is not None and status.updated_at is not None:
            followed = status.state == ResolveState.UPDATED
            state = ResolveState.OK if followed else status.state
            marks = status.marks
            if followed:
                marks = {k: m for k, m in marks.items() if m != EntryMark.CHANGED}
            self._status[firmware_id] = dataclasses.replace(
                status, state=state, updated_at=None, marks=marks
            )

    def confirm(self, firmware_id: str) -> TrackStatus:
        """Accept a NEEDS_CONFIRMATION status (tracking.accept() for tracking firmware,
        files.confirm_entry() for fixed ones), save and check again. ValueError when the
        current state is not NEEDS_CONFIRMATION."""
        status = self.check(firmware_id)
        if status.state != ResolveState.NEEDS_CONFIRMATION:
            raise ValueError(f"nothing to confirm ({status.state})")
        firmware = self._ctx.firmware.get(firmware_id)
        if firmware.source.tracking:
            accept(firmware, self._resolutions[firmware_id])
        else:
            for entry in firmware.entries:
                if status.marks.get(entry.id) == EntryMark.CHANGED:
                    confirm_entry(entry, self._ctx.paths.archive)
        self._ctx.firmware.save(firmware)
        self.invalidate(firmware_id)
        return self.check(firmware_id)

    def set_build_dir(self, firmware_id: str, path: Path) -> TrackStatus:
        """ "Change directory" for a tracking firmware: store the new build_ref path and
        check. PIO firmware take an env build folder (`<project>/.pio/build/<env>`) and store
        its project root and env; import_pio.EnvDirError (a ValueError) for anything else.
        ValueError for fixed firmware."""
        firmware = self._ctx.firmware.get(firmware_id)
        if firmware.build_ref is None:
            raise ValueError(f"{firmware.name} does not follow a build")
        if firmware.source == Source.PIO_BUILD:  # .pio/build/<env>: EnvDirError is a ValueError
            ref = env_ref(path)
            firmware.build_ref.path, firmware.build_ref.env = str(ref.project_root), ref.env
        else:
            firmware.build_ref.path = str(path.resolve())
        self._ctx.firmware.save(firmware)
        self.invalidate(firmware_id)
        return self.check(firmware_id)

    def verify_for_flash(self, firmware_id: str, shown_digest: str | None) -> Firmware:
        """Check right before a flash. BUILDING, or a digest different from `shown_digest`
        (when given) -> BuildChangedError carrying the new status. A state that is not
        flashable -> PlanError with its problems or changes. Returns the firmware to plan."""
        status = self.check(firmware_id)
        if status.state == ResolveState.BUILDING:
            raise BuildChangedError(
                N_("The build output is still being written; flash again in a moment."),
                status,
                building=True,
            )
        if shown_digest is not None and status.digest != shown_digest:
            raise BuildChangedError(
                N_(
                    f"{status.firmware.name} changed since it was shown "
                    f"({status.checked_at:%H:%M:%S}); review it and press Flash again."
                ),
                status,
                building=False,
            )
        if not status.state.flashable:
            reasons = status.problems or tuple(c.message for c in status.changes[:3])
            why = STATE_TEXT[status.state]
            raise PlanError(f"{status.firmware.name}: {why}. " + "; ".join(reasons))
        return copy.deepcopy(status.firmware)

    # ---- evaluation -------------------------------------------------------------------

    def _evaluate(
        self, firmware: Firmware, now: datetime.datetime
    ) -> tuple[TrackStatus, tuple[Path, ...], Resolution | None]:
        if firmware.source == Source.IDF_BUILD:
            resolution, inputs = self._resolve_idf(firmware, now)
        elif firmware.source == Source.PIO_BUILD:
            resolution, inputs = self._resolve_pio(firmware, now)
        elif firmware.source.tracking:
            resolution = missing(firmware, N_("This firmware type cannot be resolved here"))
            inputs = ()
        else:
            return (*self._check_fixed(firmware, now), None)
        status = TrackStatus(
            firmware_id=firmware.id,
            state=resolution.state,
            firmware=resolution.firmware,
            changes=resolution.changes,
            problems=resolution.problems,
            marks=resolution.marks,
            digest=digest(resolution.firmware),
            checked_at=now,
            updated_at=None,
            idf_version=resolution.idf_version,
            configured_target=resolution.configured_target,
            entry_notes=resolution.entry_notes,
            build_notes=resolution.build_notes,
            removed=resolution.removed,
        )
        return status, inputs, resolution

    def _resolve_idf(
        self, firmware: Firmware, now: datetime.datetime
    ) -> tuple[Resolution, tuple[Path, ...]]:
        if firmware.build_ref is None:
            return missing(firmware, N_("No build directory")), ()
        build_dir = Path(firmware.build_ref.path)
        fallback = (build_dir / FLASHER_ARGS, build_dir / PROJECT_DESCRIPTION)
        try:
            build = read_build(build_dir)
        except BuildMissingError:
            return missing(firmware, N_(f"{build_dir}: build output not found")), fallback
        except IdfBuildError as exc:  # half-written flasher_args.json, or now encrypted
            found = missing(firmware, str(exc))
            return dataclasses.replace(found, state=ResolveState.INCOMPLETE), fallback
        derived = DerivedBuild(
            files=tuple(DerivedFile(f.address, f.path, f.kind) for f in build.files),
            declared_chips=tuple(c for c in (build.chip, build.target) if c),
            configured_target=configured_target(build.config_file),
            idf_version=build.idf_version,
            flash=FlashParams(build.flash_mode, build.flash_freq, build.flash_size),
            before=build.before,
            after=build.after,
            stub=build.stub,
            inputs=build.inputs,
        )
        resolution = resolve(
            firmware,
            derived,
            self._cache,
            archive_dir=self._ctx.paths.archive,
            now=now.isoformat(timespec="seconds"),
        )
        return resolution, build.inputs

    def _resolve_pio(
        self, firmware: Firmware, now: datetime.datetime
    ) -> tuple[Resolution, tuple[Path, ...]]:
        """import_pio.read_env() of build_ref (project root + env) with the env's config and
        configured target from the PioConfigs cache and the manual framework package
        (firmware.framework_pkg, else the global default, Library.framework_pkg_default()),
        then core tracking.resolve(). Build folder gone -> MISSING; env no longer in
        platformio.ini or an unreadable build -> INCOMPLETE. Inputs always include
        platformio.ini, so editing it re-resolves."""
        if firmware.build_ref is None or not firmware.build_ref.env:
            return missing(firmware, N_("No PlatformIO project and env")), ()
        ref = EnvRef(Path(firmware.build_ref.path), firmware.build_ref.env)
        # a folder has no stamp of its own: the files every build writes make a rebuild
        # after `pio run -t clean` (or a deleted build folder) visible
        fallback = (
            ref.project_root / PLATFORMIO_INI,
            ref.build_dir,
            ref.build_dir / "firmware.bin",
            ref.build_dir / "partitions.bin",
        )
        manual = (
            firmware.framework_pkg or self._ctx.settings.get(PIO_SECTION, FRAMEWORK_PKG_KEY) or ""
        )
        try:
            build = read_env(
                ref, core=self._pio.core_dir, manual_package=manual, config=self._pio.env(ref)
            )
        except PioBuildMissingError:
            return missing(firmware, N_(f"{ref.build_dir}: build output not found")), fallback
        except (PioBuildError, OSError) as exc:  # env removed from platformio.ini, bad files
            found = missing(firmware, str(exc))
            return dataclasses.replace(found, state=ResolveState.INCOMPLETE), fallback
        derived = DerivedBuild(
            files=tuple(
                DerivedFile(f.address, f.path, f.kind, f.note, f.blank_size) for f in build.files
            ),
            declared_chips=(),
            configured_target=self._pio.target(ref),
            idf_version=None,
            flash=build.flash,
            before=None,
            after=None,
            stub=True,
            inputs=build.inputs,
            notes=build.notes,
            problems=build.problems,
        )
        resolution = resolve(
            firmware,
            derived,
            self._cache,
            archive_dir=self._ctx.paths.archive,
            now=now.isoformat(timespec="seconds"),
        )
        return resolution, build.inputs

    def _check_fixed(
        self, firmware: Firmware, now: datetime.datetime
    ) -> tuple[TrackStatus, tuple[Path, ...]]:
        marks: dict[str, EntryMark] = {}
        inputs: list[Path] = []
        problems: list[str] = []
        for entry in firmware.entries:
            path = self._entry_path(entry)
            if path is None:
                continue
            inputs.append(path)
            state = self._fixed_state(entry, path)
            if state == FileState.MISSING:
                marks[entry.id] = EntryMark.MISSING
                problems.append(f"{entry.name}: file missing")
            elif state == FileState.CHANGED:
                marks[entry.id] = EntryMark.CHANGED
                problems.append(f"{entry.name}: content changed")
        if EntryMark.MISSING in marks.values():
            state = ResolveState.MISSING
        elif marks:
            state = ResolveState.NEEDS_CONFIRMATION
        else:
            state = ResolveState.OK
        status = TrackStatus(
            firmware_id=firmware.id,
            state=state,
            firmware=firmware,
            changes=(),
            problems=tuple(problems),
            marks=marks,
            digest=digest(firmware),
            checked_at=now,
            updated_at=None,
        )
        return status, tuple(inputs)

    def _entry_path(self, entry: Entry) -> Path | None:
        scheme, _, rest = entry.file.partition(":")
        if scheme == "path":
            return Path(rest)
        if scheme == "archive":
            return self._ctx.paths.archive / f"{rest}.bin"
        return None

    def _fixed_state(self, entry: Entry, path: Path) -> FileState:
        """files.entry_state() through the cache."""
        try:
            sha = self._cache.sha256(path)
            size = self._cache.size(path)
        except OSError:
            return FileState.MISSING
        if entry.file.startswith("archive:"):
            return FileState.OK if sha == entry.sha256 else FileState.CHANGED
        same = path.name == entry.name and size == entry.size and sha == entry.sha256
        return FileState.OK if same else FileState.CHANGED


STATE_TEXT = {
    ResolveState.NEEDS_CONFIRMATION: N_("changes need confirmation"),
    ResolveState.TARGET_SWITCHED: N_("the target was switched; rebuild the project"),
    ResolveState.INCOMPLETE: N_("the build is incomplete"),
    ResolveState.MISSING: N_("the build output or a file is missing"),
    ResolveState.BUILDING: N_("the build is still being written"),
}
"""Why a firmware in that state is not flashable (short, translatable)."""
