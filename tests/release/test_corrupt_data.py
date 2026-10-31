"""Damaged and foreign data (design doc §12.8 and §7.6): real `python -m embr` processes
for what happens before the window opens, the real window for what it shows.

Specified behaviour asserted here:

* a project or firmware .toml that cannot be read (garbage, empty, truncated, bad
  values), at startup or found while running, is left out and everything else works; a
  dialog "Files that cannot be read" lists each file (name, folder, reason); a copy of
  each is kept in data/backup/<timestamp>/damaged/<name> (one per identical content, also
  across restarts); the original is not changed; firmware whose project file cannot be
  read are shown standalone and stay usable; a selected firmware whose file breaks while
  running behaves like a deleted one; each file is reported once per session;
* a damaged embr.sqlite (garbage, truncated, failing PRAGMA quick_check): a warning names
  the file and the reason, buttons "Back up and start anew" / "Exit". Exit: exit code 1,
  nothing changed. Back up: a save dialog for the copy (the native Windows dialog, driven
  as Win32 controls), then the damaged file and its -journal / -wal / -shm are removed
  and Embr starts with an empty database; cancelling the save dialog asks again;
* temp/, logs/ or a data/ folder occupied by a file, or temp/ not listable: a warning
  names each path, buttons "Rename and continue" / "Exit". Exit: code 1, nothing changed.
  Rename: what is in the way becomes "<name>.<YYYYmmdd-HHMMSS>" (-2, -3 when taken), new
  folders are created, Embr starts, nothing is deleted;
* startup messages follow the language stored in settings.ini (English and Chinese);
* settings.ini that is not valid INI is backed up and Embr starts with no settings; an
  empty embr.sqlite counts as missing; newer project / firmware files are read-only and
  never changed; a newer embr.sqlite is refused untouched;
* an archive file that is gone refuses the flash ("File missing"), one whose bytes
  changed too ("Needs confirmation");
* logs/ losing write access while running is reported when a job starts.
"""

import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import release_kit as kit

from embr import main as embr_main
from embr.app.archiving import Archiver
from embr.app.context import open_context
from embr.app.library import Library
from embr.app.tracking import Tracker
from embr.core.storage.paths import AppPaths, prepare
from embr.i18n import Language

DAMAGED_TITLE = "Files that cannot be read"
LANGUAGES = [Language.ENGLISH, Language.ZH_CN]


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
    assert paths.settings.is_file()
    yield program, Seed(kept.id, victim.id, kept_fw.id, victim_fw.id, archived.id, alone.id, sha)


def _copy(template: tuple[Path, Seed], tmp_path: Path) -> tuple[Path, Seed]:
    program, seed = template
    target = tmp_path / "program"
    shutil.copytree(program, target)
    return target, seed


def _documents(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for folder in ("data/projects", "data/firmwares", "data/archive")
        for p in (root / folder).glob("*")
        if p.is_file()
    }


def _launch(program: Path, sentinels: kit.Sentinels, tmp_path: Path, name: str) -> kit.EmbrProcess:
    return kit.EmbrProcess.source(
        program,
        env=sentinels.env(),
        cwd=sentinels.folder("cwd"),
        logs=tmp_path / "process-logs",
        name=name,
    )


def _no_traceback(process: kit.EmbrProcess) -> None:
    text = process.stderr()
    if "Traceback" in text:
        lines = [line for line in text.strip().splitlines() if line.strip()]
        pytest.fail(f"crashed: {lines[-1]}")


def _message(process: kit.EmbrProcess, timeout: float = 60.0) -> kit.UiaWindow:
    """The startup message (any window but the main window)."""
    window = process.wait_for(lambda w: w.class_name != kit.MAIN_CLASS, timeout)
    if window is None:
        _no_traceback(process)
        pytest.fail(f"exited ({process.popen.returncode}) without a message")
    return window


def _press(process: kit.EmbrProcess, button: str, language: Language) -> None:
    text = kit.translated(button, language)
    assert process.press(text), f"no button {text!r}: {[w.buttons for w in process.windows()]}"


# ---- project and firmware files ---------------------------------------------------------

DOCUMENT_MODES = ["garbage", "empty", "truncated", "bad-values"]


