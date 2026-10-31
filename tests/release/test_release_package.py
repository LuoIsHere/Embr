"""The release package (design doc §7.5, §12 item 22), built for real: one run of
`scripts/package_release.py` (PyInstaller included) for the module, then the zip is
checked: its layout, build.txt (packaging time, UTC), THIRD_PARTY_NOTICES with a full
license text for everything bundled, ESPTOOL.txt, no private strings, and the extracted
Embr.exe whose About dialog shows that build time instead of "development build".

The About dialog of the packaged Embr.exe is opened through UI Automation (the ≡ menu
button, which has no name, is found by its automation id) and the menu is driven with
posted Up / Return keys: About Embr is its last entry."""

import ast
import ctypes
import datetime
import hashlib
import importlib.metadata as metadata
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import esptool
import pytest
import release_kit as kit

from embr import __version__
from embr.i18n import Language

NAME = f"Embr-{__version__}"
ARCHIVE = kit.REPO / "dist" / "release" / f"{NAME}-windows-x64.zip"
ANALYSIS = kit.REPO / "build" / "Embr" / "PYZ-00.toc"
BUILD_TXT = "_internal/embr/resources/build.txt"
RULE = "=" * 78
COPYRIGHT = "Copyright © 2026 @LuoIsHere"
TOP_FILES = (
    "Embr.exe",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "ESPTOOL.txt",
    "THIRD_PARTY_NOTICES",
)
TEXT_SUFFIXES = {".txt", ".md", ".cfg", ".toml", ".json", ".ini", ".py", ".svg", ""}
PACKAGE_TIMEOUT = 1800


@dataclass(frozen=True)
class Package:
    archive: Path
    output: str
    started: datetime.datetime
    finished: datetime.datetime
    folder: Path
    """The zip extracted: <tmp>/Embr-<version>."""
    names: list[str]

    def read(self, member: str) -> bytes:
        return (self.folder / member).read_bytes()


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Package:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("QT_QPA_PLATFORM", None)
    started = datetime.datetime.now(datetime.UTC)
    done = subprocess.run(
        [sys.executable, str(kit.REPO / "scripts" / "package_release.py")],
        cwd=kit.REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PACKAGE_TIMEOUT,
        check=False,
    )
    finished = datetime.datetime.now(datetime.UTC)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    assert ARCHIVE.is_file()
    target = tmp_path_factory.mktemp("release_zip")
    with zipfile.ZipFile(ARCHIVE) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        zf.extractall(target)
    return Package(ARCHIVE, done.stdout, started, finished, target / NAME, names)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _crlf(data: bytes) -> bool:
    return b"\r\n" in data and re.search(rb"(?<!\r)\n", data) is None


