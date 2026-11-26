"""Read-only git queries about a project folder.

Only reads: nothing here changes a repository, its index or its configuration. Every failure
(git not installed, not a repository, no commit yet, too slow, unexpected output) gives None,
because the git state is extra information and its absence must never stop the caller.
"""

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 5.0
"""Seconds for all the queries of one read_state() call together."""

# Variables that would point git at another repository than the folder given with -C, when
# Embr itself is started from a git hook or a shell that set them.
_REPOSITORY_OVERRIDES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


@dataclass(frozen=True)
class GitState:
    """The repository state at the moment of the query, not at build time."""

    commit: str
    """Full hash of HEAD."""
    describe: str
    """`git describe --always --tags --dirty`, e.g. "v0.3.1-4-gabc1234-dirty"."""
    dirty: bool
    """Tracked files differ from HEAD. Untracked files do not count."""


def read_state(folder: Path, *, timeout: float = DEFAULT_TIMEOUT) -> GitState | None:
    """HEAD and `describe` of the repository containing `folder`, or None when git is not
    installed, `folder` is not in a repository with a commit, git fails or does not answer
    within `timeout` seconds in total. Blocks for up to `timeout`; call it off the GUI thread.
    """
    deadline = time.monotonic() + timeout
    commit = _git(folder, ["rev-parse", "HEAD"], deadline)
    if commit is None:
        return None
    describe = _git(folder, ["describe", "--always", "--tags", "--dirty"], deadline)
    if describe is None:
        return None
    return GitState(commit=commit, describe=describe, dirty=describe.endswith("-dirty"))


def _git(folder: Path, args: list[str], deadline: float) -> str | None:
    """Output of one git command (stripped), or None on any failure or empty output."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    env = {k: v for k, v in os.environ.items() if k not in _REPOSITORY_OVERRIDES}
    # GIT_OPTIONAL_LOCKS=0: `describe --dirty` would otherwise refresh the index and take
    # index.lock, which can make the user's own git command in that repository fail.
    env |= {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=remaining,
            env=env,
            creationflags=flags,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):  # not installed, cannot start, too slow
        return None
    if result.returncode != 0:
        return None
    try:
        text = result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return text or None
