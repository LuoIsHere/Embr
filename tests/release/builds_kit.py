"""Real builds and real boards for the release suite (test_boards.py, test_idf_builds.py,
test_pio_builds.py); everything else comes from release_kit.

Environment (local machine configuration, never in tracked files; see release_kit):

    EMBR_TEST_PORT_UART / _USB / _P4   the boards (release_kit.board_port())
    EMBR_TEST_IDF_PROJECT              an ESP-IDF project (a git repository) the tests may
                                       change; restored byte for byte afterwards
    EMBR_TEST_IDF_EXPORT               activation script of its ESP-IDF (export.ps1 or an EIM
                                       PowerShell profile), dot-sourced before idf.py
    EMBR_TEST_IDF_TOOLS                optional IDF_TOOLS_PATH for EMBR_TEST_IDF_EXPORT (an
                                       export.ps1 whose tools are not in ~/.espressif)
    EMBR_TEST_IDF_EXPORT_2             activation script of a second ESP-IDF (v6.x), for the
                                       version switch
    EMBR_TEST_PIO_PROJECT              a PlatformIO project the tests may change; restored
                                       byte for byte afterwards (pio is found the way Embr
                                       finds it)

The original state of each project is captured once per session (ProjectBackup) by the
session fixtures in conftest.py, which restore it after the last test (also on failure),
rebuild the original target and verify the result.

Board read-back goes through esptool's command line (the same esptool Embr bundles) in a
subprocess, never through Embr, so what Embr wrote is checked independently. Build tools run
with the caller's original environment (TMP / TEMP as before Embr started, no .venv on
PATH), so idf.py finds its own Python.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import release_kit as kit
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QLabel, QLineEdit, QWidget

IDF_PROJECT_VAR = "EMBR_TEST_IDF_PROJECT"
IDF_EXPORT_VAR = "EMBR_TEST_IDF_EXPORT"
IDF_TOOLS_VAR = "EMBR_TEST_IDF_TOOLS"
IDF_EXPORT_2_VAR = "EMBR_TEST_IDF_EXPORT_2"
PIO_PROJECT_VAR = "EMBR_TEST_PIO_PROJECT"
UART, USB, P4 = "EMBR_TEST_PORT_UART", "EMBR_TEST_PORT_USB", "EMBR_TEST_PORT_P4"

BUILD_TIMEOUT = 3600.0
BUILD_JOBS = "2"
"""Parallel jobs for builds (pio run -j, CMAKE_BUILD_PARALLEL_LEVEL): the release machine
stays usable while a test builds."""
LOW_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
"""Builds run below normal priority; the compilers they start inherit it (ninja, which
idf.py starts, has no job limit of its own)."""
"""A full build from scratch (a new target or ESP-IDF version) on Windows."""
SETTLE_S = 2.6
"""Embr treats files modified within 2 s as "being built" (design doc §3.4)."""

_ORIGINAL_ENV = dict(os.environ)
"""The environment before any Embr window ran (start_embr points TMP / TEMP at temp/)."""


# ---- tool environment -------------------------------------------------------------------


def tool_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for idf.py / pio: the original one, without this repository's
    virtual environment (idf.py's export would otherwise pick up its python.exe)."""
    env = dict(_ORIGINAL_ENV)
    for key in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "QT_QPA_PLATFORM", "UV_RUN_RECURSION"):
        env.pop(key, None)
    venv = str(kit.REPO / ".venv").lower()
    parts = env.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(p for p in parts if not p.lower().startswith(venv))
    env.update(extra or {})
    return env


def _tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-lines:])


