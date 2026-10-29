"""core/firmware/filecache: stat-only fingerprints and a hash cache keyed by size and mtime
(design doc §3.4 "检测时机")."""

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
    st = present.stat()

    found = fingerprint([present, missing, present, tmp_path])
    assert found.stamps == (
        FileStamp(str(present), st.st_size, st.st_mtime_ns),
        FileStamp(str(missing), None, None),
        FileStamp(str(tmp_path), None, None),  # not a regular file
    )
    assert found.newest_mtime_ns == st.st_mtime_ns
    assert fingerprint([missing]).newest_mtime_ns is None


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
