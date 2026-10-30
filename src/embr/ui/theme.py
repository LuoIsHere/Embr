"""Colour tokens, fonts and the application style sheet (design doc §6.1; values from the
UI mock). The light accent is the logo's Clay #C8582F; dark mode uses a lighter tone of the
same hue for contrast on dark panels."""

from dataclasses import dataclass

from PySide6.QtGui import QFont

UI_FAMILIES = ["Segoe UI", "Segoe UI Variable"]
MONO_FAMILIES = ["Cascadia Mono", "Consolas"]
UI_PX = 13
MONO_PX = 12


@dataclass(frozen=True)
class Palette:
    bg: str
    panel: str
    panel_2: str
    line: str
    line_2: str
    fg: str
    fg_2: str
    fg_3: str
    accent: str
    accent_fg: str
    accent_soft: str
    ok: str
    ok_soft: str
    warn: str
    warn_soft: str
    bad: str
    bad_soft: str
    sel: str
    hover: str
    log_bg: str
    log_fg: str
    log_note: str
    log_warn: str
    log_error: str
    log_ok: str
    log_dim: str


LIGHT = Palette(
    bg="#eef0f3",
    panel="#ffffff",
    panel_2="#f6f7f9",
    line="#d9dde3",
    line_2="#e8ebef",
    fg="#1c2026",
    fg_2="#5a6270",
    fg_3="#8a92a0",
    accent="#c8582f",
    accent_fg="#ffffff",
    accent_soft="#f8e6de",
    ok="#2e8b57",
    ok_soft="#e3f3ea",
    warn="#b7791f",
    warn_soft="#fbf0d9",
    bad="#c23b3b",
    bad_soft="#fae3e3",
    sel="#fbebe3",
    hover="#f1f3f6",
    log_bg="#15181d",
    log_fg="#d6dae0",
    log_note="#7fb7ff",
    log_warn="#ffc857",
    log_error="#ff7b7b",
    log_ok="#8fd19e",
    log_dim="#8a919c",
)

DARK = Palette(
    bg="#141619",
    panel="#1d2025",
    panel_2="#23272d",
    line="#30353d",
    line_2="#2a2f36",
    fg="#e6e8ec",
    fg_2="#a6adb8",
    fg_3="#737b87",
    accent="#e8703a",
    accent_fg="#1a1208",
    accent_soft="#3a2518",
    ok="#5fc08a",
    ok_soft="#1b3326",
    warn="#dfae4f",
    warn_soft="#3a2f16",
    bad="#e46a6a",
    bad_soft="#3d1f1f",
    sel="#3a2518",
    hover="#272b32",
    log_bg="#0e1013",
    log_fg="#cfd3d9",
    log_note="#7fb7ff",
    log_warn="#ffc857",
    log_error="#ff7b7b",
    log_ok="#8fd19e",
    log_dim="#8a919c",
)


def palette(dark: bool) -> Palette:
    return DARK if dark else LIGHT


def ui_font() -> QFont:
    font = QFont()
    font.setFamilies(UI_FAMILIES)
    font.setPixelSize(UI_PX)
    return font


def mono_font(px: int = MONO_PX) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(px)
    return font