@dataclass(frozen=True)
class Idf:
    """One ESP-IDF installation, used through its PowerShell activation script."""

    script: Path
    tools: str = ""
    """IDF_TOOLS_PATH to set before the script; empty: as the script sets it."""

    def run(self, project: Path, *args: str, timeout: float = BUILD_TIMEOUT) -> str:
        """`idf.py <args>` in `project`; pytest.fail with the output's tail on failure."""
        quoted = " ".join(_ps_quote(a) for a in args)
        lines = [
            "$ErrorActionPreference = 'Continue'",
            f"$env:IDF_TOOLS_PATH = {_ps_quote(self.tools)}" if self.tools else "",
            f". {_ps_quote(str(self.script))} *> $null",
            f"Set-Location -LiteralPath {_ps_quote(str(project))}",
            f"idf.py {quoted}",
            "exit $LASTEXITCODE",
        ]
        command = "; ".join(line for line in lines if line)
        started = time.monotonic()
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=tool_env({"CMAKE_BUILD_PARALLEL_LEVEL": BUILD_JOBS}),
            creationflags=LOW_PRIORITY,
            check=False,
        )
        output = done.stdout + done.stderr
        if done.returncode != 0:
            pytest.fail(
                f"idf.py {' '.join(args)} failed ({done.returncode}) with {self.script} in "
                f"{project}:\n{_tail(output)}"
            )
        print(f"[build] idf.py {' '.join(args)}: {time.monotonic() - started:.0f} s")
        return output


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def idf_main() -> Idf:
    """The ESP-IDF of EMBR_TEST_IDF_EXPORT (skip when unset)."""
    script = kit.env_path(IDF_EXPORT_VAR, kind="file")
    return Idf(script, os.environ.get(IDF_TOOLS_VAR, "").strip())


def idf_second() -> Idf:
    """The second ESP-IDF of EMBR_TEST_IDF_EXPORT_2 (skip when unset)."""
    return Idf(kit.env_path(IDF_EXPORT_2_VAR, kind="file"))


def find_pio() -> Path:
    """pio the way Embr finds it (PATH, %PLATFORMIO_CORE_DIR%, %USERPROFILE%\\.platformio)."""
    from embr.core.firmware.import_pio import find_pio as embr_find_pio

    found = embr_find_pio(tool_env(), shutil.which)
    if found is None:
        pytest.skip("pio not found")
    return found


def pio_core() -> Path:
    from embr.core.firmware.import_pio import core_dir

    return core_dir(tool_env())


def pio(project: Path, *args: str, timeout: float = BUILD_TIMEOUT, check: bool = True) -> str:
    """`pio <args>` in `project`; pytest.fail with the output's tail on failure (check)."""
    started = time.monotonic()
    done = subprocess.run(
        [str(find_pio()), *args, *(["-j", BUILD_JOBS] if args[:1] == ("run",) else [])],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=tool_env({"PLATFORMIO_NO_ANSI": "true", "PYTHONIOENCODING": "utf-8"}),
        creationflags=LOW_PRIORITY,
        check=False,
    )
    output = done.stdout + done.stderr
    if check and done.returncode != 0:
        pytest.fail(f"pio {' '.join(args)} failed ({done.returncode}):\n{_tail(output)}")
    print(f"[build] pio {' '.join(args)}: {time.monotonic() - started:.0f} s")
    return output


# ---- project backup ---------------------------------------------------------------------


@dataclass
class ProjectBackup:
    """Every file of a project (except the skipped top-level folders: build output, .git)
    kept in memory and copied to `store`; restore() puts the project back byte for byte
    (changed files rewritten, new files and folders removed, deleted ones recreated)."""

    root: Path
    store: Path
    skip: frozenset[str]
    files: dict[str, bytes] = field(default_factory=lambda: dict[str, bytes]())
    folders: set[str] = field(default_factory=lambda: set[str]())

    def capture(self) -> None:
        self.files.clear()
        self.folders.clear()
        for path in self._walk():
            rel = path.relative_to(self.root).as_posix()
            if path.is_dir():
                self.folders.add(rel)
            else:
                self.files[rel] = path.read_bytes()
                target = self.store / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.files[rel])

    def _walk(self) -> Iterator[Path]:
        for top in sorted(self.root.iterdir()):
            if top.name in self.skip:
                continue
            yield top
            if top.is_dir():
                yield from sorted(top.rglob("*"))

    def differences(self) -> list[str]:
        """What differs from the captured state: "changed x", "new x", "missing x"."""
        found: list[str] = []
        seen: set[str] = set()
        for path in self._walk():
            rel = path.relative_to(self.root).as_posix()
            seen.add(rel)
            if path.is_dir():
                if rel not in self.folders:
                    found.append(f"new {rel}/")
            elif rel not in self.files:
                found.append(f"new {rel}")
            elif path.read_bytes() != self.files[rel]:
                found.append(f"changed {rel}")
        found += [f"missing {r}" for r in sorted(set(self.files) | self.folders) if r not in seen]
        return found

    def restore(self) -> None:
        for path in sorted(self._walk(), key=lambda p: -len(p.parts)):
            rel = path.relative_to(self.root).as_posix()
            if path.is_dir() and rel not in self.folders:
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file() and rel not in self.files:
                path.unlink()
        for rel in sorted(self.folders):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        for rel, data in self.files.items():
            path = self.root / rel
            if not path.is_file() or path.read_bytes() != data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)


