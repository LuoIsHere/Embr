"""Fault injection (design doc §8.3): break one piece of logic, run the tests, restore.

    uv run python scripts/fault_inject.py FILE OLD NEW [pytest args ...]

OLD must occur exactly once in FILE. The file is always restored, also on Ctrl+C.
Exit code 0 when the tests caught the fault (failed), 1 when they still passed.
Local only: never commit a broken file.
"""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    path, old, new, *pytest_args = sys.argv[1:]
    file = Path(path)
    original = file.read_bytes()
    text = original.decode("utf-8")
    if text.count(old) != 1:
        print(f"OLD occurs {text.count(old)} times in {file}; it must occur exactly once")
        return 2
    try:
        file.write_bytes(text.replace(old, new).encode("utf-8"))
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", *pytest_args], check=False)
    finally:
        file.write_bytes(original)
    if result.returncode == 0:
        print(f"NOT CAUGHT: tests still pass with the fault in {file}")
        return 1
    print(f"caught: tests fail with the fault in {file} (restored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
