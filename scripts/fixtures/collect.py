"""Copy fixture build outputs from a work directory into tests/fixtures.

    uv run python scripts/fixtures/collect.py WORK_DIR [--idf SAMPLE ...] [--pio SAMPLE ...]

Only the files the tests use are copied; absolute paths in JSON files are replaced with
placeholders (<PROJECT_DIR>, <BUILD_DIR>, <IDF_PATH>, <IDF_TOOLS>, <PIO_CORE>, <HOME>).
SOURCE.toml files are not touched: they are the recipes prepare.py reads.
Run inside the activated ESP-IDF environment for IDF samples (IDF_TOOLS_PATH is used).
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
PIO_CORE = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
ABSOLUTE = re.compile(r"\b[A-Za-z]:(?:\\\\|\\|/)")


def scrub(text: str, roots: list[tuple[Path, str]]) -> str:
    forms: list[tuple[str, str]] = []
    for path, name in roots:
        p = str(path)
        forms += [(f, name) for f in {p, p.replace("\\", "/"), p.replace("\\", "\\\\")}]
    for old, new in sorted(forms, key=lambda x: -len(x[0])):
        text = re.sub(re.escape(old), new.replace("\\", "\\\\"), text, flags=re.IGNORECASE)
    return text


def write_scrubbed(src: Path, dst: Path, roots: list[tuple[Path, str]]) -> None:
    text = scrub(src.read_text(encoding="utf-8"), roots)
    if ABSOLUTE.search(text):
        print(f"warning: {dst} still contains an absolute path")
    dst.write_text(text, encoding="utf-8", newline="\n")


def esptool_line(lines: list[str], action: str) -> str:
    """The esptool command line idf.py printed for an action (flash uses port COM99)."""
    for line in lines:
        is_esptool = re.match(r"esptool(\.py)? ", line) and re.search(action, line)
        if is_esptool and (action != "merge" or "COM99" not in line):
            return line
    return ""


def collect_idf(work: Path, name: str, fixtures: Path) -> None:
    project = work / "idf" / name
    build = project / "build"
    dst = fixtures / "idf" / name / "build"
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    desc = json.loads((build / "project_description.json").read_text(encoding="utf-8"))
    roots = [
        (build, "<BUILD_DIR>"),
        (project, "<PROJECT_DIR>"),
        (Path(desc["idf_path"]), "<IDF_PATH>"),
        (Path(os.environ.get("IDF_TOOLS_PATH", "C:/Espressif/tools")), "<IDF_TOOLS>"),
        (Path.home(), "<HOME>"),
    ]
    args = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    files: set[str] = set(args["flash_files"].values())
    for rel in [*sorted(files), "flasher_args.json", "flash_args", "merged-binary.bin"]:
        if (build / rel).exists():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(build / rel, dst / rel)
    write_scrubbed(build / "project_description.json", dst / "project_description.json", roots)
    logs = [
        project / "flash_cmd.log",
        project / "merge.log",
        *sorted((build / "log").glob("idf_py_*")),
    ]
    lines = [
        ln.strip()
        for p in logs
        if p.exists()
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
    ]
    for action, out in (
        ("write[-_]flash", "idf_flash_command.txt"),
        ("merge", "idf_merge_command.txt"),
    ):
        line = esptool_line(lines, action)
        if line:
            (dst.parent / out).write_text(scrub(line, roots) + "\n", encoding="utf-8", newline="\n")
    print(f"collected idf/{name}: {len(files)} images")


def collect_pio(work: Path, name: str, fixtures: Path) -> None:
    project = work / "pio" / name
    dst = fixtures / "pio" / name
    roots = [(project, "<PROJECT_DIR>"), (PIO_CORE, "<PIO_CORE>"), (Path.home(), "<HOME>")]
    shutil.rmtree(dst / ".pio", ignore_errors=True)
    ini = (project / "platformio.ini").read_text(encoding="utf-8")
    for env in re.findall(r"^\[env:(.+)\]$", ini, re.M):
        b_src, b_dst = project / ".pio" / "build" / env, dst / ".pio" / "build" / env
        b_dst.mkdir(parents=True)
        for f in ("bootloader.bin", "partitions.bin", "firmware.bin"):
            shutil.copy2(b_src / f, b_dst / f)
        for f in ("idedata.json", "flasher_args.json"):
            if (b_src / f).exists():
                write_scrubbed(b_src / f, b_dst / f, roots)
        write_scrubbed(project / f"metadata_{env}.json", dst / f"metadata_{env}.json", roots)
    if "boot_app0.bin" in ini or "framework = arduino" in ini:
        # stand-in for the framework package that idedata / metadata point at
        pkg_src = PIO_CORE / "packages" / "framework-arduinoespressif32"
        pkg = dst / "pio_core" / "packages" / "framework-arduinoespressif32"
        (pkg / "tools" / "partitions").mkdir(parents=True, exist_ok=True)
        shutil.copy2(pkg_src / "package.json", pkg / "package.json")
        boot_app0 = pkg_src / "tools" / "partitions" / "boot_app0.bin"
        shutil.copy2(boot_app0, pkg / "tools" / "partitions" / "boot_app0.bin")
    print(f"collected pio/{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy fixture build outputs into tests/fixtures.")
    parser.add_argument("work", type=Path)
    parser.add_argument("--idf", nargs="*", default=[])
    parser.add_argument("--pio", nargs="*", default=[])
    parser.add_argument("--dest", type=Path, default=FIXTURES, help="default: tests/fixtures")
    args = parser.parse_args()
    for name in args.idf:
        collect_idf(args.work, name, args.dest)
    for name in args.pio:
        collect_pio(args.work, name, args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