def _damage_document(path: Path, kind: str, mode: str) -> bytes:
    text = path.read_text(encoding="utf-8")
    if mode == "garbage":
        data = b"\x00\xff\xfe garbage \x81[[[ = = \x00" * 8
    elif mode == "empty":
        data = b""
    elif mode == "truncated":
        data = text.encode()[: len(text) // 2]
    elif kind == "project":  # bad values: a schema_version that is not a number
        data = text.replace("schema_version = 1\n", 'schema_version = "one"\n', 1).encode()
    else:  # bad values: a source Embr does not know
        data = re.sub(r'(?m)^source = ".*"$', 'source = "bogus"', text, count=1).encode()
    assert data != text.encode()
    path.write_bytes(data)
    return data


def _reason_ok(shown: str, kind: str, mode: str, language: Language) -> bool:
    english = {
        "garbage": "not UTF-8 text",
        "empty": "empty, or not an Embr project or firmware file",
        "truncated": "not valid TOML (",
        "bad-values": "empty, or not an Embr project or firmware file"
        if kind == "project"
        else "bogus",
    }[mode]
    if mode == "truncated":
        wanted = kit.translated("not valid TOML ({0})", language).split("{0}")[0]
        return wanted in shown
    return kit.translated(english, language) in shown


def _damaged_copies(root: Path, name: str) -> list[Path]:
    return sorted((root / "data" / "backup").glob(f"*/damaged/{name}"))


def _victim_path(root: Path, seed: Seed, kind: str) -> Path:
    if kind == "project":
        return root / "data" / "projects" / f"{seed.victim_project}.toml"
    return root / "data" / "firmwares" / f"{seed.victim_firmware}.toml"


def _record_dialog(texts: list[str]) -> Callable[[kit.DialogDriver], None]:
    def handler(driver: kit.DialogDriver) -> None:
        texts.append(driver.text())
        driver.ok()  # Close

    return handler


def _assert_usable(window: object, name: str) -> None:
    kit.click_sidebar(window, name)
    kit.wait_until(
        lambda: any(kit.same_text(t, "Files match") for t in kit.check_texts(window)),
        10,
        message=lambda: f"{name}: {kit.check_texts(window)}",
    )


@pytest.mark.parametrize("mode", DOCUMENT_MODES)
@pytest.mark.parametrize("kind", ["project", "firmware"])
def test_unreadable_document_at_startup_is_left_out_and_reported(
    template: tuple[Path, Seed],
    tmp_path: Path,
    embr: Callable[..., kit.EmbrApp],
    kind: str,
    mode: str,
) -> None:
    program, seed = _copy(template, tmp_path)
    root = program / ".run"
    path = _victim_path(root, seed, kind)
    before = _documents(root)
    damaged = _damage_document(path, kind, mode)
    language = Language.ZH_CN if mode == "garbage" else Language.ENGLISH
    texts: list[str] = []
    with kit.expect_dialog(_record_dialog(texts), title=DAMAGED_TITLE):
        app = embr(root, language)
    window = app.window
    shown = texts[0]
    assert path.name in shown and str(path.parent) in shown, shown
    assert _reason_ok(shown, kind, mode, language), shown
    copies = _damaged_copies(root, path.name)
    assert [c.read_bytes() for c in copies] == [damaged]
    assert str(copies[0].parent) in shown  # "A copy of each is kept in ..."
    assert path.read_bytes() == damaged, "the damaged original was changed"
    assert f"[data] {path} cannot be read" in kit.log_text(window)
    if kind == "project":
        assert kit.sidebar_names(window, "project") == ["Kept project"]
        assert "Victim firmware" in kit.standalone_names(window)
        _assert_usable(window, "Victim firmware")
    else:
        assert "Victim firmware" not in kit.sidebar_names(window)
    _assert_usable(window, "Kept firmware")
    _assert_usable(window, "Standalone firmware")
    kit.click_sidebar(window, "Archived")
    kit.assert_no_dialog(DAMAGED_TITLE)  # once per session
    after = _documents(root)
    rel = path.relative_to(root).as_posix()
    assert {k: v for k, v in after.items() if k != rel} == {
        k: v for k, v in before.items() if k != rel
    }

    # a new session reports it again, but keeps no second copy of the same content
    app.close()
    texts.clear()
    with kit.expect_dialog(_record_dialog(texts), title=DAMAGED_TITLE):
        embr(root, language)
    assert path.name in texts[0]
    assert len(_damaged_copies(root, path.name)) == 1
    assert path.read_bytes() == damaged


def test_firmware_damaged_while_selected_behaves_like_deleted(
    template: tuple[Path, Seed], tmp_path: Path, embr: Callable[..., kit.EmbrApp]
) -> None:
    program, seed = _copy(template, tmp_path)
    root = program / ".run"
    app = embr(root, Language.ENGLISH)
    window = app.window
    kit.click_sidebar(window, "Victim firmware")
    assert window.pages.currentWidget() is window.firmware_view
    path = _victim_path(root, seed, "firmware")
    damaged = _damage_document(path, "firmware", "garbage")
    texts: list[str] = []
    with kit.expect_dialog(_record_dialog(texts), title=DAMAGED_TITLE):
        kit.click_sidebar(window, "Victim firmware")  # still listed until the refresh
    assert path.name in texts[0]
    assert "Victim firmware" not in kit.sidebar_names(window)
    assert window.pages.currentWidget() is window.empty_view
    assert path.read_bytes() == damaged
    assert len(_damaged_copies(root, path.name)) == 1
    _assert_usable(window, "Kept firmware")
    kit.assert_no_dialog(DAMAGED_TITLE)


def test_project_damaged_while_running_moves_its_firmware_to_standalone(
    template: tuple[Path, Seed], tmp_path: Path, embr: Callable[..., kit.EmbrApp]
) -> None:
    program, seed = _copy(template, tmp_path)
    root = program / ".run"
    app = embr(root, Language.ZH_CN)
    window = app.window
    kit.click_sidebar(window, "Victim firmware")
    path = _victim_path(root, seed, "project")
    damaged = _damage_document(path, "project", "truncated")
    texts: list[str] = []
    with kit.expect_dialog(_record_dialog(texts), title=DAMAGED_TITLE):
        kit.click_sidebar(window, "Kept firmware")
    assert path.name in texts[0]
    assert kit.sidebar_names(window, "project") == ["Kept project"]
    assert "Victim firmware" in kit.standalone_names(window)
    _assert_usable(window, "Victim firmware")
    kit.click_sidebar(window, "Standalone firmware")
    kit.assert_no_dialog(DAMAGED_TITLE)
    assert path.read_bytes() == damaged


def test_unreadable_document_at_startup_in_a_real_process(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels
) -> None:
    """The same through `python -m embr`: no crash, the main window with the dialog."""
    program, seed = _copy(template, tmp_path)
    root = program / ".run"
    kit.store_language(root, Language.ENGLISH)
    path = _victim_path(root, seed, "firmware")
    damaged = _damage_document(path, "firmware", "garbage")
    with _launch(program, sentinels, tmp_path, "damaged-firmware") as process:
        main = process.wait_main_window()
        assert main is not None, process.stderr()
        title = kit.translated(DAMAGED_TITLE, Language.ENGLISH)
        kit.wait_until(
            lambda: any(path.name in w.text for w in process.windows()),
            30,
            message=lambda: f"no dialog naming {path.name}: {[w.texts for w in process.windows()]}",
        )
        assert any(title in w.text or title == w.name for w in process.windows())
        assert process.close() == 0
    _no_traceback(process)
    assert path.read_bytes() == damaged
    assert sentinels.leftovers() == []


# ---- embr.sqlite ------------------------------------------------------------------------


def _damage_database(database: Path, mode: str) -> bytes:
    data = database.read_bytes()
    if mode == "garbage":
        bad = b"garbage" * 200
    elif mode == "truncated":
        bad = data[: len(data) // 2]
    else:  # quick_check: a valid header, a broken b-tree page (page 3's type byte)
        page = int.from_bytes(data[16:18], "big")
        changed = bytearray(data)
        changed[2 * page] = 0x55
        bad = bytes(changed)
    database.write_bytes(bad)
    if mode == "quick_check":  # the file opens; only the integrity check finds it
        probe = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            assert probe.execute("PRAGMA user_version").fetchone() == (2,)
            assert probe.execute("PRAGMA quick_check").fetchone() != ("ok",)
        finally:
            probe.close()
    return bad


@pytest.mark.parametrize(
    ("mode", "language"),
    [
        ("garbage", Language.ENGLISH),
        ("truncated", Language.ENGLISH),
        ("quick_check", Language.ENGLISH),
        ("garbage", Language.ZH_CN),
    ],
)
def test_damaged_database_exit_changes_nothing(
    template: tuple[Path, Seed],
    tmp_path: Path,
    sentinels: kit.Sentinels,
    mode: str,
    language: Language,
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    kit.store_language(root, language)
    database = root / "data" / "embr.sqlite"
    damaged = _damage_database(database, mode)
    before = _documents(root)
    with _launch(program, sentinels, tmp_path, f"db-{mode}") as process:
        message = _message(process)
        assert str(database) in message.text, message.texts
        intro = kit.translated(embr_main.DAMAGED_DATABASE, language).split("{detail}")[0]
        assert intro.strip() in message.text, message.texts
        buttons = {kit.translated(b, language) for b in ("Back up and start anew", "Exit")}
        assert buttons <= set(message.buttons), message.buttons
        assert not any(w.class_name == kit.MAIN_CLASS for w in process.windows())
        _press(process, "Exit", language)
        assert process.wait() == 1
    _no_traceback(process)
    assert database.read_bytes() == damaged
    assert _documents(root) == before
    assert list((root / "data" / "backup").rglob("embr.sqlite*")) == []
    assert sentinels.leftovers() == []


def test_damaged_database_backed_up_and_started_anew(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    kit.store_language(root, Language.ENGLISH)
    database = root / "data" / "embr.sqlite"
    damaged = _damage_database(database, "garbage")
    extras = {suffix: f"{suffix} content".encode() for suffix in ("-journal", "-wal", "-shm")}
    for suffix, content in extras.items():
        database.with_name(database.name + suffix).write_bytes(content)
    before = _documents(root)
    target = tmp_path / "chosen" / "damaged-copy.sqlite"
    target.parent.mkdir()
    with _launch(program, sentinels, tmp_path, "db-backup") as process:
        _message(process)
        _press(process, "Back up and start anew", Language.ENGLISH)
        process.answer_file_dialog(kit.translated(embr_main.BACKUP_TITLE, Language.ENGLISH), target)
        assert process.wait_main_window() is not None, process.stderr()
        assert process.close() == 0
    _no_traceback(process)
    assert target.read_bytes() == damaged
    for suffix in ("-journal", "-wal"):
        assert target.with_name(target.name + suffix).read_bytes() == extras[suffix]
    for suffix in ("-journal", "-wal", "-shm"):
        assert not database.with_name(database.name + suffix).exists(), suffix
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM archive_files").fetchone() == (0,)
    finally:
        connection.close()
    assert _documents(root) == before  # projects, firmware and archived files are kept
    assert sentinels.leftovers(shell_dialog=True) == []


def test_damaged_database_cancelled_save_asks_again(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    kit.store_language(root, Language.ZH_CN)
    database = root / "data" / "embr.sqlite"
    damaged = _damage_database(database, "truncated")
    with _launch(program, sentinels, tmp_path, "db-cancel") as process:
        _message(process)
        _press(process, "Back up and start anew", Language.ZH_CN)
        process.answer_file_dialog(kit.translated(embr_main.BACKUP_TITLE, Language.ZH_CN), None)
        time.sleep(1)
        again = _message(process)
        assert str(database) in again.text
        _press(process, "Exit", Language.ZH_CN)
        assert process.wait() == 1
    _no_traceback(process)
    assert database.read_bytes() == damaged
    assert sentinels.leftovers(shell_dialog=True) == []


def test_empty_database_counts_as_missing(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    (root / "data" / "embr.sqlite").write_bytes(b"")
    before = _documents(root)
    with _launch(program, sentinels, tmp_path, "db-empty") as process:
        assert process.wait_main_window() is not None, process.stderr()
        assert process.close() == 0
    _no_traceback(process)
    assert _documents(root) == before


# ---- folders in the way -----------------------------------------------------------------

BLOCKERS = ["temp", "logs", "data/projects", "temp-unlistable"]
STAMP = re.compile(r"\.\d{8}-\d{6}(-\d+)?$")


def _block(root: Path, blocker: str) -> tuple[Path, bool]:
    """(the path in the way, occupied by a file)."""
    if blocker == "temp-unlistable":
        folder = root / "temp"
        (folder / "left-over.bin").write_bytes(b"kept")
        sid = kit.current_user_sid()
        subprocess.run(
            ["icacls", str(folder), "/deny", f"*{sid}:(RD)"], capture_output=True, check=True
        )
        return folder, False
    path = root / blocker
    shutil.rmtree(path)
    path.write_bytes(b"a file in the way")
    return path, True


def _unblock(root: Path) -> None:
    """Remove the deny ACE from temp/ and any renamed temp.* (cleanup)."""
    sid = kit.current_user_sid()
    for folder in [root / "temp", *root.glob("temp.*")]:
        if folder.is_dir():
            subprocess.run(["icacls", str(folder), "/remove:d", f"*{sid}"], capture_output=True)


def _folder_line(path: Path, occupied: bool, language: Language) -> str:
    if occupied:
        return kit.translated(embr_main.OCCUPIED, language).format(path=path)
    return kit.translated(embr_main.UNREADABLE, language).split("{detail}")[0].format(path=path)


@pytest.mark.parametrize("blocker", BLOCKERS)
@pytest.mark.parametrize("language", LANGUAGES)
def test_folder_in_the_way_exit_changes_nothing(
    template: tuple[Path, Seed],
    tmp_path: Path,
    sentinels: kit.Sentinels,
    blocker: str,
    language: Language,
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    kit.store_language(root, language)
    try:
        path, occupied = _block(root, blocker)
        before = kit.snapshot(root) if occupied else {}
        with _launch(program, sentinels, tmp_path, f"exit-{blocker}") as process:
            message = _message(process)
            assert _folder_line(path, occupied, language) in message.text, message.texts
            buttons = {kit.translated(b, language) for b in ("Rename and continue", "Exit")}
            assert buttons <= set(message.buttons), message.buttons
            _press(process, "Exit", language)
            assert process.wait() == 1
        _no_traceback(process)
        if occupied:
            assert path.read_bytes() == b"a file in the way"
            assert kit.snapshot(root) == before
        assert not list(root.glob(f"{path.name}.*"))
    finally:
        _unblock(root)
    if not occupied:
        assert (path / "left-over.bin").read_bytes() == b"kept"
    assert sentinels.leftovers() == []


@pytest.mark.parametrize("blocker", BLOCKERS)
def test_folder_in_the_way_renamed_and_started(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels, blocker: str
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    kit.store_language(root, Language.ENGLISH)
    documents = _documents(root)
    taken: set[str] = set()
    try:
        path, occupied = _block(root, blocker)
        if blocker == "logs":  # the plain "<name>.<stamp>" is taken: -2 must be added
            now = datetime.now()
            for second in range(40):
                name = f"logs.{now + timedelta(seconds=second):%Y%m%d-%H%M%S}"
                (root / name).write_bytes(b"taken")
                taken.add(name)
        with _launch(program, sentinels, tmp_path, f"rename-{blocker}") as process:
            _message(process)
            _press(process, "Rename and continue", Language.ENGLISH)
            assert process.wait_main_window() is not None, process.stderr()
            assert process.close() == 0
        _no_traceback(process)
        renamed = [p for p in path.parent.glob(f"{path.name}.*") if p.name not in taken]
        assert len(renamed) == 1, [p.name for p in path.parent.iterdir()]
        assert STAMP.search(renamed[0].name), renamed[0].name
        if blocker == "logs":
            assert renamed[0].name.endswith("-2") and renamed[0].name[:-2] in taken
        assert path.is_dir()
        if occupied:
            assert renamed[0].read_bytes() == b"a file in the way"
    finally:
        _unblock(root)
    if not occupied:
        assert (renamed[0] / "left-over.bin").read_bytes() == b"kept"
    for name in taken:
        assert (root / name).read_bytes() == b"taken"
    kept = {k: v for k, v in documents.items() if not k.startswith(f"{blocker}/")}
    assert {k: v for k, v in _documents(root).items() if not k.startswith(f"{blocker}/")} == kept
    assert sentinels.leftovers() == []


# ---- settings, newer data, startup messages in both languages ----------------------------


@pytest.mark.parametrize("mode", ["garbage", "empty", "truncated"])
def test_damaged_settings_at_startup(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels, mode: str
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    before = _documents(root)
    settings = root / "settings.ini"
    data = settings.read_bytes()
    damaged = {
        "garbage": b"\x00\xff\xfe garbage \x81[[[ = = \x00" * 8,
        "empty": b"",
        "truncated": data[: len(data) // 2],
    }[mode]
    settings.write_bytes(damaged)
    with _launch(program, sentinels, tmp_path, f"settings-{mode}") as process:
        assert process.wait_main_window() is not None, process.stderr()
        assert process.close() == 0
    _no_traceback(process)
    assert _documents(root) == before
    backups = list((root / "data" / "backup").rglob("settings.ini"))
    if kit.is_valid_ini(damaged):
        assert backups == []
    else:  # copied to backup/<timestamp>/, Embr starts with no settings
        assert [b.read_bytes() for b in backups] == [damaged]
    assert kit.is_valid_ini(settings.read_bytes())  # rewritten on close
    assert sentinels.leftovers() == []


@pytest.mark.parametrize("kind", ["project", "firmware"])
def test_newer_document_is_read_only_and_kept(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels, kind: str
) -> None:
    program, seed = _copy(template, tmp_path)
    root = program / ".run"
    path = _victim_path(root, seed, kind)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("schema_version = 1\n")
    newer = text.replace("schema_version = 1\n", "schema_version = 99\n", 1)
    path.write_bytes(newer.encode() + b'future_field = "from a newer Embr"\n')
    newer_bytes = path.read_bytes()
    before = _documents(root)
    with _launch(program, sentinels, tmp_path, f"newer-{kind}") as process:
        assert process.wait_main_window() is not None, process.stderr()
        time.sleep(1.5)
        assert not any(path.name in w.text for w in process.windows()), "reported as damaged"
        assert process.close() == 0
    _no_traceback(process)
    assert path.read_bytes() == newer_bytes, "a newer document was modified"
    assert _documents(root) == before
    assert sentinels.leftovers() == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_newer_database_refused_with_message_and_untouched(
    template: tuple[Path, Seed], tmp_path: Path, sentinels: kit.Sentinels, language: Language
) -> None:
    program, _ = _copy(template, tmp_path)
    root = program / ".run"
    kit.store_language(root, language)
    database = root / "data" / "embr.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    original = database.read_bytes()
    with _launch(program, sentinels, tmp_path, "newer-db") as process:
        message = _message(process)
        intro = kit.translated(embr_main.NEWER_DATA, language).split("{version}")[0]
        assert intro.strip() in message.text, message.texts
        ok = kit.translated(embr_main.OK, language)
        assert ok in message.buttons, message.buttons
        _press(process, "OK", language)
        assert process.wait() == 1
    _no_traceback(process)
    assert database.read_bytes() == original
    assert list((root / "data" / "backup").rglob("embr.sqlite")) == []
    assert sentinels.leftovers() == []


# ---- archive files, logs/ while the real window runs ------------------------------------


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


def test_logs_folder_not_writable_while_running(
    template: tuple[Path, Seed], tmp_path: Path, embr: Callable[..., kit.EmbrApp]
) -> None:
    """logs/ loses write access while Embr runs; Connect & identify must say so (no
    crash, the port is not left busy) instead of failing silently."""
    port = kit.board_port("EMBR_TEST_PORT_UART")
    program, _ = _copy(template, tmp_path)
    app = embr(program / ".run")
    window = app.window
    with kit.deny_access(app.paths.logs, "(OI)(CI)(WD,AD)"):
        kit.pick_combo(window.connection.port, port, contains=True)
        kit.click(window.connection.identify)
        kit.pump(500)
        result = kit.wait_job_finished(window, 120)
        shown = kit.log_text(window)
    assert "logs" in shown, f"no message about logs/ (result {result!r}):\n{shown[-2000:]}"
    assert window.connection.identify.isEnabled()
    assert kit.same_text(kit.identify(window, port), "Identified"), app.log()
