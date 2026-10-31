"""Corrupted and foreign data, at startup (real `python -m embr` processes) and while the
real window runs.

Documented behaviour (src/embr/core/storage, firmware/store.py, project, files.py,
app/checklist.py, main.py) is asserted as written:

* settings.ini that is not valid INI: copied to data/backup/<timestamp>/, Embr starts
  with no settings;
* an empty embr.sqlite counts as missing (a new database is made);
* a project / firmware TOML with a higher schema_version: read-only, never modified;
* embr.sqlite with a higher user_version: "written by a newer version" message, exit 1,
  file untouched;
* an archive file that is gone: the check "File missing" refuses the flash; one whose
  bytes changed: "Needs confirmation" refuses it.

Where the behaviour is not documented (garbage, empty or truncated project / firmware
TOML and embr.sqlite; temp/ or logs/ replaced by a file or unreadable; a file corrupted
while Embr runs) the minimum requirement is asserted: no crash (no Python traceback, the
process shows a window), no silent loss of the other projects and firmware (their files
are byte-identical afterwards), a message to the user naming the damaged file or folder,
nothing written outside the program folder.
"""

import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import release_kit as kit

from embr.app.archiving import Archiver
from embr.app.context import open_context
from embr.app.library import Library
from embr.app.tracking import Tracker
from embr.core.storage.paths import AppPaths, prepare

NEWER_DATA = "written by a newer version of Embr"


@dataclass(frozen=True)
class Seed:
    """Ids in the seeded portable root (program/.run)."""

    kept_project: str
    victim_project: str
    kept_firmware: str
    victim_firmware: str
    archived_firmware: str
    standalone_firmware: str
    archive_sha: str


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Seed]]:
    """A program folder (source copy) whose .run holds two projects, firmware with files,
    an archived version and a standalone firmware, made with Embr's data layer, then
    opened and closed once by a real Embr process (settings.ini, migrated database)."""
    base = tmp_path_factory.mktemp("corrupt-template")
    program = kit.source_copy(base / "program")
    files = kit.hello_files("esp32s3_hello", base / "user-files")
    paths = AppPaths(program / ".run")
    prepare(paths)
    context = open_context(paths)
    try:
        library = Library(context)
        kept = library.create_project("Kept project", "must survive")
        victim = library.create_project("Victim project", "gets damaged")
        kept_fw = library.create_firmware("Kept firmware", kept.id, "")
        library.add_files(kept_fw.id, files)
        victim_fw = library.create_firmware("Victim firmware", victim.id, "")
        library.add_files(victim_fw.id, files)
        alone = library.create_firmware("Standalone firmware")
        library.add_files(alone.id, files)
        archived = Archiver(context, Tracker(context)).archive_version(kept_fw.id, name="Archived")
        sha = next(
            e.file.removeprefix("archive:") for e in archived.entries if e.kind.value == "app"
        )
    finally:
        context.close()
    sentinels = kit.Sentinels(base / "sentinels")
    with kit.EmbrProcess.source(
        program, env=sentinels.env(), cwd=sentinels.folder("cwd"), logs=base / "logs"
    ) as process:
        assert process.wait_main_window() is not None, process.stderr()
        assert process.close() == 0
    assert (paths.settings).is_file()
    yield program, Seed(kept.id, victim.id, kept_fw.id, victim_fw.id, archived.id, alone.id, sha)


def _copy(template: tuple[Path, Seed], tmp_path: Path) -> tuple[Path, Seed]:
    program, seed = template
    target = tmp_path / "program"
    shutil.copytree(program, target)
    return target, seed


def _documents(root: Path) -> dict[str, bytes]:
    found = {
        p.relative_to(root).as_posix(): p.read_bytes()
        for folder in ("data/projects", "data/firmwares", "data/archive")
        for p in (root / folder).glob("*")
        if p.is_file()
    }
    return found


