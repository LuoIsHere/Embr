"""scripts/package_release.py (design doc §7.5): the release zip and its notices, checked
without a PyInstaller build. Distributions are synthetic .dist-info folders put on
sys.path; main() runs with --skip-build on a synthetic repository (its module paths
monkeypatched). The real build is covered by tests/release/test_release_package.py."""

import hashlib
import importlib.metadata as metadata
import importlib.util
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import esptool
import pytest

from embr import __version__

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "package_release.py"
RULE = "=" * 78
NAME = f"Embr-{__version__}"
ZIP_NAME = f"{NAME}-windows-x64.zip"
QT_STATEMENT = "GNU General Public License, version 3"


def _load() -> Any:
    """A fresh copy of the script module (it is not a package), so patches stay local."""
    spec = importlib.util.spec_from_file_location("package_release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pr() -> Any:
    return _load()


@dataclass(frozen=True)
class _Stub:
    """What is_listed() reads of a distribution."""

    name: str
    version: str


def _dist(
    site: Path,
    name: str,
    version: str,
    files: dict[str, str] | None = None,
    *,
    top_level: tuple[str, ...] = (),
    record: bool = True,
) -> metadata.Distribution:
    """A .dist-info folder in `site` with METADATA, RECORD listing `files` (paths relative
    to `site`, written with their text) and top_level.txt."""
    info = site / f"{re.sub(r'[-.]', '_', name)}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    listed = [f"{info.name}/METADATA,,"]
    for rel, text in (files or {}).items():
        path = site / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        listed.append(f"{rel},,")
    if top_level:
        (info / "top_level.txt").write_text("\n".join(top_level) + "\n", encoding="utf-8")
        listed.append(f"{info.name}/top_level.txt,,")
    if record:
        listed.append(f"{info.name}/RECORD,,")
        (info / "RECORD").write_text("\n".join(listed) + "\n", encoding="utf-8")
    return metadata.PathDistribution(info)


def _toc(path: Path, modules: list[str]) -> None:
    """A PYZ-00.toc as PyInstaller writes it: (pyz path, [(module, source, kind), ...])."""
    entries = [(m, f"C:\\src\\{m.replace('.', '_')}.py", "PYMODULE") for m in modules]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(repr((str(path.with_suffix(".pyz")), entries)), encoding="utf-8")


def _on_path(monkeypatch: pytest.MonkeyPatch, site: Path) -> None:
    """Put `site` first on sys.path for this test (importlib.metadata searches sys.path)."""
    monkeypatch.setattr(sys, "path", [str(site), *sys.path])
    importlib.invalidate_caches()


def _sections(text: str) -> list[tuple[str, str]]:
    """(title, body) of each full-text section after the "Full license texts" heading."""
    head = "Full license texts\n------------------\n\n"
    assert text.count(head) == 1, "no single 'Full license texts' heading"
    rest = text.split(head, 1)[1]
    parts = re.split(rf"^{RULE}\n(.+)\n{RULE}\n", rest, flags=re.MULTILINE)
    assert parts[0] == "", f"text before the first section: {parts[0][:200]!r}"
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts), 2)]


# ---- normalize / is_listed ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("PySide6-Essentials", "pyside6-essentials"),
        ("PySide6_Essentials", "pyside6-essentials"),
        ("pyside6.essentials", "pyside6-essentials"),
        ("typing__extensions", "typing-extensions"),
        ("a-_.b", "a-b"),
        ("tomli_w", "tomli-w"),
        ("Pygments", "pygments"),
    ],
)
def test_normalize(pr: Any, name: str, expected: str) -> None:
    assert pr.normalize(name) == expected


