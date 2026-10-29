"""settings.ini: preferences, remembered baud rates, window sizes (design doc §7.6).

Plain configparser INI; no QSettings, no registry. Qt types are converted to text by the
UI adapters before they reach this module.
"""

import configparser
import io
import os
import shutil
from pathlib import Path

from embr.core.storage.documents import backup_folder


class Settings:
    """settings.ini held in memory. Keys keep their case, values are stored as written (no
    interpolation, so "%" is safe). Changes reach the disk only on save()."""

    def __init__(self, path: Path, backup_root: Path) -> None:
        """Read `path` if it exists; a missing file means no settings. A file that is not
        valid INI is copied to backup_root/<timestamp>/ and Embr starts with no settings
        (the next save() replaces it)."""
        self.path = path
        self._parser = _new_parser()
        if not path.is_file():
            return
        try:
            self._parser.read_string(path.read_text(encoding="utf-8"), source=str(path))
        except (configparser.Error, UnicodeDecodeError):
            shutil.copy2(path, backup_folder(backup_root) / path.name)
            self._parser = _new_parser()

    def get(self, section: str, key: str) -> str | None:
        """The stored value, or None when the section or key is absent."""
        return self._parser.get(section, key, fallback=None)

    def set(self, section: str, key: str, value: str) -> None:
        """Store a value, creating the section if needed."""
        if not self._parser.has_section(section):
            self._parser.add_section(section)
        self._parser.set(section, key, value)

    def remove(self, section: str, key: str) -> None:
        """Delete a key; no error if it is absent. A section left empty is dropped."""
        if not self._parser.has_section(section):
            return
        self._parser.remove_option(section, key)
        if not self._parser.options(section):
            self._parser.remove_section(section)

    def keys(self, section: str) -> list[str]:
        """Keys of a section in file order; empty if the section is absent."""
        return self._parser.options(section) if self._parser.has_section(section) else []

    def save(self) -> None:
        """Write the file atomically (temporary file in the same folder, then replace)."""
        text = io.StringIO()
        self._parser.write(text)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text.getvalue(), encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)


def _new_parser() -> configparser.RawConfigParser:
    # "=" only: keys such as "10c4:ea60" (baud per VID:PID) contain the default ":" delimiter
    parser = configparser.RawConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str  # pyright: ignore[reportAttributeAccessIssue]  # keep key case
    return parser
