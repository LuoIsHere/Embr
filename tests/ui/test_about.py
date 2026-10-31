"""About Embr (design doc §12 item 22): the copyright line, never translated, and the
build time written at packaging time; "development build" when run from source. Driven
offscreen: Dialog.exec is replaced by a recorder of the dialog's texts."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QLabel, QWidget

from embr import __version__
from embr.i18n import Language, current, set_language
from embr.ui import dialogs

COPYRIGHT = "Copyright © 2026 @LuoIsHere"
BUILT = "2026-11-01 08:12 UTC"


@pytest.fixture
def shown(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The texts of the About dialog's labels once about() has run, in order."""
    texts: list[str] = []

    def record(dialog: dialogs.Dialog) -> int:
        texts.extend(label.text() for label in dialog.findChildren(QLabel))
        return 0

    monkeypatch.setattr(dialogs.Dialog, "exec", record)
    yield texts


@pytest.fixture(params=[Language.ENGLISH, Language.ZH_CN], ids=["en", "zh_CN"])
def language(request: pytest.FixtureRequest) -> Iterator[Language]:
    before = current()
    chosen: Language = request.param
    set_language(chosen)
    try:
        yield chosen
    finally:
        set_language(before)


def _about(texts: list[str]) -> list[str]:
    parent = QWidget()
    try:
        dialogs.about(parent, "5.3.1")
    finally:
        parent.deleteLater()
    assert texts, "about() showed no dialog"
    return texts


FULLWIDTH_COLON = chr(0xFF1A)
BUILD_LINE = {
    Language.ENGLISH: "Build time: {0}",
    Language.ZH_CN: "构建时间" + FULLWIDTH_COLON + "{0}",
}
DEVELOPMENT = {Language.ENGLISH: "development build", Language.ZH_CN: "开发构建"}


def test_about_shows_the_copyright_untranslated(
    shown: list[str], language: Language, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dialogs, "build_time", lambda: BUILT)
    texts = _about(shown)
    assert COPYRIGHT in texts
    assert any(__version__ in t and "5.3.1" in t for t in texts)


def test_about_shows_the_packaged_build_time(
    shown: list[str], language: Language, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dialogs, "build_time", lambda: BUILT)
    texts = _about(shown)
    assert BUILD_LINE[language].format(BUILT) in texts
    assert not any(DEVELOPMENT[language] in t for t in texts)
    assert not any("development build" in t for t in texts)


def test_about_from_source_says_development_build(shown: list[str], language: Language) -> None:
    """No patching: a source checkout has no build.txt."""
    texts = _about(shown)
    assert BUILD_LINE[language].format(DEVELOPMENT[language]) in texts
    assert COPYRIGHT in texts
