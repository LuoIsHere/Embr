"""Qt values to and from settings.ini text (core.storage.settings stores strings only)."""

import base64

from PySide6.QtCore import QByteArray

from embr.core.storage.settings import Settings

UI_SECTION = "ui"


def save_bytes(settings: Settings, key: str, value: QByteArray) -> None:
    """E.g. QWidget.saveGeometry(): stored as base64 text."""
    settings.set(UI_SECTION, key, base64.b64encode(bytes(value.data())).decode("ascii"))


def load_bytes(settings: Settings, key: str) -> QByteArray | None:
    """None when absent or not valid base64."""
    text = settings.get(UI_SECTION, key)
    if text is None:
        return None
    try:
        return QByteArray(base64.b64decode(text, validate=True))
    except ValueError:
        return None


def save_ints(settings: Settings, key: str, values: list[int]) -> None:
    """E.g. QSplitter.sizes(): stored as "236,600,330"."""
    settings.set(UI_SECTION, key, ",".join(str(v) for v in values))


def load_ints(settings: Settings, key: str) -> list[int] | None:
    """None when absent or malformed."""
    text = settings.get(UI_SECTION, key)
    if not text:
        return None
    try:
        return [int(v) for v in text.split(",")]
    except ValueError:
        return None


def save_flag(settings: Settings, key: str, value: bool) -> None:
    settings.set(UI_SECTION, key, "1" if value else "0")


def load_flag(settings: Settings, key: str, default: bool) -> bool:
    text = settings.get(UI_SECTION, key)
    return default if text not in ("0", "1") else text == "1"


def save_choice(settings: Settings, key: str, value: str) -> None:
    settings.set(UI_SECTION, key, value)


def load_choice(settings: Settings, key: str, choices: tuple[str, ...], default: str) -> str:
    """The stored value when it is one of `choices`, else `default`."""
    text = settings.get(UI_SECTION, key)
    return text if text in choices else default