def git_status(repo: Path) -> list[str]:
    done = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return sorted(line for line in done.stdout.splitlines() if line.strip())


# ---- the ESP-IDF test project -----------------------------------------------------------

HELLO_SOURCE = "main/hello_world_main.c"
COUNTDOWN = 'printf("Restarting in %d seconds...\\n", i);'
MARK = "EMBR-MARK"


def build_facts(build: Path) -> tuple[str, str] | None:
    """(target, IDF version) of a build folder, None when it has no description."""
    try:
        data = json.loads((build / "project_description.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return str(data.get("target", "")), str(data.get("git_revision", ""))


@dataclass
class IdfProject:
    """The ESP-IDF test project for one session (fixture idf_project in conftest.py)."""

    root: Path
    idf: Idf
    backup: ProjectBackup
    status: list[str]
    """git status --porcelain when the session started."""
    original: tuple[str, str]
    """(target, IDF version) of the original build."""

    @property
    def build(self) -> Path:
        return self.root / "build"

    @property
    def name(self) -> str:
        data = json.loads((self.build / "project_description.json").read_text(encoding="utf-8"))
        return str(data["project_name"])

    def reset(self) -> None:
        """Sources and configuration as they were; the original target built with the
        original ESP-IDF (a full build when the build folder holds another target or
        version; otherwise incremental, i.e. seconds)."""
        self.backup.restore()
        if build_facts(self.build) != self.original:
            shutil.rmtree(self.build, ignore_errors=True)
        self.idf.run(self.root, "build")
        self.backup.restore()  # a build may rewrite sdkconfig(.old); keep the originals

    def mark(self, text: str) -> None:
        """Make the countdown line print `EMBR-MARK <text>` (once a second)."""
        path = self.root / HELLO_SOURCE
        source = path.read_text(encoding="utf-8")
        original = self.backup.files[HELLO_SOURCE].decode("utf-8")
        if COUNTDOWN not in original:
            pytest.fail(f"{path} has no line {COUNTDOWN!r} to put a marker on")
        line = f'printf("Restarting in %d seconds... {MARK} {text}\\n", i);'
        source = re.sub(r'printf\("Restarting in %d seconds\.\.\.[^;]*;', lambda _: line, source)
        path.write_text(source, encoding="utf-8")

    def restore_final(self) -> list[str]:
        """Restore, rebuild the original target, verify; returns problems (empty: ok)."""
        problems: list[str] = []
        self.backup.restore()
        try:
            if build_facts(self.build) != self.original:
                shutil.rmtree(self.build, ignore_errors=True)
            self.idf.run(self.root, "build")
        except BaseException as exc:  # pytest.fail inside: report, keep restoring
            problems.append(f"rebuild failed: {exc}")
        self.backup.restore()
        problems += self.backup.differences()
        status = git_status(self.root)
        if status != self.status:
            problems.append(f"git status {status} (was {self.status})")
        if build_facts(self.build) != self.original:
            problems.append(f"build is {build_facts(self.build)}, was {self.original}")
        return problems


# ---- the PlatformIO test project --------------------------------------------------------

PIO_INI = "platformio.ini"
ARDUINO_MAIN = "src/main.cpp"
PIO_MARK_LINE = 'Serial.println("{mark} " EMBR_PIO_MARK);'


@dataclass
class PioProject:
    """The PlatformIO test project for one session (fixture pio_project in conftest.py)."""

    root: Path
    backup: ProjectBackup
    original_env: str
    """The env whose build the session ends with (rebuilt by restore_final())."""

    def build_dir(self, env: str) -> Path:
        return self.root / ".pio" / "build" / env

    def reset(self) -> None:
        """Sources and platformio.ini as they were (the build folders stay as they are)."""
        self.backup.restore()

    def write_ini(self, text: str) -> None:
        (self.root / PIO_INI).write_text(text, encoding="utf-8")

    def mark_sketch(self) -> None:
        """The Arduino sketch prints `EMBR-MARK <EMBR_PIO_MARK>` every loop; the env sets
        the macro with build_flags = -DEMBR_PIO_MARK=\\"...\\" (an env without it prints
        "none")."""
        path = self.root / ARDUINO_MAIN
        source = self.backup.files[ARDUINO_MAIN].decode("utf-8")
        head = '#ifndef EMBR_PIO_MARK\n#define EMBR_PIO_MARK "none"\n#endif\n'
        if "void loop() {" not in source:
            pytest.fail(f"{path} has no loop() to put a marker in")
        # the boards' pins are not ours to drive: lines setting or writing a GPIO output
        # (the original sketch blinks an LED pin) are left out of the test builds
        source = "".join(
            line
            for line in source.splitlines(keepends=True)
            if not re.search(r"\b(pinMode|digitalWrite)\s*\(", line)
        )
        source = source.replace("#include <Arduino.h>", "#include <Arduino.h>\n" + head, 1)
        source = source.replace(
            "void loop() {", "void loop() {\n  " + PIO_MARK_LINE.format(mark=MARK), 1
        )
        path.write_text(source, encoding="utf-8")

    def run(self, *args: str, timeout: float = BUILD_TIMEOUT, check: bool = True) -> str:
        return pio(self.root, *args, timeout=timeout, check=check)

    def restore_final(self) -> list[str]:
        problems: list[str] = []
        self.backup.restore()
        try:
            self.run("run", "-e", self.original_env)
        except BaseException as exc:
            problems.append(f"rebuild failed: {exc}")
        self.backup.restore()
        problems += self.backup.differences()
        firmware = self.build_dir(self.original_env) / "firmware.bin"
        if not firmware.is_file():
            problems.append(f"{firmware} missing after the rebuild")
        return problems


def ini_envs_of(root: Path) -> list[str]:
    text = (root / PIO_INI).read_text(encoding="utf-8")
    return re.findall(r"(?m)^[ \t]*\[env:([^\]]+)\]", text)


def ini_env(name: str, board: str, mark: str | None = None, extra: str = "") -> str:
    """One [env:...] section (Arduino on the official platform unless `extra` overrides)."""
    lines = [f"[env:{name}]", "platform = espressif32", f"board = {board}", "framework = arduino"]
    if mark is not None:
        lines.append(f'build_flags = -DEMBR_PIO_MARK=\\"{mark}\\"')
    text = "\n".join(lines) + "\n"
    for line in extra.strip().splitlines():
        key = line.split("=", 1)[0].strip()
        text = re.sub(rf"(?m)^{re.escape(key)} = .*\n", "", text)
        text += line.strip() + "\n"
    return text + "\n"


# ---- boards -----------------------------------------------------------------------------


def connection_of(device: str) -> Any:
    from embr.core.serial import list_ports

    found = [p for p in list_ports() if p.device.upper() == device.upper()]
    if not found:
        pytest.fail(f"{device} is not connected")
    return found[0].connection


def native(device: str) -> bool:
    from embr.core.serial import ConnectionType

    return connection_of(device) != ConnectionType.UART_BRIDGE


def _esptool(device: str, *args: str, timeout: float = 300) -> str:
    kit.wait_port(device, True, 20)
    after = "watchdog-reset" if native(device) else "hard-reset"
    argv = [sys.executable, "-m", "esptool", "--port", device, "--after", after, *args]
    output = ""
    for attempt in range(3):
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = done.stdout + done.stderr
        if done.returncode == 0:
            return output
        time.sleep(1.5 + attempt)  # a native port re-enumerating after the last reset
        kit.wait_port(device, True, 20)
    pytest.fail(f"esptool {' '.join(args)} on {device} failed:\n{_tail(output)}")


def read_back(device: str, address: int, size: int, folder: Path) -> bytes:
    """`size` bytes of flash at `address`, read with esptool (independent of Embr)."""
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"readback-{device}-{address:x}-{size:x}-{time.monotonic_ns()}.bin"
    _esptool(device, "read-flash", f"0x{address:x}", f"0x{size:x}", str(out))
    data = out.read_bytes()
    out.unlink()
    return data


def write_back(device: str, pairs: Sequence[tuple[int, Path]]) -> None:
    """Put known files on the board with esptool (repair after a test that wrote junk)."""
    args = ["write-flash"]
    for address, path in pairs:
        args += [f"0x{address:x}", str(path)]
    _esptool(device, *args)


def regions(device: str, spans: Sequence[tuple[int, int]], folder: Path) -> list[bytes]:
    return [read_back(device, a, s, folder) for a, s in spans]


def listen(device: str, seconds: float, *, reset: bool = False) -> str:
    """What the board prints on `device` for `seconds` (the port is reopened when it
    re-enumerates). A UART bridge is opened with DTR and RTS released (no reset) unless
    `reset` (then the board is reset through RTS first: release_kit.serial_after_reset)."""
    import serial

    if reset and not native(device):
        return kit.serial_after_reset(device, seconds)
    chunks: list[bytes] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with serial.Serial() as port:
                port.port = device
                port.baudrate = 115200
                port.timeout = 0.1
                port.dtr = False
                port.rts = False
                port.open()
                while time.monotonic() < deadline:
                    chunks.append(port.read(4096))
        except serial.SerialException:
            time.sleep(0.2)
    return b"".join(chunks).decode("utf-8", errors="replace")


def open_port_now(device: str, window_s: float = 0.0) -> None:
    """Open and close `device` with pyserial (another program using the port); native USB
    gets `window_s` to re-enumerate after the reset that ended a job."""
    import serial

    deadline = time.monotonic() + window_s
    while True:
        try:
            serial.Serial(device).close()
            return
        except serial.SerialException as exc:
            if time.monotonic() > deadline:
                pytest.fail(f"{device} cannot be opened by another program: {exc}")
            time.sleep(0.2)


def fixture_pairs(sample: str) -> list[tuple[int, Path]]:
    """(address, file) of an IDF fixture sample's flasher_args.json."""
    build = kit.IDF / sample / "build"
    args = cast(
        dict[str, Any], json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    )
    files = cast(dict[str, str], args["flash_files"])
    return sorted((int(a, 16), build / f) for a, f in files.items())


# ---- driving the window -----------------------------------------------------------------


def combo_devices(window: Any) -> list[str]:
    combo = cast(QComboBox, window.connection.port)
    return [combo.itemText(i).split(" ", 1)[0] for i in range(combo.count())]


def wait_combo_port(window: Any, device: str, present: bool = True, timeout: float = 20) -> None:
    kit.wait_until(
        lambda: (device in combo_devices(window)) == present,
        timeout,
        message=lambda: f"{device} {'not' if present else 'still'} in {combo_devices(window)}",
    )


def pick_port(window: Any, device: str) -> None:
    wait_combo_port(window, device)
    kit.pick_combo(window.connection.port, f"{device} ", contains=True)


def identify(window: Any, device: str) -> None:
    """Pick the port and Connect & identify; fail unless it ends "Identified"."""
    pick_port(window, device)
    kit.click(window.connection.identify)
    result = kit.wait_job_finished(window, 120)
    if not kit.same_text(result, "Identified"):
        pytest.fail(f"identify on {device}: {result!r}\n{kit.log_text(window)[-3000:]}")


def flash(window: Any, *, attempts: int = 2) -> str:
    """Click Flash (enabled) and wait for the result text. A click the window turns down
    because the build changed since it was shown (logged "[flash] not started") is
    repeated, as a user would."""
    for _ in range(attempts):
        kit.wait_until(
            lambda: window.connection.flash.isEnabled(),
            15,
            message=lambda: f"Flash stays disabled; checks: {kit.check_texts(window)}",
        )
        kit.click(window.connection.flash)
        kit.pump(200)
        if not kit.job_running(window) and NOT_STARTED in kit.log_text(window):
            kit.pump(int(SETTLE_S * 1000))
            continue
        return kit.wait_job_finished(window)
    pytest.fail(f"Flash was turned down {attempts} times:\n{kit.log_text(window)[-2000:]}")


NOT_STARTED = "[flash] not started"
"""Logged when a click on Flash finds the build changed since it was shown."""


def last_record(app: kit.EmbrApp) -> Any:
    found = app.context.history.query(limit=1)
    return found[0] if found else None


def records(app: kit.EmbrApp) -> list[Any]:
    return cast(list[Any], app.context.history.query())


def firmware_by_name(app: kit.EmbrApp, name: str) -> Any:
    hits = [f for f in app.context.firmware.all() if f.name == name]
    if len(hits) != 1:
        pytest.fail(f"{len(hits)} firmware named {name!r}")
    return hits[0]


def status_of(app: kit.EmbrApp, name: str) -> Any:
    """The tracker's full check of a firmware (what selecting it shows)."""
    return app.window.library.tracker.check(firmware_by_name(app, name).id)


def wait_state(app: kit.EmbrApp, name: str, *states: str, timeout: float = 30) -> Any:
    """Re-check the firmware (as window activation does) until its state is one of
    `states` (ResolveState values)."""
    found: list[Any] = []

    def ok() -> bool:
        try:
            kit.click_sidebar(app.window, name)
        except RuntimeError:  # the tree was rebuilt under the click (a background refresh)
            return False
        status = status_of(app, name)
        found[:] = [status]
        return str(status.state.value) in states

    kit.wait_until(
        ok,
        timeout,
        message=lambda: (
            f"{name}: state {found[0].state if found else '?'}, wanted {states}; "
            f"problems {found[0].problems if found else ''}"
        ),
        interval_ms=500,
    )
    app.window.changed()  # the window shows this state too (banner, checks, Flash)
    kit.pump(100)
    return found[0]


def banner_text(window: Any) -> str:
    banner = window.firmware_view.banner
    if not banner.isVisible():
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(banner.text.text()))).strip()