@dataclass
class Outcome:
    main_window: kit.UiaWindow | None
    message: kit.UiaWindow | None
    returncode: int
    stderr: str

    @property
    def shown(self) -> str:
        windows = [w for w in (self.main_window, self.message) if w is not None]
        return "\n".join(w.text for w in windows)


def _start(program: Path, sentinels: kit.Sentinels, tmp_path: Path) -> Outcome:
    """Start Embr, take what it shows (its main window, or a message), close it."""
    with kit.EmbrProcess.source(
        program, env=sentinels.env(), cwd=sentinels.folder("cwd"), logs=tmp_path / "logs"
    ) as process:
        window = process.wait_any_window(60)
        if window is None:
            return Outcome(None, None, process.wait(), process.stderr())
        if window.class_name == kit.MAIN_CLASS:
            time.sleep(1.5)  # let the first refresh fill the window
            main = next((w for w in process.windows() if w.class_name == kit.MAIN_CLASS), window)
            others = [w for w in process.windows() if w.class_name != kit.MAIN_CLASS]
            code = process.close()
            return Outcome(main, others[0] if others else None, code, process.stderr())
        if not process.press("OK"):
            process.close()
        return Outcome(None, window, process.wait(), process.stderr())


def _assert_minimum(
    outcome: Outcome, before: dict[str, bytes], root: Path, damaged: str, mention: str
) -> None:
    """No crash, a window shown, the undamaged documents byte-identical, a message that
    names `mention`."""
    if "Traceback" in outcome.stderr:
        lines = [line for line in outcome.stderr.strip().splitlines() if line.strip()]
        where = [line.strip() for line in lines if line.strip().startswith('File "')][-3:]
        pytest.fail(f"crashed (exit {outcome.returncode}): {lines[-1]}\n" + "\n".join(where))
    assert outcome.main_window or outcome.message, f"no window (exit {outcome.returncode})"
    after = _documents(root)
    lost = [rel for rel, data in before.items() if rel != damaged and after.get(rel) != data]
    assert lost == [], f"other data changed or lost: {lost}"
    assert mention in outcome.shown, f"no message naming {mention!r}; shown:\n{outcome.shown}"


