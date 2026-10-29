"""Keep the shown state of firmware in step with their sources (design doc §3.4
"检测时机"). One Tracker serves the sidebar flags, the workspace and the flash button.

check() first compares a fingerprint (size and modification time of every source file)
with the previous check and returns the cached status when nothing changed; only then is
the source resolved again, hashing just the files whose stat changed. Tracking firmware
resolve through core.firmware.tracking (content-only changes are accepted and saved at
once); fixed firmware check their "path:" and "archive:" entries by name, path and hash.
"""

import datetime
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.core.firmware.filecache import FileCache
from embr.core.firmware.model import Firmware
from embr.core.firmware.tracking import Change, EntryMark, ResolveState

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
    ) -> None:
        raise NotImplementedError

    def check(self, firmware_id: str) -> TrackStatus:
        """Stat the firmware's source files. Fingerprint unchanged since the last check
        (and no invalidate() since) -> the cached status, nothing read. Otherwise resolve
        again. A newest file younger than SETTLE_SECONDS -> BUILDING (not cached, so the
        next check looks again). UPDATED resolutions are accepted and saved; updated_at is
        set. Raises KeyError for an unknown firmware."""
        raise NotImplementedError

    def cached(self, firmware_id: str) -> TrackStatus | None:
        """The last status, without any file access; None before the first check."""
        raise NotImplementedError

    def invalidate(self, firmware_id: str) -> None:
        """The firmware itself changed (overrides, user entries, build directory, deleted):
        the next check() resolves again even with an unchanged fingerprint."""
        raise NotImplementedError

    def mark_seen(self, firmware_id: str) -> None:
        """The user left the firmware: clear updated_at."""
        raise NotImplementedError

    def confirm(self, firmware_id: str) -> TrackStatus:
        """Accept a NEEDS_CONFIRMATION status (tracking.accept() for tracking firmware,
        files.confirm_entry() for fixed ones), save and check again. ValueError when the
        current state is not NEEDS_CONFIRMATION."""
        raise NotImplementedError

    def set_build_dir(self, firmware_id: str, path: Path) -> TrackStatus:
        """ "Change directory" for a tracking firmware: store the new build_ref path and
        check. ValueError for fixed firmware."""
        raise NotImplementedError

    def verify_for_flash(self, firmware_id: str, shown_digest: str | None) -> Firmware:
        """Check right before a flash. BUILDING, or a digest different from `shown_digest`
        (when given) -> BuildChangedError carrying the new status. A state that is not
        flashable -> PlanError with its problems or changes. Returns the firmware to plan."""
        raise NotImplementedError