def confirm_banner(window: Any) -> None:
    """Click the banner's Confirm entries button."""
    banner = window.firmware_view.banner
    kit.wait_until(banner.isVisible, 10, message="no banner")
    if not kit.same_text(banner.action.text(), "Confirm entries"):
        pytest.fail(f"banner button is {banner.action.text()!r}: {banner_text(window)}")
    kit.click(banner.action)


def new_fixed_firmware(
    window: Any,
    dialogs: kit.FileDialogs,
    name: str,
    files: Sequence[Path],
    *,
    place: str = "Standalone (no project)",
) -> None:
    """New firmware (New menu) placed in `place`, then + Add entry > Files… with `files`."""

    def fill(driver: kit.DialogDriver) -> None:
        driver.type("Name", name)
        driver.pick("Place in", place)
        driver.ok()

    with kit.expect_dialog(fill, title="New firmware"):
        new_menu(window, "New firmware")
    kit.click_sidebar(window, name)
    add_files(window, dialogs, files)


def new_menu(window: Any, item: str, attempts: int = 3) -> None:
    """release_kit.new_menu() on the window brought to the front first; when the menu did
    not open (seen once on a heavily loaded machine after a long build had blocked the
    event loop), the click is repeated."""
    for attempt in range(attempts):
        window.raise_()
        window.activateWindow()
        kit.pump(200)
        try:
            kit.new_menu(window, item)
            return
        except AssertionError as exc:
            if "did not appear" not in str(exc) or attempt == attempts - 1:
                raise


