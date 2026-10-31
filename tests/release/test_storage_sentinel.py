"""Storage sentinel (design doc §7.6): after one complete flow — new project, imports,
move firmware, archive, merge, flash, read flash, migration, .embr export and import —
nothing new outside the program folder.

The flow runs in a child process (storage_flow.py) driving the real window, with TEMP,
TMP, APPDATA, LOCALAPPDATA, USERPROFILE / HOME and the working folder pointed at empty
sentinel folders, PLATFORMIO_CORE_DIR at a fake PIO core among the user's files and pio
kept off PATH (Embr never builds; the import reads the fixture's build output). Flash and
read flash use EMBR_TEST_PORT_UART. Afterwards: the sentinels are empty, the user's
folder got exactly the three files the user saved, the repository's own .run is
untouched, the day log holds the segments and every new record points at it."""

import datetime
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, cast

import release_kit as kit

V1_RECORD = "01JC0000000000000000000001"


def _path_without_pio() -> str:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    keep = [p for p in parts if p and not (Path(p) / "pio.exe").is_file()]
    return os.pathsep.join(keep)


def test_complete_flow_writes_only_inside_the_program_folder(
    sentinels: kit.Sentinels, tmp_path: Path
) -> None:
    port = kit.board_port("EMBR_TEST_PORT_UART")
    root = tmp_path / "Embr"
    (root / "data").mkdir(parents=True)
    shutil.copyfile(kit.DB_V1, root / "data" / "embr.sqlite")  # migrated at start
    user = tmp_path / "user"
    kit.idf_build_copy("esp32s3_hello", user / "idf_s3")
    kit.pio_project_copy("espidf_esp32s3", user / "pio_s3", user / "pio_core")
    kit.hello_files("esp32s3_hello", user / "bins")
    user_before = kit.snapshot(user)
    repo_run = kit.snapshot(kit.REPO / ".run")
    listing = kit.user_folder_listing()
    report = tmp_path / "report" / "report.json"
    report.parent.mkdir()
    env = sentinels.env(
        {"PLATFORMIO_CORE_DIR": str(user / "pio_core"), "PATH": _path_without_pio()}
    )
    command = [
        sys.executable,
        str(Path(__file__).with_name("storage_flow.py")),
        *("--root", str(root), "--user", str(user), "--port", port, "--report", str(report)),
    ]
    with kit.EmbrProcess(
        command, env=env, cwd=sentinels.folder("cwd"), logs=tmp_path / "logs", name="flow"
    ) as child:
        code = child.wait(1200)
    loaded: object = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    result = cast(dict[str, Any], loaded)
    assert code == 0 and result.get("ok"), (
        f"flow failed (exit {code}): {result.get('error')}\nnotes: {result.get('notes')}\n"
        f"slot errors: {result.get('slot_errors')}\nstderr: {child.stderr()[-4000:]}"
    )
    assert "Release flow (2)" in result["projects"], result["projects"]

    # nothing outside the program folder
    assert sentinels.leftovers() == []
    assert kit.suspicious_new_entries(listing) == []
    assert kit.snapshot(kit.REPO / ".run") == repo_run
    user_after = kit.snapshot(user)
    assert kit.new_entries(user_before, user_after) == [
        "out",
        "out/dump.bin",
        "out/merged.bin",
        "out/release.embr",
    ]
    assert kit.new_or_changed(user_before, user_after) == [
        "out/dump.bin",
        "out/merged.bin",
        "out/release.embr",
    ], "a user file next to the imports was changed"

    # everything else inside the program folder, in its fixed layout
    top = {p.name for p in root.iterdir()}
    assert top <= {"data", "logs", "temp", "settings.ini"}, top
    assert sorted(p.name for p in (root / "temp").iterdir()) == ["embr.lock"]
    backups = [p.name for p in (root / "data" / "backup").rglob("*") if p.is_file()]
    assert backups == ["embr.sqlite"]

    # the day log and the records
    day = f"{datetime.date.today().isoformat()}.log"
    log = (root / "logs" / day).read_text(encoding="utf-8")
    operations = [line.split(" | ")[1] for line in log.splitlines() if line.startswith("=== ")]
    assert operations == ["identify", "flash", "read_flash"], operations
    connection = sqlite3.connect(root / "data" / "embr.sqlite")
    try:
        rows = connection.execute(
            "SELECT id, operation, result, log_file FROM flash_records WHERE id != ?",
            (V1_RECORD,),
        ).fetchall()
    finally:
        connection.close()
    assert sorted((r[1], r[2]) for r in rows) == [("flash", "ok"), ("read_flash", "ok")]
    for record_id, _, _, log_file in rows:
        assert log_file == day
        assert f"=== {record_id} | " in log
    boards = list((root / "logs" / "boards").glob("*.log"))
    assert len(boards) == 1

    # the flashed hello_world runs on the board
    assert "Hello world!" in kit.serial_after_reset(port, 5)
