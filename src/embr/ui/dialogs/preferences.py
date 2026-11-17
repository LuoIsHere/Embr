"""The Settings dialog."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QLineEdit,
    QWidget,
)

from embr.app.library import Library
from embr.core.storage.paths import AppPaths
from embr.i18n import tr
from embr.ui.dialogs.base import Dialog
from embr.ui.widgets import browse_row, label


def settings(parent: QWidget, paths: AppPaths, library: Library) -> None:
    """Program folder (read-only) and the global framework package path for PlatformIO
    Arduino firmware (saved on Close; empty means auto-detect)."""
    dialog = Dialog(parent, tr("Settings"), tr("Close"), danger=False)
    dialog.cancel.hide()
    package = QLineEdit(library.framework_pkg_default())
    detected = library.detected_framework_pkg()
    package.setPlaceholderText(
        tr(f"auto-detect (now: {detected})") if detected else tr("auto-detect (nothing found)")
    )
    row, browse = browse_row(package)

    def pick() -> None:
        chosen = QFileDialog.getExistingDirectory(dialog, tr("Framework package"), package.text())
        if chosen:
            package.setText(chosen)

    browse.clicked.connect(pick)
    dialog.body.addWidget(
        label(
            tr(
                "PlatformIO framework package (the framework-arduinoespressif32 folder that "
                "boot_app0.bin of Arduino firmware comes from). Empty: auto-detect. A firmware "
                "can override it on its Parameters tab."
            ),
            tone="3",
            wrap=True,
        )
    )
    dialog.body.addLayout(row)
    folder = label(str(paths.root), mono=True, wrap=True)
    folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    dialog.field(tr("Program folder"), folder)
    dialog.field(
        tr("Data, logs and settings (portable: inside the program folder, not configurable)"),
        label("data\\ · logs\\ · temp\\ · settings.ini", mono=True),
    )
    dialog.exec()
    if package.text().strip() != library.framework_pkg_default():
        library.set_framework_pkg_default(package.text())
