"""The About dialog."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QLabel,
    QWidget,
)

from embr import __version__
from embr.build import build_time
from embr.i18n import N_, tr
from embr.ui.dialogs.base import Dialog

REPOSITORY = "https://github.com/LuoIsHere/Embr"
RELEASES = "https://github.com/LuoIsHere/Embr/releases"
COPYRIGHT = "Copyright © 2026 @LuoIsHere"
DEVELOPMENT_BUILD = N_("development build")
"""About's build time when running from source."""


def about(parent: QWidget, esptool_version: str) -> None:
    dialog = Dialog(parent, tr("About Embr"), tr("Close"), danger=False)
    dialog.cancel.hide()
    dialog.text(tr(f"Embr {__version__} · built-in esptool {esptool_version} · GPL-3.0-or-later"))
    dialog.text(COPYRIGHT)
    when = build_time() or tr(DEVELOPMENT_BUILD)
    dialog.text(tr(f"Build time: {when}"))
    for url in (REPOSITORY, RELEASES):
        anchor = f'<a href="{url}">{url.removeprefix("https://")}</a>'
        link = QLabel(
            tr(f"Repository: {anchor}") if url == REPOSITORY else tr(f"Releases: {anchor}")
        )
        link.setTextFormat(Qt.TextFormat.RichText)
        link.setOpenExternalLinks(False)
        link.linkActivated.connect(_open_url)
        dialog.body.addWidget(link)
    dialog.text(
        tr(
            "Interface font: HarmonyOS Sans (Copyright 2021 Huawei Device Co., Ltd.), used "
            "under the HarmonyOS Sans Fonts License Agreement."
        )
    )
    dialog.text(tr("Third-party notices: THIRD_PARTY_NOTICES in the program folder."))
    dialog.exec()


def _open_url(href: str) -> None:
    QDesktopServices.openUrl(QUrl(href))
