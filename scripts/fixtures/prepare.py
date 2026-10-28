"""Recreate the fixture projects in a work directory from tests/fixtures/*/SOURCE.toml.

    uv run python scripts/fixtures/prepare.py WORK_DIR --idf-path IDF_PATH [--sketch main.cpp]

ESP-IDF samples start from IDF_PATH/examples/get-started/hello_world. PlatformIO Arduino
samples need an Arduino sketch (--sketch); the ESP-IDF framework sample reuses the
hello_world source. Build with build_idf.ps1 / build_pio.py, then run collect.py.
"""

import argparse
import shutil
import sys
import tomllib
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def prepare_idf(name: str, source: dict[str, object], work: Path, hello: Path) -> None:
    project = work / "idf" / name
    (project / "main").mkdir(parents=True, exist_ok=True)
    root_cmake = (hello / "CMakeLists.txt").read_text(encoding="utf-8")
    write(
        project / "CMakeLists.txt",
        root_cmake.replace("project(hello_world)", f"project({source['project_name']})"),
    )
    for f in ("CMakeLists.txt", "hello_world_main.c"):
        shutil.copy2(hello / "main" / f, project / "main" / f)
    write(project / "sdkconfig.defaults", str(source["sdkconfig_defaults"]))
    if "partitions_csv" in source:
        write(project / "partitions.csv", str(source["partitions_csv"]))


def prepare_pio(
    name: str, source: dict[str, object], work: Path, hello: Path, sketch: Path | None
) -> None:
    project = work / "pio" / name
    (project / "src").mkdir(parents=True, exist_ok=True)
    ini = str(source["platformio_ini"])
    write(project / "platformio.ini", ini)
    if "partitions_csv" in source:
        write(project / "partitions.csv", str(source["partitions_csv"]))
    if "framework = espidf" in ini:
        write(
            project / "CMakeLists.txt",
            "cmake_minimum_required(VERSION 3.16.0)\n"
            "include($ENV{IDF_PATH}/tools/cmake/project.cmake)\n"
            f"project({name})\n",
        )
        write(
            project / "src" / "CMakeLists.txt",
            'idf_component_register(SRCS "main.c" PRIV_REQUIRES spi_flash)\n',
        )
        shutil.copy2(hello / "main" / "hello_world_main.c", project / "src" / "main.c")
    elif sketch is None:
        print(f"skip {name}: needs --sketch")
    else:
        shutil.copy2(sketch, project / "src" / "main.cpp")


def main() -> int:
    parser = argparse.ArgumentParser(description="Recreate the fixture projects.")
    parser.add_argument("work", type=Path)
    parser.add_argument("--idf-path", type=Path, required=True)
    parser.add_argument("--sketch", type=Path, help="Arduino sketch for the PlatformIO samples")
    args = parser.parse_args()
    hello = args.idf_path / "examples" / "get-started" / "hello_world"
    for toml_path in sorted(FIXTURES.glob("*/*/SOURCE.toml")):
        kind, name = toml_path.parent.parent.name, toml_path.parent.name
        source: dict[str, object] = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        if kind == "idf":
            prepare_idf(name, source, args.work, hello)
        elif kind == "pio":
            prepare_pio(name, source, args.work, hello, args.sketch)
        else:
            continue
        print(f"prepared {kind}/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
