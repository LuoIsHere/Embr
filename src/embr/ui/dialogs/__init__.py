"""In-app dialogs: Esc closes, destructive buttons are red and say what they
affect. One module per part of the window; everything is used through this package."""

from embr.ui.dialogs.archive import archive_version, clean_archive
from embr.ui.dialogs.base import (
    Dialog,
    Placement,
    PlacementFields,
    ask_choice,
    ask_text,
    confirm,
    internal_error,
    notice,
    replace_ok,
)
from embr.ui.dialogs.device import IMPORT_LABEL, ReadRequest, erase_flash, merge, read_flash
from embr.ui.dialogs.imports import IdfImport, PioImport, import_idf, import_pio
from embr.ui.dialogs.info import COPYRIGHT, DEVELOPMENT_BUILD, RELEASES, REPOSITORY, about
from embr.ui.dialogs.manage import ARCHIVE_KEPT, move_firmware, new_firmware, new_project
from embr.ui.dialogs.packages import PACKAGE_FILTER, export_firmware, export_project, import_package
from embr.ui.dialogs.preferences import settings

__all__ = [
    "ARCHIVE_KEPT",
    "COPYRIGHT",
    "DEVELOPMENT_BUILD",
    "IMPORT_LABEL",
    "PACKAGE_FILTER",
    "RELEASES",
    "REPOSITORY",
    "Dialog",
    "IdfImport",
    "PioImport",
    "Placement",
    "PlacementFields",
    "ReadRequest",
    "about",
    "archive_version",
    "ask_choice",
    "ask_text",
    "clean_archive",
    "confirm",
    "erase_flash",
    "export_firmware",
    "export_project",
    "import_idf",
    "import_package",
    "import_pio",
    "internal_error",
    "merge",
    "move_firmware",
    "new_firmware",
    "new_project",
    "notice",
    "read_flash",
    "replace_ok",
    "settings",
]