def style_sheet(p: Palette) -> str:
    """The whole application's QSS. Widgets opt in with object names and the dynamic
    properties `kind` (buttons, edits), `tag` (status labels) and `tone` (text colour)."""
    mono = ", ".join(f'"{f}"' for f in MONO_FAMILIES)
    return f"""
* {{ outline: none; }}
QWidget {{ color: {p.fg}; }}
QMainWindow, QDialog, #window {{ background: {p.panel}; }}
QToolTip {{ background: {p.panel}; color: {p.fg}; border: 1px solid {p.line}; padding: 4px 6px; }}

/* ---- frame areas ---- */
#topbar, #cmdbar {{ background: {p.panel_2}; }}
#topbar {{ border-bottom: 1px solid {p.line}; }}
#cmdbar {{ border-top: 1px solid {p.line}; }}
#sidebar {{ background: {p.panel_2}; border-right: 1px solid {p.line}; }}
#right {{ border-left: 1px solid {p.line}; background: {p.panel}; }}
#center, #pane, #pane > QWidget {{ background: {p.panel}; }}
#brand {{ font-weight: 600; }}
QSplitter::handle {{ background: {p.panel_2}; }}
QSplitter::handle:hover {{ background: {p.line}; }}
#section {{ border-bottom: 1px solid {p.line_2}; }}
#raction {{ border-top: 1px solid {p.line_2}; background: {p.panel}; }}
#loghead {{ border-bottom: 1px solid {p.line_2}; }}

/* ---- text tones ---- */
QLabel[tone="2"] {{ color: {p.fg_2}; }}
QLabel[tone="3"] {{ color: {p.fg_3}; }}
QLabel[tone="bad"] {{ color: {p.bad}; }}
QLabel[mono="1"] {{ font-family: {mono}; font-size: {MONO_PX}px; }}
QLabel[heading="1"] {{ font-size: 15px; font-weight: 600; }}
QLabel[group="1"] {{ color: {p.fg_3}; font-size: 11px; letter-spacing: 1px; }}

/* ---- tags ---- */
QLabel[tag] {{ font-size: 10px; font-weight: 600; padding: 0px 5px; border-radius: 3px;
  min-height: 16px; max-height: 16px; }}
QLabel[tag="ok"] {{ background: {p.ok_soft}; color: {p.ok}; }}
QLabel[tag="warn"] {{ background: {p.warn_soft}; color: {p.warn}; }}
QLabel[tag="bad"] {{ background: {p.bad_soft}; color: {p.bad}; }}
QLabel[tag="info"] {{ background: {p.accent_soft}; color: {p.accent}; }}
QLabel[tag="gray"] {{ background: {p.line_2}; color: {p.fg_2}; }}
QLabel[tag="na"] {{ background: {p.line_2}; color: {p.fg_3}; }}
QLabel[check] {{ font-size: 10px; font-weight: 700; border-radius: 3px;
  min-height: 17px; max-height: 17px; min-width: 52px; max-width: 52px; }}
QLabel[check="pass"] {{ background: {p.ok_soft}; color: {p.ok}; }}
QLabel[check="warn"] {{ background: {p.warn_soft}; color: {p.warn}; }}
QLabel[check="refuse"] {{ background: {p.bad_soft}; color: {p.bad}; }}
QLabel[check="later"] {{ background: {p.line_2}; color: {p.fg_3}; }}

/* ---- hints ---- */
QLabel[hint] {{ padding: 7px 9px; border-radius: 5px; font-size: 12px; }}
QLabel[hint="warn"] {{ background: {p.warn_soft}; color: {p.warn}; }}
QLabel[hint="ok"] {{ background: {p.ok_soft}; color: {p.ok}; }}
#banner {{ background: {p.warn_soft}; border-radius: 5px; }}
#banner QLabel {{ color: {p.warn}; font-size: 12px; }}

/* ---- buttons ---- */
QPushButton {{ background: {p.panel}; border: 1px solid {p.line}; border-radius: 5px;
  padding: 0px 10px; min-height: 26px; color: {p.fg}; }}
QPushButton:hover {{ background: {p.hover}; }}
QPushButton:disabled {{ color: {p.fg_3}; }}
QPushButton[kind="ibtn"] {{ color: {p.fg_2}; padding: 0px 7px; min-height: 24px; min-width: 12px; }}
QPushButton[kind="ibtn"]:hover {{ color: {p.fg}; }}
QPushButton[kind="ibtn"][copied="1"] {{ color: {p.ok}; border-color: {p.ok}; }}
QPushButton[kind="primary"] {{ background: {p.accent}; border-color: {p.accent};
  color: {p.accent_fg};
  font-weight: 600; font-size: 14px; min-height: 32px; }}
QPushButton[kind="primary"]:hover {{ background: {p.accent}; border-color: {p.fg_3}; }}
QPushButton[kind="primary"]:disabled {{ background: {p.accent_soft}; border-color: {p.accent_soft};
  color: {p.fg_3}; }}
QLabel#flashtext {{ font-weight: 600; font-size: 14px; color: {p.accent_fg};
  background: transparent; }}
QLabel#keycap {{ font-size: 11px; font-weight: 600; color: {p.accent_fg}; background: transparent;
  border: 1px solid {p.accent_fg}; border-radius: 4px; padding: 0px 5px; }}
QLabel#flashtext:disabled {{ color: {p.fg_3}; }}
QLabel#keycap:disabled {{ color: {p.fg_3}; border-color: {p.fg_3}; }}
QPushButton[kind="danger"] QLabel#flashtext {{ color: #ffffff; }}
QPushButton[kind="danger"] QLabel#keycap {{ color: #ffffff; border-color: #ffffff; }}
QCheckBox[warn="1"] {{ color: {p.warn}; }}
QPushButton[kind="danger"] {{ background: {p.bad}; border-color: {p.bad}; color: #ffffff;
  font-weight: 600; font-size: 14px; min-height: 32px; }}
QPushButton[kind="danger-small"] {{ background: {p.bad}; border-color: {p.bad}; color: #ffffff; }}
QPushButton[kind="accent-small"] {{ background: {p.accent}; border-color: {p.accent};
  color: {p.accent_fg}; font-weight: 600; }}
QPushButton[kind="newbtn"] {{ background: transparent; border: 1px dashed {p.line}; color: {p.fg_2};
  min-height: 26px; }}
QPushButton[kind="newbtn"]:hover {{ border-color: {p.accent}; color: {p.accent}; }}
QPushButton[kind="wc"], QPushButton[kind="wc-close"] {{ background: transparent; border: none;
  border-radius: 0px; min-width: 46px; max-width: 46px; padding: 0px; }}
QPushButton[kind="wc"]:hover {{ background: {p.hover}; }}
QPushButton[kind="wc-close"]:hover {{ background: #c42b1c; }}
QPushButton[kind="tab"] {{ background: transparent; border: none;
  border-bottom: 2px solid transparent;
  border-radius: 0px; color: {p.fg_2}; padding: 4px 10px; min-height: 22px; }}
QPushButton[kind="tab"]:checked {{ color: {p.fg}; border-bottom-color: {p.accent}; }}
QPushButton[kind="addrow"] {{ background: transparent; border: none; color: {p.fg_3}; }}
QPushButton[kind="addrow"]:hover {{ color: {p.accent}; }}
#addrow {{ border: 1px dashed {p.line}; border-radius: 6px; }}

/* ---- inputs ---- */
QLineEdit, QPlainTextEdit[kind="notes"], QComboBox {{ background: {p.panel};
  border: 1px solid {p.line}; border-radius: 5px; padding: 0px 6px; min-height: 24px;
  selection-background-color: {p.accent_soft};
  selection-color: {p.fg}; }}
QPlainTextEdit[kind="notes"] {{ padding: 4px 6px; font-size: 12px; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit[kind="notes"]:focus {{ border-color: {p.accent}; }}
QLineEdit[kind="search"] {{ padding-left: 24px; min-height: 26px; }}
QLineEdit[kind="cell"] {{ background: transparent; border: 1px solid transparent;
  border-radius: 4px; padding: 0px 4px; min-height: 22px; }}
QLineEdit[kind="cell"]:hover, QLineEdit[kind="cell"]:focus {{ border-color: {p.line};
  background: {p.panel_2}; }}
QLineEdit[kind="cell"][mono="1"] {{ font-family: {mono}; font-size: {MONO_PX}px; }}
#tabline {{ border-bottom: 1px solid {p.line_2}; }}
QLineEdit[kind="mono"] {{ font-family: {mono}; font-size: {MONO_PX}px; }}
QComboBox:disabled {{ color: {p.fg_3}; background: {p.panel_2}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {p.panel}; border: 1px solid {p.line};
  selection-background-color: {p.sel}; selection-color: {p.fg}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}

/* ---- tree ---- */
#tree {{ background: transparent; border: none; }}
#tree::item {{ border-radius: 5px; padding: 0px; }}
#tree::item:hover {{ background: {p.hover}; }}
#tree::item:selected {{ background: {p.sel}; color: {p.fg}; }}
#tree QWidget[dropping="1"] {{ border: 2px dashed {p.accent}; background: {p.accent_soft};
  border-radius: 5px; }}

/* ---- tables and lists ---- */
QTableView {{ background: {p.panel}; border: none; gridline-color: transparent;
  selection-background-color: {p.sel}; selection-color: {p.fg}; }}
QTableView::item {{ border-bottom: 1px solid {p.line_2}; padding: 0px 6px; }}
QHeaderView::section {{ background: {p.panel}; color: {p.fg_3}; border: none;
  border-bottom: 1px solid {p.line}; padding: 4px 8px; font-size: 11px; }}
#records QPushButton[kind="record"] {{ text-align: left; background: transparent; border: none;
  border-bottom: 1px solid {p.line_2}; border-radius: 0px; padding: 6px 4px; }}
#box {{ border: 1px solid {p.line}; border-radius: 6px; }}
#boxhead {{ background: {p.panel_2}; border-bottom: 1px solid {p.line}; font-size: 12px;
  font-weight: 600; padding: 6px 10px; border-top-left-radius: 6px; border-top-right-radius: 6px; }}
#rec {{ border-bottom: 1px solid {p.line_2}; }}
#rec:hover {{ background: {p.hover}; }}

/* ---- progress and log ---- */
QProgressBar {{ background: {p.line_2}; border: none; border-radius: 4px; max-height: 8px;
  min-height: 8px; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 4px; }}
#log {{ background: {p.log_bg}; color: {p.log_fg}; border: none; font-family: {mono};
  font-size: 11.5px; padding: 6px; }}
#cmdtext {{ color: {p.fg_2}; font-family: {mono}; font-size: {MONO_PX}px; }}

/* ---- menus and dialogs ---- */
QMenu {{ background: {p.panel}; border: 1px solid {p.line}; border-radius: 6px; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 10px; border-radius: 4px; }}
QMenu::item:selected {{ background: {p.hover}; color: {p.fg}; }}
QMenu::item:disabled {{ color: {p.fg_3}; }}
QMenu::separator {{ height: 1px; background: {p.line_2}; margin: 4px 0px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0px; }}
QScrollBar::handle:vertical {{ background: {p.line}; border-radius: 4px; min-height: 24px;
  margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0px; }}
QScrollBar::handle:horizontal {{ background: {p.line}; border-radius: 4px; min-width: 24px;
  margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""
