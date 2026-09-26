"""Dark / light themes for the XMEye NVR Tools GUI (palette + Qt style sheet)."""

from __future__ import annotations

import tempfile
from pathlib import Path

DARK = dict(
    window="#15181c", chrome="#101316", sidebar="#181b20", input="#111418",
    border="#2b3139", divider="#242a31", row_line="#1d2126", row_hover="#1b2025",
    text="#e3e6ea", text2="#c4cad1", muted="#99a2ad", label="#7d8692", faint="#4a525c",
    accent="#5cc8d6", accent_hover="#78d4e0", on_accent="#0b1a1d",
    accent_soft="#17333a", accent_soft_fg="#bff0f6",
    hover="#1e2328", button="#1f242a", track="#262c33", disk="#6c7682",
    sel_bg="#172a2e", sel_border="#1f3b40", sel_text="#8fb9bf", sel_btn="#2f5157", sel_hover="#1d353a",
    green="#6fcf97", amber="#d9a441", red="#e5645a", red_text="#e5847b", red_hover="#2a1c1b",
    log_time="#5a626c", scroll="#2b3139", switch_off="#3a414a",
)

LIGHT = dict(
    window="#ffffff", chrome="#eef0f3", sidebar="#f6f7f9", input="#ffffff",
    border="#d3d8de", divider="#e2e5e9", row_line="#eef0f3", row_hover="#f5f7f9",
    text="#1a1d21", text2="#30363d", muted="#5f6772", label="#6b7480", faint="#b3bac2",
    accent="#0e8a9a", accent_hover="#0b7a88", on_accent="#ffffff",
    accent_soft="#e3f3f5", accent_soft_fg="#0b6f7c",
    hover="#eceef1", button="#f1f3f5", track="#e6e9ed", disk="#8a929c",
    sel_bg="#e6f4f6", sel_border="#c9e5e9", sel_text="#3f6f76", sel_btn="#9ccdd4", sel_hover="#d6edf0",
    green="#1e7a4a", amber="#b7801d", red="#d0463b", red_text="#c0392b", red_hover="#fbeceb",
    log_time="#9aa1aa", scroll="#c5cbd2", switch_off="#c5cbd2",
)

# status -> (pill background, pill text, progress bar)
PILLS = {
    "dark": {
        "Done": ("#16291f", "#6fcf97", "#3f8f63"),
        "Failed": ("#2e1a19", "#ef7d73", "#e5645a"),
        "Converting": ("#2c2515", "#e2b454", "#d9a441"),
        "Downloading": ("#15292d", "#6fd3e0", "#5cc8d6"),
        "Queued": ("#1f242a", "#99a2ad", "#4a525c"),
    },
    "light": {
        "Done": ("#e5f4ec", "#1e7a4a", "#3aa56c"),
        "Failed": ("#fbe9e7", "#c0392b", "#d0463b"),
        "Converting": ("#faf1de", "#946510", "#b7801d"),
        "Downloading": ("#e1f3f6", "#0b7a88", "#0e8a9a"),
        "Queued": ("#f1f3f5", "#5f6772", "#b3bac2"),
    },
}

MONO = '"Cascadia Mono", "JetBrains Mono", Consolas, monospace'

_current = "dark"


def current_name() -> str:
    return _current


def colors() -> dict:
    return LIGHT if _current == "light" else DARK


def pill(status: str):
    table = PILLS[_current]
    if status.startswith("Converting"):
        return table["Converting"]
    if status in ("Downloading", "Cancelling", "Stopping"):
        return table["Downloading"]
    if status in ("Stopped", "Cancelled"):
        return table["Queued"]
    return table.get(status, table["Queued"])


def _icons(c: dict) -> dict:
    folder = Path(tempfile.gettempdir()) / "xmeye-nvr-tools-theme" / _current
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "check": f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
                 f'<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="{c["on_accent"]}" stroke-width="2" '
                 f'stroke-linecap="round" stroke-linejoin="round"/></svg>',
        "chevron": f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
                   f'<path d="M2 3.5l3 3 3-3" fill="none" stroke="{c["muted"]}" stroke-width="1.5" '
                   f'stroke-linecap="round" stroke-linejoin="round"/></svg>',
    }
    paths = {}
    for name, svg in files.items():
        path = folder / f"{name}.svg"
        path.write_text(svg, encoding="utf-8")
        paths[name] = path.as_posix()
    return paths


