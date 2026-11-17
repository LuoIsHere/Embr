"""The Parameters tab: name, notes, flash and reset settings, build directory."""

from pathlib import Path
from typing import cast

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from embr.app.chips import firmware_mcus
from embr.core import chipinfo
from embr.core.firmware.entries import FolderError
from embr.core.firmware.files import local_file
from embr.core.firmware.import_pio import BOOT_APP0
from embr.core.firmware.model import Firmware, Source, editable_params
from embr.core.flasher.plan import AFTER_MODES, BEFORE_MODES
from embr.i18n import tr
from embr.ui.owner import Owner
from embr.ui.widgets import (
    browse_row,
    button,
    clear_layout,
    elided,
    field_caption,
    label,
    small,
    tag,
    titled_box,
)


class ParamsTab(QWidget):
    def __init__(self, owner: Owner) -> None:
        super().__init__()
        self._owner = owner
        self._firmware: Firmware | None = None
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnMinimumWidth(0, 150)
        self.grid.setColumnStretch(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.grid)
        self.extra = QVBoxLayout()
        layout.addLayout(self.extra)
        layout.addStretch(1)

    def editing(self) -> bool:
        """Focus is in one of this page's editable text fields. Rebuilding the page now
        would drop what is typed, and the field losing focus would save a half-typed value."""
        focused = QApplication.focusWidget()
        return (
            isinstance(focused, (QLineEdit, QPlainTextEdit))
            and not focused.isReadOnly()
            and self.isAncestorOf(focused)
        )

    def show_firmware(self, firmware: Firmware) -> None:
        self._firmware = firmware
        clear_layout(self.grid)
        clear_layout(self.extra)
        locked = self._owner.is_locked(firmware.id) or firmware.read_only
        editable: frozenset[str] = frozenset() if locked else editable_params(firmware)
        self._row = 0
        name = QLineEdit(firmware.name)
        name.setMaximumWidth(420)
        name.setReadOnly(locked)
        name.editingFinished.connect(lambda: self._rename(name))
        self._add(tr("Name"), name, tr("unique within the project, or among standalone firmware"))
        self._add(
            tr("Chip"), self._chip(firmware), tr("from the entries: MCU column on the Entries tab")
        )
        self._add(*self._origin(firmware))
        if firmware.source == Source.PIO_BUILD:
            self._add(
                tr("Framework package"),
                self._package(firmware, locked),
                tr(
                    "Arduino: boot_app0.bin comes from this package; empty uses the Settings path, "
                    "else auto-detection"
                ),
            )
        self._add(tr("Flash mode / freq / size"), self._flash(firmware, "flash_mode" in editable))
        self._add(
            tr("Reset before"),
            self._combo(firmware, "before", BEFORE_MODES, firmware.before, "before" in editable),
            tr("saved setting; may be adjusted to the connection type, see Checks"),
        )
        self._add(
            tr("Reset after"),
            self._combo(firmware, "after", AFTER_MODES, firmware.after, "after" in editable),
        )
        group = (
            (firmware.device or tr("(ungrouped)"))
            if firmware.project_id
            else tr("— (standalone firmware)")
        )
        self._add(
            tr("Group"),
            label(group, tone="2"),
            tr("change via the ⋯ menu → Move to project…, or drag in the sidebar"),
        )
        notes = NotesEdit(
            firmware.notes, tr("What this firmware is, which board revision, why it exists…")
        )
        notes.setReadOnly("notes" not in editable)
        notes.committed.connect(self._set_notes)
        self._add(
            tr("Notes"),
            notes,
            tr("shown in grey below the name; saved when the field loses focus"),
        )
        tags = QLineEdit(", ".join(firmware.tags))
        tags.setPlaceholderText(tr("comma-separated, e.g. release, s3"))
        tags.setMaximumWidth(420)
        tags.setReadOnly("tags" not in editable)
        tags.editingFinished.connect(
            lambda: self._set("tags", [t.strip() for t in tags.text().split(",") if t.strip()])
        )
        self._add(tr("Tags"), tags)
        if firmware.source == Source.ARCHIVED:
            self.extra.addWidget(
                _box(
                    tr("Archive"),
                    [
                        tr(
                            "Fixed snapshot: files are referenced by content hash; the "
                            "original paths are no longer needed."
                        ),
                        tr("Entry names are the recorded original file names; notes are editable."),
                    ],
                )
            )

    def _add(self, title: str, widget: QWidget, note: str = "") -> None:
        self.grid.addWidget(field_caption(title), self._row, 0)
        if note:
            box = QWidget()
            column = QVBoxLayout(box)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            column.addWidget(widget)
            remark = small(label(note, tone="3", wrap=True))
            column.addWidget(remark)
            widget = box
        self.grid.addWidget(widget, self._row, 1)
        self._row += 1

    def _origin(self, firmware: Firmware) -> tuple[str, QWidget]:
        if firmware.source == Source.ARCHIVED:
            return tr("Archived from"), label(firmware.archived_from or "—", tone="2")
        if firmware.build_ref is not None:
            return tr("Build directory"), self._build_dir(firmware)
        text = firmware.folder or "—"
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(elided(text, mono=True, middle=True), 1)
        move = button(tr("Re-locate folder…"), "ibtn")
        move.setEnabled(bool(firmware.folder) and not self._owner.is_locked(firmware.id))
        move.setToolTip(tr("Point every file at the same place in another folder"))
        move.clicked.connect(lambda: self._relocate_folder(firmware))
        row.addWidget(move)
        column.addLayout(row)
        remark = label(
            tr(
                "fixed firmware: files must be in this folder or below it; entries are recorded "
                "by name, path and hash"
            ),
            tone="3",
            wrap=True,
        )
        column.addWidget(small(remark))
        return tr("Files folder"), box

    def _relocate_folder(self, firmware: Firmware) -> None:
        chosen = QFileDialog.getExistingDirectory(self, tr("Re-locate folder"), firmware.folder)
        if not chosen:
            return
        try:
            left = self._owner.library.relocate_folder(firmware.id, Path(chosen))
        except (FolderError, ValueError) as exc:
            self._owner.note("error", f"[firmware] {exc}")
            return
        if left:
            names = ", ".join(left)
            self._owner.note("warning", f"[firmware] not found under {chosen}, unchanged: {names}")
        else:
            self._owner.note("ok", f"[firmware] {firmware.name}: files now in {chosen}")
        self._owner.changed()

    def _build_dir(self, firmware: Firmware) -> QWidget:
        assert firmware.build_ref is not None
        status = self._owner.library.tracker.cached(firmware.id)
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(6)
        env = firmware.build_ref.env
        shown = f"{firmware.build_ref.path}  ·  env {env}" if env else firmware.build_ref.path
        row.addWidget(elided(shown, mono=True, middle=True), 1)
        change = button(tr("Change…"), "ibtn")
        change.setEnabled(not self._owner.is_locked(firmware.id))
        change.clicked.connect(lambda: self._owner.firmware_action("directory"))
        row.addWidget(change)
        column.addLayout(row)
        resolved = (firmware.last_resolved or {}).get("resolved_at")
        facts = [
            tr("exists") if Path(firmware.build_ref.path).is_dir() else tr("not found"),
            tr(f"last resolved {str(resolved).replace('T', ' ')}") if resolved else "",
            f"ESP-IDF {status.idf_version}" if status and status.idf_version else "",
            _target_text(status.configured_target, bool(env))
            if status and status.configured_target
            else "",
        ]
        column.addWidget(small(label(" · ".join(f for f in facts if f), tone="3")))
        for line in status.build_notes if status else ():
            note = label(tr(line), tone="3", wrap=True)
            small(note)
            column.addWidget(note)
        return box

    def _package(self, firmware: Firmware, locked: bool) -> QWidget:
        status = self._owner.library.tracker.cached(firmware.id)
        entries = status.firmware.entries if status else []
        boot = next(
            (str(f) for e in entries if e.name == BOOT_APP0 and (f := local_file(e))),
            None,
        )
        auto = str(Path(boot).parents[2]) if boot else ""
        default = self._owner.library.framework_pkg_default()
        path = QLineEdit(firmware.framework_pkg)
        path.setPlaceholderText(
            tr(f"Settings path: {default}")
            if default
            else tr(f"auto-detected: {auto}")
            if auto
            else tr("auto-detect (nothing found)")
        )
        path.setReadOnly(locked)
        path.editingFinished.connect(lambda: self._set_package(firmware, path.text()))
        row, browse = browse_row(path, "ibtn")
        browse.setEnabled(not locked)

        def pick() -> None:
            chosen = QFileDialog.getExistingDirectory(
                self, tr("Framework package"), path.text() or auto
            )
            if chosen:
                path.setText(chosen)
                self._set_package(firmware, chosen)

        browse.clicked.connect(pick)
        row.setContentsMargins(0, 0, 0, 0)
        box = QWidget()
        box.setLayout(row)
        return box

    def _set_package(self, firmware: Firmware, text: str) -> None:
        if text.strip() == firmware.framework_pkg:
            return
        self._owner.library.set_framework_pkg(firmware.id, text)
        shown = text.strip() or "the Settings path / auto-detection"  # logs stay English
        self._owner.note("dim", f"[firmware] {firmware.name}: framework package {shown}")
        self._owner.changed()

    def _chip(self, firmware: Firmware) -> QWidget:
        mcus = firmware_mcus(self._owner.context, firmware)
        return _derived(", ".join(m.label for m in mcus) or "—")

    def _flash(self, firmware: Firmware, editable: bool) -> QWidget:
        flash = firmware.flash
        if not editable:
            return _derived(f"{flash.mode} / {flash.freq} / {flash.size}")
        chips = {m.chip for m in firmware_mcus(self._owner.context, firmware)}
        chip = chipinfo.get_chip(chips.pop()) if len(chips) == 1 else None
        freqs = (
            chip.flash_freqs
            if chip
            else sorted({f for c in chipinfo.supported_chips() for f in c.flash_freqs})
        )
        sizes = (
            chip.flash_sizes
            if chip
            else sorted({s for c in chipinfo.supported_chips() for s in c.flash_sizes})
        )
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(
            self._combo(firmware, "flash_mode", ("keep", *chipinfo.FLASH_MODES), flash.mode, True)
        )
        row.addWidget(self._combo(firmware, "flash_freq", ("keep", *freqs), flash.freq, True))
        row.addWidget(
            self._combo(firmware, "flash_size", ("keep", "detect", *sizes), flash.size, True)
        )
        row.addWidget(label(tr("keep = leave the bootloader header as built"), tone="3"))
        row.addStretch(1)
        return widget

    def _combo(
        self, firmware: Firmware, name: str, options: tuple[str, ...], value: str, editable: bool
    ) -> QWidget:
        if not editable:
            return _derived(value)
        combo = QComboBox()
        combo.addItems(list(options))
        combo.setCurrentText(value)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.activated.connect(lambda: self._set(name, combo.currentText()))
        return _left(combo) if name in ("before", "after") else combo

    def _set_notes(self, text: str) -> None:
        self._set("notes", text)

    def _set(self, name: str, value: object) -> None:
        firmware = self._firmware
        if firmware is None:
            return
        try:
            self._owner.library.set_param(firmware.id, name, value)
        except ValueError as exc:
            self._owner.note("error", f"[firmware] {firmware.name}: {exc}")
        else:
            shown = ", ".join(cast(list[str], value)) if isinstance(value, list) else value
            if name != "notes":
                self._owner.note("dim", f"[firmware] {firmware.name}: {name} → {shown}")
        self._owner.changed()

    def _rename(self, widget: QLineEdit) -> None:
        firmware = self._firmware
        if firmware is None or widget.isReadOnly() or widget.text().strip() == firmware.name:
            return
        try:
            renamed = self._owner.library.rename_firmware(firmware.id, widget.text())
        except ValueError:
            widget.setText(firmware.name)
            return
        self._owner.note("dim", f"[firmware] renamed to '{renamed.name}'")
        self._owner.changed()


