"""Build versions in embr.sqlite: immutable copies of a tracked firmware's flashable content.

A version belongs to the firmware it was taken from and is numbered within it (#1, #2...).
Numbers come from version_counters and are never given out twice, even after the latest
version is deleted. The files of a version live in the archive; this module only reads and
writes the index rows and never touches a file.

Transactions: insert(), delete() and delete_firmware() do not commit, so the caller can put
them in one `with connection:` block together with the archive rows of the same files.
rename(), set_note() and set_pinned() stand alone and commit themselves.
"""

import dataclasses
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from embr.core.storage.ids import new_id


class Origin(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"
    CLI = "cli"


@dataclass(frozen=True)
class VersionEntry:
    name: str
    kind: str
    """EntryKind value, or "elf" for an ELF file kept with the version (never flashed)."""
    address: int | None
    sha256: str
    size: int
    enabled: bool
    note: str = ""


@dataclass(frozen=True)
class VersionParams:
    """Flash settings of the build at the time of the version. Defaults match a firmware
    document's defaults, so a key missing from older or newer rows reads sensibly."""

    flash_mode: str = "keep"
    flash_freq: str = "keep"
    flash_size: str = "keep"
    before: str = "default-reset"
    after: str = "hard-reset"
    stub: bool = True
    idf_version: str | None = None
    use_app_version: bool = True
    """Whether app_version means something for this build and is used for the default name
    and shown. False for Arduino builds, whose app descriptor holds the Arduino core's own
    version text rather than anything about the sketch."""


@dataclass(frozen=True)
class VersionDraft:
    """Everything insert() needs; id, number and (unless given) name are assigned there."""

    firmware_id: str
    created_at: str
    """ISO 8601 local time with seconds."""
    origin: Origin
    digest: str
    """tracking.digest(firmware, erase_all=False) of the content."""
    params: VersionParams
    entries: tuple[VersionEntry, ...]
    chip: str | None = None
    app_version: str | None = None
    git_commit: str | None = None
    git_describe: str | None = None
    git_dirty: bool | None = None
    build_path: str = ""
    note: str = ""
    pinned: bool = False
    name: str = ""
    """Empty: the default name (see default_name)."""


@dataclass(frozen=True)
class Version:
    id: str
    firmware_id: str
    seq: int
    name: str
    created_at: str
    origin: Origin
    digest: str
    note: str
    pinned: bool
    app_version: str | None
    """As read from the image, kept even when params.use_app_version is False."""
    git_commit: str | None
    git_describe: str | None
    git_dirty: bool | None
    chip: str | None
    params: VersionParams
    build_path: str
    entries: tuple[VersionEntry, ...] = field(default=())

    @property
    def shown_app_version(self) -> str | None:
        """app_version when it is used for this build, else None."""
        return self.app_version if self.params.use_app_version else None


def base_name(git_describe: str | None, app_version: str | None, created_at: str) -> str:
    """The first of git_describe, app_version and the creation time ("YYYY-MM-DD HH:MM")
    that is not empty. Pass app_version=None when the build does not use it."""
    return git_describe or app_version or minute(created_at)


def default_name(base: str, created_at: str, taken: Iterable[str]) -> str:
    """`base`, or "<base> (YYYY-MM-DD HH:MM)" when another version of the same firmware is
    already called `base`, so a run of builds with the same describe text (no new commit,
    a fixed PROJECT_VER) does not produce a list of equal names."""
    return f"{base} ({minute(created_at)})" if base in set(taken) else base


def is_renamed(version: Version) -> bool:
    """The name differs from both default spellings (with and without the time). Both are
    computed from the stored fields, so no default name has to be stored."""
    base = base_name(version.git_describe, version.shown_app_version, version.created_at)
    return version.name not in (base, f"{base} ({minute(version.created_at)})")


def minute(created_at: str) -> str:
    """ "2026-11-26T10:05:09" -> "2026-11-26 10:05"."""
    return created_at[:16].replace("T", " ")


class VersionStore:
    """Versions over an open embr.sqlite connection (see storage.database)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        self._version = 0

    @property
    def version(self) -> int:
        """Goes up with every change made through this store, so a view can tell whether
        it has to rebuild. Changes made by another process are not counted."""
        return self._version

    def insert(self, draft: VersionDraft) -> Version:
        """Add a version with the next number of its firmware. Does not commit (see the
        module notes). Taking the number is the first statement, so the transaction holds
        the write lock from there on: another process creating a version of the same
        firmware waits and gets the following number."""
        (seq,) = self._db.execute(
            "INSERT INTO version_counters (firmware_id, last_seq) VALUES (?, 1)"
            " ON CONFLICT (firmware_id) DO UPDATE SET last_seq = last_seq + 1"
            " RETURNING last_seq",
            (draft.firmware_id,),
        ).fetchall()[0]
        name = draft.name.strip()
        if not name:
            base = base_name(
                draft.git_describe,
                draft.app_version if draft.params.use_app_version else None,
                draft.created_at,
            )
            name = default_name(base, draft.created_at, self._names(draft.firmware_id))
        version = Version(
            id=new_id(),
            firmware_id=draft.firmware_id,
            seq=int(seq),
            name=name,
            created_at=draft.created_at,
            origin=draft.origin,
            digest=draft.digest,
            note=draft.note,
            pinned=draft.pinned,
            app_version=draft.app_version,
            git_commit=draft.git_commit,
            git_describe=draft.git_describe,
            git_dirty=draft.git_dirty,
            chip=draft.chip,
            params=draft.params,
            build_path=draft.build_path,
            entries=draft.entries,
        )
        self._db.execute(
            f"INSERT INTO versions ({', '.join(_COLUMNS)})"
            f" VALUES ({', '.join('?' for _ in _COLUMNS)})",
            _row(version),
        )
        self._db.executemany(
            "INSERT INTO version_entries (version_id, position, name, kind, address, sha256,"
            " size, enabled, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (version.id, i, e.name, e.kind, e.address, e.sha256, e.size, e.enabled, e.note)
                for i, e in enumerate(version.entries)
            ],
        )
        self._version += 1
        return version

    def get(self, version_id: str) -> Version | None:
        found = self._select("WHERE id = ?", [version_id])
        return found[0] if found else None

    def for_firmware(self, firmware_id: str) -> list[Version]:
        """Versions of a firmware, newest (highest number) first."""
        return self._select("WHERE firmware_id = ? ORDER BY seq DESC", [firmware_id])

    def latest(self, firmware_id: str) -> Version | None:
        found = self._select("WHERE firmware_id = ? ORDER BY seq DESC LIMIT 1", [firmware_id])
        return found[0] if found else None

    def rename(self, version_id: str, name: str) -> Version:
        """Set the name; an empty (or blank) name brings back the default name, worked out
        against the names of the firmware's other versions. Raises KeyError for an unknown
        version."""
        with self._db:
            version = self.get(version_id)
            if version is None:
                raise KeyError(version_id)
            name = name.strip()
            if not name:
                base = base_name(
                    version.git_describe, version.shown_app_version, version.created_at
                )
                others = self._names(version.firmware_id, besides=version_id)
                name = default_name(base, version.created_at, others)
            self._db.execute("UPDATE versions SET name = ? WHERE id = ?", (name, version_id))
        self._version += 1
        return dataclasses.replace(version, name=name)

    def set_note(self, version_id: str, note: str) -> None:
        with self._db:
            self._db.execute("UPDATE versions SET note = ? WHERE id = ?", (note, version_id))
        self._version += 1

    def set_pinned(self, version_id: str, pinned: bool) -> None:
        with self._db:
            self._db.execute(
                "UPDATE versions SET pinned = ? WHERE id = ?", (int(pinned), version_id)
            )
        self._version += 1

    def delete(self, version_ids: Iterable[str]) -> set[str]:
        """Remove versions and their entries; the number counter is left as it is. Does not
        commit (see the module notes). Returns the sha256 of every file the removed versions
        referred to, for the caller to decide which archive files are no longer needed."""
        ids = sorted(set(version_ids))
        if not ids:
            return set()
        marks = ", ".join("?" for _ in ids)
        shas = {
            str(r[0])
            for r in self._db.execute(
                f"SELECT DISTINCT sha256 FROM version_entries WHERE version_id IN ({marks})", ids
            )
        }
        self._db.execute(f"DELETE FROM versions WHERE id IN ({marks})", ids)
        self._version += 1
        return shas

    def delete_firmware(self, firmware_id: str) -> set[str]:
        """Remove every version of a firmware and its number counter, so nothing of it is
        left once the firmware itself is deleted. Does not commit. Returns the sha256 of the
        files the removed versions referred to."""
        ids = [
            str(r[0])
            for r in self._db.execute(
                "SELECT id FROM versions WHERE firmware_id = ?", (firmware_id,)
            )
        ]
        shas = self.delete(ids)
        self._db.execute("DELETE FROM version_counters WHERE firmware_id = ?", (firmware_id,))
        self._version += 1
        return shas

    def referenced(self, *, besides: str | None = None) -> set[str]:
        """sha256 of every file referred to by any version; with `besides`, by any version
        of another firmware than that one."""
        rows = self._db.execute(
            "SELECT DISTINCT e.sha256 FROM version_entries e"
            " JOIN versions v ON v.id = e.version_id WHERE v.firmware_id IS NOT ?",
            (besides,),
        )
        return {str(r[0]) for r in rows}

    def flash_counts(self, firmware_id: str) -> dict[str, list[tuple[str, str]]]:
        """For each version of the firmware that was flashed successfully: (mac, timestamp)
        of each such flash, oldest first. Versions never flashed are absent."""
        rows = self._db.execute(
            "SELECT r.version_id, r.mac, r.timestamp FROM flash_records r"
            " JOIN versions v ON v.id = r.version_id"
            " WHERE v.firmware_id = ? AND r.operation = 'flash' AND r.result = 'ok'"
            " AND r.mac IS NOT NULL ORDER BY r.timestamp, r.rowid",
            (firmware_id,),
        )
        counts: dict[str, list[tuple[str, str]]] = {}
        for version_id, mac, timestamp in rows:
            counts.setdefault(str(version_id), []).append((str(mac), str(timestamp)))
        return counts

    def _names(self, firmware_id: str, *, besides: str = "") -> set[str]:
        rows = self._db.execute(
            "SELECT name FROM versions WHERE firmware_id = ? AND id != ?", (firmware_id, besides)
        )
        return {str(r[0]) for r in rows}

    def _select(self, clause: str, params: list[object]) -> list[Version]:
        """Two queries whatever the number of versions: the versions, then their entries."""
        rows = self._db.execute(f"SELECT {', '.join(_COLUMNS)} FROM versions {clause}", params)
        versions = [_version(dict(zip(_COLUMNS, row, strict=True))) for row in rows]
        if not versions:
            return []
        ids = [v.id for v in versions]
        marks = ", ".join("?" for _ in ids)
        entries: dict[str, list[VersionEntry]] = {}
        for row in self._db.execute(
            "SELECT version_id, name, kind, address, sha256, size, enabled, note"
            f" FROM version_entries WHERE version_id IN ({marks})"
            " ORDER BY version_id, position",
            ids,
        ):
            version_id, name, kind, address, sha256, size, enabled, note = row
            entries.setdefault(str(version_id), []).append(
                VersionEntry(
                    name=str(name),
                    kind=str(kind),
                    address=address if isinstance(address, int) else None,
                    sha256=str(sha256),
                    size=int(size),
                    enabled=bool(enabled),
                    note=str(note or ""),
                )
            )
        return [dataclasses.replace(v, entries=tuple(entries.get(v.id, ()))) for v in versions]


_COLUMNS = (
    "id",
    "firmware_id",
    "seq",
    "name",
    "created_at",
    "origin",
    "digest",
    "note",
    "pinned",
    "app_version",
    "git_commit",
    "git_describe",
    "git_dirty",
    "chip",
    "params",
    "build_path",
)


def _row(version: Version) -> tuple[object, ...]:
    p = version.params
    params = {
        "flash_mode": p.flash_mode,
        "flash_freq": p.flash_freq,
        "flash_size": p.flash_size,
        "before": p.before,
        "after": p.after,
        "stub": p.stub,
        "idf_version": p.idf_version,
        "use_app_version": p.use_app_version,
    }
    return (
        version.id,
        version.firmware_id,
        version.seq,
        version.name,
        version.created_at,
        version.origin.value,
        version.digest,
        version.note,
        int(version.pinned),
        version.app_version,
        version.git_commit,
        version.git_describe,
        None if version.git_dirty is None else int(version.git_dirty),
        version.chip,
        json.dumps(params, sort_keys=True),
        version.build_path,
    )


def _version(v: dict[str, object]) -> Version:
    return Version(
        id=str(v["id"]),
        firmware_id=str(v["firmware_id"]),
        seq=int(str(v["seq"])),
        name=str(v["name"]),
        created_at=str(v["created_at"]),
        origin=Origin(str(v["origin"])),
        digest=str(v["digest"]),
        note=str(v["note"] or ""),
        pinned=bool(v["pinned"]),
        app_version=_opt_str(v["app_version"]),
        git_commit=_opt_str(v["git_commit"]),
        git_describe=_opt_str(v["git_describe"]),
        git_dirty=None if v["git_dirty"] is None else bool(v["git_dirty"]),
        chip=_opt_str(v["chip"]),
        params=_params(str(v["params"])),
        build_path=str(v["build_path"] or ""),
    )


def _params(text: str) -> VersionParams:
    """Parse the params JSON; unknown keys are ignored, missing or mistyped ones take their
    default, so a row written by another Embr version still reads."""
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw: dict[str, object] = {str(k): v for k, v in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    default = VersionParams()

    def text_or(key: str, fallback: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else fallback

    def flag_or(key: str, fallback: bool) -> bool:
        value = raw.get(key)
        return value if isinstance(value, bool) else fallback

    idf = raw.get("idf_version")
    return VersionParams(
        flash_mode=text_or("flash_mode", default.flash_mode),
        flash_freq=text_or("flash_freq", default.flash_freq),
        flash_size=text_or("flash_size", default.flash_size),
        before=text_or("before", default.before),
        after=text_or("after", default.after),
        stub=flag_or("stub", default.stub),
        idf_version=idf if isinstance(idf, str) else None,
        use_app_version=flag_or("use_app_version", default.use_app_version),
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