def _sections(text: str) -> dict[str, str]:
    """Full-text sections of the packaged THIRD_PARTY_NOTICES: title -> body."""
    head = "Full license texts\n------------------\n\n"
    assert text.count(head) == 1, "no single 'Full license texts' heading"
    parts = re.split(rf"^{RULE}\n(.+)\n{RULE}\n", text.split(head, 1)[1], flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def _notice_entries(start: str) -> dict[str, str]:
    """normalized name -> version of the entries in the repository THIRD_PARTY_NOTICES
    section whose heading starts with `start` (headings are underlined with dashes and may
    wrap onto a second line)."""
    lines = (kit.REPO / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8").splitlines()
    sections: list[tuple[str, list[str]]] = []
    current: list[str] = []
    for line in lines:
        if re.fullmatch(r"-{10,}", line):
            begin = len(current)
            while begin > 0 and current[begin - 1].strip():
                begin -= 1
            heading = " ".join(current[begin:])
            del current[begin:]
            current = []
            sections.append((heading, current))
            continue
        current.append(line)
    found = [body for heading, body in sections if heading.startswith(start)]
    assert len(found) == 1, f"no single section {start!r}"
    entries: dict[str, str] = {}
    for line in found[0]:
        match = re.match(r"^(\S+)[ \t]+(\d\S*)", line)
        if match:
            entries[_normalize(match[1])] = match[2]
    return entries


def _analysis_modules() -> set[str]:
    toc = ast.literal_eval(ANALYSIS.read_text(encoding="utf-8"))
    return {entry[0].split(".")[0] for entry in toc[1]}


def _bundled() -> list[metadata.Distribution]:
    """Distributions owning a top-level module of the PYZ or a top-level folder / file of
    _internal (embr excluded), worked out here independently of the script."""
    owners = metadata.packages_distributions()
    internal = kit.REPO / "dist" / "Embr" / "_internal"
    modules = _analysis_modules() | {p.name.split(".")[0] for p in internal.iterdir()}
    names = {n for m in modules for n in owners.get(m, []) if _normalize(n) != "embr"}
    return [metadata.distribution(n) for n in sorted(names, key=_normalize)]


# ---- the zip ----------------------------------------------------------------------------


def test_script_reports_the_archive(package: Package) -> None:
    data = package.archive.read_bytes()
    last = package.output.strip().splitlines()[-1]
    assert str(package.archive) in last
    assert f"{len(data)} bytes" in last
    assert hashlib.sha256(data).hexdigest() in last


def test_zip_layout(package: Package) -> None:
    assert all(n.startswith(f"{NAME}/") for n in package.names), "entries outside the folder"
    files = {n.removeprefix(f"{NAME}/") for n in package.names if not n.endswith("/")}
    for name in (*TOP_FILES, BUILD_TXT):
        assert name in files, f"{name} missing from the zip"
    assert any(n.startswith("_internal/PySide6/") for n in files)
    for copied in ("LICENSE", "README.md", "CHANGELOG.md"):
        assert package.read(copied) == (kit.REPO / copied).read_bytes(), copied
    built = kit.REPO / "dist" / "Embr" / "Embr.exe"
    assert package.read("Embr.exe") == built.read_bytes()
    assert not (kit.SRC / "embr" / "resources" / "build.txt").exists(), "build.txt in src/"


def test_build_time_is_the_packaging_time_in_utc(package: Package) -> None:
    text = package.read(BUILD_TXT).decode("utf-8")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC\r?\n?", text)
    assert match, f"build.txt is {text!r}"
    built = datetime.datetime.strptime(match[1], "%Y-%m-%d %H:%M").replace(tzinfo=datetime.UTC)
    earliest = package.started.replace(second=0, microsecond=0)
    assert earliest <= built <= package.finished, (built, package.started, package.finished)


def test_notices_have_a_full_text_for_everything_bundled(package: Package) -> None:
    data = package.read("THIRD_PARTY_NOTICES")
    assert _crlf(data), "THIRD_PARTY_NOTICES is not CRLF throughout"
    text = data.decode("utf-8").replace("\r\n", "\n")
    listed = (kit.REPO / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    assert text.startswith(listed.rstrip() + "\n\n"), "the repository list is not kept as is"
    sections = _sections(text)
    titles = list(sections)
    assert titles[0].startswith(f"Python {sys.version.split()[0]}")
    python_license = (Path(sys.base_prefix) / "LICENSE.txt").read_text(encoding="utf-8")
    assert sections[titles[0]] == python_license.strip()
    assert titles[-2:] == ["HarmonyOS Sans SC (Regular, Bold)", "Lucide icons"]

    bundled = _bundled()
    keys = {_normalize(d.name) for d in bundled}
    assert {"esptool", "pyside6-essentials", "shiboken6", "pyserial"} <= keys, keys
    expected = [f"{d.name} {d.version}" for d in bundled]
    assert titles[1:-2] == expected
    in_notices = _notice_entries("Bundled in the release package")
    for dist in bundled:
        body = sections[f"{dist.name} {dist.version}"]
        assert len(body) > 100, f"{dist.name}: {body!r}"
        assert in_notices.get(_normalize(dist.name)) == dist.version, f"{dist.name} not listed"
    for qt in ("PySide6_Essentials 6.12.0", "shiboken6 6.12.0"):
        assert "GNU General Public License, version 3" in sections[qt], qt
    serial = (kit.REPO / "scripts" / "licenses" / "pyserial.txt").read_text(encoding="utf-8")
    assert sections["pyserial 3.5"] == serial.strip()

    # no commercial license text of any bundled distribution
    for dist in bundled:
        for file in dist.files or []:
            if "commercial" in file.name.lower():
                first = next(
                    line for line in file.read_text(encoding="utf-8").splitlines() if line.strip()
                )
                assert first.strip() not in text, f"{file} copied into the notices"


def test_other_esptool_dependencies_are_not_bundled(package: Package) -> None:
    """THIRD_PARTY_NOTICES says they are not in the release package."""
    other = _notice_entries("Other dependencies of esptool")
    assert other
    owners = metadata.packages_distributions()
    modules = {m for m, dists in owners.items() if any(_normalize(d) in other for d in dists)}
    assert modules, "no installed module of the other dependencies found"
    assert modules & _analysis_modules() == set()
    tops = {
        n.removeprefix(f"{NAME}/_internal/").split("/")[0].split(".")[0]
        for n in package.names
        if n.startswith(f"{NAME}/_internal/")
    }
    assert modules & tops == set()


def test_esptool_note(package: Package) -> None:
    data = package.read("ESPTOOL.txt")
    assert _crlf(data), "ESPTOOL.txt is not CRLF throughout"
    text = data.decode("utf-8")
    version = esptool.__version__
    assert f"esptool {version}, unmodified" in text
    assert f"https://github.com/espressif/esptool/tree/v{version}" in text
    assert f"https://pypi.org/project/esptool/{version}/" in text


def _private_paths() -> dict[str, str]:
    paths = {
        "home folder": str(Path.home()),
        "repository": str(kit.REPO),
        "virtual environment": sys.prefix,
        "Python installation": sys.base_prefix,
    }
    for key, value in os.environ.items():
        if key.startswith("EMBR_TEST_") and len(value) > 3 and (":" in value or "\\" in value):
            paths[key] = value
    return paths


def _private_words() -> dict[str, str]:
    """Searched for in text files only, as whole words."""
    words = {
        "user name": os.environ.get("USERNAME", ""),
        "computer name": os.environ.get("COMPUTERNAME", ""),
    }
    words |= {k: v for k, v in os.environ.items() if k.startswith("EMBR_TEST_PORT")}
    return {k: v for k, v in words.items() if len(v) >= 4}


MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")


def test_no_private_strings(package: Package) -> None:
    paths = _private_paths()
    words = _private_words()
    found: list[str] = []
    for member in package.folder.rglob("*"):
        if not member.is_file():
            continue
        rel = member.relative_to(package.folder).as_posix()
        data = member.read_bytes().lower()
        for what, value in paths.items():
            for form in {value, value.replace("\\", "/")}:
                for encoding in ("utf-8", "utf-16-le"):
                    if form.lower().encode(encoding) in data:
                        found.append(f"{rel}: {what} ({encoding})")
        if member.suffix.lower() in TEXT_SUFFIXES:
            text = data.decode("utf-8", errors="replace")
            for what, value in words.items():
                if re.search(rf"(?<![\w]){re.escape(value.lower())}(?![\w])", text):
                    found.append(f"{rel}: {what}")
            if MAC.search(text):
                found.append(f"{rel}: a MAC address")
    assert found == []


# ---- the packaged Embr.exe ------------------------------------------------------------------

MENU_BUTTON_ID = "QApplication.EmbrWindow.window.topbar.QPushButton"
_PRESS_MENU_BUTTON = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$found = @()
foreach ($p in @(%(pids)s)) {
  $cond = New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, [int]$p)
  $wins = $A::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
  foreach ($w in $wins) {
    if ($w.Current.ClassName -ne "EmbrWindow") { continue }
    $id = New-Object System.Windows.Automation.PropertyCondition($A::AutomationIdProperty, '%(id)s')
    $found += @($w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $id))
  }
}
$buttons = @($found | Where-Object {
  $_.Current.ControlType.ProgrammaticName -eq "ControlType.Button" })
if ($buttons.Count -gt 0) {
  $first = $buttons | Sort-Object { $_.Current.BoundingRectangle.Left } | Select-Object -First 1
  $first.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
  "pressed"
}
"""
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_RETURN = 0x0D
VK_UP = 0x26


def _post_key(hwnd: int, key: int) -> None:
    user32: Any = ctypes.windll.user32
    user32.PostMessageW(hwnd, WM_KEYDOWN, key, 1)
    user32.PostMessageW(hwnd, WM_KEYUP, key, 0xC0000001)


def _open_about(process: kit.EmbrProcess, title: str) -> None:
    """≡ menu (invoked through UI Automation) -> Up (the last entry, About Embr) ->
    Return, posted to the menu's window; waits for the dialog titled `title`."""
    pids = process.pids()
    before = {w.hwnd for w in kit.top_windows(pids)}
    script = _PRESS_MENU_BUTTON % {"pids": ",".join(map(str, sorted(pids))), "id": MENU_BUTTON_ID}
    kit.poll_until(
        lambda: "pressed" in kit._powershell(script),  # pyright: ignore[reportPrivateUsage]
        60,
        "the ≡ menu button was not found",
    )
    popup: list[kit.NativeWindow] = []

    def menu_shown() -> bool:
        popup[:] = [w for w in kit.top_windows(pids) if w.hwnd not in before]
        return bool(popup)

    kit.poll_until(menu_shown, 15, "the ≡ menu did not open")
    time.sleep(0.5)
    for key in (VK_UP, VK_RETURN):
        _post_key(popup[0].hwnd, key)
        time.sleep(0.2)
    kit.poll_until(
        lambda: any(w.title == title for w in kit.top_windows(pids)),
        15,
        f"no {title!r} dialog; windows: {kit.top_windows(pids)}",
    )


@pytest.mark.parametrize("language", [Language.ENGLISH, Language.ZH_CN], ids=["en", "zh_CN"])
def test_packaged_about_shows_the_build_time(
    package: Package, language: Language, sentinels: kit.Sentinels, tmp_path: Path
) -> None:
    built = package.read(BUILD_TXT).decode("utf-8").strip()
    folder = tmp_path / NAME
    shutil.copytree(package.folder, folder)
    kit.store_language(folder, language)
    wanted = kit.translated(f"Build time: {built}", language)
    development = kit.translated("development build", language)
    title = kit.translated("About Embr", language)
    with kit.EmbrProcess(
        [str(folder / "Embr.exe")],
        env=sentinels.env(),
        cwd=sentinels.folder("cwd"),
        logs=tmp_path / "logs",
    ) as process:
        assert process.wait_main_window(90) is not None, process.stderr()
        _open_about(process, title)
        texts: list[str] = []

        def shown() -> bool:
            texts[:] = [t for w in process.windows(deep=True) for t in w.texts]
            return wanted in texts

        kit.poll_until(shown, 60, f"About does not show {wanted!r}")
        assert COPYRIGHT in texts
        assert not any(development in t for t in texts), texts
        assert not any("development build" in t for t in texts), texts
        assert process.close() == 0