class NotesEdit(QPlainTextEdit):
    """Multi-line notes, committed when the field loses focus."""

    committed = Signal(str)

    def __init__(self, text: str, placeholder: str) -> None:
        super().__init__(text)
        self.setProperty("kind", "notes")
        self.setPlaceholderText(placeholder)
        self.setMaximumWidth(420)
        self.setFixedHeight(76)
        self._initial = text

    def focusOutEvent(self, event: object) -> None:  # type: ignore[override]
        super().focusOutEvent(event)  # type: ignore[arg-type]
        text = self.toPlainText().strip()
        if text != self._initial:
            self._initial = text
            self.committed.emit(text)


def _target_text(target: str, pio: bool) -> str:
    """The configured target chip: from the env's board in platformio.ini, or sdkconfig."""
    name = chipinfo.display_name(target)
    return tr(f"platformio.ini target {name}") if pio else tr(f"sdkconfig target {name}")


def _derived(text: str) -> QWidget:
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(label(text, tone="2"))
    row.addWidget(tag(tr("derived"), "gray"))
    row.addStretch(1)
    return widget


def _left(widget: QWidget) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget)
    row.addStretch(1)
    return box


def _box(title: str, lines: list[str]) -> QWidget:
    frame, layout = titled_box(title, 4)
    for line in lines:
        item = label(f"•  {line}", tone="2", wrap=True)
        item.setContentsMargins(10, 0, 10, 0)
        layout.addWidget(item)
    return frame
