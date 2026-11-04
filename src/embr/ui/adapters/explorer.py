"""Show a file or folder in Windows Explorer."""

import subprocess

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from embr.app.locations import Location


def reveal(location: Location) -> None:
    """Raises OSError when Explorer cannot be started."""
    if location.selects_file:
        # a string, not a list: Explorer wants the quotes around the path alone
        subprocess.Popen(f'explorer /select,"{location.path}"')
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(location.path)))
