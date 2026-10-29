"""settings.ini: preferences, remembered baud rates, window sizes (design doc §7.6).

Plain configparser INI; no QSettings, no registry. Qt types are converted to text by the
UI adapters before they reach this module.
"""

from pathlib import Path


class Settings:
    """settings.ini held in memory. Keys keep their case, values are stored as written (no
    interpolation, so "%" is safe). Changes reach the disk only on save()."""

    def __init__(self, path: Path, backup_root: Path) -> None:
        """Read `path` if it exists; a missing file means no settings. A file that is not
        valid INI is copied to backup_root/<timestamp>/ and Embr starts with no settings
        (the next save() replaces it)."""
        raise NotImplementedError

    def get(self, section: str, key: str) -> str | None:
        """The stored value, or None when the section or key is absent."""
        raise NotImplementedError

    def set(self, section: str, key: str, value: str) -> None:
        """Store a value, creating the section if needed."""
        raise NotImplementedError

    def remove(self, section: str, key: str) -> None:
        """Delete a key; no error if it is absent. A section left empty is dropped."""
        raise NotImplementedError

    def keys(self, section: str) -> list[str]:
        """Keys of a section in file order; empty if the section is absent."""
        raise NotImplementedError

    def save(self) -> None:
        """Write the file atomically (temporary file in the same folder, then replace)."""
        raise NotImplementedError
