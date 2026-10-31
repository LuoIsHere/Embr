"""Program-level release tests: real Embr processes (`python -m embr` from a copy of
src/, whose portable root is <copy>/.run as for any source run, or the packaged
Embr.exe), started with empty sentinel folders as TEMP / TMP / APPDATA / LOCALAPPDATA /
USERPROFILE / working folder, and checked through their windows (UI Automation) and
files.

Design rules checked (design doc §7.6): startup clears temp/ and keeps logs/ and data/;
a program folder that is not writable gets a message and nothing is written elsewhere;
one working instance per folder; an old embr.sqlite is migrated with a backup; the
packaged build starts, writes only inside its folder and exits cleanly."""

import hashlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import release_kit as kit

from embr.app.context import open_context
from embr.app.library import Library
from embr.core.storage.paths import AppPaths, prepare

NOT_WRITABLE = "is not writable"
ALREADY_RUNNING = "Embr is already running from this folder"


@pytest.fixture
def program(tmp_path: Path) -> Path:
    """A fresh program folder: a copy of src/; its portable root is program/.run."""
    return kit.source_copy(tmp_path / "program")


def _launch(
    program: Path, sentinels: kit.Sentinels, tmp_path: Path, name: str = "embr"
) -> kit.EmbrProcess:
    return kit.EmbrProcess.source(
        program,
        env=sentinels.env(),
        cwd=sentinels.folder("cwd"),
        logs=tmp_path / "process-logs",
        name=name,
    )


def _assert_clean(sentinels: kit.Sentinels, listing: dict[Path, set[str]]) -> None:
    assert sentinels.leftovers() == [], "written outside the program folder"
    assert kit.suspicious_new_entries(listing) == []


def _seed(root: Path, tmp_path: Path) -> dict[str, bytes]:
    """A project, a fixed firmware with the S3 hello files (archived as a version) in
    `root`, made with Embr's own data layer; returns relative path -> bytes of data/."""
    paths = AppPaths(root)
    prepare(paths)
    context = open_context(paths)
    try:
        library = Library(context)
        project = library.create_project("Kept project", "seeded")
        firmware = library.create_firmware("Kept firmware", project.id, "")
        library.add_files(firmware.id, kit.hello_files("esp32s3_hello", tmp_path / "files"))
    finally:
        context.close()
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in (root / "data").rglob("*")
        if p.is_file() and p.suffix == ".toml"
    }


def test_startup_clears_temp_and_keeps_logs_and_data(
    program: Path, sentinels: kit.Sentinels, tmp_path: Path
) -> None:
    root = program / ".run"
    data = _seed(root, tmp_path)
    junk = [root / "temp" / "read-01ABC.bin", root / "temp" / "sub" / "deep" / "x.txt"]
    for path in junk:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"left over")
    logs = {
        root / "logs" / "2026-01-01.log": b"=== 01OLD | flash | 2026-01-01 10:00:00\n",
        root / "logs" / "boards" / "aa-bb-cc-dd-ee-ff.log": b"board log\n",
    }
    for path, content in logs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    listing = kit.user_folder_listing()
    with _launch(program, sentinels, tmp_path) as process:
        assert process.wait_main_window() is not None, process.stderr()
        assert sorted(p.name for p in (root / "temp").iterdir()) == ["embr.lock"]
        for path, content in logs.items():
            assert path.read_bytes() == content
        for rel, content in data.items():
            assert (root / rel).read_bytes() == content
        assert process.close() == 0, process.stderr()
    assert "Traceback" not in process.stderr()
    _assert_clean(sentinels, listing)


def test_program_folder_not_writable_shows_message_and_exits(
    program: Path, sentinels: kit.Sentinels, tmp_path: Path
) -> None:
    root = program / ".run"
    root.mkdir()
    before = kit.snapshot(program)
    listing = kit.user_folder_listing()
    with kit.deny_access(root), _launch(program, sentinels, tmp_path) as process:
        message = process.wait_any_window()
        assert message is not None, f"exited without a message: {process.stderr()}"
        assert message.class_name != kit.MAIN_CLASS
        assert NOT_WRITABLE in message.text and str(root) in message.text, message.texts
        assert process.press("OK") or process.close() is not None
        assert process.wait() == 1
    assert kit.snapshot(program) == before
    assert "Traceback" not in process.stderr()
    _assert_clean(sentinels, listing)


