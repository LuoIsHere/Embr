"""Check a release package made by package_release.py (release workflow), then run the
worker check on the Embr.exe extracted from it.

    uv run python scripts/verify_release.py dist/release/embr_<version>_windows_x64.zip

Checks: the name, one top folder of the same name, the required files, build.txt in
"YYYY-MM-DD HH:MM UTC", the full license texts in THIRD_PARTY_NOTICES, the .sha256 file
next to the zip.
"""

import hashlib
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from embr import __version__

SCRIPTS = Path(__file__).resolve().parent
NAME = f"embr_{__version__}_windows_x64"
REQUIRED = (
    "Embr.exe",
    "LICENSE",
    "THIRD_PARTY_NOTICES",
    "ESPTOOL.txt",
    "README.md",
    "CHANGELOG.md",
    "_internal/embr/resources/build.txt",
    "_internal/embr/resources/esptool.cfg",
)
BUILD_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
FULL_TEXTS = "Full license texts"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_release.py <zip>")
        return 2
    archive = Path(sys.argv[1]).resolve()
    problems = check(archive)
    if not problems:
        with tempfile.TemporaryDirectory() as folder:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(folder)
            exe = Path(folder) / NAME / "Embr.exe"
            worker = subprocess.run(
                [sys.executable, str(SCRIPTS / "check_frozen_worker.py"), str(exe)], check=False
            )
            if worker.returncode != 0:
                problems.append("the worker check failed on the extracted Embr.exe")
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    print(f"ok: {archive.name}")
    return 0


def check(archive: Path) -> list[str]:
    """Problems of the zip and its .sha256 file, without extracting it."""
    if archive.name != f"{NAME}.zip":
        return [f"{archive.name} is not {NAME}.zip"]
    if not archive.is_file():
        return [f"{archive} not found"]
    problems: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        outside = sorted(n for n in names if not n.startswith(f"{NAME}/"))
        if outside:
            problems.append(f"entries outside {NAME}/: {', '.join(outside[:5])}")
        for file in REQUIRED:
            if f"{NAME}/{file}" not in names:
                problems.append(f"{file} missing")
        build = f"{NAME}/_internal/embr/resources/build.txt"
        if build in names:
            text = zf.read(build).decode("utf-8").strip()
            if not BUILD_TIME.fullmatch(text):
                problems.append(f"build.txt holds {text!r}, not YYYY-MM-DD HH:MM UTC")
        notices = f"{NAME}/THIRD_PARTY_NOTICES"
        if notices in names and FULL_TEXTS not in zf.read(notices).decode("utf-8"):
            problems.append(f"THIRD_PARTY_NOTICES has no {FULL_TEXTS!r} section")
    checksum = Path(f"{archive}.sha256")
    expected = f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    if not checksum.is_file():
        problems.append(f"{checksum.name} missing")
    elif checksum.read_bytes().decode("utf-8") != expected:
        problems.append(f"{checksum.name} does not match the zip")
    return problems


if __name__ == "__main__":
    sys.exit(main())
