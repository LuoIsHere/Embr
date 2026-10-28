"""Build PlatformIO fixture projects prepared by prepare.py and save metadata per env.

    uv run python scripts/fixtures/build_pio.py WORK_DIR SAMPLE [SAMPLE ...]

Local paths are mapped away from the binaries with -ffile-prefix-map passed through
PLATFORMIO_BUILD_FLAGS (platformio.ini expands ${platformio.packages_dir} with
backslashes, which does not match the forward-slash paths GCC embeds).
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def pio_exe() -> str:
    found = shutil.which("pio")
    if found:
        return found
    core = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    return str(core / "penv" / "Scripts" / "pio.exe")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    work = Path(sys.argv[1]) / "pio"
    pio = pio_exe()
    core = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    for name in sys.argv[2:]:
        project = work / name
        flags = (
            f"-ffile-prefix-map={(core / 'packages').as_posix()}=/pio/packages "
            f"-ffile-prefix-map={project.resolve().as_posix()}=."
        )
        env = dict(os.environ, PLATFORMIO_BUILD_FLAGS=flags)
        print(f"=== {name}")
        with (project / "build.log").open("w", encoding="utf-8") as log:
            code = subprocess.run(
                [pio, "run"], cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT
            ).returncode
        print(f"build exit: {code}")
        ini = (project / "platformio.ini").read_text(encoding="utf-8")
        for env_name in re.findall(r"^\[env:(.+)\]$", ini, re.M):
            out = project / f"metadata_{env_name}.json"
            with out.open("w", encoding="utf-8") as f:
                subprocess.run(
                    [pio, "project", "metadata", "--json-output", "-e", env_name],
                    cwd=project,
                    stdout=f,
                    check=False,
                )
            print(f"metadata {env_name} saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
