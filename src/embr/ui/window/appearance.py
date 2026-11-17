"""How the window looks and where it was: theme, language, the window menu and the layout."""

from typing import TYPE_CHECKING

import esptool
from PySide6.QtCore import QLocale, QPoint
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMenu,
)

from embr.i18n import N_, SETTINGS_KEY, Language, current, effective, tr
from embr.ui import dialogs
from embr.ui.adapters import settings as ui_settings
from embr.ui.adapters.scheme import SchemeWatcher
from embr.ui.adapters.settings import stored_language
from embr.ui.theme import Palette, load_fonts, palette, style_sheet, ui_font

if TYPE_CHECKING:
    from embr.ui.window.main import EmbrWindow


def apply_theme(app: QApplication, p: Palette) -> None:
    app.setStyle("Fusion")
    load_fonts()
    app.setFont(ui_font())
    qp = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, p.panel),
        (QPalette.ColorRole.Base, p.panel),
        (QPalette.ColorRole.AlternateBase, p.panel_2),
        (QPalette.ColorRole.Button, p.panel),
        (QPalette.ColorRole.Text, p.fg),
        (QPalette.ColorRole.WindowText, p.fg),
        (QPalette.ColorRole.ButtonText, p.fg),
        (QPalette.ColorRole.PlaceholderText, p.fg_3),
        (QPalette.ColorRole.Highlight, p.accent),
        (QPalette.ColorRole.HighlightedText, p.accent_fg),
        (QPalette.ColorRole.ToolTipBase, p.panel),
        (QPalette.ColorRole.ToolTipText, p.fg),
        (QPalette.ColorRole.Mid, p.line),
        (QPalette.ColorRole.Midlight, p.line_2),
        (QPalette.ColorRole.Link, p.accent),
    ):
        qp.setColor(role, QColor(color))
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.fg_3))
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.fg_3))
    app.setPalette(qp)
    app.setStyleSheet(style_sheet(p))


THEMES = ("system", "light", "dark")
"""Theme menu choices; "system" follows Windows."""


THEME_LABELS = {"system": N_("System"), "light": N_("Light"), "dark": N_("Dark")}


LANGUAGE_LABELS = (
    (Language.SYSTEM, N_("System")),
    (Language.ENGLISH, "English"),
    (Language.ZH_CN, "简体中文"),
)
"""Language menu; each language is named in itself."""


class Appearance:
    """Theme, language and layout of the window, kept in settings.ini."""

    def __init__(self, window: "EmbrWindow") -> None:
        self._window = window
        settings = window.context.settings
        self._scheme = SchemeWatcher(window)
        self._theme = ui_settings.load_choice(settings, "theme", THEMES, "system")
        self._language = stored_language(settings)
        self._language_dialog: dialogs.Dialog | None = None
        self.palette = palette(self._dark())
        self._scheme.changed.connect(self._scheme_changed)

    def window_menu(self) -> None:
        menu = QMenu(self._window)
        menu.addAction(
            tr("Settings…"),
            lambda: dialogs.settings(
                self._window, self._window.context.paths, self._window.library
            ),
        )
        menu.addAction(tr("Clean archive…"), self._window.library_actions.clean_archive)
        themes = menu.addMenu(tr("Theme"))
        group = QActionGroup(themes)
        for mode in THEMES:
            action = themes.addAction(tr(THEME_LABELS[mode]), lambda m=mode: self._set_theme(m))
            action.setCheckable(True)
            action.setChecked(mode == self._theme)
            group.addAction(action)
        languages = menu.addMenu(tr("Language"))
        language_group = QActionGroup(languages)
        for choice, text in LANGUAGE_LABELS:
            action = languages.addAction(tr(text), lambda c=choice: self._set_language(c))
            action.setCheckable(True)
            action.setChecked(choice == self._language)
            language_group.addAction(action)
        menu.addSeparator()
        menu.addAction(tr("About Embr"), lambda: dialogs.about(self._window, esptool.__version__))
        button = self._window.topbar.menu_button
        menu.exec(button.mapToGlobal(QPoint(0, button.height() + 4)))

    def expand_log(self, on: bool) -> None:
        """The log takes the whole centre column (the workspace above it is hidden)."""
        self._window.workspace.setVisible(not on)

    def _scheme_changed(self, dark: bool) -> None:
        if self._theme == "system":
            self.palette = palette(dark)
            self.apply_palette()
            self._window.changed()

    def _dark(self) -> bool:
        return self._scheme.dark if self._theme == "system" else self._theme == "dark"

    def _set_theme(self, mode: str) -> None:
        """System follows the Windows light / dark setting; the choice is kept in
        settings.ini."""
        self._theme = mode
        ui_settings.save_choice(self._window.context.settings, "theme", mode)
        self.palette = palette(self._dark())
        self.apply_palette()
        self._window.changed()

    def _set_language(self, choice: Language) -> None:
        """Store the choice; the interface changes after a restart. The
        dialog offers Restart, disabled while a job runs and enabled when it ends."""
        if choice == self._language:
            return
        self._language = choice
        settings = self._window.context.settings
        ui_settings.save_choice(settings, SETTINGS_KEY, choice.value)
        settings.save()
        if effective(choice, QLocale.system().uiLanguages()) == current():
            return
        dialog = dialogs.Dialog(self._window, tr("Language"), tr("Restart"), danger=False)
        dialog.cancel.setText(tr("OK"))
        dialog.text(tr("The new language is used after Embr restarts."))
        busy = dialog.text(
            tr("A job is running: the choice is saved; Restart becomes available when it ends.")
        )
        busy.setVisible(self._window.ops.busy)
        dialog.ok.setEnabled(not self._window.ops.busy)
        self._language_dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._language_dialog = None
        if accepted and not self._window.ops.busy:
            self._window.restart_requested = True
            self._window.close()

    def apply_palette(self) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, self.palette)
        self._window.topbar.apply_palette(self.palette)
        self._window.sidebar_panel.apply_palette(self.palette)
        self._window.connection.apply_palette(self.palette)
        self._window.log_panel.apply_palette(self.palette)

    def restore_layout(self) -> None:
        settings = self._window.context.settings
        geometry = ui_settings.load_bytes(settings, "window")
        if geometry is not None:
            self._window.restoreGeometry(geometry)
        else:
            self._window.resize(1280, 800)
        columns = ui_settings.load_ints(settings, "columns")
        if columns is not None and len(columns) == 3:
            self._window.columns.setSizes(columns)
        log = ui_settings.load_ints(settings, "log")
        if log is not None and len(log) == 2:
            self._window.center_split.setSizes(log)
        if not ui_settings.load_flag(settings, "sidebar", True):
            self._window.toggle_sidebar()

    def allow_restart(self) -> None:
        """A job ended: an open language dialog may offer Restart now."""
        if self._language_dialog is not None:
            self._language_dialog.ok.setEnabled(True)
