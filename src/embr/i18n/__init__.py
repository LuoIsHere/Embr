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
    raise NotImplementedError


def set_language(language: Language) -> None:
    """Use `language` (ENGLISH or ZH_CN) from now on. ValueError for SYSTEM."""
    raise NotImplementedError


def current() -> Language:
    """The language set by set_language(); ENGLISH before any call."""
    raise NotImplementedError


def table(language: Language) -> Mapping[str, str]:
    """English key -> translation for `language`; empty for ENGLISH. ValueError for
    SYSTEM."""
    raise NotImplementedError


def tr(text: str) -> str:
    """`text` in the current language (see the module docstring)."""
    raise NotImplementedError


def N_(text: str) -> str:
    """Return `text` unchanged; marks an app-layer message for keys_in_source()."""
    return text


def template_key(parts: Sequence[str | None]) -> str:
    """Key of a formatted string from its literal parts and its placeholders (None):
    ["Files missing: ", None, ". Re-locate"] -> "Files missing: {0}. Re-locate".
    Literal braces are doubled, as str.format() expects."""
    raise NotImplementedError


def keys_in_source(path: Path) -> list[str]:
    """Keys of every tr(...) and N_(...) call in a Python file whose first argument is a
    string literal or an f-string (an f-string gives its template_key(); implicit
    concatenation is one string), in file order, duplicates kept. Other arguments
    (variables, calls) are skipped."""
    raise NotImplementedError
