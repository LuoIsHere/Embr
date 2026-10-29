"""Archive use cases (design doc §3.4 "归档此版本", "归档"): archive a version of a firmware,
archive a single entry, clean unreferenced archive files.
"""

from embr.app.context import AppContext
from embr.app.tracking import Tracker
from embr.core.archive import ArchiveFile
from embr.core.firmware.model import Entry, Firmware


class Archiver:
    def __init__(self, context: AppContext, tracker: Tracker) -> None:
        raise NotImplementedError

    def preview(self, firmware_id: str) -> list[Entry]:
        """Entries an archived version would contain (the current resolution for tracking
        firmware: derived entries with overrides plus user entries; enabled or not).
        PlanError when the firmware is not flashable (missing, incomplete, target switched,
        needs confirmation, a file changed)."""
        raise NotImplementedError

    def archive_version(self, firmware_id: str, *, name: str = "", notes: str = "") -> Firmware:
        """New ARCHIVED firmware in the source's project and group: every "path:" entry
        copied into the archive and referenced as "archive:<sha256>" (blank entries stay
        blank), addresses, enabled states and notes kept, chip, flash, reset and erase_all
        copied, archived_from describing the source (name, time, build directory). Default
        name: store.default_name_archived(); made unique. The source firmware is unchanged.
        PlanError as in preview()."""
        raise NotImplementedError

    def archive_entry(self, firmware_id: str, entry_id: str) -> Entry:
        """Copy one "path:" entry of a fixed firmware (or a user entry of a tracking one)
        into the archive and point the entry at it; the name stays, the note is kept.
        ValueError for derived entries and entries already archived; PlanError when its
        file is missing or changed."""
        raise NotImplementedError

    def unreferenced(self) -> list[ArchiveFile]:
        """Archive files no firmware refers to (entries and user entries of all firmware)."""
        raise NotImplementedError

    def clean(self, sha256s: list[str]) -> None:
        """Delete the given archive files; hashes still referenced are skipped."""
        raise NotImplementedError
