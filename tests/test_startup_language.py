"""main: the language of the messages shown before the data is open comes from settings.ini,
read as Settings reads it (keys such as "10c4:ea60" hold a ":")."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QLocale

from embr import main
from embr.i18n import Language, current, set_language

# pyright: reportPrivateUsage=false


@pytest.fixture(autouse=True)
def _keep_language(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """English as the system language (so falling back to it shows), restored after."""

    def english() -> QLocale:
        return QLocale(QLocale.Language.English, QLocale.Country.UnitedStates)

    monkeypatch.setattr(QLocale, "system", english)
    language = current()
    set_language(Language.ENGLISH)
    yield
    set_language(language)


def test_the_stored_language_is_read_next_to_baud_rates(tmp_path: Path) -> None:
    settings = tmp_path / "settings.ini"
    settings.write_text(
        "[ui]\nlanguage = zh_CN\n\n[baud]\n10c4:ea60 = 921600\n10c4:ea70 = 115200\n",
        encoding="utf-8",
    )
    main._startup_language(settings)
    assert current() == Language.ZH_CN
    assert settings.read_text(encoding="utf-8").startswith("[ui]")  # read only


def test_an_unreadable_settings_file_changes_nothing(tmp_path: Path) -> None:
    settings = tmp_path / "settings.ini"
    settings.write_bytes(b"\xff\xfe not text")
    main._startup_language(settings)  # falls back to the system language
    assert current() == Language.ENGLISH
    assert settings.read_bytes() == b"\xff\xfe not text"
    assert not (tmp_path / "backup").exists()
