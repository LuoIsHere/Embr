"""core/archive: content-addressed copies with name records (design doc §3.4 "归档")."""

import datetime
import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from embr.core.archive import Archive, ArchiveFile
from embr.core.storage.database import open_database

START = datetime.datetime(2026, 1, 2, 3, 4, 5)


class Tick:
    """Clock that moves one second per call."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> datetime.datetime:
        self.calls += 1
        return START + datetime.timedelta(seconds=self.calls - 1)


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    directory = tmp_path / "archive"
    directory.mkdir()
    connection = open_database(tmp_path / "embr.sqlite", tmp_path / "backup")
    yield Archive(directory, connection, clock=Tick())
    connection.close()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_add_copies_and_leaves_the_source_alone(archive: Archive, tmp_path: Path) -> None:
    data = b"\xe9" + b"\x01" * 255
    source = tmp_path / "src" / "app.bin"
    source.parent.mkdir()
    source.write_bytes(data)
    mtime = source.stat().st_mtime_ns

    record = archive.add(
        source, original_name="app.bin", original_path=str(source), note="v1", source_firmware="fw"
    )

    sha = _sha(data)
    assert (record.sha256, record.original_name, record.original_path) == (
        sha,
        "app.bin",
        str(source),
    )
    assert (record.note, record.source_firmware, record.archived_at) == (
        "v1",
        "fw",
        "2026-01-02T03:04:05",
    )
    assert source.read_bytes() == data and source.stat().st_mtime_ns == mtime
    assert archive.path(sha) == archive.directory / f"{sha}.bin"
    assert archive.has(sha) and archive.path(sha).read_bytes() == data
    assert archive.files() == [ArchiveFile(sha, len(data), "2026-01-02T03:04:05")]
    with pytest.raises(OSError):
        archive.add(tmp_path / "missing.bin", original_name="missing.bin")


def test_same_content_is_stored_once_with_every_name(archive: Archive, tmp_path: Path) -> None:
    data = b"\x02" * 300
    source = tmp_path / "app.bin"
    source.write_bytes(data)

    first = archive.add(source, original_name="app.bin")
    second = archive.add(data, original_name="copy.bin", note="from a dump")

    sha = _sha(data)
    assert first.sha256 == second.sha256 == sha and first.id != second.id
    assert [f.sha256 for f in archive.files()] == [sha]
    assert [(n.original_name, n.note) for n in archive.names(sha)] == [
        ("app.bin", ""),
        ("copy.bin", "from a dump"),
    ]
    assert [p.name for p in archive.directory.iterdir()] == [f"{sha}.bin"]  # no temp files


def test_unreferenced_and_delete(archive: Archive) -> None:
    kept = archive.add(b"kept", original_name="kept.bin")
    dropped = archive.add(b"dropped", original_name="dropped.bin")

    assert [f.sha256 for f in archive.unreferenced([kept.sha256])] == [dropped.sha256]
    archive.delete([dropped.sha256, "0" * 64])

    assert not archive.has(dropped.sha256) and not archive.path(dropped.sha256).exists()
    assert [f.sha256 for f in archive.files()] == [kept.sha256]
    assert archive.names(dropped.sha256) == []
    assert archive.has(kept.sha256)
