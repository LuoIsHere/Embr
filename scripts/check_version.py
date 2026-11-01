"""Check that a release tag matches the version of the source (release workflow).

    uv run python scripts/check_version.py v0.1.0

The tag must be v<major>.<minor>.<patch>, and <major>.<minor>.<patch> must equal both the
version in pyproject.toml and embr.__version__.
"""

import re
import sys
import tomllib
from pathlib import Path

from embr import __version__

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
TAG = re.compile(r"v(\d+\.\d+\.\d+)")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_version.py <tag>")
        return 2
    tag = sys.argv[1]
    match = TAG.fullmatch(tag)
    if match is None:
        print(f"FAIL: tag {tag!r} is not v<major>.<minor>.<patch>")
        return 1
    with PYPROJECT.open("rb") as file:
        project = tomllib.load(file)["project"]["version"]
    problems = [
        f"{where} is {value}"
        for where, value in (("pyproject.toml", project), ("embr.__version__", __version__))
        if value != match.group(1)
    ]
    if problems:
        print(f"FAIL: tag {tag} does not match the source: " + "; ".join(problems))
        return 1
    print(f"ok: tag {tag} matches version {project}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