def add_files(window: Any, dialogs: kit.FileDialogs, files: Sequence[Path]) -> None:
    dialogs.queue_open_many(list(files))
    kit.choose_menu_from(window.firmware_view.entries.add_button, "Files…")
    kit.pump(100)


def new_project(window: Any, name: str) -> None:
    def fill(driver: kit.DialogDriver) -> None:
        driver.type("Name (unique)", name)
        driver.ok()

    with kit.expect_dialog(fill, title="New project"):
        new_menu(window, "New project")


def import_idf(
    window: Any,
    dialogs: kit.FileDialogs,
    build: Path,
    *,
    name: str = "",
    place: str = "Standalone (no project)",
) -> None:
    """New > Import ESP-IDF build…, Browse… to `build`, wait for the preview, Import."""

    def fill(driver: kit.DialogDriver) -> None:
        dialogs.queue_directory(build)
        driver.press("Browse…")
        kit.wait_until(lambda: driver.ok_enabled() or _preview_shown(driver), 30)
        if name:
            driver.type("Name", name)
        driver.pick("Place in", place)
        driver.ok()

    with kit.expect_dialog(fill, title="Import ESP-IDF build"):
        new_menu(window, "Import ESP-IDF build…")
    kit.pump(200)


def _preview_shown(driver: kit.DialogDriver) -> bool:
    return any("ESP-IDF" in t for t in driver.texts())


