"""Everything a running Embr works on, opened from the portable data folder."""

import sqlite3
from dataclasses import dataclass

from embr.core.archive import Archive
from embr.core.firmware.store import FirmwareStore
from embr.core.history import History
from embr.core.logs import LogBook
from embr.core.project import ProjectStore
from embr.core.storage.database import open_database
from embr.core.storage.paths import AppPaths
from embr.core.storage.settings import Settings


@dataclass
class AppContext:
    paths: AppPaths
    projects: ProjectStore
    firmware: FirmwareStore
    database: sqlite3.Connection
    history: History
    logs: LogBook
    settings: Settings
    archive: Archive

    def close(self) -> None:
        """Save the settings and close the database."""
        self.settings.save()
        self.database.close()


def open_context(paths: AppPaths) -> AppContext:
    """Open the data under `paths` (storage.paths.prepare() has run): the database is
    migrated (storage.database.NewerSchemaError propagates), settings.ini is read."""
    database = open_database(paths.database, paths.backup)
    return AppContext(
        paths=paths,
        projects=ProjectStore(paths.projects, paths.backup),
        firmware=FirmwareStore(paths.firmwares, paths.backup),
        database=database,
        history=History(database),
        logs=LogBook(paths.logs),
        settings=Settings(paths.settings, paths.backup),
        archive=Archive(paths.archive, database),
    )