NOTICES = """Header line mentioning click 8.5.0 in the middle
Bundled in the release package
------------------------------
PySide6-Essentials 6.12.0  LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; used
                           under GPL-3.0-only
typing_extensions 4.16.0   PSF-2.0
tomli-w 1.2.0              MIT
\trich 15.0.0             MIT
    Pygments 2.21.0        BSD-2-Clause
see colorama 0.4.6
markdown-it-py\t4.2.0
"""


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("PySide6-Essentials", "6.12.0"),
        ("PySide6_Essentials", "6.12.0"),
        ("pyside6.essentials", "6.12.0"),
        ("typing_extensions", "4.16.0"),
        ("typing-extensions", "4.16.0"),
        ("Typing.Extensions", "4.16.0"),
        ("tomli_w", "1.2.0"),
        ("markdown_it_py", "4.2.0"),
    ],
)
def test_is_listed_accepts_any_spelling_of_the_name(pr: Any, name: str, version: str) -> None:
    assert pr.is_listed(NOTICES, _Stub(name, version))


@pytest.mark.parametrize(
    ("name", "version", "why"),
    [
        ("PySide6-Essentials", "6.12.1", "other version"),
        ("PySide6-Essentials", "6.12", "version prefix"),
        ("typing_extensions", "4.16.0.post1", "longer version"),
        ("tomli", "1.2.0", "name prefix"),
        ("tomli-w-x", "1.2.0", "longer name"),
        ("shiboken6", "6.12.0", "not listed at all"),
        ("rich", "15.0.0", "indented with a tab"),
        ("Pygments", "2.21.0", "indented with spaces"),
        ("click", "8.5.0", "named in the middle of a line"),
        ("colorama", "0.4.6", "second word of a line"),
        ("under", "GPL-3.0-only", "continuation line"),
    ],
)
def test_is_listed_needs_the_name_at_line_start_and_the_exact_version(
    pr: Any, name: str, version: str, why: str
) -> None:
    assert not pr.is_listed(NOTICES, _Stub(name, version)), why


def test_is_listed_with_a_real_distribution(pr: Any) -> None:
    dist = metadata.distribution("esptool")
    assert pr.is_listed(f"esptool {dist.version}  GPL-2.0-or-later\n", dist)
    assert not pr.is_listed("esptool 0.0.1  GPL-2.0-or-later\n", dist)


# ---- bundled_distributions ----------------------------------------------------------------


