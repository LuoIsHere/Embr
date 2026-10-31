"""embr.build.build_time() (design doc §12 item 22): the packaged build carries
embr/resources/build.txt, written by Embr.spec at packaging time; a source checkout has
none, and About then says "development build".

The present / empty cases run build_time() in a child Python on a minimal copy of the
embr package (embr/__init__.py, embr/build.py, embr/resources/), so the real lookup
through importlib.resources is exercised, not a patched one."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import embr
from embr.build import build_time

SRC_EMBR = Path(embr.__file__).resolve().parent


def _copy_package(root: Path) -> Path:
    """root/embr with __init__.py, build.py and an empty resources/ (a namespace package,
    as in the source tree); returns the resources folder."""
    package = root / "embr"
    package.mkdir(parents=True)
    for name in ("__init__.py", "build.py"):
        shutil.copy2(SRC_EMBR / name, package / name)
    resources = package / "resources"
    resources.mkdir()
    return resources


def _build_time_in(root: Path) -> str | None:
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import embr.build; "
        "assert embr.build.__file__.startswith(sys.argv[1]), embr.build.__file__; "
        "print(repr(embr.build.build_time()))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", script, str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    shown = done.stdout.strip()
    return None if shown == "None" else shown.strip("'")


def test_source_checkout_has_no_build_file() -> None:
    assert not (SRC_EMBR / "resources" / "build.txt").exists()
    assert build_time() is None


def test_absent_build_file_gives_none(tmp_path: Path) -> None:
    _copy_package(tmp_path)
    assert _build_time_in(tmp_path) is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("2026-11-01 08:12 UTC\n", "2026-11-01 08:12 UTC"),
        ("2026-11-01 08:12 UTC", "2026-11-01 08:12 UTC"),
        ("  2026-11-01 08:12 UTC \r\n\r\n", "2026-11-01 08:12 UTC"),
    ],
)
def test_present_build_file_gives_its_stripped_text(
    tmp_path: Path, content: str, expected: str
) -> None:
    resources = _copy_package(tmp_path)
    (resources / "build.txt").write_bytes(content.encode("utf-8"))
    assert _build_time_in(tmp_path) == expected


@pytest.mark.parametrize("content", ["", "\n", "  \r\n\t "])
def test_empty_build_file_gives_none(tmp_path: Path, content: str) -> None:
    resources = _copy_package(tmp_path)
    (resources / "build.txt").write_bytes(content.encode("utf-8"))
    assert _build_time_in(tmp_path) is None
