"""scripts/check_version.py (release workflow): a release tag must be v<major>.<minor>.<patch>
and name the version of both pyproject.toml and embr.__version__. main() runs on a
synthetic pyproject.toml (PYPROJECT and __version__ monkeypatched); the script is also run
as the workflow runs it, on the real repository."""

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from embr import __version__

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_version.py"


def _load() -> Any:
    """A fresh copy of the script module (it is not a package), so patches stay local."""
    spec = importlib.util.spec_from_file_location("check_version_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cv() -> Any:
    return _load()


def _source(
    cv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: str, package: str
) -> None:
    """pyproject.toml with version `project`; embr.__version__ as seen by the script =
    `package`."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f'[project]\nname = "embr"\nversion = "{project}"\n\n[tool.other]\nversion = "9.9.9"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cv, "PYPROJECT", pyproject)
    monkeypatch.setattr(cv, "__version__", package)


def _run(
    cv: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["check_version.py", *args])
    code = cv.main()
    return code, capsys.readouterr().out


@pytest.mark.parametrize("args", [(), ("v1.2.3", "v1.2.3")], ids=["none", "two"])
def test_usage_on_a_wrong_argument_count(
    cv: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], args: Any
) -> None:
    code, out = _run(cv, monkeypatch, capsys, *args)
    assert code == 2
    assert out.startswith("usage: check_version.py <tag>")
    assert "ok:" not in out


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "V1.2.3",
        "vv1.2.3",
        "v1.2",
        "v1",
        "v1.2.3.4",
        "v1.2.3-rc1",
        "v1.2.3rc1",
        "v1.2.x",
        "v1..3",
        "v1.2.3 ",
        " v1.2.3",
        "v1.2.3\n",
        "refs/tags/v1.2.3",
        "release-v1.2.3",
        "v1-2-3",
        "",
    ],
)
def test_refuses_a_tag_that_is_not_v_major_minor_patch(
    cv: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tag: str,
) -> None:
    _source(cv, tmp_path, monkeypatch, "1.2.3", "1.2.3")
    code, out = _run(cv, monkeypatch, capsys, tag)
    assert code == 1
    assert out == f"FAIL: tag {tag!r} is not v<major>.<minor>.<patch>\n"


@pytest.mark.parametrize(
    ("project", "package", "named"),
    [
        ("1.2.4", "1.2.4", ["pyproject.toml is 1.2.4", "embr.__version__ is 1.2.4"]),
        ("1.2.4", "1.2.3", ["pyproject.toml is 1.2.4"]),
        ("1.2.3", "1.2.4", ["embr.__version__ is 1.2.4"]),
        ("1.2.30", "1.2.3", ["pyproject.toml is 1.2.30"]),
        ("1.2.3", "01.2.3", ["embr.__version__ is 01.2.3"]),
    ],
    ids=["both", "pyproject", "package", "pyproject-longer", "package-leading-zero"],
)
def test_refuses_a_tag_that_does_not_match_the_source(
    cv: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: str,
    package: str,
    named: list[str],
) -> None:
    _source(cv, tmp_path, monkeypatch, project, package)
    code, out = _run(cv, monkeypatch, capsys, "v1.2.3")
    assert code == 1
    assert out.startswith("FAIL: tag v1.2.3 does not match the source: ")
    assert out.count("\n") == 1
    for problem in named:
        assert problem in out
    for where in {"pyproject.toml", "embr.__version__"} - {n.split()[0] for n in named}:
        assert where not in out, f"{where} matches the tag but is named"
    assert "ok:" not in out


def test_accepts_the_matching_tag(
    cv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _source(cv, tmp_path, monkeypatch, "12.0.345", "12.0.345")
    code, out = _run(cv, monkeypatch, capsys, "v12.0.345")
    assert code == 0
    assert out == "ok: tag v12.0.345 matches version 12.0.345\n"


def test_reads_the_repository_pyproject(cv: Any) -> None:
    assert cv.PYPROJECT == REPO / "pyproject.toml"
    with cv.PYPROJECT.open("rb") as file:
        assert tomllib.load(file)["project"]["version"] == __version__
    assert cv.__version__ == __version__


def _script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_script_on_the_repository() -> None:
    ok = _script(f"v{__version__}")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ok.stdout == f"ok: tag v{__version__} matches version {__version__}\n"

    major, minor, patch = (int(p) for p in __version__.split("."))
    other = _script(f"v{major}.{minor}.{patch + 1}")
    assert other.returncode == 1
    assert other.stdout.startswith(f"FAIL: tag v{major}.{minor}.{patch + 1} does not match")
    assert "pyproject.toml" in other.stdout and "embr.__version__" in other.stdout

    bare = _script(__version__)
    assert bare.returncode == 1
    assert bare.stdout.startswith(f"FAIL: tag {__version__!r} is not v<major>")

    usage = _script()
    assert usage.returncode == 2
    assert usage.stdout.startswith("usage:")
