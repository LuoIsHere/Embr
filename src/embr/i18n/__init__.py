"""Interface text translation (design doc §12 item 10, §12.5).

Source text stays English and is its own key. tr(text) returns the text in the current
language: the exact key, else a template key whose {0}, {1}… placeholders match the
text (so formatted strings translate: tr(f"{name}: file missing") finds the key
"{0}: file missing"); each placeholder value is passed through tr() too (exact key
or template), so a message built from other messages translates as a whole.
Text without a translation is returned unchanged, and English (the default) returns
everything unchanged. Logs never go through tr(); words the glossary keeps in English
(PASS, Flash, chip names, esptool modes…) are simply left untranslated in the table.

N_(text) marks a message made outside the UI (app layer) that the UI shows through tr();
it returns the text unchanged and exists so keys_in_source() finds it.
"""

import ast
import re
import string
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path


class Language(StrEnum):
    SYSTEM = "system"
    ENGLISH = "en"
    ZH_CN = "zh_CN"


SETTINGS_KEY = "language"
"""settings.ini [ui] language: a Language value; anything else counts as SYSTEM."""


def effective(choice: Language, ui_languages: Sequence[str]) -> Language:
    """The language to show. SYSTEM -> ZH_CN when the first system UI language is
    Simplified Chinese (zh-CN, zh-Hans, zh-Hans-CN, zh_CN; case and "-"/"_" ignored),
    else ENGLISH; other choices are returned as they are."""
    if choice != Language.SYSTEM:
        return choice
    first = ui_languages[0].replace("_", "-").lower() if ui_languages else ""
    chinese = first in ("zh-cn", "zh-hans") or first.startswith("zh-hans-")
    return Language.ZH_CN if chinese else Language.ENGLISH


def set_language(language: Language) -> None:
    """Use `language` (ENGLISH or ZH_CN) from now on. ValueError for SYSTEM."""
    global _current, _exact, _patterns
    found = table(language)
    patterns: list[tuple[re.Pattern[str], tuple[int, ...], str]] = []
    for key, value in found.items():
        compiled = _compile(key)
        if compiled is not None:
            patterns.append((*compiled, value))
    patterns.sort(key=lambda p: -len(p[0].pattern))  # the most specific template first
    _current, _exact, _patterns = language, found, patterns


def current() -> Language:
    """The language set by set_language(); ENGLISH before any call."""
    return _current


def table(language: Language) -> Mapping[str, str]:
    """English key -> translation for `language`; empty for ENGLISH. ValueError for
    SYSTEM."""
    if language == Language.SYSTEM:
        raise ValueError("SYSTEM has no table; resolve it with effective() first")
    if language == Language.ENGLISH:
        return {}
    from embr.i18n.zh_cn import TEXT

    return TEXT


def tr(text: str) -> str:
    """`text` in the current language (see the module docstring)."""
    if not _exact:
        return text
    found = _exact.get(text)
    if found is not None:
        return found
    for pattern, order, value in _patterns:
        match = pattern.fullmatch(text)
        if match is not None:
            values = [""] * len(order)
            for group, index in zip(match.groups(), order, strict=True):
                values[index] = tr(group)
            return value.format(*values)
    return text


def N_(text: str) -> str:
    """Return `text` unchanged; marks an app-layer message for keys_in_source()."""
    return text


def template_key(parts: Sequence[str | None]) -> str:
    """Key of a formatted string from its literal parts and its placeholders (None):
    ["Files missing: ", None, ". Re-locate"] -> "Files missing: {0}. Re-locate".
    Literal braces are doubled, as str.format() expects."""
    out: list[str] = []
    count = 0
    for part in parts:
        if part is None:
            out.append(f"{{{count}}}")
            count += 1
        else:
            out.append(part.replace("{", "{{").replace("}", "}}"))
    return "".join(out)


def keys_in_source(path: Path) -> list[str]:
    """Keys of every tr(...) and N_(...) call in a Python file whose first argument is a
    string literal or an f-string (an f-string gives its template_key(); implicit
    concatenation is one string), in file order, duplicates kept. Other arguments
    (variables, calls) are skipped."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if name not in ("tr", "N_"):
            continue
        key = _literal_key(node.args[0])
        if key is not None:
            found.append((node.lineno, node.col_offset, key))
    return [key for _, _, key in sorted(found)]


_current = Language.ENGLISH
_exact: Mapping[str, str] = {}
_patterns: list[tuple[re.Pattern[str], tuple[int, ...], str]] = []
"""(pattern, placeholder index of each group, translation) for template keys."""


def _compile(key: str) -> tuple[re.Pattern[str], tuple[int, ...]] | None:
    pieces: list[str] = []
    order: list[int] = []
    for literal, field, _, _ in string.Formatter().parse(key):
        pieces.append(re.escape(literal))
        if field is not None:
            if not field.isdigit():
                return None
            pieces.append("(.+?)")
            order.append(int(field))
    if not order:
        return None
    return re.compile("".join(pieces), re.DOTALL), tuple(order)


def _literal_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str | None] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(None)
        return template_key(parts)
    return None
