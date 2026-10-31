"""Projects: optional containers of firmware with a group list and defaults (design doc
§3.1, §3.2). Operations that touch firmware take the FirmwareStore explicitly; firmware
never depends on this module."""

from dataclasses import dataclass, field
from pathlib import Path

from embr.core.firmware.model import Firmware, Source
from embr.core.firmware.store import FirmwareStore, ReadOnlyError, now
from embr.core.storage.documents import (
    Document,
    Migration,
    doc_bool,
    doc_str,
    doc_strs,
    doc_table,
    load_document,
    save_document,
    upgrade_document,
)
from embr.core.storage.ids import new_id, unique_name

PROJECT_SCHEMA_VERSION = 1


class DuplicateGroupError(ValueError):
    """A group with that name already exists in the project."""


@dataclass
class ProjectDefaults:
    """Copied once into firmware created in the project."""

    before: str = "default-reset"
    after: str = "hard-reset"
    erase_all: bool = False


@dataclass
class Project:
    id: str
    name: str
    """Unique across all projects."""
    notes: str = ""
    tags: list[str] = field(default_factory=lambda: list[str]())
    devices: list[str] = field(default_factory=lambda: list[str]())
    """Group names, in display order; empty groups allowed."""
    defaults: ProjectDefaults = field(default_factory=ProjectDefaults)
    created_at: str = ""
    updated_at: str = ""
    read_only: bool = False
    """Loaded from a file written by a newer Embr; saving is refused."""


_MIGRATIONS: dict[int, Migration] = {}
"""migrations[n] upgrades a project document from version n to n + 1."""


