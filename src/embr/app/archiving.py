"""Archive use cases (design doc §3.4 "归档此版本", "归档"): archive a version of a firmware,
archive a single entry, clean unreferenced archive files.
"""

import dataclasses
import datetime
from collections.abc import Callable
from pathlib import Path

from embr.app.context import AppContext
from embr.app.flashplan import PlanError
from embr.app.tracking import Tracker
from embr.core.archive import ArchiveFile
from embr.core.firmware.files import FileState, entry_state
from embr.core.firmware.model import Entry, Firmware, Source
from embr.core.firmware.store import default_name_archived
from embr.core.storage.ids import new_id

_ARCHIVE = "archive:"
_PATH = "path:"


@dataclasses.dataclass(frozen=True)
class ArchiveUsage:
    file: ArchiveFile
    firmware_ids: tuple[str, ...]
    """Ids of the firmware whose entries or user entries refer to the file, ordered by
    firmware name; empty when
    nothing refers to it (Clean archive deletes exactly those)."""


class Archiver:
    def __init__(
        self,
        context: AppContext,
        tracker: Tracker,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        self._ctx = context
        self._tracker = tracker
        self._clock = clock

    def preview(self, firmware_id: str) -> list[Entry]:
        """Entries an archived version would contain (the current resolution for tracking
        firmware: derived entries with overrides plus user entries; enabled or not).
        PlanError when the firmware is not flashable (missing, incomplete, target switched,
        needs confirmation, a file changed)."""
        status = self._tracker.check(firmware_id)
        if not status.state.flashable:
            reasons = status.problems or tuple(c.message for c in status.changes[:3])
            raise PlanError(f"{status.firmware.name} cannot be archived now: " + "; ".join(reasons))
        return [dataclasses.replace(e) for e in status.firmware.entries]

    def archive_version(self, firmware_id: str, *, name: str = "", notes: str = "") -> Firmware:
        """New ARCHIVED firmware in the source's project and group: every "path:" entry
        copied into the archive and referenced as "archive:<sha256>" (blank entries stay
        blank), addresses, enabled states and notes kept, chip, flash, reset and erase_all
        copied, archived_from describing the source (name, time, build directory). Default
        name: store.default_name_archived(); made unique. The source firmware is unchanged.
        PlanError as in preview()."""
        entries = self.preview(firmware_id)
        resolved = self._tracker.check(firmware_id).firmware
        now = self._clock()
        archived = [self._archived_copy(e, resolved.name) for e in entries]
        created = self._ctx.firmware.create(
            name.strip() or default_name_archived(resolved.name, now.date()),
            source=Source.ARCHIVED,
            project_id=resolved.project_id,
            device=resolved.device,
            before=resolved.before,
            after=resolved.after,
            erase_all=resolved.erase_all,
        )
        created.chip = resolved.chip
        created.flash = dataclasses.replace(resolved.flash)
        created.tags = list(resolved.tags)
        created.notes = notes
        origin = f"{resolved.name}, archived {now.isoformat(timespec='seconds')}"
        if resolved.build_ref is not None:
            origin += f", from {resolved.build_ref.path}"
        created.archived_from = origin
        created.entries = archived
        self._ctx.firmware.save(created)
        return created

    def archive_entry(self, firmware_id: str, entry_id: str) -> Entry:
        """Copy one "path:" entry of a fixed firmware (or a user entry of a tracking one)
        into the archive and point the entry at it; the name stays, the note is kept.
        ValueError for derived entries and entries already archived; PlanError when its
        file is missing or changed."""
        firmware = self._ctx.firmware.get(firmware_id)
        stored = firmware.user_entries if firmware.source.tracking else firmware.entries
        entry = next((e for e in stored if e.id == entry_id), None)
        if entry is None:
            raise ValueError(f"{firmware.name}: this entry comes from the build output")
        if not entry.file.startswith(_PATH):
            raise ValueError(f"{entry.name} is not a file on disk")
        if entry_state(entry, self._ctx.paths.archive) != FileState.OK:
            raise PlanError(f"{entry.name}: file missing or changed since it was recorded")
        record = self._ctx.archive.add(
            Path(entry.file.removeprefix(_PATH)),
            original_name=entry.name,
            original_path=entry.file.removeprefix(_PATH),
            note=entry.note,
            source_firmware=firmware.name,
        )
        entry.file = _ARCHIVE + record.sha256
        self._ctx.firmware.save(firmware)
        self._tracker.invalidate(firmware_id)
        return entry

    def usage(self) -> list[ArchiveUsage]:
        """Every indexed archive file with the firmware referring to it, in the archive
        index order (what the Clean archive dialog shows before deleting anything)."""
        users: dict[str, list[Firmware]] = {}
        for firmware in self._ctx.firmware.all():
            shas = {
                e.file.removeprefix(_ARCHIVE)
                for e in [*firmware.entries, *firmware.user_entries]
                if e.file.startswith(_ARCHIVE)
            }
            for sha in shas:
                users.setdefault(sha, []).append(firmware)
        return [
            ArchiveUsage(
                f,
                tuple(fw.id for fw in sorted(users.get(f.sha256, []), key=lambda fw: fw.name)),
            )
            for f in self._ctx.archive.files()
        ]

    def unreferenced(self) -> list[ArchiveFile]:
        """Archive files no firmware refers to (entries and user entries of all firmware)."""
        return self._ctx.archive.unreferenced(self._referenced())

    def clean(self, sha256s: list[str]) -> None:
        """Delete the given archive files; hashes still referenced are skipped."""
        referenced = self._referenced()
        self._ctx.archive.delete(s for s in sha256s if s not in referenced)

    def _referenced(self) -> set[str]:
        return {
            e.file.removeprefix(_ARCHIVE)
            for f in self._ctx.firmware.all()
            for e in [*f.entries, *f.user_entries]
            if e.file.startswith(_ARCHIVE)
        }

    def _archived_copy(self, entry: Entry, source_name: str) -> Entry:
        copy = dataclasses.replace(entry, id=new_id())
        if entry.file.startswith(_PATH):
            path = entry.file.removeprefix(_PATH)
            record = self._ctx.archive.add(
                Path(path),
                original_name=entry.name,
                original_path=path,
                note=entry.note,
                source_firmware=source_name,
            )
            if record.sha256 != entry.sha256:
                raise PlanError(f"{entry.name} changed while it was being archived")
            copy.file = _ARCHIVE + record.sha256
        return copy
