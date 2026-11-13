"""core/firmware/filecache: stat-only fingerprints and a hash cache keyed by size and mtime."""

import hashlib
import os
from pathlib import Path

import pytest
from embr_testkit import write_newer

from embr.core.firmware.filecache import FileCache, FileStamp, fingerprint


def test_fingerprint_stats_existing_and_missing_files(tmp_path: Path) -> None:
    present = tmp_path / "app.bin"
    present.write_bytes(b"\x01" * 64)
    missing = tmp_path / "missing.bin"
    folder = tmp_path / "build"
    folder.mkdir()
    os.utime(folder, ns=(10**9, 10**9))  # older than the file
    st = present.stat()

    found = fingerprint([present, missing, present, folder])
    assert found.stamps == (
        FileStamp(str(present), st.st_size, st.st_mtime_ns),
        FileStamp(str(missing), None, None),
        FileStamp(str(folder), None, 10**9),  # a folder: its time only
    )
    assert found.newest_mtime_ns == st.st_mtime_ns
    assert fingerprint([missing]).newest_mtime_ns is None


def test_a_file_added_to_a_folder_changes_its_stamp(tmp_path: Path) -> None:
    folder = tmp_path / "build"
    folder.mkdir()
    os.utime(folder, ns=(10**9, 10**9))
    before = fingerprint([folder])
    (folder / "renamed.bin").write_bytes(b"\x01")
    assert fingerprint([folder]) != before


def test_file_is_hashed_again_only_after_its_stat_changed(tmp_path: Path) -> None:
    path = tmp_path / "app.bin"
    first = b"\x01" * 100
    write_newer(path, first)
    cache = FileCache()

    assert cache.sha256(path) == hashlib.sha256(first).hexdigest()
    assert (cache.size(path), cache.head(path, 4), cache.read(path)) == (100, first[:4], first)
    assert cache.sha256(path) == hashlib.sha256(first).hexdigest()
    assert cache.hashed == 1

    second = b"\x02" * 100  # same size, newer mtime
    write_newer(path, second)
    assert cache.sha256(path) == hashlib.sha256(second).hexdigest()
    assert cache.hashed == 2

    third = b"\x03" * 50  # other size, same mtime
    st = path.stat()
    path.write_bytes(third)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert cache.sha256(path) == hashlib.sha256(third).hexdigest()
    assert cache.hashed == 3

    cache.forget(path)
    cache.sha256(path)
    assert cache.hashed == 4

    path.unlink()
    with pytest.raises(FileNotFoundError):
        cache.sha256(path)


def test_a_file_written_while_it_is_read_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "app.bin"
    first, second = b"\x01" * 100, b"\x02" * 100  # same size: only the stats tell
    write_newer(path, first)
    real = Path.read_bytes
    pending = [second]

    def read_then_written(self: Path) -> bytes:
        data = real(self)
        if self == path and pending:
            write_newer(path, pending.pop())  # a build writing on
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_written)
    cache = FileCache()
    # hash and size from the same bytes, not from a later stat
    assert cache.digest(path) == (hashlib.sha256(first).hexdigest(), 100)
    assert not cache.agrees(fingerprint([path]).stamps[0])  # a result built on it is stale

    assert cache.digest(path) == (hashlib.sha256(second).hexdigest(), 100)  # read again
    assert cache.agrees(fingerprint([path]).stamps[0])
    assert cache.hashed == 2


def test_agrees_with_files_it_holds_nothing_for(tmp_path: Path) -> None:
    path = tmp_path / "app.bin"
    write_newer(path, b"\x01" * 10)
    cache = FileCache()
    assert cache.agrees(fingerprint([path]).stamps[0])
    loaded = cache.load(path)
    assert (loaded.sha256, loaded.size, loaded.data) == (
        hashlib.sha256(b"\x01" * 10).hexdigest(),
        10,
        b"\x01" * 10,
    )
    assert cache.agrees(fingerprint([path]).stamps[0])
    write_newer(path, b"\x02" * 10)
    assert not cache.agrees(fingerprint([path]).stamps[0])