def test_second_instance_shows_message_and_first_keeps_running(
    program: Path, sentinels: kit.Sentinels, tmp_path: Path
) -> None:
    root = program / ".run"
    listing = kit.user_folder_listing()
    with _launch(program, sentinels, tmp_path, "first") as first:
        assert first.wait_main_window() is not None, first.stderr()
        with _launch(program, sentinels, tmp_path, "second") as second:
            message = second.wait_any_window()
            assert message is not None, f"second exited without a message: {second.stderr()}"
            assert message.class_name != kit.MAIN_CLASS, "a second working window opened"
            assert ALREADY_RUNNING in message.text and str(root) in message.text
            assert [w.class_name for w in second.windows()] == [message.class_name]
            assert second.press("OK")
            assert second.wait() == 1
        assert first.running()
        assert [w.class_name for w in first.windows()] == [kit.MAIN_CLASS]
        assert (root / "temp" / "embr.lock").is_file()
        assert first.close() == 0, first.stderr()
    assert "Traceback" not in first.stderr() + second.stderr()
    _assert_clean(sentinels, listing)


def test_v1_database_migrated_on_start_with_backup(
    program: Path, sentinels: kit.Sentinels, tmp_path: Path
) -> None:
    root = program / ".run"
    (root / "data").mkdir(parents=True)
    original = kit.DB_V1.read_bytes()
    shutil.copyfile(kit.DB_V1, root / "data" / "embr.sqlite")
    listing = kit.user_folder_listing()
    with _launch(program, sentinels, tmp_path) as process:
        assert process.wait_main_window() is not None, process.stderr()
        assert process.close() == 0, process.stderr()
    backups = [p for p in (root / "data" / "backup").rglob("*") if p.is_file()]
    assert [p.name for p in backups] == ["embr.sqlite"]
    assert backups[0].read_bytes() == original
    assert backups[0].parent.parent == root / "data" / "backup"  # backup/<timestamp>/
    connection = sqlite3.connect(root / "data" / "embr.sqlite")
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        rows = connection.execute("SELECT id, operation, firmware_name FROM flash_records")
        assert rows.fetchall() == [("01JC0000000000000000000001", "flash", "Sample firmware")]
    finally:
        connection.close()
    assert hashlib.sha256(original).hexdigest() == (
        "ac34b0e7fad17c59977d0e1b9614a00a0be7d13aaa2ec5ef4b64841505b12726"
    )
    _assert_clean(sentinels, listing)


# ---- packaged build ---------------------------------------------------------------------

DIST_EXE = kit.REPO / "dist" / "Embr" / "Embr.exe"


def _packaged_build() -> Path:
    """dist/Embr, built with `uv run pyinstaller Embr.spec --noconfirm` unless an Embr.exe
    newer than every file in src/ (and Embr.spec) exists."""
    sources = [p for p in kit.SRC.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    newest = max(p.stat().st_mtime for p in [*sources, kit.REPO / "Embr.spec"])
    if not DIST_EXE.is_file() or DIST_EXE.stat().st_mtime <= newest:
        done = subprocess.run(
            ["uv", "run", "pyinstaller", "Embr.spec", "--noconfirm"],
            cwd=kit.REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
        )
        assert done.returncode == 0, done.stderr[-4000:]
    assert DIST_EXE.is_file()
    return DIST_EXE.parent


def test_packaged_build_starts_writes_only_its_folder_and_exits_cleanly(
    sentinels: kit.Sentinels, tmp_path: Path
) -> None:
    folder = tmp_path / "Embr"
    shutil.copytree(_packaged_build(), folder)
    exe = folder / "Embr.exe"
    before = kit.snapshot(folder)
    listing = kit.user_folder_listing()
    with kit.EmbrProcess(
        [str(exe)], env=sentinels.env(), cwd=sentinels.folder("cwd"), logs=tmp_path / "logs"
    ) as process:
        assert process.wait_main_window(90) is not None, process.stderr()
        for sub in ("data/projects", "data/firmwares", "data/archive", "logs", "temp"):
            assert (folder / sub).is_dir(), sub
        assert (folder / "data" / "embr.sqlite").is_file()
        assert process.close() == 0
    created = kit.new_entries(before, kit.snapshot(folder))
    assert created, "the packaged build wrote nothing into its folder"
    top = {c.split("/", 1)[0] for c in created}
    assert top <= {"data", "logs", "temp", "settings.ini"}, sorted(top)
    assert (folder / "settings.ini").is_file()
    _assert_clean(sentinels, listing)

    check = subprocess.run(
        [sys.executable, str(kit.REPO / "scripts" / "check_frozen_worker.py"), str(exe)],
        cwd=kit.REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert "ok: events arrive over pipes" in check.stdout