def stylesheet() -> str:
    c = colors()
    i = _icons(c)
    return f"""
QWidget {{ color: {c['text']}; }}
QMainWindow, QWidget#Main, QStackedWidget, QWidget#Page {{ background: {c['window']}; }}
QWidget#Sidebar, QScrollArea#SidebarScroll {{ background: {c['sidebar']}; border: none; }}
QScrollArea#SidebarScroll {{ border-right: 1px solid {c['divider']}; }}
QDialog {{ background: {c['sidebar']}; }}
QToolTip {{ background: {c['sidebar']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px 6px; }}

QLabel#Section {{ color: {c['label']}; font-size: 8pt; font-weight: 600; }}
QLabel#Field {{ color: {c['muted']}; font-size: 9pt; }}
QLabel#Muted {{ color: {c['muted']}; }}
QLabel#Faint {{ color: {c['faint']}; }}
QLabel#Title {{ font-size: 12pt; font-weight: 600; }}
QLabel#BigTime {{ font-family: {MONO}; font-size: 22pt; }}
QFrame#Divider {{ background: {c['divider']}; max-height: 1px; min-height: 1px; border: none; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {c['input']}; border: 1px solid {c['border']}; border-radius: 6px;
    padding: 5px 9px; min-height: 20px;
    selection-background-color: {c['accent']}; selection-color: {c['on_accent']};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
QLineEdit#Mono {{ font-family: {MONO}; }}
QSpinBox#StepperValue, QDoubleSpinBox#StepperValue {{
    border-radius: 0; border-top: none; border-bottom: none; padding: 3px 4px; min-width: 42px;
    background: transparent;
}}
QWidget#Stepper {{ background: {c['input']}; border: 1px solid {c['border']}; border-radius: 6px; }}
QToolButton#StepperButton {{ background: transparent; border: none; color: {c['muted']}; min-width: 24px; font-size: 11pt; }}
QToolButton#StepperButton:hover {{ color: {c['text']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{i['chevron']}"); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{
    background: {c['sidebar']}; border: 1px solid {c['border']}; outline: none;
    selection-background-color: {c['accent_soft']}; selection-color: {c['text']};
}}

QPushButton {{
    background: {c['button']}; border: 1px solid {c['border']}; border-radius: 6px;
    padding: 6px 14px; color: {c['text']};
}}
QPushButton:hover {{ background: {c['hover']}; }}
QPushButton:disabled {{ color: {c['faint']}; }}
QPushButton[primary="true"] {{ background: {c['accent']}; color: {c['on_accent']}; border: 1px solid {c['accent']}; font-weight: 600; padding: 6px 18px; }}
QPushButton[primary="true"]:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
QPushButton[danger="true"] {{ color: {c['red_text']}; }}
QPushButton[danger="true"]:hover {{ background: {c['red_hover']}; }}
QPushButton[link="true"] {{ background: transparent; border: none; color: {c['accent']}; padding: 0 2px; }}
QPushButton[link="true"]:hover {{ color: {c['accent_hover']}; text-decoration: underline; }}
QPushButton#DateField {{ font-family: {MONO}; text-align: left; background: {c['input']}; padding: 6px 12px; }}
QPushButton#DateField:hover {{ border-color: {c['accent']}; }}
QPushButton#CamChip {{ background: {c['input']}; color: {c['muted']}; min-height: 22px; padding: 4px 0; font-weight: 500; }}
QPushButton#CamChip:checked {{ background: {c['accent_soft']}; border-color: {c['accent']}; color: {c['accent_soft_fg']}; }}
QPushButton#Segment {{ border-radius: 0; border: none; border-right: 1px solid {c['border']}; background: transparent; color: {c['text2']}; padding: 6px 14px; }}
QPushButton#Segment:hover {{ background: {c['hover']}; }}
QPushButton#Segment:checked {{ background: {c['divider']}; color: {c['text']}; }}
QFrame#SegmentGroup {{ border: 1px solid {c['border']}; border-radius: 6px; }}
QPushButton#Chip {{ border-radius: 12px; padding: 3px 11px; font-size: 9pt; color: {c['text2']}; background: transparent; }}
QPushButton#Chip:hover {{ background: {c['hover']}; }}
QPushButton#Pager {{ padding: 3px 9px; }}

QFrame#SearchBar {{ background: {c['window']}; border-bottom: 1px solid {c['divider']}; }}
QFrame#Toolbar {{ background: {c['window']}; border-bottom: 1px solid {c['divider']}; }}
QFrame#Footer {{ background: {c['window']}; border-top: 1px solid {c['divider']}; }}
QFrame#SelectionBar {{ background: {c['sel_bg']}; border-bottom: 1px solid {c['sel_border']}; }}
QFrame#SelectionBar QLabel#SelSub {{ color: {c['sel_text']}; }}
QFrame#SelectionBar QPushButton {{ background: transparent; border-color: {c['sel_btn']}; }}
QFrame#SelectionBar QPushButton:hover {{ background: {c['sel_hover']}; }}
QFrame#SelectionBar QPushButton[primary="true"] {{ background: {c['accent']}; border-color: {c['accent']}; }}
QFrame#SelectionBar QPushButton[link="true"] {{ border: none; }}
QFrame#LogPanel {{ background: {c['input']}; border-top: 1px solid {c['divider']}; }}
QFrame#DialogFooter {{ background: {c['window']}; border-top: 1px solid {c['divider']}; }}
QFrame#DialogHeader {{ border-bottom: 1px solid {c['divider']}; }}
QFrame#VRule {{ background: {c['divider']}; max-width: 1px; min-width: 1px; border: none; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {c['divider']}; background: {c['window']}; }}
QTabWidget::tab-bar {{ left: 12px; }}
QTabBar {{ background: {c['window']}; }}
QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: 11px 14px; border: none; border-bottom: 2px solid transparent; font-weight: 500; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom-color: {c['accent']}; }}
QTabBar::tab:hover:!selected {{ color: {c['text2']}; }}

QTableView {{
    background: {c['window']}; border: none; gridline-color: transparent; outline: none;
    selection-background-color: {c['accent_soft']}; selection-color: {c['text']};
}}
QTableView::item {{ border-bottom: 1px solid {c['row_line']}; padding: 0 8px; }}
QTableView::item:hover {{ background: {c['row_hover']}; }}
QTableView::item:selected {{ background: {c['accent_soft']}; color: {c['text']}; }}
QTableView::indicator {{ width: 14px; height: 14px; border-radius: 4px; border: 1px solid {c['faint']}; background: transparent; }}
QTableView::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; image: url("{i['check']}"); }}
QHeaderView {{ background: {c['window']}; }}
QHeaderView::section {{
    background: {c['window']}; color: {c['label']}; border: none; border-bottom: 1px solid {c['divider']};
    padding: 8px 8px; font-size: 9pt;
}}
QTableCornerButton::section {{ background: {c['window']}; border: none; }}

QProgressBar {{ background: {c['track']}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 3px; }}
QProgressBar#DiskBar::chunk {{ background: {c['disk']}; }}

QTextEdit#Log {{ background: {c['input']}; border: none; font-family: {MONO}; font-size: 9pt; padding: 0 12px; }}

QListWidget#TimeColumn {{
    background: {c['input']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 3px; outline: none;
    font-family: {MONO};
}}
QListWidget#TimeColumn::item {{ height: 26px; border-radius: 4px; color: {c['text2']}; }}
QListWidget#TimeColumn::item:hover {{ background: {c['hover']}; }}
QListWidget#TimeColumn::item:selected {{ background: {c['accent']}; color: {c['on_accent']}; font-weight: 600; }}

QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {c['sidebar']}; }}
QCalendarWidget QToolButton {{ background: transparent; color: {c['text']}; border: none; padding: 4px 8px; font-weight: 600; }}
QCalendarWidget QToolButton:hover {{ background: {c['hover']}; border-radius: 6px; }}
QCalendarWidget QToolButton::menu-indicator {{ image: none; }}
QCalendarWidget QAbstractItemView {{
    background: {c['sidebar']}; color: {c['text']}; outline: none; border: none;
    selection-background-color: {c['accent']}; selection-color: {c['on_accent']};
}}
QCalendarWidget QAbstractItemView:disabled {{ color: {c['faint']}; }}
QCalendarWidget QSpinBox {{ min-width: 60px; }}

QStatusBar {{ background: {c['chrome']}; border-top: 1px solid {c['divider']}; color: {c['muted']}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {c['muted']}; font-size: 9pt; padding: 0 4px; }}
QMenuBar {{ background: {c['chrome']}; border-bottom: 1px solid {c['divider']}; padding: 2px 6px; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 4px; background: transparent; color: {c['text2']}; }}
QMenuBar::item:selected {{ background: {c['hover']}; }}
QMenu {{ background: {c['sidebar']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['accent_soft']}; color: {c['text']}; }}
QMenu::item:disabled {{ color: {c['faint']}; }}
QMenu::separator {{ height: 1px; background: {c['divider']}; margin: 4px 6px; }}

QSplitter::handle {{ background: {c['divider']}; }}
QSplitter::handle:vertical {{ height: 1px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {c['scroll']}; border-radius: 3px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {c['faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMessageBox {{ background: {c['sidebar']}; }}
"""


def apply(app, name: str):
    from PySide6.QtGui import QColor, QFont, QPalette

    global _current
    _current = "light" if name == "light" else "dark"
    c = colors()
    palette = QPalette()
    role = QPalette.ColorRole
    for key, value in (
        (role.Window, c["window"]), (role.WindowText, c["text"]),
        (role.Base, c["input"]), (role.AlternateBase, c["row_hover"]),
        (role.Text, c["text"]), (role.Button, c["button"]), (role.ButtonText, c["text"]),
        (role.Highlight, c["accent"]), (role.HighlightedText, c["on_accent"]),
        (role.ToolTipBase, c["sidebar"]), (role.ToolTipText, c["text"]),
        (role.PlaceholderText, c["faint"]), (role.Link, c["accent"]),
    ):
        palette.setColor(key, QColor(value))
    palette.setColor(QPalette.ColorGroup.Disabled, role.Text, QColor(c["faint"]))
    palette.setColor(QPalette.ColorGroup.Disabled, role.ButtonText, QColor(c["faint"]))
    palette.setColor(QPalette.ColorGroup.Disabled, role.WindowText, QColor(c["faint"]))
    app.setPalette(palette)
    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Noto Sans", "sans-serif"])
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(stylesheet())
