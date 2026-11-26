"""core/vcs: read-only git state of a project folder."""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from embr.core import vcs
from embr.core.vcs import GitState, read_state

HAS_GIT = shutil.which("git") is not None


def _completed(stdout: bytes, returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess([], returncode, stdout, b"")


def test_git_not_installed_gives_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(vcs.subprocess, "run", missing)
    assert read_state(tmp_path) is None


def test_a_slow_git_gives_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired("git", kwargs["timeout"])

    monkeypatch.setattr(vcs.subprocess, "run", slow)
    assert read_state(tmp_path) is None


@pytest.mark.parametrize(
    "outputs",
    [
        [_completed(b"", 128)],  # not a repository, or no commit yet
        [_completed(b"a" * 40 + b"\n"), _completed(b"", 128)],
        [_completed(b"\xff\xfe\n")],  # not text
        [_completed(b"\n")],  # nothing
    ],
    ids=["rev-parse-fails", "describe-fails", "not-text", "empty"],
)
def test_a_failing_git_gives_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outputs: list[subprocess.CompletedProcess[bytes]],
) -> None:
    answers = iter(outputs)

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return next(answers)

    monkeypatch.setattr(vcs.subprocess, "run", run)
    assert read_state(tmp_path) is None


def test_queries_take_no_lock_and_ignore_an_inherited_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str], float]] = []
    answers = iter([_completed(b"a" * 40 + b"\n"), _completed(b"v1.0-dirty\n")])

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, kwargs["env"], kwargs["timeout"]))
        return next(answers)

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(vcs.subprocess, "run", run)
    state = read_state(tmp_path, timeout=3)

    assert state == GitState(commit="a" * 40, describe="v1.0-dirty", dirty=True)
    assert [c[0][3:] for c in calls] == [
        ["rev-parse", "HEAD"],
        ["describe", "--always", "--tags", "--dirty"],
    ]
    assert all(c[0][:3] == ["git", "-C", str(tmp_path)] for c in calls)
    for _, env, timeout in calls:
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert "GIT_DIR" not in env
        assert 0 < timeout <= 3  # both share one deadline


def test_no_time_left_gives_none_without_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("git started")

    monkeypatch.setattr(vcs.subprocess, "run", never)
    assert read_state(tmp_path, timeout=0) is None


# --- with a real git -----------------------------------------------------------------------


@pytest.fixture
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No user or system git configuration; a fixed identity for commits."""
    if not HAS_GIT:
        pytest.skip("git is not installed")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "test@example.invalid")


def _git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)


@pytest.mark.usefixtures("isolated_git")
def test_real_repository(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    (repo / "main").mkdir(parents=True)
    tracked = repo / "main" / "app.c"
    tracked.write_text("int x;\n", encoding="utf-8")

    _git(repo, "init", "-q")
    assert read_state(repo) is None  # no commit yet
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")

    clean = read_state(repo / "main")  # any folder inside the repository
    assert clean is not None
    assert len(clean.commit) == 40 and not clean.dirty
    assert clean.describe == clean.commit[: len(clean.describe)]  # no tag: short hash

    (repo / "notes.txt").write_text("untracked", encoding="utf-8")
    untracked_only = read_state(repo)
    assert untracked_only is not None and not untracked_only.dirty

    _git(repo, "tag", "v1.0")
    tracked.write_text("int y;\n", encoding="utf-8")
    dirty = read_state(repo)
    assert dirty == GitState(commit=clean.commit, describe="v1.0-dirty", dirty=True)
    assert not (repo / ".git" / "index.lock").exists()


@pytest.mark.usefixtures("isolated_git")
def test_a_folder_outside_any_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    # keeps git from finding a repository above tmp_path
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert read_state(folder) is None
