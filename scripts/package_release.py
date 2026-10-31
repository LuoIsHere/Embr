"""Build the release package (design doc §7.5): dist/release/Embr-<version>-windows-x64.zip
holding the folder Embr-<version>/ with Embr.exe, _internal/, LICENSE, THIRD_PARTY_NOTICES
(the list from the repository plus the full license texts of everything bundled),
ESPTOOL.txt, README.md and CHANGELOG.md.

    uv run python scripts/package_release.py [--skip-build]

--skip-build packages the existing dist/Embr (built with `pyinstaller Embr.spec`).
The bundled distributions are taken from PyInstaller's analysis (build/Embr/PYZ-00.toc);
each must be listed in THIRD_PARTY_NOTICES with its version, or packaging stops.
"""

import argparse
import ast
import hashlib
import importlib.metadata as metadata
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import esptool

from embr import __version__

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist" / "Embr"
RELEASE = REPO / "dist" / "release"
ANALYSIS = REPO / "build" / "Embr" / "PYZ-00.toc"
LICENSES = Path(__file__).resolve().parent / "licenses"
"""License texts the bundled packages do not ship themselves."""
FONT_LICENSE = REPO / "src" / "embr" / "resources" / "fonts" / "LICENSE_Fonts"
COPIED = ("LICENSE", "README.md", "CHANGELOG.md")
LICENSE_FILE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE)", re.IGNORECASE)
RULE = "=" * 78

# distribution (normalized) -> text used instead of the files in its metadata
STATED = {
    "pyside6-essentials": "Used under the GNU General Public License, version 3 (one of the "
    "licenses offered: LGPL-3.0-only, GPL-2.0-only or GPL-3.0-only); the full text is in "
    "LICENSE. Qt includes third-party components under their own licenses, listed at "
    "https://doc.qt.io/qt-6/licenses-used-in-qt.html",
    "shiboken6": "Used under the GNU General Public License, version 3 (one of the licenses "
    "offered: LGPL-3.0-only, GPL-2.0-only or GPL-3.0-only); the full text is in LICENSE.",
}
SUPPLIED = {"pyserial": "pyserial.txt"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if not args.skip_build:
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "Embr.spec", "--noconfirm"], cwd=REPO, check=True
        )
    exe = DIST / "Embr.exe"
    if not exe.is_file() or not (DIST / "_internal" / "embr" / "resources" / "build.txt").is_file():
        print(f"FAIL: {DIST} is not a complete build (Embr.exe, build.txt)")
        return 1

    listed = (REPO / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    bundled = bundled_distributions()
    missing = [f"{d.name} {d.version}" for d in bundled if not is_listed(listed, d)]
    if missing:
        print("FAIL: bundled but not listed in THIRD_PARTY_NOTICES: " + ", ".join(missing))
        return 1

    name = f"Embr-{__version__}"
    stage = RELEASE / name
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(DIST, stage)
    for file in COPIED:
        shutil.copy2(REPO / file, stage / file)
    write_text(stage / "THIRD_PARTY_NOTICES", listed.rstrip() + "\n\n" + license_texts(bundled))
    write_text(stage / "ESPTOOL.txt", esptool_note())

    archive = RELEASE / f"{name}-windows-x64.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(stage.rglob("*")):
            zf.write(path, path.relative_to(RELEASE).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"{archive} {archive.stat().st_size} bytes sha256 {digest}")
    return 0


def bundled_distributions() -> list[metadata.Distribution]:
    """Distributions whose top-level modules PyInstaller put into the PYZ (embr excluded)."""
    toc = ast.literal_eval(ANALYSIS.read_text(encoding="utf-8"))
    modules = {entry[0].split(".")[0] for entry in toc[1]}
    owners = metadata.packages_distributions()
    names = {d for module in modules for d in owners.get(module, []) if d != "embr"}
    return sorted((metadata.distribution(n) for n in names), key=lambda d: normalize(d.name))


def is_listed(listed: str, dist: metadata.Distribution) -> bool:
    """A line of THIRD_PARTY_NOTICES starts with the name (any spelling of - _ .) and the
    version."""
    wanted = (normalize(dist.name), dist.version)
    lines = re.findall(r"^(\S+)[ \t]+(\S+)", listed, re.MULTILINE)
    return any((normalize(name), version) == wanted for name, version in lines)


def license_texts(bundled: list[metadata.Distribution]) -> str:
    sections = [
        section(
            f"Python {sys.version.split()[0]} (Python Software Foundation License; the "
            "third-party components of the Python runtime follow)",
            (Path(sys.base_prefix) / "LICENSE.txt").read_text(encoding="utf-8"),
        )
    ]
    for dist in bundled:
        key = normalize(dist.name)
        title = f"{dist.name} {dist.version}"
        if key in STATED:
            sections.append(section(title, STATED[key]))
        elif key in SUPPLIED:
            sections.append(section(title, (LICENSES / SUPPLIED[key]).read_text(encoding="utf-8")))
        else:
            files = [
                f
                for f in dist.files or []
                if LICENSE_FILE.search(f.name) and "commercial" not in f.name.lower()
            ]
            if not files:
                raise SystemExit(f"FAIL: no license file for {title}; add one to {LICENSES}")
            text = "\n\n".join(f"--- {f}\n\n{f.read_text(encoding='utf-8').strip()}" for f in files)
            sections.append(section(title, text))
    sections.append(
        section("HarmonyOS Sans SC (Regular, Bold)", FONT_LICENSE.read_text(encoding="utf-8"))
    )
    sections.append(section("Lucide icons", (LICENSES / "lucide.txt").read_text(encoding="utf-8")))
    return "Full license texts\n------------------\n\n" + "\n".join(sections)


def section(title: str, text: str) -> str:
    return f"{RULE}\n{title}\n{RULE}\n\n{text.strip()}\n"


def esptool_note() -> str:
    version = esptool.__version__
    return (
        f"Embr {__version__} includes esptool {version}, unmodified.\n\n"
        f"esptool is licensed under the GNU General Public License, version 2 or later;\n"
        f"see THIRD_PARTY_NOTICES. Its source code is available at\n"
        f"https://github.com/espressif/esptool/tree/v{version}\n"
        f"and https://pypi.org/project/esptool/{version}/\n\n"
        f"Embr uses only this built-in esptool. An esptool installed elsewhere on the\n"
        f"computer, and esptool.cfg files elsewhere on the computer, are not used.\n"
    )


def write_text(path: Path, text: str) -> None:
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\r\n")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


if __name__ == "__main__":
    sys.exit(main())
