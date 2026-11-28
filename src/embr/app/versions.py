"""Build versions of tracking firmware: back up the current build, delete versions, and
reclaim the archive files only versions used.

A backup runs in three steps so that copying large images can leave the GUI thread:
prepare() and commit() run on the thread that owns the database, copy() only touches
files and may run anywhere. Every prepare() that returns Prepared must be followed by
commit() or abandon(). backup() runs all three in a row (no GUI thread to keep free).
A firmware has at most one backup in progress.

Files: a version's files are stored with Archive.store()/register(), without a name record.
Deleting versions removes exactly the files that only they used: files still referred to by
a firmware entry or another version stay, and no other unreferenced file is touched (that
is Clean archive's job).
"""

import datetime
import hashlib
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from embr.app.archiving import firmware_references
from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.tracking import STATE_TEXT, BuildChangedError, Tracker, TrackStatus
from embr.core import vcs
from embr.core.firmware.files import entry_file, is_blank
from embr.core.firmware.image_header import read_image_header
from embr.core.firmware.import_idf import IdfBuildError, read_build
from embr.core.firmware.import_pio import EnvRef, Framework, framework_of
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Entry, Firmware, Source
from embr.core.firmware.tracking import digest
from embr.core.vcs import GitState
from embr.core.versions import Origin, Version, VersionDraft, VersionEntry, VersionParams
from embr.i18n import N_


class VersionBusyError(PlanError):
    """A backup of the same firmware is already in progress."""


@dataclass(frozen=True)
class Prepared:
    """What prepare() captured on the database thread for copy() and commit()."""

    firmware: Firmware
    """Deep copy of the resolved firmware the backup is made of."""
    digest: str
    """digest(firmware, erase_all=False)."""
    status: TrackStatus
    sources: tuple[Path | None, ...]
    """File of each entry of `firmware`, in order; None for a blank entry."""
    project_dir: Path | None
    """Folder whose git state is recorded; None when unknown."""
    use_app_version: bool


@dataclass(frozen=True)
class Copied:
    """What copy() stored and read; no database row is written yet."""

    files: tuple[tuple[str, int], ...]
    """(sha256, size) of each entry, in the order of Prepared.firmware.entries."""
    app_version: str | None
    git: GitState | None