def _damage(path: Path, mode: str) -> None:
    data = path.read_bytes()
    if mode == "garbage":
        path.write_bytes(b"\x00\xff\xfe garbage \x81[[[ = = \x00" * 8)
    elif mode == "empty":
        path.write_bytes(b"")
    elif mode == "truncated":
        path.write_bytes(data[: len(data) // 2])
    else:
        raise ValueError(mode)


MODES = ["garbage", "empty", "truncated"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", ["project", "firmware"])
def test_damaged_document_at_startup(
    template: tuple[Path, Seed],
    tmp_path: Path,
    sentinels: kit.Sentinels,
    kind: str,
    mode: str,
) -> None:
    program, seed = _copy(template, tmp_path)
    root = program / ".run"
    if kind == "project":
        damaged = f"data/projects/{seed.victim_project}.toml"
    else:
        damaged = f"data/firmwares/{seed.victim_firmware}.toml"
    before = _documents(root)
    _damage(root / damaged, mode)
    outcome = _start(program, sentinels, tmp_path)
    _assert_minimum(outcome, before, root, damaged, Path(damaged).name)
    assert sentinels.leftovers() == []


@pytest.mark.parametrize("mode", MODES)
def test_damaged_settings_at_startup(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels, mode: str
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    before = _documents(root)
    _damage(root / "settings.ini", mode)
    damaged = (root / "settings.ini").read_bytes()
    outcome = _start(program, sentinels, tmp_path)
    assert "Traceback" not in outcome.stderr, outcome.stderr[-3000:]
    assert outcome.main_window is not None, f"did not start: {outcome.shown}"
    assert outcome.returncode == 0
    assert _documents(root) == before
    backups = [p for p in (root / "data" / "backup").rglob("settings.ini")]
    if kit.is_valid_ini(damaged):
        assert backups == []
    else:  # documented: copied to backup/<timestamp>/, Embr starts with no settings
        assert [b.read_bytes() for b in backups] == [damaged]
    assert kit.is_valid_ini((root / "settings.ini").read_bytes())  # rewritten on close
    assert sentinels.leftovers() == []


@pytest.mark.parametrize("mode", MODES)
def test_damaged_database_at_startup(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels, mode: str
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    before = _documents(root)
    database = root / "data" / "embr.sqlite"
    _damage(database, mode)
    damaged = database.read_bytes()
    outcome = _start(program, sentinels, tmp_path)
    if mode == "empty":  # documented: an empty file counts as no database
        assert outcome.main_window is not None and outcome.returncode == 0, outcome.stderr
        assert _documents(root) == before
    else:
        _assert_minimum(outcome, before, root, "", "embr.sqlite")
        if outcome.main_window is None:  # refused: the damaged file must be kept as it was
            assert database.read_bytes() == damaged
    assert sentinels.leftovers() == []


@pytest.mark.parametrize("kind", ["project", "firmware"])
def test_newer_document_is_read_only_and_kept(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels, kind: str
) -> None:
    program, seed = _copy(template, tmp_path)
    root = program / ".run"
    if kind == "project":
        path = root / "data" / "projects" / f"{seed.victim_project}.toml"
    else:
        path = root / "data" / "firmwares" / f"{seed.victim_firmware}.toml"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("schema_version = 1\n")
    newer = text.replace("schema_version = 1\n", "schema_version = 99\n", 1).encode()
    path.write_bytes(newer + b'future_field = "from a newer Embr"\n')
    newer = path.read_bytes()
    before = _documents(root)
    outcome = _start(program, sentinels, tmp_path)
    assert "Traceback" not in outcome.stderr, outcome.stderr[-3000:]
    assert outcome.main_window is not None and outcome.returncode == 0
    assert path.read_bytes() == newer, "a newer document was modified"
    assert _documents(root) == before
    assert sentinels.leftovers() == []


def test_newer_database_refused_with_message_and_untouched(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels
) -> None:
    import sqlite3

    program, _ = _copy(template, tmp_path)
    database = program / ".run" / "data" / "embr.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    original = database.read_bytes()
    outcome = _start(program, sentinels, tmp_path)
    assert outcome.main_window is None, "opened data of a newer Embr"
    assert outcome.message is not None and NEWER_DATA in outcome.message.text, outcome.shown
    assert outcome.returncode == 1
    assert database.read_bytes() == original
    assert [p for p in (program / ".run" / "data" / "backup").rglob("embr.sqlite")] == []
    assert sentinels.leftovers() == []


@pytest.mark.parametrize("folder", ["temp", "logs"])
def test_folder_replaced_by_a_file(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels, folder: str
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    before = _documents(root)
    shutil.rmtree(root / folder)
    (root / folder).write_bytes(b"not a folder")
    outcome = _start(program, sentinels, tmp_path)
    _assert_minimum(outcome, before, root, "", str(root / folder))
    assert sentinels.leftovers() == []


def test_temp_folder_unreadable(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    before = _documents(root)
    (root / "temp" / "left-over.bin").write_bytes(b"x")
    with kit.deny_access(root / "temp", "(RD)"):  # listing the folder is denied
        outcome = _start(program, sentinels, tmp_path)
    _assert_minimum(outcome, before, root, "", str(root / "temp"))
    assert sentinels.leftovers() == []


# ---- while the real window runs ---------------------------------------------------------


def _running(
    template: tuple[Path, Seed], tmp_path: Path, embr: Callable[..., kit.EmbrApp]
) -> tuple[kit.EmbrApp, Seed]:
    program, seed = _copy(template, tmp_path)
    return embr(program / ".run"), seed


def _maybe_identify(app: kit.EmbrApp) -> None:
    """With EMBR_TEST_PORT_UART set, identify the board first, so a refused flash is
    refused by the file check and not only by a missing identification."""
    import os

    port = os.environ.get("EMBR_TEST_PORT_UART", "").strip()
    if port and kit.port_present(port):
        assert kit.same_text(kit.identify(app.window, port), "Identified"), app.log()


@pytest.mark.parametrize("when", ["startup", "running"])
@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_archive_file_missing_or_tampered_refuses_flash(
    template: tuple[Path, Seed],
    tmp_path: Path,
    embr: Callable[..., kit.EmbrApp],
    when: str,
    damage: str,
) -> None:
    program, seed = _copy(template, tmp_path)
    root = program / ".run"
    archive_file = root / "data" / "archive" / f"{seed.archive_sha}.bin"

    def spoil() -> None:
        if damage == "missing":
            archive_file.unlink()
        else:
            data = bytearray(archive_file.read_bytes())
            data[-1] ^= 0xFF
            archive_file.write_bytes(bytes(data))

    if when == "startup":
        spoil()
    app = embr(root)
    window = app.window
    _maybe_identify(app)
    kit.click_sidebar(window, "Archived")
    if when == "running":
        before = kit.check_texts(window)
        assert any(kit.same_text(t, "Files match") for t in before), before
        spoil()
        kit.click_sidebar(window, "Kept firmware")
        kit.click_sidebar(window, "Archived")
    expected = "File missing" if damage == "missing" else "Needs confirmation"
    kit.wait_until(
        lambda: any(kit.same_text(t, expected) for t in kit.check_texts(window)),
        10,
        message=lambda: f"{expected!r} not among the checks: {kit.check_texts(window)}",
    )
    assert not window.connection.flash.isEnabled()
    assert "Kept firmware" in kit.sidebar_names(window)


@pytest.mark.parametrize("kind", ["project", "firmware"])
def test_document_damaged_while_running(
    template: tuple[Path, Seed],
    tmp_path: Path,
    embr: Callable[..., kit.EmbrApp],
    kind: str,
) -> None:
    app, seed = _running(template, tmp_path, embr)
    window = app.window
    root = app.paths.root
    before = _documents(root)
    if kind == "project":
        damaged = f"data/projects/{seed.victim_project}.toml"
    else:
        damaged = f"data/firmwares/{seed.victim_firmware}.toml"
    _damage(root / damaged, "garbage")
    errors: list[BaseException] = []
    try:
        kit.click_sidebar(window, "Kept firmware")
        kit.click_sidebar(window, "Standalone firmware")
        kit.pump(500)
    except BaseException as exc:  # an exception that escaped a slot into the test
        errors.append(exc)
    names = kit.sidebar_names(window)
    shown = kit.log_text(window) + "\n" + "\n".join(kit.all_texts(window))
    assert errors == [], f"exception while refreshing: {errors!r}"
    assert {"Kept firmware", "Standalone firmware"} <= set(names), names
    after = _documents(root)
    assert [r for r, d in before.items() if r != damaged and after.get(r) != d] == []
    assert Path(damaged).name in shown, f"no message naming the damaged file:\n{shown[-2000:]}"


def test_logs_folder_not_writable_while_running(
    template: tuple[Path, Seed], tmp_path: Path, embr: Callable[..., kit.EmbrApp]
) -> None:
    """logs/ loses write access while Embr runs; Connect & identify must say so (no
    crash, the port is not left busy) instead of failing silently."""
    port = kit.board_port("EMBR_TEST_PORT_UART")
    app, _ = _running(template, tmp_path, embr)
    window = app.window
    logs = app.paths.logs
    with kit.deny_access(logs, "(OI)(CI)(W,AD)"):
        kit.pick_combo(window.connection.port, port, contains=True)
        kit.click(window.connection.identify)
        kit.pump(500)
        result = kit.wait_job_finished(window, 120)
        shown = kit.log_text(window)
    assert "logs" in shown, f"no message about logs/ (result {result!r}):\n{shown[-2000:]}"
    assert window.connection.identify.isEnabled()
    assert kit.same_text(kit.identify(window, port), "Identified"), app.log()
