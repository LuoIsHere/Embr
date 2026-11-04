"""Where an entry or a firmware lives on disk, for "Show in Explorer". Stats only; nothing is
opened here."""

from dataclasses import dataclass
from pathlib import Path

from embr.core.firmware.import_pio import EnvRef
from embr.core.firmware.model import Entry, Firmware


@dataclass(frozen=True)
class Location:
    path: Path
    selects_file: bool
    """True: `path` is a file, shown selected in its folder. False: `path` is a folder."""


def entry_location(entry: Entry) -> Location | None:
    """A "path:" entry's file, or its folder when the file is gone; None when the folder is
    gone too. Archived copies (named by their hash) and blank entries have no place to show."""
    if not entry.file.startswith("path:"):
        return None
    path = Path(entry.file[len("path:") :])
    if path.is_file():
        return Location(path, selects_file=True)
    return Location(path.parent, selects_file=False) if path.parent.is_dir() else None


def firmware_location(firmware: Firmware) -> Location | None:
    """A tracking firmware's build directory, a fixed firmware's folder; None when it has
    none (archived, or no file added yet) or the folder is gone."""
    ref = firmware.build_ref
    if ref is None:
        folder = Path(firmware.folder) if firmware.folder else None
    elif ref.env is None:
        folder = Path(ref.path)
    else:
        folder = EnvRef(Path(ref.path), ref.env).build_dir
    return Location(folder, selects_file=False) if folder and folder.is_dir() else None