class BuildVersions:
    def __init__(
        self,
        context: AppContext,
        tracker: Tracker,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        self._ctx = context
        self._tracker = tracker
        self._clock = clock
        self._running: dict[str, Prepared] = {}

    # ---- backing up ---------------------------------------------------------------------

    def prepare(self, firmware_id: str) -> Prepared | Version:
        """Check the firmware and capture its current build. Returns the latest version
        when its content (digest without the erase option) is the same: nothing to back
        up. PlanError when the firmware does not follow a build or its state is not OK or
        UPDATED; VersionBusyError while a backup of it is in progress; KeyError for an
        unknown firmware."""
        status = self._tracker.check(firmware_id)
        firmware = deepcopy(status.firmware)
        if not firmware.source.tracking:
            raise PlanError(N_(f"{firmware.name} does not follow a build; it has no versions."))
        if not status.state.flashable:
            reasons = status.problems or tuple(c.message for c in status.changes[:3])
            why = STATE_TEXT[status.state]
            raise PlanError(f"{firmware.name}: {why}. " + "; ".join(reasons))
        content = digest(firmware, erase_all=False)
        latest = self._ctx.versions.latest(firmware_id)
        if latest is not None and latest.digest == content:
            return latest
        if firmware_id in self._running:
            raise VersionBusyError(N_(f"{firmware.name}: a backup is already in progress."))
        archive_dir = self._ctx.paths.archive
        prepared = Prepared(
            firmware=firmware,
            digest=content,
            status=status,
            sources=tuple(
                None if is_blank(e) else entry_file(e, archive_dir) for e in firmware.entries
            ),
            project_dir=self._project_dir(firmware),
            use_app_version=self._uses_app_version(firmware),
        )
        self._running[firmware_id] = prepared
        return prepared

    def copy(self, prepared: Prepared) -> Copied:
        """Store every entry's file in the archive (a blank entry as its 0xFF bytes), read
        the app version and query git. No database access; may run on any thread.
        BuildChangedError when a file no longer has the content prepare() saw (the build
        is running again) or cannot be read."""
        files: list[tuple[str, int]] = []
        app_version: str | None = None
        for entry, source in zip(prepared.firmware.entries, prepared.sources, strict=True):
            try:
                data = b"\xff" * entry.size if source is None else source.read_bytes()
            except OSError:  # gone or unreadable: the build output is being rewritten
                data = None
            # compared before storing, so a newer build leaves no unindexed file behind
            if data is None or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise BuildChangedError(
                    N_(
                        f"{entry.name} changed while it was being backed up; "
                        "try again when the build has finished."
                    ),
                    prepared.status,
                    building=True,
                )
            files.append(self._ctx.archive.store(data))
            if app_version is None and entry.kind == EntryKind.APP and entry.enabled:
                header = read_image_header(data)
                app_version = header.app_version if header is not None else None
        git = vcs.read_state(prepared.project_dir) if prepared.project_dir is not None else None
        return Copied(tuple(files), app_version, git)

    def commit(
        self, prepared: Prepared, copied: Copied, *, origin: Origin, note: str = ""
    ) -> Version:
        """Index the stored files and write the version, in one transaction; the backup is
        no longer in progress afterwards, whatever happens. KeyError when the firmware was
        deleted while the files were copied (nothing is written)."""
        firmware = prepared.firmware
        try:
            if self._running.get(firmware.id) is not prepared:
                raise KeyError(firmware.id)
            draft = VersionDraft(
                firmware_id=firmware.id,
                created_at=self._clock().isoformat(timespec="seconds"),
                origin=origin,
                digest=prepared.digest,
                params=VersionParams(
                    flash_mode=firmware.flash.mode,
                    flash_freq=firmware.flash.freq,
                    flash_size=firmware.flash.size,
                    before=firmware.before,
                    after=firmware.after,
                    stub=firmware.stub,
                    idf_version=prepared.status.idf_version,
                    use_app_version=prepared.use_app_version,
                ),
                entries=tuple(
                    _version_entry(e, sha, size)
                    for e, (sha, size) in zip(firmware.entries, copied.files, strict=True)
                ),
                chip=firmware.chip,
                app_version=copied.app_version,
                git_commit=copied.git.commit if copied.git is not None else None,
                git_describe=copied.git.describe if copied.git is not None else None,
                git_dirty=copied.git.dirty if copied.git is not None else None,
                build_path=firmware.build_ref.path if firmware.build_ref is not None else "",
                note=note,
            )
            with self._ctx.database:
                for sha, size in dict(copied.files).items():
                    self._ctx.archive.register(sha, size)
                return self._ctx.versions.insert(draft)
        finally:
            self.abandon(prepared)

    def abandon(self, prepared: Prepared) -> None:
        """The backup failed or is not wanted; files already stored stay in the archive
        folder and are reused by the next backup of the same content."""
        if self._running.get(prepared.firmware.id) is prepared:
            del self._running[prepared.firmware.id]

    def backup(self, firmware_id: str, *, origin: Origin, note: str = "") -> Version:
        """prepare(), copy() and commit() in a row on the calling thread. Returns the
        latest version unchanged when the content is the same; errors as in those steps."""
        prepared = self.prepare(firmware_id)
        if isinstance(prepared, Version):
            return prepared
        try:
            copied = self.copy(prepared)
        except BaseException:
            self.abandon(prepared)
            raise
        return self.commit(prepared, copied, origin=origin, note=note)

    def busy(self, firmware_id: str) -> bool:
        """A backup of the firmware is in progress."""
        return firmware_id in self._running

    # ---- deleting -----------------------------------------------------------------------

    def delete(self, version_ids: Iterable[str]) -> None:
        """Delete the versions (unknown ids are ignored) and the archive files only they
        used."""
        with self._ctx.database:
            used = self._ctx.versions.delete(version_ids)
        self._reclaim(used)

    def forget_firmware(self, firmware_id: str) -> None:
        """The firmware was deleted: drop all its versions and its number counter, and the
        archive files only they used. A backup in progress for it writes nothing."""
        self._running.pop(firmware_id, None)
        with self._ctx.database:
            used = self._ctx.versions.delete_firmware(firmware_id)
        self._reclaim(used)

    def exclusive_size(self, firmware_id: str) -> int:
        """Bytes deleting all versions of the firmware would free: the files its versions
        use that no firmware entry and no version of another firmware refers to."""
        sizes = {
            e.sha256: e.size
            for v in self._ctx.versions.for_firmware(firmware_id)
            for e in v.entries
        }
        kept = firmware_references(self._ctx) | self._ctx.versions.referenced(besides=firmware_id)
        return sum(size for sha, size in sizes.items() if sha not in kept)

    def _reclaim(self, used: set[str]) -> None:
        kept = firmware_references(self._ctx) | self._ctx.versions.referenced()
        self._ctx.archive.delete(used - kept)

    # ---- build facts --------------------------------------------------------------------

    @staticmethod
    def _project_dir(firmware: Firmware) -> Path | None:
        """ESP-IDF: project_description.json's project_path; PlatformIO: the project root."""
        ref = firmware.build_ref
        if ref is None:
            return None
        if firmware.source == Source.PIO_BUILD:
            return Path(ref.path)
        try:
            return read_build(Path(ref.path)).project_path
        except IdfBuildError:
            return None

    def _uses_app_version(self, firmware: Firmware) -> bool:
        """False for a PlatformIO Arduino build: its app descriptor carries the Arduino
        core's version. The framework is decided as the resolve decides it (framework_of:
        the env's configured framework, else whether the build has flasher_args.json)."""
        ref = firmware.build_ref
        if firmware.source != Source.PIO_BUILD or ref is None or not ref.env:
            return True
        env = EnvRef(Path(ref.path), ref.env)
        return framework_of(env.build_dir, self._tracker.pio.env(env)) != Framework.ARDUINO


def _version_entry(entry: Entry, sha256: str, size: int) -> VersionEntry:
    return VersionEntry(
        name=entry.name,
        kind=entry.kind.value,
        address=entry.address,
        sha256=sha256,
        size=size,
        enabled=entry.enabled,
        note=entry.note,
    )