def env_boxes(driver: kit.DialogDriver) -> dict[str, QCheckBox]:
    """The env check boxes of the PlatformIO import dialog, by env name."""
    return {
        kit.text_of(b).split(" · ", 1)[0].strip(): b for b in kit.find_all(driver.dialog, QCheckBox)
    }


def import_pio(
    window: Any,
    dialogs: kit.FileDialogs,
    env_dir: Path,
    envs: Sequence[str],
    *,
    place: str = "Standalone (no project)",
    inspect: Callable[[kit.DialogDriver], None] | None = None,
) -> None:
    """New > Import PlatformIO build…, Browse… to `env_dir`, wait for the env list, check
    exactly `envs`, Import (`inspect` sees the dialog first)."""

    def fill(driver: kit.DialogDriver) -> None:
        dialogs.queue_directory(env_dir)
        driver.press("Browse…")
        kit.wait_until(
            lambda: env_dir.name in env_boxes(driver),
            90,
            message=lambda: f"env list not shown: {driver.texts()}",
        )
        kit.pump(200)
        if inspect is not None:
            inspect(driver)
        for env, box in env_boxes(driver).items():
            if box.isEnabled():
                kit.set_checked(box, env in envs)
        driver.pick("Place in", place)
        driver.ok()

    with kit.expect_dialog(fill, title="Import PlatformIO build", timeout=120):
        new_menu(window, "Import PlatformIO build…")
    kit.pump(200)


