"""i18n: interface text translation (design doc §12 item 10, §12.5). The Chinese table is
the real one, so tr() checks are written against whatever it holds; an empty table makes
those loops pass vacuously until the translation lands."""

import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from string import Formatter

import pytest

import embr
from embr.i18n import (
    Language,
    current,
    effective,
    keys_in_source,
    set_language,
    table,
    template_key,
    tr,
)

UNKNOWN = ["zz no such key", "zz no such key {0} {", "zz {{x}} } {name}"]


@pytest.fixture
def restore() -> Iterator[None]:
    yield
    set_language(Language.ENGLISH)


@pytest.fixture
def english(restore: None) -> None:
    set_language(Language.ENGLISH)


@pytest.fixture
def chinese(restore: None) -> None:
    set_language(Language.ZH_CN)


def _fields(text: str) -> list[str]:
    """Placeholder names of a str.format() template; [] when text is not a valid one."""
    try:
        return [name for _, name, _, _ in Formatter().parse(text) if name is not None]
    except ValueError:
        return []


def _template_fields(key: str) -> int:
    """Number of {0}, {1}… placeholders of a template key; 0 for any other key."""
    fields = _fields(key)
    return len(set(fields)) if fields and all(f.isdigit() for f in fields) else 0


def _pattern(key: str) -> re.Pattern[str] | None:
    """Regex that matches every text the template `key` formats to."""
    try:
        parsed = list(Formatter().parse(key))
    except ValueError:
        return None
    body = "".join(
        re.escape(lit) + ("(.*?)" if name is not None else "") for lit, name, _, _ in parsed
    )
    return re.compile(body, re.DOTALL)


def _claimed_by_one_template(key: str, text: str, keys: dict[str, re.Pattern[str] | None]) -> bool:
    """True when `text` is no exact key and `key` is the only template that matches it."""
    if text in keys:
        return False
    return [k for k, p in keys.items() if p is not None and p.fullmatch(text)] == [key]


# --- effective -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ui_languages", "expected"),
    [
        (["zh-CN"], Language.ZH_CN),
        (["zh-Hans"], Language.ZH_CN),
        (["zh-Hans-CN"], Language.ZH_CN),
        (["zh_CN"], Language.ZH_CN),
        (["ZH-cn"], Language.ZH_CN),
        (["zh-CN", "en-US"], Language.ZH_CN),
        (["en-US"], Language.ENGLISH),
        (["zh-TW"], Language.ENGLISH),
        (["zh-Hant-TW"], Language.ENGLISH),
        (["ja-JP"], Language.ENGLISH),
        ([], Language.ENGLISH),
        (["en-US", "zh-CN"], Language.ENGLISH),
    ],
)
def test_system_follows_the_first_ui_language(ui_languages: list[str], expected: Language) -> None:
    assert effective(Language.SYSTEM, ui_languages) == expected


@pytest.mark.parametrize("choice", [Language.ENGLISH, Language.ZH_CN])
@pytest.mark.parametrize("ui_languages", [["zh-CN"], ["en-US"], []])
def test_explicit_choice_ignores_the_system(choice: Language, ui_languages: list[str]) -> None:
    assert effective(choice, ui_languages) == choice


# --- set_language / current / table ----------------------------------------------------


def test_english_before_any_call() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from embr.i18n import current; print(current().value)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == Language.ENGLISH.value


def test_set_language_changes_current(restore: None) -> None:
    set_language(Language.ZH_CN)
    assert current() == Language.ZH_CN
    set_language(Language.ENGLISH)
    assert current() == Language.ENGLISH


def test_system_is_no_concrete_language(restore: None) -> None:
    with pytest.raises(ValueError):
        set_language(Language.SYSTEM)
    with pytest.raises(ValueError):
        table(Language.SYSTEM)


def test_english_table_is_empty() -> None:
    assert len(table(Language.ENGLISH)) == 0


# --- tr --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["Flash", "", "Files missing: {0}. Re-locate", "{", "a }{ b", "{{x}} {name}"]
)
def test_english_returns_text_unchanged(english: None, text: str) -> None:
    assert tr(text) == text


def test_english_returns_every_chinese_key_unchanged(english: None) -> None:
    for key in table(Language.ZH_CN):
        assert tr(key) == key


@pytest.mark.parametrize("text", UNKNOWN)
def test_chinese_returns_unknown_text_unchanged(chinese: None, text: str) -> None:
    assert tr(text) == text


def test_chinese_translates_exact_keys(chinese: None) -> None:
    for key, translation in table(Language.ZH_CN).items():
        if not _fields(key):
            assert tr(key) == translation, key


def test_chinese_translates_formatted_text(chinese: None) -> None:
    zh = table(Language.ZH_CN)
    keys = {k: _pattern(k) for k in zh}
    for key, translation in zh.items():
        count = _template_fields(key)
        if not count:
            continue
        values = [f"«A{i}»" for i in range(count)]
        text = key.format(*values)
        if _claimed_by_one_template(key, text, keys):
            assert tr(text) == translation.format(*values), key


def test_chinese_translates_a_placeholder_value_that_is_a_key(chinese: None) -> None:
    zh = table(Language.ZH_CN)
    keys = {k: _pattern(k) for k in zh}
    values = [k for k, t in zh.items() if t != k and "{" not in k and "}" not in k]
    for key, translation in zh.items():
        if _template_fields(key) != 1:
            continue
        for value in values:
            text = key.format(value)
            if _claimed_by_one_template(key, text, keys):
                assert tr(text) == translation.format(zh[value])
                return
    pytest.skip("no one-placeholder template and key pair in the Chinese table yet")


# --- table consistency -----------------------------------------------------------------


def test_translations_keep_placeholders_and_are_not_empty() -> None:
    for key, translation in table(Language.ZH_CN).items():
        assert translation, key
        assert set(_fields(translation)) == set(_fields(key)), key


def test_every_interface_text_has_a_translation() -> None:
    package = Path(embr.__file__).parent
    keys = {
        key
        for folder in ("ui", "app")
        for path in sorted((package / folder).rglob("*.py"))
        for key in keys_in_source(path)
    }
    assert sorted(keys - set(table(Language.ZH_CN))) == []


# --- template_key ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (["Files missing: ", None, ". Re-locate"], "Files missing: {0}. Re-locate"),
        (["Copy ", None, " to ", None, " (", None, ")"], "Copy {0} to {1} ({2})"),
        ([None, " and ", None], "{0} and {1}"),
        (["Use {x} or ", None, "}"], "Use {{x}} or {0}}}"),
        (["Close"], "Close"),
        (["{x}"], "{{x}}"),
        ([None], "{0}"),
    ],
)
def test_template_key(parts: list[str | None], expected: str) -> None:
    assert template_key(parts) == expected


# --- keys_in_source --------------------------------------------------------------------

SOURCE = """\
from embr.i18n import N_, tr

tr("a")
N_("b")
tr(f"x {y} z")
tr("p" "q")
tr(f"{v!r:>4} left")
tr(f"{{x}} {y}")
tr(variable)
tr(call())
label("c")
label(tr("m"), tr("n"))


def later():
    return tr("a")
"""


def test_keys_in_source(tmp_path: Path) -> None:
    path = tmp_path / "screen.py"
    path.write_text(SOURCE, encoding="utf-8")

    assert keys_in_source(path) == [
        "a",
        "b",
        "x {0} z",
        "pq",
        "{0} left",
        "{{x}} {0}",
        "m",
        "n",
        "a",
    ]
