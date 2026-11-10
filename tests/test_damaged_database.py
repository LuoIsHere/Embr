"""main: backing up a damaged records database and starting anew, before the window opens.
The message boxes and the save dialog are replaced, so nothing waits for a click."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog

from embr import main

# pyright: reportPrivateUsage=false


def test_a_database_in_use_is_reported_and_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "embr.sqlite"
    database.write_bytes(b"damaged")
    wal = tmp_path / "embr.sqlite-wal"
    wal.write_bytes(b"wal")
    copies = iter([tmp_path / "copy1.sqlite", tmp_path / "copy2.sqlite"])
    shown: list[str] = []

    def choose(_message: str, _proceed: str) -> bool:
        return True  # Back up and start anew

    def fatal(message: str) -> int:
        shown.append(message)
        return 1

    def save_as(*_args: object) -> tuple[str, str]:
        return str(next(copies)), ""

    busy = [database]
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self in busy:  # the first attempt: another program still has it open
            busy.remove(self)
            raise PermissionError(13, "in use by another program")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(main, "_choose", choose)
    monkeypatch.setattr(main, "_fatal", fatal)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", save_as)
    monkeypatch.setattr(Path, "unlink", unlink)

    assert main._replace_damaged_database(database, "file is not a database")

    assert len(shown) == 1 and "could not be removed" in shown[0]
    assert str(tmp_path / "copy1.sqlite") in shown[0]
    assert (tmp_path / "copy1.sqlite").read_bytes() == b"damaged"
    assert (tmp_path / "copy1.sqlite-wal").read_bytes() == b"wal"
    assert not database.exists() and not wal.exists()