def test_bundled_distributions_from_the_analysis(
    pr: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    _dist(site, "embrtest-foo", "1.0", top_level=("embrtestfoo",))
    _dist(site, "Embrtest_Ns_A", "2.0", top_level=("embrtestns",))
    _dist(site, "embrtest.ns.b", "3.0", top_level=("embrtestns",))
    _dist(site, "embrtest-unused", "4.0", top_level=("embrtestunused",))
    _dist(site, "embr", "9.9.9", top_level=("embr",))  # as a non-editable install maps it
    _on_path(monkeypatch, site)
    toc = tmp_path / "build" / "Embr" / "PYZ-00.toc"
    _toc(
        toc,
        [
            "embrtestfoo",
            "embrtestfoo.sub.deep",
            "embrtestns.part",
            "embr",
            "embr.build",
            "embr.ui.dialogs",
            "serial.tools.list_ports",
            "json",
            "json.decoder",
            "no_such_module_anywhere",
        ],
    )
    monkeypatch.setattr(pr, "ANALYSIS", toc)
    found = pr.bundled_distributions()
    names = [d.name for d in found]
    assert names == ["embrtest-foo", "Embrtest_Ns_A", "embrtest.ns.b", "pyserial"]
    assert all(isinstance(d, metadata.Distribution) for d in found)
    assert [d.version for d in found[:3]] == ["1.0", "2.0", "3.0"]


def test_bundled_distributions_is_sorted_by_normalized_name(
    pr: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    _dist(site, "Embrtest_Zeta", "1", top_level=("embrtestzeta",))
    _dist(site, "embrtest-alpha", "1", top_level=("embrtestalpha",))
    _dist(site, "EMBRTEST.Mid", "1", top_level=("embrtestmid",))
    _on_path(monkeypatch, site)
    toc = tmp_path / "PYZ-00.toc"
    _toc(toc, ["embrtestzeta", "embrtestmid.x", "embrtestalpha"])
    monkeypatch.setattr(pr, "ANALYSIS", toc)
    assert [d.name for d in pr.bundled_distributions()] == [
        "embrtest-alpha",
        "EMBRTEST.Mid",
        "Embrtest_Zeta",
    ]


# ---- license_texts ------------------------------------------------------------------------


@pytest.fixture
def supplied(pr: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """LICENSES and FONT_LICENSE pointed at sentinel texts."""
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    (licenses / "pyserial.txt").write_text("PYSERIAL SUPPLIED TEXT\n", encoding="utf-8")
    (licenses / "lucide.txt").write_text("LUCIDE SUPPLIED TEXT\n", encoding="utf-8")
    font = tmp_path / "LICENSE_Fonts"
    font.write_text("FONT LICENSE TEXT\n", encoding="utf-8")
    monkeypatch.setattr(pr, "LICENSES", licenses)
    monkeypatch.setattr(pr, "FONT_LICENSE", font)
    return licenses


def _bundle(site: Path) -> list[metadata.Distribution]:
    foo = _dist(
        site,
        "embrtest-foo",
        "1.0",
        {
            "embrtest_foo-1.0.dist-info/licenses/LICENSE": "FOO LICENSE TEXT\n",
            "embrtest_foo-1.0.dist-info/licenses/NOTICE": "FOO NOTICE TEXT\n",
            "embrtest_foo-1.0.dist-info/licenses/LicenseRef-Commercial.txt": "FOO COMMERCIAL\n",
            "embrtestfoo/vendored/COPYING": "FOO VENDORED COPYING\n",
            "embrtestfoo/__init__.py": "NOT A LICENSE\n",
        },
    )
    qt = _dist(
        site,
        "PySide6_Essentials",
        "6.12.0",
        {
            "pyside6_essentials-6.12.0.dist-info/licenses/LicenseRef-Qt-Commercial.txt": "QT C\n",
            "pyside6_essentials-6.12.0.dist-info/licenses/LICENSE": "QT METADATA LICENSE\n",
        },
    )
    shiboken = _dist(
        site,
        "shiboken6",
        "6.12.0",
        {"shiboken6-6.12.0.dist-info/licenses/LICENSE": "SHIBOKEN METADATA LICENSE\n"},
    )
    serial = _dist(site, "pyserial", "3.5", {"serial/__init__.py": "x = 1\n"})
    return [foo, qt, serial, shiboken]


def test_license_texts_sections(pr: Any, supplied: Path, tmp_path: Path) -> None:
    text = pr.license_texts(_bundle(tmp_path / "site"))
    sections = _sections(text)
    titles = [t for t, _ in sections]
    assert titles[0].startswith(f"Python {sys.version.split()[0]}")
    assert titles[1:] == [
        "embrtest-foo 1.0",
        "PySide6_Essentials 6.12.0",
        "pyserial 3.5",
        "shiboken6 6.12.0",
        "HarmonyOS Sans SC (Regular, Bold)",
        "Lucide icons",
    ]
    body = dict(sections)
    python_license = (Path(sys.base_prefix) / "LICENSE.txt").read_text(encoding="utf-8")
    assert body[titles[0]] == python_license.strip()

    foo = body["embrtest-foo 1.0"]
    for wanted in ("FOO LICENSE TEXT", "FOO NOTICE TEXT", "FOO VENDORED COPYING"):
        assert wanted in foo
    assert "FOO COMMERCIAL" not in foo, "a commercial license file was included"
    assert "NOT A LICENSE" not in foo

    for title, metadata_text in (
        ("PySide6_Essentials 6.12.0", "QT METADATA LICENSE"),
        ("shiboken6 6.12.0", "SHIBOKEN METADATA LICENSE"),
    ):
        assert QT_STATEMENT in body[title]
        assert "LICENSE" in body[title]  # "the full text is in LICENSE"
        assert metadata_text not in body[title]
        assert "QT C" not in body[title]
    assert "licenses-used-in-qt" in body["PySide6_Essentials 6.12.0"]

    assert body["pyserial 3.5"] == "PYSERIAL SUPPLIED TEXT"
    assert body["HarmonyOS Sans SC (Regular, Bold)"] == "FONT LICENSE TEXT"
    assert body["Lucide icons"] == "LUCIDE SUPPLIED TEXT"
    assert "FOO COMMERCIAL" not in text and "QT C\n" not in text


def test_license_texts_statement_follows_any_spelling(
    pr: Any, supplied: Path, tmp_path: Path
) -> None:
    qt = _dist(
        tmp_path / "site",
        "pyside6.essentials",
        "6.12.0",
        {"x.dist-info/LICENSE": "QT METADATA LICENSE\n"},
    )
    body = dict(_sections(pr.license_texts([qt])))
    assert QT_STATEMENT in body["pyside6.essentials 6.12.0"]
    assert "QT METADATA LICENSE" not in body["pyside6.essentials 6.12.0"]


def test_license_texts_without_bundled_distributions(pr: Any, supplied: Path) -> None:
    titles = [t for t, _ in _sections(pr.license_texts([]))]
    assert len(titles) == 3
    assert titles[0].startswith("Python ")
    assert titles[1:] == ["HarmonyOS Sans SC (Regular, Bold)", "Lucide icons"]


@pytest.mark.parametrize(
    ("files", "record"),
    [
        ({"embrtestbare/__init__.py": "x = 1\n"}, True),
        ({"embrtestbare-1.0.dist-info/licenses/LicenseRef-Commercial.txt": "C\n"}, True),
        ({"embrtestbare-1.0.dist-info/COMMERCIAL_LICENSE": "C\n"}, True),
        ({}, False),  # no RECORD: dist.files is None
    ],
    ids=["no-license-file", "only-commercial", "only-commercial-upper", "no-record"],
)
def test_license_texts_stops_without_a_license_file(
    pr: Any, supplied: Path, tmp_path: Path, files: dict[str, str], record: bool
) -> None:
    bare = _dist(tmp_path / "site", "embrtestbare", "1.0", files, record=record)
    with pytest.raises(SystemExit) as stopped:
        pr.license_texts([bare])
    message = str(stopped.value.code)
    assert message.startswith("FAIL: no license file")
    assert "embrtestbare 1.0" in message


def test_license_texts_defaults_point_at_existing_texts(pr: Any) -> None:
    assert pr.LICENSES == REPO / "scripts" / "licenses"
    assert pr.FONT_LICENSE == REPO / "src" / "embr" / "resources" / "fonts" / "LICENSE_Fonts"
    serial = (pr.LICENSES / "pyserial.txt").read_text(encoding="utf-8")
    assert "BSD" in serial and "Chris Liechti" in serial
    assert "ISC License" in (pr.LICENSES / "lucide.txt").read_text(encoding="utf-8")
    assert "HarmonyOS Sans" in pr.FONT_LICENSE.read_text(encoding="utf-8")
    text = pr.license_texts([metadata.distribution("pyserial")])
    assert dict(_sections(text))["pyserial 3.5"] == serial.strip()


# ---- esptool_note -------------------------------------------------------------------------


def test_esptool_note(pr: Any) -> None:
    note = pr.esptool_note()
    version = esptool.__version__
    assert f"esptool {version}, unmodified" in note
    assert f"Embr {__version__}" in note
    assert "GNU General Public License, version 2 or later" in note
    assert f"https://github.com/espressif/esptool/tree/v{version}" in note
    assert f"https://pypi.org/project/esptool/{version}/" in note
    assert "THIRD_PARTY_NOTICES" in note


# ---- main() on a synthetic repository --------------------------------------------------------


@dataclass
class _Repo:
    root: Path
    dist: Path
    release: Path

    @property
    def archive(self) -> Path:
        return self.release / ZIP_NAME

    @property
    def stage(self) -> Path:
        return self.release / NAME


REPO_NOTICES = (
    "Embr includes or depends on the following third-party software.\n\n"
    "Bundled in the release package\n"
    "------------------------------\n"
    "embrtest-foo 1.0           MIT\n"
)


@pytest.fixture
def repo(pr: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Repo:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "LICENSE").write_bytes(b"GPL TEXT\nline two\n")
    (root / "README.md").write_bytes("# Embr\n\nr\u00e9sum\u00e9\n".encode())
    (root / "CHANGELOG.md").write_bytes(b"# Changelog\r\n\r\n- one\r\n")
    (root / "THIRD_PARTY_NOTICES").write_text(REPO_NOTICES, encoding="utf-8", newline="\n")
    dist = root / "dist" / "Embr"
    resources = dist / "_internal" / "embr" / "resources"
    resources.mkdir(parents=True)
    (dist / "Embr.exe").write_bytes(b"MZ fake exe")
    (resources / "build.txt").write_text("2026-11-01 08:12 UTC\n", encoding="utf-8")
    (dist / "_internal" / "base_library.zip").write_bytes(b"PK fake")
    toc = root / "build" / "Embr" / "PYZ-00.toc"
    _toc(toc, ["embrtestfoo", "embrtestfoo.core", "json", "embr.build"])
    site = tmp_path / "site"
    _dist(
        site,
        "embrtest-foo",
        "1.0",
        {"embrtest_foo-1.0.dist-info/LICENSE": "FOO LICENSE TEXT\n"},
        top_level=("embrtestfoo",),
    )
    _on_path(monkeypatch, site)
    release = root / "dist" / "release"
    monkeypatch.setattr(pr, "REPO", root)
    monkeypatch.setattr(pr, "DIST", dist)
    monkeypatch.setattr(pr, "RELEASE", release)
    monkeypatch.setattr(pr, "ANALYSIS", toc)
    monkeypatch.setattr(sys, "argv", ["package_release.py", "--skip-build"])
    return _Repo(root, dist, release)


def _bare_lf(data: bytes) -> bool:
    return re.search(rb"(?<!\r)\n", data) is not None


def test_main_packages_the_release(
    pr: Any, repo: _Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pr.main() == 0
    out = capsys.readouterr().out
    archive = repo.archive
    assert archive.is_file()
    data = archive.read_bytes()
    assert str(archive) in out
    assert str(len(data)) in out
    assert hashlib.sha256(data).hexdigest() in out

    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        files = {n for n in names if not n.endswith("/")}
        read = {n: zf.read(n) for n in files}
    assert all(n.startswith(f"{NAME}/") for n in names), names
    expected = {
        f"{NAME}/{p}"
        for p in (
            "Embr.exe",
            "_internal/embr/resources/build.txt",
            "_internal/base_library.zip",
            "LICENSE",
            "README.md",
            "CHANGELOG.md",
            "ESPTOOL.txt",
            "THIRD_PARTY_NOTICES",
        )
    }
    assert files == expected
    # the zip holds exactly the staged folder
    staged = {
        f"{NAME}/{p.relative_to(repo.stage).as_posix()}"
        for p in repo.stage.rglob("*")
        if p.is_file()
    }
    assert staged == files
    assert read[f"{NAME}/Embr.exe"] == b"MZ fake exe"
    for copied in ("LICENSE", "README.md", "CHANGELOG.md"):
        assert read[f"{NAME}/{copied}"] == (repo.root / copied).read_bytes(), copied

    notices = read[f"{NAME}/THIRD_PARTY_NOTICES"]
    assert not _bare_lf(notices), "THIRD_PARTY_NOTICES is not CRLF throughout"
    notices_text = notices.decode("utf-8").replace("\r\n", "\n")
    assert notices_text.startswith(REPO_NOTICES.rstrip() + "\n\n")
    titles = [t for t, _ in _sections(notices_text)]
    assert "embrtest-foo 1.0" in titles
    assert dict(_sections(notices_text))["embrtest-foo 1.0"].endswith("FOO LICENSE TEXT")

    note = read[f"{NAME}/ESPTOOL.txt"]
    assert not _bare_lf(note), "ESPTOOL.txt is not CRLF throughout"
    assert note.decode("utf-8") == pr.esptool_note().replace("\n", "\r\n")


def test_main_replaces_an_earlier_package(
    pr: Any, repo: _Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.stage.mkdir(parents=True)
    (repo.stage / "stale.txt").write_text("old", encoding="utf-8")
    repo.archive.write_bytes(b"not a zip")
    assert pr.main() == 0
    assert not (repo.stage / "stale.txt").exists()
    with zipfile.ZipFile(repo.archive) as zf:
        assert f"{NAME}/stale.txt" not in zf.namelist()
        assert f"{NAME}/Embr.exe" in zf.namelist()


@pytest.mark.parametrize("missing", ["Embr.exe", "_internal/embr/resources/build.txt"])
def test_main_refuses_an_incomplete_build(
    pr: Any, repo: _Repo, missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo.dist / missing).unlink()
    assert pr.main() == 1
    assert "FAIL" in capsys.readouterr().out
    assert not repo.archive.exists()


@pytest.mark.parametrize(
    "line",
    ["embrtest-foo 1.1           MIT\n", "", "  embrtest-foo 1.0  MIT\n"],
    ids=["other-version", "absent", "indented"],
)
def test_main_refuses_a_bundled_distribution_not_listed(
    pr: Any, repo: _Repo, line: str, capsys: pytest.CaptureFixture[str]
) -> None:
    notices = REPO_NOTICES.replace("embrtest-foo 1.0           MIT\n", line)
    (repo.root / "THIRD_PARTY_NOTICES").write_text(notices, encoding="utf-8")
    assert pr.main() == 1
    out = capsys.readouterr().out
    assert "FAIL: bundled but not listed" in out
    assert "embrtest-foo 1.0" in out
    assert not repo.archive.exists()


def test_main_accepts_another_spelling_in_the_notices(
    pr: Any, repo: _Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    notices = REPO_NOTICES.replace("embrtest-foo 1.0", "Embrtest_Foo 1.0")
    (repo.root / "THIRD_PARTY_NOTICES").write_text(notices, encoding="utf-8")
    assert pr.main() == 0


def test_main_stops_on_a_bundled_distribution_without_license(
    pr: Any, repo: _Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site2"
    _dist(
        site, "embrtest-bare", "2.0", {"embrtestbare/__init__.py": ""}, top_level=("embrtestbare",)
    )
    _on_path(monkeypatch, site)
    _toc(repo.root / "build" / "Embr" / "PYZ-00.toc", ["embrtestfoo", "embrtestbare"])
    notices = REPO_NOTICES + "embrtest-bare 2.0          MIT\n"
    (repo.root / "THIRD_PARTY_NOTICES").write_text(notices, encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        pr.main()
    assert str(stopped.value.code).startswith("FAIL: no license file for embrtest-bare 2.0")
    assert not repo.archive.exists()


def test_main_builds_with_pyinstaller_unless_skipped(
    pr: Any, repo: _Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append((list(command), options))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pr.subprocess, "run", run)
    assert pr.main() == 0
    assert calls == []

    monkeypatch.setattr(sys, "argv", ["package_release.py"])
    assert pr.main() == 0
    assert len(calls) == 1
    command, options = calls[0]
    assert command[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "Embr.spec" in command
    assert options.get("cwd") == repo.root
    assert options.get("check") is True


def test_main_stops_when_the_build_fails(
    pr: Any, repo: _Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(pr.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["package_release.py"])
    with pytest.raises(subprocess.CalledProcessError):
        pr.main()
    assert not repo.archive.exists()


def test_script_paths_point_into_the_repository(pr: Any) -> None:
    assert pr.REPO == REPO
    assert pr.DIST == REPO / "dist" / "Embr"
    assert pr.RELEASE == REPO / "dist" / "release"
    assert pr.ANALYSIS == REPO / "build" / "Embr" / "PYZ-00.toc"
    for name in pr.COPIED:
        assert (REPO / name).is_file(), name
    assert set(pr.COPIED) == {"LICENSE", "README.md", "CHANGELOG.md"}
