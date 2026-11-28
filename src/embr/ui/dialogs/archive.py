"""Archiving a version, backing up a build and cleaning the archive."""

import html
from collections.abc import Callable, Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLineEdit,
    QWidget,
)

from embr.app.archiving import ArchiveUsage
from embr.core.firmware.files import local_file
from embr.core.firmware.model import Entry
from embr.i18n import tr
from embr.ui.dialogs.base import Dialog


def archive_version(
    parent: QWidget, source_name: str, default_name: str, entries: list[Entry]
) -> tuple[str, str] | None:
    dialog = Dialog(parent, tr("Archive this version"), tr("Archive"), danger=False)
    name = QLineEdit(default_name)
    dialog.field(tr("Name"), name)
    notes = QLineEdit()
    notes.setPlaceholderText(tr("why this version is kept"))
    dialog.field(tr("Notes"), notes)
    copied = [e for e in entries if local_file(e) is not None]
    lines = "".join("<br>•&nbsp;" + tr(f"{html.escape(e.name)} ({e.size:,} bytes)") for e in copied)
    dialog.text(
        tr(
            f"A new archived firmware is created next to <b>{html.escape(source_name)}</b>, which "
            f"stays as it is. Files copied into data/archive/:{lines}"
        )
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return name.text().strip(), notes.text().strip()


def backup_build(parent: QWidget, firmware_name: str) -> str | None:
    """The note for a new version of the firmware's current build ("" for none), or None
    when cancelled."""
    dialog = Dialog(parent, tr("Back up this build"), tr("Back up"), danger=False)
    dialog.text(
        tr(
            f"The current build of <b>{html.escape(firmware_name)}</b> is copied into "
            "data/archive/ and added to its versions."
        )
    )
    note = QLineEdit()
    note.setPlaceholderText(tr("why this version is kept"))
    dialog.field(tr("Note"), note)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return note.text().strip()


def clean_archive(
    parent: QWidget,
    usage: list[ArchiveUsage],
    names: Mapping[str, str],
    show_firmware: Callable[[str], None],
) -> list[str]:
    """What the archive holds and why files are kept; returns the hashes to delete (the
    unreferenced ones, after confirming), else []. `names`: firmware id -> name. A firmware
    name is a link that closes the dialog and shows that firmware."""
    unused = [u.file for u in usage if not u.firmware_ids]
    kept = [u for u in usage if u.firmware_ids]
    dialog = Dialog(
        parent,
        tr("Clean archive"),
        tr("Delete files") if unused else tr("Close"),
        danger=bool(unused),
    )
    if not unused:
        dialog.cancel.hide()
    total = sum(u.file.size for u in usage)
    dialog.text(
        tr(f"The archive holds 1 file ({total:,} bytes).")
        if len(usage) == 1
        else tr(f"The archive holds {len(usage)} files ({total:,} bytes).")
    )
    if kept:
        users: dict[str, int] = {}
        for u in kept:
            for firmware_id in u.firmware_ids:
                users[firmware_id] = users.get(firmware_id, 0) + 1
        links = ", ".join(
            f'<a href="{html.escape(fid)}">{html.escape(names.get(fid, fid))}</a> ({count})'
            for fid, count in sorted(users.items(), key=lambda item: names.get(item[0], ""))
        )
        kept_text = dialog.text(
            tr(
                f"1 file is kept because firmware refers to it: {links}. Archived versions "
                "are firmware too; delete one of them first to free its files."
            )
            if len(kept) == 1
            else tr(
                f"{len(kept)} files are kept because firmware refers to them: {links}. Archived "
                "versions are firmware too; delete one of them first to free its files."
            )
        )
        chosen: list[str] = []

        def show(firmware_id: str) -> None:
            chosen.append(firmware_id)
            dialog.reject()

        kept_text.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        kept_text.linkActivated.connect(show)
    else:
        chosen = []
    if unused:
        size = sum(f.size for f in unused)
        lines = "".join(
            "<br>•&nbsp;"
            + tr(f"{f.sha256[:12]}… {f.size:,} bytes, archived {f.first_archived[:10]}")
            for f in unused[:12]
        )
        more = tr(f"<br>…and {len(unused) - 12} more") if len(unused) > 12 else ""
        dialog.text(
            tr(
                f"<b>Delete 1 file ({size:,} bytes) that no firmware refers to?</b> "
                f"This cannot be undone.{lines}"
            )
            if len(unused) == 1
            else tr(
                f"<b>Delete {len(unused)} files ({size:,} bytes) that no firmware refers to?</b> "
                f"This cannot be undone.{lines}{more}"
            )
        )
    else:
        dialog.text(tr("<b>Nothing to clean:</b> every archived file is in use."))
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    if chosen:
        show_firmware(chosen[0])
    return [f.sha256 for f in unused] if accepted and unused else []
