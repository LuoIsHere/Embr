"""Project overview (design doc §6.1 "中间工作区 — 选中项目时")."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from embr.app.chips import firmware_chips
from embr.core.firmware.model import Firmware
from embr.core.flasher.plan import AFTER_MODES, BEFORE_MODES
from embr.core.project import DuplicateGroupError, Project, ProjectDefaults
from embr.ui import dialogs
from embr.ui.owner import Owner
from embr.ui.sidebar import chips_text
from embr.ui.widgets import button, clear_layout, label, tag
from embr.ui.workspace import NotesEdit


class FirmwareLink(QFrame):
    clicked = Signal(str)

    def __init__(self, firmware: Firmware, chips: tuple[str, ...]) -> None:
        super().__init__()
        self.setObjectName("rec")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._id = firmware.id
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 6, 4, 6)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(QLabel(firmware.name))
        text.addWidget(label(firmware.notes or "", tone="3"))
        row.addLayout(text, 1)
        if chips:
            row.addWidget(tag(chips_text(chips), "gray"))

    def mousePressEvent(self, event: object) -> None:  # type: ignore[override]
        self.clicked.emit(self._id)


class ProjectView(QScrollArea):
    def __init__(self, owner: Owner) -> None:
        super().__init__()
        self._owner = owner
        self._project: Project | None = None
        self.setObjectName("pane")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self.content = QVBoxLayout(holder)
        self.content.setContentsMargins(14, 10, 14, 14)
        self.content.setSpacing(12)
        self.setWidget(holder)

    def show_project(self, project_id: str) -> None:
        project = self._owner.library.project(project_id)
        self._project = project
        clear_layout(self.content)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnMinimumWidth(0, 150)
        grid.setColumnStretch(1, 1)
        self._grid, self._row = grid, 0
        name = QLineEdit(project.name)
        name.setMaximumWidth(420)
        name.editingFinished.connect(lambda: self._rename(name))
        self._add("Name", self._with_note(name, "unique across all projects"))
        notes = NotesEdit(project.notes, "hardware revision, purpose…")
        notes.committed.connect(self._set_notes)
        self._add("Notes", notes)
        self._add("Defaults for new firmware", self._defaults(project))
        self._add("Groups", self._groups(project))
        self.content.addLayout(grid)
        self.content.addWidget(self._firmware_box(project))
        delete = button("Delete project…")
        delete.clicked.connect(self._delete)
        row = QHBoxLayout()
        row.addWidget(delete)
        row.addStretch(1)
        self.content.addLayout(row)
        self.content.addStretch(1)

    def _add(self, title: str, widget: QWidget) -> None:
        caption = label(title, tone="3")
        caption.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._grid.addWidget(caption, self._row, 0)
        self._grid.addWidget(widget, self._row, 1)
        self._row += 1

    def _with_note(self, widget: QWidget, note: str) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget)
        row.addWidget(label(note, tone="3"))
        row.addStretch(1)
        return box

    def _defaults(self, project: Project) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(8)
        before = QComboBox()
        before.addItems(list(BEFORE_MODES))
        before.setCurrentText(project.defaults.before)
        after = QComboBox()
        after.addItems(list(AFTER_MODES))
        after.setCurrentText(project.defaults.after)
        erase = QCheckBox("erase all before flashing")
        erase.setChecked(project.defaults.erase_all)

        def save() -> None:
            defaults = ProjectDefaults(before.currentText(), after.currentText(), erase.isChecked())
            self._owner.library.set_project_defaults(project.id, defaults)

        before.activated.connect(save)
        after.activated.connect(save)
        erase.toggled.connect(save)
        for caption, widget in (("before", before), ("after", after)):
            row.addWidget(label(caption, tone="2"))
            row.addWidget(widget)
        row.addWidget(erase)
        row.addStretch(1)
        column.addLayout(row)
        note = label(
            "copied once when a firmware is created here; changing them later does not touch "
            "existing firmware",
            tone="3",
        )
        note.setStyleSheet("font-size: 12px;")
        column.addWidget(note)
        return box

    def _groups(self, project: Project) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        members = self._owner.library.project_firmware(project.id)
        if not project.devices:
            column.addWidget(label("no groups yet", tone="3"))
        for group in project.devices:
            count = sum(1 for f in members if f.device == group)
            name = QLineEdit(group)
            name.editingFinished.connect(lambda g=group, w=name: self._rename_group(g, w))
            remove = button(
                "Remove", "ibtn", tooltip="Remove group (its firmware becomes ungrouped)"
            )
            remove.clicked.connect(lambda _c=False, g=group, n=count: self._remove_group(g, n))
            column.addLayout(self._group_row(name, remove, label(f"{count} firmware", tone="3")))
        new = QLineEdit()
        new.setPlaceholderText("new group, e.g. Main board")
        add = button("Add", "ibtn")
        add.clicked.connect(lambda: self._add_group(new))
        new.returnPressed.connect(lambda: self._add_group(new))
        column.addSpacing(2)
        column.addLayout(self._group_row(new, add, QLabel("")))
        note = label(
            "Groups are display folders inside this project. Drag a firmware onto a group in the "
            "sidebar to move it there; drop it on the project name to make it ungrouped.",
            tone="3",
            wrap=True,
        )
        note.setStyleSheet("font-size: 12px;")
        note.setMaximumWidth(560)
        column.addWidget(note)
        return box

    def _group_row(self, edit: QLineEdit, action: QWidget, count: QLabel) -> QHBoxLayout:
        edit.setFixedWidth(260)
        action.setFixedWidth(80)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(edit)
        row.addWidget(action)
        row.addWidget(count)
        row.addStretch(1)
        return row

    def _firmware_box(self, project: Project) -> QWidget:
        members = self._owner.library.project_firmware(project.id)
        frame = QFrame()
        frame.setObjectName("box")
        frame.setMaximumWidth(640)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(0)
        head = QLabel(f"Firmware in this project ({len(members)})")
        head.setObjectName("boxhead")
        layout.addWidget(head)
        inner = QVBoxLayout()
        inner.setContentsMargins(10, 4, 10, 0)
        inner.setSpacing(0)
        groups = [*project.devices, ""]
        for group in groups:
            listed = [
                f
                for f in members
                if f.device == group or (not group and f.device not in project.devices)
            ]
            if not group and not listed:
                continue
            caption = label(group or "Ungrouped", tone="2" if group else "3")
            caption.setStyleSheet("font-size: 12px; font-weight: 600; margin-top: 8px;")
            inner.addWidget(caption)
            if not listed:
                inner.addWidget(label("(empty)", tone="3"))
            for firmware in listed:
                link = FirmwareLink(firmware, firmware_chips(self._owner.context, firmware))
                link.clicked.connect(self._owner.select_firmware)
                inner.addWidget(link)
        if not members and not project.devices:
            inner.addWidget(label("(none)", tone="3"))
        layout.addLayout(inner)
        return frame

    # ---- edits ------------------------------------------------------------------------

    def _rename(self, widget: QLineEdit) -> None:
        project = self._project
        if project is None or widget.text().strip() == project.name:
            return
        try:
            renamed = self._owner.library.rename_project(project.id, widget.text())
        except ValueError:
            widget.setText(project.name)
            return
        self._owner.note("dim", f"[project] renamed '{project.name}' → '{renamed.name}'")
        self._owner.changed()

    def _set_notes(self, text: str) -> None:
        if self._project is not None:
            self._owner.library.set_project_notes(self._project.id, text)
            self._owner.changed()

    def _add_group(self, widget: QLineEdit) -> None:
        project = self._project
        name = widget.text().strip()
        if project is None or not name:
            return
        try:
            self._owner.library.add_group(project.id, name)
        except DuplicateGroupError:
            self._owner.note("error", f"[group] '{name}' already exists")
            return
        self._owner.note("dim", f"[group] added '{name}'")
        self._owner.changed()

    def _rename_group(self, old: str, widget: QLineEdit) -> None:
        project = self._project
        new = widget.text().strip()
        if project is None or not new or new == old:
            widget.setText(old)
            return
        try:
            self._owner.library.rename_group(project.id, old, new)
        except DuplicateGroupError:
            self._owner.note("error", f"[group] '{new}' already exists")
            widget.setText(old)
            return
        self._owner.note("dim", f"[group] renamed '{old}' → '{new}'")
        self._owner.changed()

    def _remove_group(self, group: str, count: int) -> None:
        project = self._project
        if project is None:
            return
        effect = (
            f"{count} firmware in it will stay in the project as <b>Ungrouped</b>. "
            "Nothing is deleted."
            if count
            else "The group is empty."
        )
        accepted, _ = dialogs.confirm(
            self,
            "Remove group",
            [f"Remove the group <b>{_esc(group)}</b> from {_esc(project.name)}?", effect],
            "Remove group",
        )
        if accepted:
            self._owner.library.remove_group(project.id, group)
            self._owner.note(
                "warning", f"[group] removed '{group}'; {count} firmware now ungrouped"
            )
            self._owner.changed()

    def _delete(self) -> None:
        project = self._project
        if project is None:
            return
        count = len(self._owner.library.project_firmware(project.id))
        accepted, delete_all = dialogs.confirm(
            self,
            "Delete project",
            [
                f"Delete <b>{_esc(project.name)}</b>? By default its {count} firmware become "
                "standalone.",
                "Archived files are kept (use Clean archive); flash records are not affected.",
            ],
            "Delete project",
            checkbox="Also delete all firmware in this project",
        )
        if not accepted:
            return
        self._owner.library.delete_project(project.id, delete_firmware=delete_all)
        kept = "with its firmware" if delete_all else "; firmware kept as standalone"
        self._owner.note("warning", f"[project] '{project.name}' deleted {kept}")
        self._project = None
        self._owner.changed()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