def entries_of(app: kit.EmbrApp, name: str) -> list[tuple[int | None, str, str, bool]]:
    """(address, name, file reference, enabled) of the firmware's resolved entries."""
    firmware = status_of(app, name).firmware
    return [(e.address, e.name, e.file, e.enabled) for e in firmware.entries]


def sidebar_dimmed(window: Any, name: str) -> bool:
    """The firmware row is dimmed (not for the identified board)."""
    from PySide6.QtWidgets import QGraphicsOpacityEffect

    item = kit.sidebar_row(window, name)
    widget = cast(QWidget, window.sidebar_panel.tree.itemWidget(item, 0))
    effect = widget.graphicsEffect()
    tip = widget.toolTip()
    told = "Not for the identified board" in tip or "不适用于已识别的设备" in tip
    return isinstance(effect, QGraphicsOpacityEffect) and effect.opacity() < 0.6 and told


def checks(window: Any) -> str:
    return " | ".join(kit.check_texts(window))


def has_check(window: Any, title: str) -> bool:
    return any(kit.same_text(t, title) for t in kit.check_texts(window))


def first_line_edit(driver: kit.DialogDriver) -> QLineEdit:
    edits = kit.find_all(driver.dialog, QLineEdit)
    if not edits:
        pytest.fail(f"no line edit in dialog {driver.title!r}")
    return edits[0]


@contextlib.contextmanager
def dismissing(match: Callable[[QDialog], bool]) -> Generator[list[str]]:
    """While the block runs, every visible dialog for which `match` is true is rejected
    (its texts recorded in the yielded list), so a dialog the product should not have
    opened cannot hang the test; the test then asserts the list is empty."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    seen: list[str] = []
    done: set[int] = set()

    def poll() -> None:
        for widget in QApplication.topLevelWidgets():
            if not isinstance(widget, QDialog) or not widget.isVisible() or id(widget) in done:
                continue
            if match(widget):
                done.add(id(widget))
                texts = [kit.text_of(w) for w in widget.findChildren(QLabel)]
                seen.append(f"{widget.windowTitle()}: {' / '.join(t for t in texts if t)}")
                widget.reject()

    timer = QTimer()
    timer.setInterval(100)
    timer.timeout.connect(poll)
    timer.start()
    try:
        yield seen
    finally:
        timer.stop()


@contextlib.contextmanager
def restoring_board(device: str, pairs: Sequence[tuple[int, Path]]) -> Generator[None]:
    """Write `pairs` back with esptool afterwards when the block failed (the board keeps a
    test program)."""
    try:
        yield
    except BaseException:
        with contextlib.suppress(BaseException):
            write_back(device, pairs)
        raise
