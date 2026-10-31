"""THIRD_PARTY_NOTICES against the installed runtime closure of embr (the distributions
embr's Requires-Dist pulls in on this platform, recursively): a dependency bump, a new
dependency or a dropped one without a notices update fails here, before packaging."""

import importlib.metadata as metadata
import re
import sys
from pathlib import Path

from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parents[1]
NOTICES = REPO / "THIRD_PARTY_NOTICES"
BUNDLED = "Bundled in the release package"
OTHER = "Other dependencies of esptool"
ASSETS = "Bundled assets"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sections() -> dict[str, list[str]]:
    """Heading -> its lines. A heading is underlined with dashes and may wrap: it is the
    lines between the previous blank line and the dashes, joined with spaces."""
    lines = NOTICES.read_text(encoding="utf-8").splitlines()
    found: dict[str, list[str]] = {}
    current: list[str] = []
    for line in lines:
        if re.fullmatch(r"-{10,}", line):
            start = len(current)
            while start > 0 and current[start - 1].strip():
                start -= 1
            heading = " ".join(current[start:])
            del current[start:]
            current = found.setdefault(heading, [])
            continue
        current.append(line)
    return found


def _section(start: str) -> list[str]:
    sections = _sections()
    matches = [heading for heading in sections if heading.startswith(start)]
    assert len(matches) == 1, f"no single section starting {start!r}: {list(sections)}"
    return sections[matches[0]]


def _entries(lines: list[str]) -> dict[str, tuple[str, str]]:
    """normalized name -> (name, version) of each line that starts with a name and a
    version (a word beginning with a digit)."""
    found: dict[str, tuple[str, str]] = {}
    for line in lines:
        match = re.match(r"^(\S+)[ \t]+(\d\S*)", line)
        if match:
            found[_normalize(match[1])] = (match[1], match[2])
    return found


def _runtime_closure() -> dict[str, metadata.Distribution]:
    """normalized name -> installed distribution, for everything embr needs at run time
    here (markers evaluated for this interpreter, no extras), embr itself excluded."""
    found: dict[str, metadata.Distribution] = {}
    pending = list(metadata.requires("embr") or [])
    while pending:
        requirement = Requirement(pending.pop())
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        key = _normalize(requirement.name)
        if key in found:
            continue
        dist = metadata.distribution(requirement.name)
        found[key] = dist
        pending.extend(dist.requires or [])
    return found


def test_sections_are_there() -> None:
    sections = _sections()
    for start in (BUNDLED, OTHER, ASSETS):
        assert any(h.startswith(start) for h in sections), start


def _version_problems(listed: dict[str, tuple[str, str]]) -> list[str]:
    problems: list[str] = []
    for name, version in listed.values():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name} {version} listed, not installed")
            continue
        if installed != version:
            problems.append(f"{name} {version} listed, {installed} installed")
    return problems


def test_bundled_versions_match_the_installed_ones() -> None:
    """Some bundled packages are no Requires-Dist of anything on this Python (e.g.
    typing_extensions: PyInstaller follows its conditional imports), so every listed one
    is checked against what is installed, not only the runtime closure."""
    listed = _entries(_section(BUNDLED))
    python = listed.pop("python")
    assert python[1] == f"{sys.version_info.major}.{sys.version_info.minor}", python
    assert listed, "no bundled distribution listed"
    assert _version_problems(listed) == []


def test_other_dependency_versions_match_the_installed_ones() -> None:
    listed = _entries(_section(OTHER))
    assert listed, "no other dependency listed"
    assert _version_problems(listed) == []


def test_every_runtime_dependency_is_listed_once() -> None:
    bundled = _entries(_section(BUNDLED))
    other = _entries(_section(OTHER))
    assert set(bundled) & set(other) == set(), "listed in both sections"
    closure = _runtime_closure()
    assert closure, "embr has no runtime dependencies installed?"
    missing = sorted(
        f"{d.name} {d.version}" for k, d in closure.items() if k not in bundled and k not in other
    )
    assert missing == [], "installed with embr but not in THIRD_PARTY_NOTICES"


def test_other_dependencies_are_runtime_dependencies() -> None:
    """That section says these are installed when Embr runs from source."""
    closure = _runtime_closure()
    listed = _entries(_section(OTHER))
    stale = sorted(f"{n} {v}" for k, (n, v) in listed.items() if k not in closure)
    assert stale == [], "listed as a dependency of esptool but not installed with embr"