class ProjectStore:
    """data/projects/<id>.toml, one file per project. Firmware lists are not stored here;
    membership is the firmware's project_id."""

    def __init__(self, directory: Path, backup_root: Path) -> None:
        self.directory = directory
        self.backup_root = backup_root

    def all(self) -> list[Project]:
        """Every project, sorted by name; newer schema versions read-only."""
        if not self.directory.is_dir():
            return []
        return sorted((self._load(p) for p in self.directory.glob("*.toml")), key=lambda p: p.name)

    def get(self, project_id: str) -> Project:
        """Raises KeyError if missing."""
        path = self._path(project_id)
        if not path.is_file():
            raise KeyError(project_id)
        return self._load(path)

    def create(
        self, name: str, notes: str = "", defaults: ProjectDefaults | None = None
    ) -> Project:
        """New project, saved; the name is made globally unique with " (2)" suffixes."""
        stamp = now()
        project = Project(
            id=new_id(),
            name=unique_name(name, [p.name for p in self.all()]),
            notes=notes,
            defaults=defaults or ProjectDefaults(),
            created_at=stamp,
            updated_at=stamp,
        )
        self.save(project)
        return project

    def save(self, project: Project) -> None:
        """Write the TOML file and update updated_at. Raises
        embr.core.firmware.store.ReadOnlyError if read_only."""
        if project.read_only:
            raise ReadOnlyError(f"{project.name} was written by a newer Embr")
        project.updated_at = now()
        if not project.created_at:
            project.created_at = project.updated_at
        save_document(
            self._path(project.id), project_to_doc(project), current=PROJECT_SCHEMA_VERSION
        )

    def rename(self, project_id: str, name: str) -> Project:
        """Rename, made globally unique. Firmware keep their project_id."""
        project = self.get(project_id)
        project.name = unique_name(name, [p.name for p in self.all() if p.id != project_id])
        self.save(project)
        return project

    def delete(
        self, project_id: str, firmware: FirmwareStore, *, delete_firmware: bool = False
    ) -> None:
        """Delete the project. Its firmware become standalone (names made unique among
        standalone firmware), or are deleted with delete_firmware."""
        path = self._path(project_id)
        if not path.is_file():
            raise KeyError(project_id)
        for fw in firmware.in_scope(project_id):
            if delete_firmware:
                firmware.delete(fw.id)
            else:
                firmware.move(fw.id, None)
        path.unlink()

    def add_group(self, project_id: str, name: str) -> Project:
        """Append a group. DuplicateGroupError if it exists."""
        project = self.get(project_id)
        if name in project.devices:
            raise DuplicateGroupError(name)
        project.devices.append(name)
        self.save(project)
        return project

    def rename_group(self, project_id: str, old: str, new: str, firmware: FirmwareStore) -> Project:
        """Rename a group and every firmware of the project in it. DuplicateGroupError if
        `new` exists."""
        project = self.get(project_id)
        if new in project.devices:
            raise DuplicateGroupError(new)
        project.devices[project.devices.index(old)] = new
        self.save(project)
        for fw in firmware.in_scope(project_id):
            if fw.device == old:
                firmware.move(fw.id, project_id, new)
        return project

    def remove_group(self, project_id: str, name: str, firmware: FirmwareStore) -> Project:
        """Remove a group; its firmware stay in the project, ungrouped."""
        project = self.get(project_id)
        project.devices.remove(name)
        self.save(project)
        for fw in firmware.in_scope(project_id):
            if fw.device == name:
                firmware.move(fw.id, project_id, "")
        return project

    def create_firmware(
        self,
        firmware: FirmwareStore,
        project_id: str,
        name: str,
        *,
        device: str = "",
        source: Source = Source.MANUAL,
    ) -> Firmware:
        """New firmware in the project with the project's current defaults copied in
        (IDF tracking firmware copy only erase_all). An unknown `device` is added as a group."""
        project = self._ensure_group(project_id, device)
        defaults = project.defaults
        idf = source == Source.IDF_BUILD  # IDF firmware take reset modes from the build
        return firmware.create(
            name,
            source=source,
            project_id=project_id,
            device=device,
            before="default-reset" if idf else defaults.before,
            after="hard-reset" if idf else defaults.after,
            erase_all=defaults.erase_all,
        )

    def move_firmware(
        self, firmware: FirmwareStore, firmware_id: str, project_id: str | None, device: str = ""
    ) -> Firmware:
        """Move a firmware into a project (and group) or out to standalone (project_id None).
        A `device` not yet in the project's list is added to it."""
        if project_id is not None:
            self._ensure_group(project_id, device)
        return firmware.move(firmware_id, project_id, device)

    def _ensure_group(self, project_id: str, device: str) -> Project:
        project = self.get(project_id)
        if device and device not in project.devices:
            project.devices.append(device)
            self.save(project)
        return project

    def _path(self, project_id: str) -> Path:
        return self.directory / f"{project_id}.toml"

    def _load(self, path: Path) -> Project:
        loaded = load_document(
            path,
            current=PROJECT_SCHEMA_VERSION,
            migrations=_MIGRATIONS,
            backup_root=self.backup_root,
        )
        project = project_from_doc(loaded.data)
        project.read_only = loaded.read_only
        return project


def upgrade_project_doc(doc: Document) -> Document:
    """A project document upgraded to PROJECT_SCHEMA_VERSION in memory (see
    storage.documents.upgrade_document)."""
    return upgrade_document(doc, current=PROJECT_SCHEMA_VERSION, migrations=_MIGRATIONS)


def project_to_doc(project: Project) -> Document:
    d = project.defaults
    return {
        "id": project.id,
        "name": project.name,
        "notes": project.notes,
        "tags": list(project.tags),
        "devices": list(project.devices),
        "defaults": {"before": d.before, "after": d.after, "erase_all": d.erase_all},
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def project_from_doc(doc: Document) -> Project:
    d = doc_table(doc, "defaults") or {}
    return Project(
        id=doc_str(doc, "id"),
        name=doc_str(doc, "name"),
        notes=doc_str(doc, "notes"),
        tags=doc_strs(doc, "tags"),
        devices=doc_strs(doc, "devices"),
        defaults=ProjectDefaults(
            doc_str(d, "before", "default-reset"),
            doc_str(d, "after", "hard-reset"),
            doc_bool(d, "erase_all"),
        ),
        created_at=doc_str(doc, "created_at"),
        updated_at=doc_str(doc, "updated_at"),
    )
