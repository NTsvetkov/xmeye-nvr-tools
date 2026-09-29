#!/usr/bin/env python3
"""PySide6 desktop front end for xmeye-nvr-tools."""

from __future__ import annotations

import html
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QDateTime,
    QModelIndex,
    QObject,
    QRectF,
    QRunnable,
    QSettings,
    QSize,
    QThreadPool,
    QTime,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QPainter, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QCalendarWidget,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QDoubleSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import nvr_theme
from nvr_backend import (
    CancelledError,
    ConnectionSettings,
    ConversionOutcome,
    DownloadOutcome,
    LowDiskSpaceError,
    OperationCancellation,
    RecordingRef,
    convert_recording,
    disk_free_info,
    format_size,
    local_status,
    local_paths,
    prepare_download,
    repair_recording,
    search_recordings,
)
from nvr_fetch import format_duration


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def qt_datetime(value: QDateTime) -> datetime:
    return value.toPython()


_MONO_FONT = None


def mono_font() -> QFont:
    global _MONO_FONT
    if _MONO_FONT is None:
        _MONO_FONT = QFont()
        _MONO_FONT.setFamilies(["Cascadia Mono", "JetBrains Mono", "Consolas", "Courier New"])
        _MONO_FONT.setStyleHint(QFont.StyleHint.Monospace)
        _MONO_FONT.setPointSizeF(9.5)
    return _MONO_FONT


def make_button(text, primary=False, danger=False, link=False, name=None):
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        button.setProperty("primary", True)
    if danger:
        button.setProperty("danger", True)
    if link:
        button.setProperty("link", True)
    if name:
        button.setObjectName(name)
    return button


def make_label(text="", name=None):
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    return label


def repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class ToggleSwitch(QCheckBox):
    """QCheckBox painted as a sliding switch: label on the left, switch on the right."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(26)

    def sizeHint(self):
        size = self.fontMetrics().boundingRect(self.text()).size()
        return QSize(size.width() + 60, max(26, size.height() + 8))

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        c = nvr_theme.colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1.0 if self.isEnabled() else 0.4)
        painter.setPen(QColor(c["text"]))
        painter.drawText(
            self.rect().adjusted(0, 0, -46, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text(),
        )
        track = QRectF(self.width() - 36, (self.height() - 20) / 2, 34, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c["accent"] if self.isChecked() else c["switch_off"]))
        painter.drawRoundedRect(track, 10, 10)
        knob_x = track.right() - 18 if self.isChecked() else track.left() + 2
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(knob_x, track.top() + 2, 16, 16))


class Stepper(QWidget):
    """Wraps a spin box as  [ − | value | + ]."""

    def __init__(self, spin: QAbstractSpinBox):
        super().__init__()
        self.setObjectName("Stepper")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.spin = spin
        spin.setObjectName("StepperValue")
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        minus, plus = QToolButton(), QToolButton()
        minus.setText("\u2212"); plus.setText("+")
        for button in (minus, plus):
            button.setObjectName("StepperButton")
            button.setAutoRepeat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        minus.clicked.connect(spin.stepDown)
        plus.clicked.connect(spin.stepUp)
        layout.addWidget(minus); layout.addWidget(spin); layout.addWidget(plus)


class CameraGrid(QWidget):
    """Toggle chips, one per NVR channel, four per row."""

    def __init__(self):
        super().__init__()
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self.buttons: list[QPushButton] = []

    def rebuild(self, count, selected):
        for button in self.buttons:
            self._layout.removeWidget(button)
            button.deleteLater()
        self.buttons = []
        for channel in range(count):
            button = make_button(str(channel + 1), name="CamChip")
            button.setCheckable(True)
            button.setChecked(channel in selected)
            button.setToolTip(f"Camera {channel + 1}")
            self._layout.addWidget(button, channel // 4, channel % 4)
            self.buttons.append(button)

    def selected(self):
        return [index for index, button in enumerate(self.buttons) if button.isChecked()]

    def set_all(self, checked):
        for button in self.buttons:
            button.setChecked(checked)


class DateTimePicker(QPushButton):
    """Single field showing date + time; opens DateTimeDialog on click."""

    dateTimeChanged = Signal(QDateTime)

    def __init__(self, title="Choose date and time", is_end=False):
        super().__init__()
        self.setObjectName("DateField")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(mono_font())
        self.title = title
        self.is_end = is_end
        self.partner: DateTimePicker | None = None
        self._value = QDateTime.currentDateTime()
        self.clicked.connect(self._choose)
        self._refresh()

    def _refresh(self):
        self.setText(
            f"{self._value.toString('yyyy-MM-dd')}   {self._value.toString('HH:mm:ss')}   \u25be"
        )

    def setDateTime(self, value: QDateTime):
        time = value.time()
        self._value = QDateTime(value.date(), QTime(time.hour(), time.minute(), time.second()))
        self._refresh()
        self.dateTimeChanged.emit(self._value)

    def dateTime(self) -> QDateTime:
        return QDateTime(self._value)

    def _choose(self):
        other = self.partner.dateTime() if self.partner else None
        dialog = DateTimeDialog(self.dateTime(), self.title, other, self.is_end, self.window())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.setDateTime(dialog.dateTime())


class DateTimeDialog(QDialog):
    """Calendar + hour/minute/second columns, with quick picks and range preview."""

    def __init__(self, value: QDateTime, title, other: QDateTime | None = None,
                 is_end=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.other = other
        self.is_end = is_end
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame(); header.setObjectName("DialogHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 20, 14)
        header_layout.setSpacing(2)
        header_layout.addWidget(make_label(title, "Title"))
        self.range_label = make_label("", "Muted")
        header_layout.addWidget(self.range_label)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(20, 16, 20, 16)
        body.setSpacing(20)
        left = QVBoxLayout(); left.setSpacing(10)
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(False)
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.calendar.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        self.calendar.setSelectedDate(value.date())
        self.calendar.setMinimumSize(300, 250)
        left.addWidget(self.calendar)
        day_chips = QHBoxLayout(); day_chips.setSpacing(6)
        for text, offset in (("Today", 0), ("Yesterday", -1), ("Week ago", -7)):
            chip = make_button(text, name="Chip")
            chip.clicked.connect(
                lambda _=False, days=offset: self.calendar.setSelectedDate(QDate.currentDate().addDays(days))
            )
            day_chips.addWidget(chip)
        day_chips.addStretch()
        left.addLayout(day_chips)
        body.addLayout(left)
        rule = QFrame(); rule.setObjectName("VRule")
        body.addWidget(rule)

        right = QVBoxLayout(); right.setSpacing(10)
        time_row = QHBoxLayout(); time_row.setSpacing(10)
        self.time_label = make_label("", "BigTime")
        self.time_label.setFont(mono_font())
        big = self.time_label.font(); big.setPointSize(22); self.time_label.setFont(big)
        time_row.addWidget(self.time_label)
        time_row.addWidget(make_label("24h", "Faint"), 0, Qt.AlignmentFlag.AlignBottom)
        time_row.addStretch()
        right.addLayout(time_row)
        columns = QHBoxLayout(); columns.setSpacing(8)
        self.columns: list[QListWidget] = []
        current = value.time()
        for label, count, selected in (
            ("HOUR", 24, current.hour()), ("MIN", 60, current.minute()), ("SEC", 60, current.second()),
        ):
            box = QVBoxLayout(); box.setSpacing(4)
            box.addWidget(make_label(label, "Section"), 0, Qt.AlignmentFlag.AlignHCenter)
            column = QListWidget(); column.setObjectName("TimeColumn")
            column.addItems([f"{item:02d}" for item in range(count)])
            for row in range(count):
                column.item(row).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            column.setFixedSize(72, 204)
            column.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            column.setCurrentRow(selected)
            column.currentRowChanged.connect(self._update_labels)
            box.addWidget(column)
            columns.addLayout(box)
            self.columns.append(column)
        columns.addStretch()
        right.addLayout(columns)
        time_chips = QGridLayout(); time_chips.setSpacing(6)
        now = QTime.currentTime()
        picks = (("00:00", 0, 0, 0), ("06:00", 6, 0, 0), ("12:00", 12, 0, 0),
                 ("18:00", 18, 0, 0), ("23:59:59", 23, 59, 59), ("Now", now.hour(), now.minute(), now.second()))
        for index, (text, hour, minute, second) in enumerate(picks):
            chip = make_button(text, name="Chip")
            chip.clicked.connect(lambda _=False, h=hour, m=minute, s=second: self._set_time(h, m, s))
            time_chips.addWidget(chip, index // 3, index % 3)
        right.addLayout(time_chips)
        right.addStretch()
        body.addLayout(right)
        root.addLayout(body)

        footer = QFrame(); footer.setObjectName("DialogFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(20, 12, 20, 12)
        footer_layout.setSpacing(10)
        self.summary_label = make_label("", "Muted")
        footer_layout.addWidget(self.summary_label)
        footer_layout.addStretch()
        cancel = make_button("Cancel")
        apply_button = make_button("Apply", primary=True)
        apply_button.setDefault(True)
        cancel.clicked.connect(self.reject)
        apply_button.clicked.connect(self.accept)
        footer_layout.addWidget(cancel); footer_layout.addWidget(apply_button)
        root.addWidget(footer)

        self.calendar.selectionChanged.connect(self._update_labels)
        self.calendar.activated.connect(lambda _date: self.accept())
        self._update_labels()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._center_columns)

    def _center_columns(self):
        for column in self.columns:
            if column.currentItem():
                column.scrollToItem(column.currentItem(), QAbstractItemView.ScrollHint.PositionAtCenter)

    def _set_time(self, hour, minute, second):
        for column, value in zip(self.columns, (hour, minute, second)):
            column.setCurrentRow(value)
        self._center_columns()

    def _paint_range(self, value: QDateTime):
        c = nvr_theme.colors()
        self.calendar.setDateTextFormat(QDate(), QTextCharFormat())
        if self.other is None:
            return
        start, end = (self.other, value) if self.is_end else (value, self.other)
        if end > start and start.daysTo(end) <= 400:
            band = QTextCharFormat()
            band.setBackground(QColor(c["sel_bg"]))
            day = start.date()
            while day <= end.date():
                self.calendar.setDateTextFormat(day, band)
                day = day.addDays(1)
        mark = QTextCharFormat()
        mark.setBackground(QColor(c["accent_soft"]))
        mark.setForeground(QColor(c["accent_soft_fg"]))
        mark.setFontWeight(QFont.Weight.DemiBold)
        self.calendar.setDateTextFormat(self.other.date(), mark)

    def _update_labels(self, *_):
        value = self.dateTime()
        self.time_label.setText(value.toString("HH:mm:ss"))
        self.summary_label.setText(value.toString("ddd, dd MMM yyyy  \u00b7  HH:mm:ss"))
        self._paint_range(value)
        if self.other is None:
            self.range_label.setText("")
            return
        start, end = (self.other, value) if self.is_end else (value, self.other)
        hours = start.secsTo(end) / 3600
        if hours <= 0:
            self.range_label.setText("End must be later than start")
        elif hours >= 48:
            self.range_label.setText(f"Range: {round(hours / 24)} days")
        else:
            self.range_label.setText(f"Range: {hours:.1f} h")

    def dateTime(self) -> QDateTime:
        time = QTime(*(column.currentRow() for column in self.columns))
        return QDateTime(self.calendar.selectedDate(), time)


class _PaintedCellDelegate(QStyledItemDelegate):
    def _background(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)


class StatusDelegate(_PaintedCellDelegate):
    """Queue status drawn as a coloured pill."""

    def paint(self, painter, option, index):
        self._background(painter, option, index)
        text = index.data() or ""
        if not text:
            return
        background, foreground, _bar = nvr_theme.pill(text)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = option.fontMetrics.horizontalAdvance(text) + 32
        rect = QRectF(option.rect.left() + 8, option.rect.center().y() - 10.5, width, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(rect, 11, 11)
        painter.setBrush(QColor(foreground))
        painter.drawEllipse(QRectF(rect.left() + 10, rect.center().y() - 3, 6, 6))
        painter.setPen(QColor(foreground))
        painter.drawText(rect.adjusted(22, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.restore()


class ProgressDelegate(_PaintedCellDelegate):
    """Per-row progress bar coloured by the row's status."""

    def paint(self, painter, option, index):
        self._background(painter, option, index)
        text = index.data() or "0%"
        try:
            value = max(0, min(100, int(text.rstrip("%"))))
        except ValueError:
            value = 0
        status = index.siblingAtColumn(5).data() or ""
        c = nvr_theme.colors()
        _bg, _fg, bar = nvr_theme.pill(status)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        track = QRectF(rect.left() + 8, rect.center().y() - 2, max(10, rect.width() - 64), 5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c["track"]))
        painter.drawRoundedRect(track, 2.5, 2.5)
        if value:
            painter.setBrush(QColor(bar))
            painter.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * value / 100, 5), 2.5, 2.5)
        painter.setPen(QColor(c["muted"]))
        painter.drawText(
            QRectF(track.right() + 6, rect.top(), 44, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            f"{value}%" if value else "\u2014",
        )
        painter.restore()


class ResultsModel(QAbstractTableModel):
    selection_changed = Signal()
    page_changed = Signal()
    HEADERS = ("", "Camera", "Start", "End", "Duration", "Size", "Local status")

    def __init__(self, output_dir: Path):
        super().__init__()
        self._items: list[RecordingRef] = []
        self._selected: set[tuple[int, str]] = set()
        self.output_dir = output_dir
        self._local_statuses: dict[tuple[int, str], str] = {}
        self.page_size: int | None = 100
        self.page = 0

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._page_items())

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._page_items()):
            return None
        item = self._page_items()[index.row()]
        recording = item.recording
        column = index.column()
        if column == 0 and role == Qt.ItemDataRole.CheckStateRole:
            return (
                Qt.CheckState.Checked
                if item.key in self._selected
                else Qt.CheckState.Unchecked
            )
        if role == Qt.ItemDataRole.ForegroundRole and column in (3, 6):
            c = nvr_theme.colors()
            if column == 3:
                return QColor(c["muted"])
            status = self._local_status(item)
            if "MP4" in status:
                return QColor(c["green"])
            if status != "not downloaded":
                return QColor(c["amber"])
            return QColor(c["muted"])
        if role == Qt.ItemDataRole.FontRole and column in (2, 3):
            return mono_font()
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (4, 5):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.DisplayRole:
            values = (
                "",
                f"Camera {item.channel + 1}",
                recording.begin.strftime("%Y-%m-%d %H:%M:%S"),
                recording.end.strftime("%Y-%m-%d %H:%M:%S"),
                format_duration(recording.duration.total_seconds()),
                format_size(recording.size_bytes),
                self._local_status(item),
            )
            return values[column]
        if role == Qt.ItemDataRole.UserRole:
            sort_values = (
                item.key,
                item.channel,
                recording.begin,
                recording.end,
                recording.duration.total_seconds(),
                recording.size_bytes,
                self._local_status(item),
            )
            return sort_values[column]
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if index.column() != 0 or role != Qt.ItemDataRole.CheckStateRole:
            return False
        item = self._page_items()[index.row()]
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
            self._selected.add(item.key)
        else:
            self._selected.discard(item.key)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()
        return True

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        reverse = order == Qt.SortOrder.DescendingOrder
        key_functions = (
            lambda item: item.key,
            lambda item: item.channel,
            lambda item: item.recording.begin,
            lambda item: item.recording.end,
            lambda item: item.recording.duration.total_seconds(),
            lambda item: item.recording.size_bytes,
            lambda item: self._local_status(item),
        )
        self.layoutAboutToBeChanged.emit()
        self._items.sort(key=key_functions[column], reverse=reverse)
        self.page = 0
        self.layoutChanged.emit()
        self.page_changed.emit()

    def _page_items(self):
        if self.page_size is None:
            return self._items
        start = self.page * self.page_size
        return self._items[start:start + self.page_size]

    def set_items(self, items: list[RecordingRef]):
        self.beginResetModel()
        self._items = list(items)
        self._refresh_local_status_cache()
        self._selected.clear()
        self.page = 0
        self.endResetModel()
        self.selection_changed.emit()
        self.page_changed.emit()

    def set_page_size(self, size: int | None):
        self.beginResetModel()
        self.page_size = size
        self.page = 0
        self.endResetModel()
        self.page_changed.emit()

    def page_count(self):
        if self.page_size is None or not self._items:
            return 1
        return max(1, math.ceil(len(self._items) / self.page_size))

    def set_page(self, page: int):
        page = max(0, min(page, self.page_count() - 1))
        if page == self.page:
            return
        self.beginResetModel()
        self.page = page
        self.endResetModel()
        self.page_changed.emit()

    def select_visible(self):
        for item in self._page_items():
            self._selected.add(item.key)
        if self.rowCount():
            self.dataChanged.emit(
                self.index(0, 0), self.index(self.rowCount() - 1, 0),
                [Qt.ItemDataRole.CheckStateRole],
            )
        self.selection_changed.emit()

    def clear_selection(self):
        self._selected.clear()
        if self.rowCount():
            self.dataChanged.emit(
                self.index(0, 0), self.index(self.rowCount() - 1, 0),
                [Qt.ItemDataRole.CheckStateRole],
            )
        self.selection_changed.emit()

    def selected_items(self):
        return [item for item in self._items if item.key in self._selected]

    def refresh_local_status(self, output_dir: Path):
        self.output_dir = output_dir
        self._refresh_local_status_cache()
        if self.rowCount():
            self.dataChanged.emit(
                self.index(0, 6), self.index(self.rowCount() - 1, 6),
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole],
            )

    def _refresh_local_status_cache(self):
        self._local_statuses = {
            item.key: local_status(item, self.output_dir) for item in self._items
        }

    def _local_status(self, item: RecordingRef) -> str:
        return self._local_statuses.get(item.key, "not downloaded")

    @property
    def total_count(self):
        return len(self._items)


class SearchSignals(QObject):
    log = Signal(str)
    finished = Signal(object)
    failed = Signal(object)


class SearchWorker(QRunnable):
    def __init__(self, settings, channels, begin, end, cancel_event):
        super().__init__()
        self.settings = settings
        self.channels = channels
        self.begin = begin
        self.end = end
        self.cancel_event = cancel_event
        self.signals = SearchSignals()

    def run(self):
        try:
            items = search_recordings(
                self.settings, self.channels, self.begin, self.end,
                self.signals.log.emit, self.cancel_event,
            )
            self.signals.finished.emit(items)
        except Exception as exc:
            self.signals.failed.emit(exc)


@dataclass
class QueueEntry:
    item: RecordingRef
    settings: ConnectionSettings
    output_dir: Path
    convert: bool
    delete_raw: bool
    min_free_percent: float
    status: str = "Queued"
    progress: int = 0
    speed: float = 0.0
    cancel_event: OperationCancellation = field(default_factory=OperationCancellation)
    cancel_status: str = "Cancelled"
    retry_after_stop: bool = False
    force_download: bool = False

    @property
    def key(self):
        return (
            self.settings.host,
            self.settings.port,
            str(self.output_dir.resolve()),
            *self.item.key,
        )


class WorkSignals(QObject):
    log = Signal(str)
    progress = Signal(object, int, int, float)
    finished = Signal(object, object)
    failed = Signal(object, object)


class DownloadWorker(QRunnable):
    def __init__(self, entry):
        super().__init__()
        self.entry = entry
        self.signals = WorkSignals()

    def run(self):
        try:
            outcome = prepare_download(
                self.entry.item, self.entry.settings, self.entry.output_dir,
                self.entry.convert, self.entry.min_free_percent,
                self.entry.cancel_event,
                lambda done, total, speed: self.signals.progress.emit(
                    self.entry, done, total, speed
                ),
                self.signals.log.emit,
                force=self.entry.force_download,
            )
            self.signals.finished.emit(self.entry, outcome)
        except Exception as exc:
            self.signals.failed.emit(self.entry, exc)


class ConversionWorker(QRunnable):
    def __init__(self, entry, outcome):
        super().__init__()
        self.entry = entry
        self.outcome = outcome
        self.signals = WorkSignals()

    def run(self):
        try:
            conversion: ConversionOutcome = convert_recording(
                self.outcome.raw_path,
                self.entry.cancel_event,
                self.signals.log.emit,
                force=self.entry.force_download,
            )
            if (
                self.entry.delete_raw
                and not conversion.repaired
                and not self.entry.cancel_event.is_set()
            ):
                self.outcome.raw_path.unlink()
                self.signals.log.emit(
                    f"Deleted raw file: {self.outcome.raw_path.name}"
                )
            elif self.entry.delete_raw and conversion.repaired:
                self.signals.log.emit(
                    "Raw file kept because conversion used automatic repair: "
                    f"{self.outcome.raw_path.name}"
                )
            self.signals.finished.emit(self.entry, self.outcome)
        except Exception as exc:
            self.signals.failed.emit(self.entry, exc)


class RepairSignals(QObject):
    finished = Signal(object, str)
    failed = Signal(object)


class RepairWorker(QRunnable):
    def __init__(self, source, target):
        super().__init__()
        self.source = source
        self.target = target
        self.signals = RepairSignals()

    def run(self):
        try:
            path, summary = repair_recording(self.source, self.target)
            self.signals.finished.emit(path, summary)
        except Exception as exc:
            self.signals.failed.emit(exc)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("0% - XMEye NVR Tools")
        self.resize(1280, 900)
        self.settings = QSettings("xmeye-nvr-tools", "nvr-gui")
        self.search_pool = QThreadPool(self)
        self.download_pool = QThreadPool(self)
        self.conversion_pool = QThreadPool(self)
        self.misc_pool = QThreadPool(self)
        self.search_cancel: OperationCancellation | None = None
        self._closing = False
        self._close_ready = False
        self.queue_entries: list[QueueEntry] = []
        self.queue_running = False
        self.active_downloads: set[tuple[int, str]] = set()
        self.active_conversions: set[tuple[int, str]] = set()
        self.waiting_conversion: list[tuple[QueueEntry, DownloadOutcome]] = []
        self.queue_rows: dict[tuple[int, str], int] = {}
        self._build_ui()
        self._load_settings()
        self._connect_signals()
        self._update_disk_space()
        self._update_results_totals()
        self._update_page_controls()
        self._update_password_label()
        self._update_host_status()
        self._update_queue_status()
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_queue_status)
        self.status_timer.start(1000)

    def _build_ui(self):
        central = QWidget(); central.setObjectName("Main")
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())

        main = QWidget(); main.setObjectName("Main")
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self._build_search_bar())
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_results_page(), "Search results")
        self.tabs.addTab(self._build_queue_page(), "Download queue")
        self.content_splitter = QSplitter(Qt.Orientation.Vertical)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.addWidget(self.tabs)
        self.content_splitter.addWidget(self._build_log_panel())
        self.content_splitter.setStretchFactor(0, 4)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setSizes([640, 150])
        main_layout.addWidget(self.content_splitter, 1)
        outer.addWidget(main, 1)

        self._build_status_bar()
        self._build_menus()

    @staticmethod
    def _section(text):
        return make_label(text.upper(), "Section")

    @staticmethod
    def _field(text):
        return make_label(text, "Field")

    @staticmethod
    def _stepper_row(text, spin):
        row = QHBoxLayout()
        row.addWidget(QLabel(text))
        row.addStretch()
        row.addWidget(Stepper(spin))
        return row

    def _build_sidebar(self):
        panel = QWidget(); panel.setObjectName("Sidebar")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(self._section("Device"))
        head.addStretch()
        self.password_label = QLabel()
        self.password_label.setToolTip("Read from the XMEYE_PASSWORD environment variable at start-up")
        head.addWidget(self.password_label)
        layout.addLayout(head)
        self.host_edit = QLineEdit(); self.host_edit.setFont(mono_font())
        self.host_edit.setPlaceholderText("192.168.1.10")
        self.port_spin = QSpinBox(); self.port_spin.setRange(1, 65535)
        self.port_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.port_spin.setFont(mono_font()); self.port_spin.setFixedWidth(84)
        grid = QGridLayout(); grid.setHorizontalSpacing(8); grid.setVerticalSpacing(4)
        grid.addWidget(self._field("Host"), 0, 0); grid.addWidget(self._field("Port"), 0, 1)
        grid.addWidget(self.host_edit, 1, 0); grid.addWidget(self.port_spin, 1, 1)
        grid.setColumnStretch(0, 1)
        layout.addLayout(grid)
        layout.addWidget(self._field("Username"))
        self.user_edit = QLineEdit()
        layout.addWidget(self.user_edit)
        layout.addSpacing(14)

        head = QHBoxLayout()
        head.addWidget(self._section("Cameras"))
        head.addStretch()
        self.cams_all_button = make_button("All", link=True)
        self.cams_none_button = make_button("None", link=True)
        head.addWidget(self.cams_all_button); head.addWidget(self.cams_none_button)
        layout.addLayout(head)
        self.camera_grid = CameraGrid()
        layout.addWidget(self.camera_grid)
        self.camera_count = QSpinBox(); self.camera_count.setRange(1, 64)
        layout.addLayout(self._stepper_row("Channels", self.camera_count))
        layout.addSpacing(14)

        layout.addWidget(self._section("Output"))
        row = QHBoxLayout(); row.setSpacing(6)
        self.output_edit = QLineEdit(); self.output_edit.setFont(mono_font())
        self.browse_button = make_button("Browse")
        row.addWidget(self.output_edit, 1); row.addWidget(self.browse_button)
        layout.addLayout(row)
        self.disk_bar = QProgressBar(); self.disk_bar.setObjectName("DiskBar")
        self.disk_bar.setRange(0, 1000); self.disk_bar.setTextVisible(False)
        layout.addWidget(self.disk_bar)
        self.disk_label = make_label("", "Muted")
        self.disk_label.setTextFormat(Qt.TextFormat.RichText)
        self.disk_label.setWordWrap(True)
        layout.addWidget(self.disk_label)
        layout.addSpacing(14)

        layout.addWidget(self._section("Processing"))
        self.convert_check = ToggleSwitch("Convert to MP4")
        self.delete_raw_check = ToggleSwitch("Delete raw after conversion")
        layout.addWidget(self.convert_check)
        layout.addWidget(self.delete_raw_check)
        divider = QFrame(); divider.setObjectName("Divider")
        layout.addWidget(divider)
        self.downloads_spin = QSpinBox(); self.downloads_spin.setRange(1, 16)
        self.conversions_spin = QSpinBox(); self.conversions_spin.setRange(1, 16)
        self.min_free_spin = QDoubleSpinBox(); self.min_free_spin.setRange(0, 100)
        self.min_free_spin.setSuffix(" %"); self.min_free_spin.setDecimals(1)
        self.min_free_spin.setSingleStep(0.5)
        layout.addLayout(self._stepper_row("Parallel downloads", self.downloads_spin))
        layout.addLayout(self._stepper_row("Parallel conversions", self.conversions_spin))
        layout.addLayout(self._stepper_row("Keep free space", self.min_free_spin))
        layout.addStretch()

        scroll = QScrollArea(); scroll.setObjectName("SidebarScroll")
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(300)
        return scroll

    def _build_search_bar(self):
        bar = QFrame(); bar.setObjectName("SearchBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(12)
        self.from_edit = DateTimePicker("Start of period")
        self.to_edit = DateTimePicker("End of period", is_end=True)
        self.from_edit.partner, self.to_edit.partner = self.to_edit, self.from_edit
        for text, picker in (("From", self.from_edit), ("To", self.to_edit)):
            column = QVBoxLayout(); column.setSpacing(4)
            column.addWidget(self._field(text)); column.addWidget(picker)
            layout.addLayout(column)
            if picker is self.from_edit:
                layout.addWidget(make_label("\u2192", "Faint"), 0, Qt.AlignmentFlag.AlignBottom)
        group = QFrame(); group.setObjectName("SegmentGroup")
        group_layout = QHBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(0)
        self.preset_group = QButtonGroup(self)
        self.today_button = make_button("Today", name="Segment")
        self.yesterday_button = make_button("Yesterday", name="Segment")
        self.last_week_button = make_button("Last 7 days", name="Segment")
        for button in (self.today_button, self.yesterday_button, self.last_week_button):
            button.setCheckable(True)
            self.preset_group.addButton(button)
            group_layout.addWidget(button)
        layout.addWidget(group, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addStretch()
        self.search_button = make_button("Search", primary=True)
        self.search_button.setMinimumWidth(110)
        layout.addWidget(self.search_button, 0, Qt.AlignmentFlag.AlignBottom)
        return bar

    def _build_results_page(self):
        page = QWidget(); page.setObjectName("Page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QFrame(); bar.setObjectName("SelectionBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(20, 8, 20, 8)
        bar_layout.setSpacing(12)
        self.selection_label = QLabel()
        bold = self.selection_label.font(); bold.setWeight(QFont.Weight.DemiBold)
        self.selection_label.setFont(bold)
        self.selection_size_label = make_label("", "SelSub")
        self.clear_selection_button = make_button("Clear", link=True)
        self.select_visible_button = make_button("Select page", link=True)
        self.add_queue_button = make_button("Add to queue")
        self.download_now_button = make_button("Download now", primary=True)
        for widget in (self.selection_label, self.selection_size_label,
                       self.clear_selection_button, self.select_visible_button):
            bar_layout.addWidget(widget)
        bar_layout.addStretch()
        bar_layout.addWidget(self.add_queue_button)
        bar_layout.addWidget(self.download_now_button)
        layout.addWidget(bar)

        self.results_model = ResultsModel(PROJECT_ROOT / "downloads")
        self.results_table = QTableView(); self.results_table.setModel(self.results_model)
        self.results_table.setSortingEnabled(True)
        self.results_table.setShowGrid(False)
        self.results_table.setWordWrap(False)
        self.results_table.setMouseTracking(True)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.verticalHeader().hide()
        self.results_table.verticalHeader().setDefaultSectionSize(34)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.results_table, 1)

        footer = QFrame(); footer.setObjectName("Footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(20, 6, 20, 6)
        footer_layout.setSpacing(10)
        self.results_totals = make_label("", "Muted")
        footer_layout.addWidget(self.results_totals)
        footer_layout.addStretch()
        footer_layout.addWidget(make_label("Rows per page", "Muted"))
        self.page_size_combo = QComboBox(); self.page_size_combo.addItems(["50", "100", "250", "All"])
        footer_layout.addWidget(self.page_size_combo)
        self.page_label = make_label("", "Muted")
        footer_layout.addWidget(self.page_label)
        self.prev_button = make_button("\u2039", name="Pager")
        self.next_button = make_button("\u203a", name="Pager")
        self.prev_button.setToolTip("Previous page"); self.next_button.setToolTip("Next page")
        footer_layout.addWidget(self.prev_button); footer_layout.addWidget(self.next_button)
        layout.addWidget(footer)
        return page

    def _build_queue_page(self):
        page = QWidget(); page.setObjectName("Page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QFrame(); toolbar.setObjectName("Toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(20, 8, 20, 8)
        toolbar_layout.setSpacing(8)
        self.start_queue_button = make_button("\u25b6  Start queue", primary=True)
        self.start_queue_button.setMinimumWidth(130)
        self.start_queue_button.setToolTip(
            "Pause starts no new downloads; active downloads and all pending conversions finish"
        )
        rule = QFrame(); rule.setObjectName("VRule"); rule.setFixedHeight(20)
        self.retry_failed_button = make_button("Retry failed")
        self.retry_failed_button.setToolTip("Re-queue failed, stopped and cancelled items")
        self.clear_completed_button = make_button("Clear completed")
        self.remove_queue_button = make_button("Remove selected")
        self.cancel_active_button = make_button("Cancel active", danger=True)
        for widget in (self.start_queue_button, rule, self.retry_failed_button,
                       self.clear_completed_button, self.remove_queue_button,
                       self.cancel_active_button):
            toolbar_layout.addWidget(widget)
        toolbar_layout.addStretch()
        self.queue_stats_label = make_label("", "Muted")
        self.queue_stats_label.setTextFormat(Qt.TextFormat.RichText)
        toolbar_layout.addWidget(self.queue_stats_label)
        layout.addWidget(toolbar)

        self.queue_table = QTableWidget(0, 8)
        self.queue_table.setHorizontalHeaderLabels(
            ["Camera", "Start", "End", "Duration", "Size", "Status", "Progress", "Speed"]
        )
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_table.setShowGrid(False)
        self.queue_table.setWordWrap(False)
        self.queue_table.setMouseTracking(True)
        self.queue_table.verticalHeader().hide()
        self.queue_table.verticalHeader().setDefaultSectionSize(36)
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column, width in enumerate((100, 175, 175, 90, 100, 160, 220, 110)):
            self.queue_table.setColumnWidth(column, width)
        self.queue_table.setColumnHidden(2, True)
        header.setStretchLastSection(True)
        self.queue_table.setItemDelegateForColumn(5, StatusDelegate(self.queue_table))
        self.queue_table.setItemDelegateForColumn(6, ProgressDelegate(self.queue_table))
        layout.addWidget(self.queue_table, 1)
        return page

    def _build_log_panel(self):
        panel = QFrame(); panel.setObjectName("LogPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 6)
        layout.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(20, 8, 20, 4)
        head.addWidget(self._section("Log"))
        head.addStretch()
        self.copy_log_button = make_button("Copy", link=True)
        self.clear_log_button = make_button("Clear", link=True)
        head.addWidget(self.copy_log_button); head.addWidget(self.clear_log_button)
        layout.addLayout(head)
        self.log_edit = QTextEdit(); self.log_edit.setObjectName("Log")
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(mono_font())
        layout.addWidget(self.log_edit)
        panel.setMinimumHeight(90)
        return panel

    def _build_status_bar(self):
        bar = self.statusBar()
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(8, 2, 8, 2)
        row.setSpacing(10)
        self.run_label = QLabel(); self.run_label.setTextFormat(Qt.TextFormat.RichText)
        self.overall_progress = QProgressBar()
        self.overall_progress.setTextVisible(False)
        self.overall_progress.setFixedWidth(220)
        self.overall_label = QLabel("0%")
        self.speed_label = QLabel()
        self.eta_label = QLabel()
        for widget in (self.run_label, self.overall_progress, self.overall_label,
                       self.speed_label, self.eta_label):
            row.addWidget(widget)
        row.addStretch()
        bar.addWidget(holder, 1)
        self.host_status_label = QLabel()
        bar.addPermanentWidget(self.host_status_label)

    def _build_menus(self):
        tools_menu = self.menuBar().addMenu("Tools")
        repair_action = QAction("Repair .xmeye...", self)
        repair_action.triggered.connect(self._repair_file)
        tools_menu.addAction(repair_action)
        view_menu = self.menuBar().addMenu("View")
        theme_menu = view_menu.addMenu("Theme")
        group = QActionGroup(self)
        for name, text in (("dark", "Dark"), ("light", "Light")):
            action = QAction(text, self, checkable=True)
            action.setChecked(nvr_theme.current_name() == name)
            action.triggered.connect(lambda _=False, value=name: self._set_theme(value))
            group.addAction(action)
            theme_menu.addAction(action)

    def _connect_signals(self):
        self.camera_count.valueChanged.connect(self._rebuild_cameras)
        self.cams_all_button.clicked.connect(lambda: self.camera_grid.set_all(True))
        self.cams_none_button.clicked.connect(lambda: self.camera_grid.set_all(False))
        self.convert_check.toggled.connect(self.delete_raw_check.setEnabled)
        self.output_edit.editingFinished.connect(self._output_changed)
        self.browse_button.clicked.connect(self._browse_output)
        self.host_edit.textChanged.connect(self._update_host_status)
        self.port_spin.valueChanged.connect(self._update_host_status)
        self.today_button.clicked.connect(lambda: self._set_day(0))
        self.yesterday_button.clicked.connect(lambda: self._set_day(-1))
        self.last_week_button.clicked.connect(lambda: self._set_last_days(7))
        self.from_edit.dateTimeChanged.connect(self._manual_period_change)
        self.to_edit.dateTimeChanged.connect(self._manual_period_change)
        self.search_button.clicked.connect(self._search)
        self.results_model.selection_changed.connect(self._update_results_totals)
        self.results_model.page_changed.connect(self._update_page_controls)
        self.select_visible_button.clicked.connect(self.results_model.select_visible)
        self.clear_selection_button.clicked.connect(self.results_model.clear_selection)
        self.page_size_combo.currentTextChanged.connect(self._page_size_changed)
        self.prev_button.clicked.connect(lambda: self.results_model.set_page(self.results_model.page - 1))
        self.next_button.clicked.connect(lambda: self.results_model.set_page(self.results_model.page + 1))
        self.add_queue_button.clicked.connect(lambda: self._add_selected(False))
        self.download_now_button.clicked.connect(lambda: self._add_selected(True))
        self.start_queue_button.clicked.connect(self._toggle_queue)
        self.remove_queue_button.clicked.connect(self._remove_selected_queue)
        self.clear_completed_button.clicked.connect(self._clear_completed)
        self.retry_failed_button.clicked.connect(self._retry_failed)
        self.cancel_active_button.clicked.connect(self._cancel_active)
        self.queue_table.customContextMenuRequested.connect(self._queue_context_menu)
        self.downloads_spin.valueChanged.connect(self._apply_pool_limits)
        self.conversions_spin.valueChanged.connect(self._apply_pool_limits)
        self.min_free_spin.valueChanged.connect(self._update_disk_space)
        self.copy_log_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self.log_edit.toPlainText())
        )
        self.clear_log_button.clicked.connect(self.log_edit.clear)

    def _load_settings(self):
        self.host_edit.setText(self.settings.value("host", "192.168.66.153"))
        self.port_spin.setValue(int(self.settings.value("port", 34567)))
        self.user_edit.setText(self.settings.value("username", "koko"))
        count = int(self.settings.value("camera_count", 8))
        selected = {
            int(value) for value in str(self.settings.value("selected_cameras", "0")).split(",")
            if value.strip().isdigit()
        }
        self.camera_count.blockSignals(True); self.camera_count.setValue(count); self.camera_count.blockSignals(False)
        self._rebuild_cameras(count, selected)
        output = self.settings.value("output_directory", str(PROJECT_ROOT / "downloads"))
        self.output_edit.setText(str(output))
        self.convert_check.setChecked(self.settings.value("convert", False, type=bool))
        self.delete_raw_check.setChecked(self.settings.value("delete_raw", False, type=bool))
        self.delete_raw_check.setEnabled(self.convert_check.isChecked())
        self.downloads_spin.setValue(int(self.settings.value("concurrent_downloads", 2)))
        self.conversions_spin.setValue(int(self.settings.value("concurrent_conversions", 1)))
        self.min_free_spin.setValue(float(self.settings.value("minimum_free_percent", 5.0)))
        page_size = str(self.settings.value("result_page_size", "100"))
        index = self.page_size_combo.findText(page_size)
        self.page_size_combo.setCurrentIndex(index if index >= 0 else 1)
        self.results_model.set_page_size(None if page_size == "All" else int(page_size))
        splitter_state = self.settings.value("content_splitter_v2")
        if splitter_state is not None:
            self.content_splitter.restoreState(splitter_state)
        now = QDateTime.currentDateTime()
        self.to_edit.setDateTime(now)
        self.from_edit.setDateTime(now.addDays(-1))
        self.results_model.refresh_local_status(Path(self.output_edit.text()))
        self._apply_pool_limits()

    def _save_settings(self):
        self.settings.setValue("host", self.host_edit.text().strip())
        self.settings.setValue("port", self.port_spin.value())
        self.settings.setValue("username", self.user_edit.text().strip())
        self.settings.setValue("camera_count", self.camera_count.value())
        self.settings.setValue("selected_cameras", ",".join(map(str, self._selected_cameras())))
        self.settings.setValue("output_directory", self.output_edit.text().strip())
        self.settings.setValue("convert", self.convert_check.isChecked())
        self.settings.setValue("delete_raw", self.delete_raw_check.isChecked())
        self.settings.setValue("concurrent_downloads", self.downloads_spin.value())
        self.settings.setValue("concurrent_conversions", self.conversions_spin.value())
        self.settings.setValue("minimum_free_percent", self.min_free_spin.value())
        self.settings.setValue("result_page_size", self.page_size_combo.currentText())
        self.settings.setValue("content_splitter_v2", self.content_splitter.saveState())

    def _rebuild_cameras(self, count, selected=None):
        if selected is None:
            selected = set(self._selected_cameras())
        self.camera_grid.rebuild(count, selected)

    def _selected_cameras(self):
        return self.camera_grid.selected()

    def _connection_settings(self):
        password = os.environ.get("XMEYE_PASSWORD", "")
        if not password:
            raise ValueError("XMEYE_PASSWORD is not set. Restart the GUI after setting it.")
        host = self.host_edit.text().strip()
        username = self.user_edit.text().strip()
        if not host or not username:
            raise ValueError("Host and username are required.")
        return ConnectionSettings(host, self.port_spin.value(), username, password)

    def _set_day(self, offset):
        day = datetime.now().date() + timedelta(days=offset)
        start = datetime.combine(day, datetime.min.time())
        end = datetime.combine(day, datetime.max.time()).replace(microsecond=0)
        if offset == 0:
            end = datetime.now().replace(microsecond=0)
        self._apply_period(start, end, self.today_button if offset == 0 else self.yesterday_button)

    def _set_last_days(self, days):
        end = datetime.now().replace(microsecond=0)
        start = datetime.combine((end - timedelta(days=days - 1)).date(), datetime.min.time())
        self._apply_period(start, end, self.last_week_button)

    def _apply_period(self, start, end, button):
        self._applying_preset = True
        self.from_edit.setDateTime(QDateTime(start))
        self.to_edit.setDateTime(QDateTime(end))
        self._applying_preset = False
        button.setChecked(True)

    def _manual_period_change(self, *_):
        if getattr(self, "_applying_preset", False):
            return
        self.preset_group.setExclusive(False)
        for button in self.preset_group.buttons():
            button.setChecked(False)
        self.preset_group.setExclusive(True)

    def _search(self):
        if self.search_cancel is not None:
            self.search_cancel.set()
            self.search_button.setEnabled(False)
            self._log("Cancelling search...")
            return
        try:
            settings = self._connection_settings()
            channels = self._selected_cameras()
            if not channels:
                raise ValueError("Select at least one camera.")
            begin, end = qt_datetime(self.from_edit.dateTime()), qt_datetime(self.to_edit.dateTime())
            if end <= begin:
                raise ValueError("The end time must be later than the start time.")
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot search", str(exc)); return
        self._save_settings()
        self.search_cancel = OperationCancellation()
        worker = SearchWorker(settings, channels, begin, end, self.search_cancel)
        worker.signals.log.connect(self._log)
        worker.signals.finished.connect(self._search_finished)
        worker.signals.failed.connect(self._search_failed)
        self.search_button.setText("Cancel search")
        self.results_model.set_items([])
        self.search_pool.start(worker)

    def _search_finished(self, items):
        self.search_cancel = None
        if self._closing:
            return
        self.search_button.setText("Search"); self.search_button.setEnabled(True)
        self.results_model.set_items(items)
        self._update_results_totals()

    def _search_failed(self, exc):
        self.search_cancel = None
        if self._closing:
            return
        self.search_button.setText("Search"); self.search_button.setEnabled(True)
        if isinstance(exc, (CancelledError, InterruptedError)):
            self._log("Search cancelled.")
        else:
            self._log(f"Search failed: {exc}")
            QMessageBox.critical(self, "Search failed", str(exc))

    def _page_size_changed(self, text):
        self.results_model.set_page_size(None if text == "All" else int(text))

    def _update_page_controls(self):
        count = self.results_model.page_count()
        self.page_label.setText(f"Page {self.results_model.page + 1} / {count}")
        self.prev_button.setEnabled(self.results_model.page > 0)
        self.next_button.setEnabled(self.results_model.page + 1 < count)

    def _update_results_totals(self):
        selected = self.results_model.selected_items()
        total_size = sum(item.recording.size_bytes for item in selected)
        total = self.results_model.total_count
        self.selection_label.setText(f"{len(selected)} selected")
        self.selection_size_label.setText(format_size(total_size) if selected else "")
        self.results_totals.setText(f"Found {total} recording(s)")
        self.tabs.setTabText(0, f"Search results  {total}" if total else "Search results")
        self.add_queue_button.setEnabled(bool(selected))
        self.download_now_button.setEnabled(bool(selected))

    def _browse_output(self):
        directory = QFileDialog.getExistingDirectory(
            self, "Choose output directory", self.output_edit.text()
        )
        if directory:
            self.output_edit.setText(directory); self._output_changed()

    def _output_changed(self):
        self.results_model.refresh_local_status(Path(self.output_edit.text()).expanduser())
        self._update_disk_space(); self._save_settings()

    def _update_disk_space(self):
        c = nvr_theme.colors()
        try:
            free, total, percent = disk_free_info(Path(self.output_edit.text()).expanduser())
            minimum = self.min_free_spin.value()
            warning = min(100.0, max(minimum + 5.0, minimum * 1.5))
            if percent < minimum:
                color = c["red"]; label = "below minimum"
            elif percent < warning:
                color = c["amber"]; label = "low"
            else:
                color = c["green"]; label = "healthy"
            self.disk_bar.setValue(int((100.0 - percent) * 10))
            self.disk_label.setText(
                f'<span style="color:{color}; font-weight:600">{format_size(free)} free</span>'
                f" of {format_size(total)} \u00b7 {percent:.1f}% \u00b7 {label}"
            )
            self.disk_label.setToolTip(f"Queue pauses below {minimum:.1f}% free space")
        except Exception as exc:
            self.disk_bar.setValue(0)
            self.disk_label.setText(
                f'<span style="color:{c["red"]}">Disk space unavailable: {html.escape(str(exc))}</span>'
            )

    def _update_password_label(self):
        c = nvr_theme.colors()
        available = bool(os.environ.get("XMEYE_PASSWORD"))
        self.password_label.setText("\u25cf Password set" if available else "\u25cf XMEYE_PASSWORD not set")
        self.password_label.setStyleSheet(
            f"color: {c['green'] if available else c['red']}; font-size: 9pt;"
        )

    def _update_host_status(self, *_):
        self.host_status_label.setText(f"{self.host_edit.text().strip()}:{self.port_spin.value()}")

    def _set_theme(self, name):
        nvr_theme.apply(QApplication.instance(), name)
        self.settings.setValue("theme", name)
        self._update_disk_space()
        self._update_password_label()
        self._update_queue_status()
        for widget in (self.results_table.viewport(), self.queue_table.viewport(),
                       self.convert_check, self.delete_raw_check):
            widget.update()

    def _add_selected(self, start_now):
        selected = self.results_model.selected_items()
        if not selected:
            QMessageBox.information(self, "Nothing selected", "Select recordings first."); return
        try:
            settings = self._connection_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot add to queue", str(exc)); return
        output_dir = Path(self.output_edit.text()).expanduser()
        convert = self.convert_check.isChecked()
        delete_raw = self.delete_raw_check.isChecked()
        min_free_percent = self.min_free_spin.value()
        existing = {entry.key for entry in self.queue_entries}
        added = 0
        for item in selected:
            entry = QueueEntry(
                item, settings, output_dir, convert, delete_raw, min_free_percent
            )
            if entry.key not in existing:
                self.queue_entries.append(entry); existing.add(entry.key); added += 1
        self._refresh_queue_table()
        self._log(f"Added {added} recording(s) to the queue; duplicates skipped.")
        if start_now:
            self._start_queue()

    def _start_queue(self):
        self._save_settings()
        for entry in self.queue_entries:
            if entry.status in ("Stopped", "Cancelled"):
                entry.status = "Queued"
                entry.progress = 0
                entry.speed = 0
                entry.cancel_event.clear()
                self._update_queue_row(entry)
        self.queue_running = True
        self._log("Queue started.")
        self._pump_queue()

    def _pump_queue(self):
        if self._closing:
            self._update_overall_progress()
            return
        if self.queue_running:
            limit = self.downloads_spin.value()
            for entry in self.queue_entries:
                if len(self.active_downloads) >= limit:
                    break
                if entry.status != "Queued":
                    continue
                entry.cancel_event.clear()
                entry.cancel_status = "Cancelled"
                entry.retry_after_stop = False
                entry.status = "Downloading"; entry.progress = 0
                self.active_downloads.add(entry.key)
                self._update_queue_row(entry)
                worker = DownloadWorker(entry)
                worker.signals.log.connect(self._log)
                worker.signals.progress.connect(self._download_progress)
                worker.signals.finished.connect(self._download_finished)
                worker.signals.failed.connect(self._work_failed)
                self.download_pool.start(worker)
        # Pausing only stops new downloads. Already downloaded work, including
        # files finishing after the pause click, must drain through conversion.
        self._pump_conversions()
        self._update_overall_progress()
        if self.queue_running and not self.active_downloads and not self.active_conversions and not self.waiting_conversion:
            if not any(entry.status == "Queued" for entry in self.queue_entries):
                self.queue_running = False

    def _download_progress(self, entry, done, total, speed):
        entry.progress = min(100, int(done * 100 / total)) if total else 0
        entry.speed = speed
        self._update_queue_row(entry)
        self._update_overall_progress()

    def _download_finished(self, entry, outcome):
        self.active_downloads.discard(entry.key)
        if entry.cancel_event.is_set():
            self._finish_cancelled_entry(entry)
            self._pump_queue()
            return
        entry.progress = 100; entry.speed = 0
        if outcome.conversion_needed:
            entry.status = "Converting (waiting)"
            self.waiting_conversion.append((entry, outcome))
        else:
            entry.force_download = False
            entry.status = "Done"
        self._update_queue_row(entry)
        self.results_model.refresh_local_status(Path(self.output_edit.text()).expanduser())
        self._pump_queue()

    def _pump_conversions(self):
        while self.waiting_conversion and len(self.active_conversions) < self.conversions_spin.value():
            entry, outcome = self.waiting_conversion.pop(0)
            if entry.cancel_event.is_set():
                entry.status = entry.cancel_status
                self._update_queue_row(entry)
                continue
            entry.status = "Converting"; self.active_conversions.add(entry.key)
            self._update_queue_row(entry)
            worker = ConversionWorker(entry, outcome)
            worker.signals.log.connect(self._log)
            worker.signals.finished.connect(self._conversion_finished)
            worker.signals.failed.connect(self._work_failed)
            self.conversion_pool.start(worker)

    def _conversion_finished(self, entry, _outcome):
        self.active_conversions.discard(entry.key)
        if entry.cancel_event.is_set():
            self._finish_cancelled_entry(entry)
            self._pump_queue()
            return
        entry.force_download = False
        entry.status = "Done"; entry.progress = 100
        self._update_queue_row(entry)
        self.results_model.refresh_local_status(Path(self.output_edit.text()).expanduser())
        self._pump_queue()

    def _work_failed(self, entry, exc):
        self.active_downloads.discard(entry.key); self.active_conversions.discard(entry.key)
        if isinstance(exc, (CancelledError, InterruptedError)):
            self._finish_cancelled_entry(entry)
        elif isinstance(exc, LowDiskSpaceError):
            entry.status = "Queued"; self.queue_running = False
            self._log(f"Disk-space warning: {exc}")
            if not self._closing:
                QMessageBox.warning(self, "Queue paused: low disk space", str(exc))
        else:
            entry.status = "Failed"; self._log(f"Failed: {exc}")
        entry.speed = 0
        self._update_queue_row(entry)
        self._pump_queue(); self._update_disk_space()

    def _finish_cancelled_entry(self, entry):
        entry.speed = 0
        if entry.retry_after_stop:
            entry.retry_after_stop = False
            entry.status = "Queued"
            entry.progress = 0
            entry.cancel_event.clear()
            self._log(f"Queued again: {entry.item.recording.filename}")
        else:
            entry.status = entry.cancel_status
            self._log(f"{entry.status}: {entry.item.recording.filename}")
        self._update_queue_row(entry)

    def _queue_values(self, entry):
        recording = entry.item.recording
        return (
            f"Camera {entry.item.channel + 1}",
            recording.begin.strftime("%Y-%m-%d %H:%M:%S"),
            recording.end.strftime("%Y-%m-%d %H:%M:%S"),
            format_duration(recording.duration.total_seconds()),
            format_size(recording.size_bytes),
            entry.status,
            f"{entry.progress}%",
            f"{format_size(entry.speed)}/s" if entry.speed else "—",
        )

    def _update_queue_row(self, entry):
        row = self.queue_rows.get(entry.key)
        if row is None or row >= self.queue_table.rowCount():
            return
        values = self._queue_values(entry)
        for column in (5, 6, 7):
            item = self.queue_table.item(row, column)
            if item is None:
                self.queue_table.setItem(row, column, QTableWidgetItem(values[column]))
            elif item.text() != values[column]:
                item.setText(values[column])

    def _update_overall_progress(self):
        value = 0
        if self.queue_entries:
            terminal_statuses = {"Done", "Failed", "Stopped", "Cancelled"}
            values = []
            for entry in self.queue_entries:
                if entry.status in terminal_statuses:
                    values.append(100)
                elif entry.status == "Converting (waiting)":
                    values.append(90)
                elif entry.status == "Converting":
                    values.append(95)
                elif entry.status in ("Downloading", "Cancelling", "Stopping"):
                    values.append(min(90, int(entry.progress * 0.9)))
                else:
                    values.append(0)
            value = int(sum(values) / len(values))
        self.overall_progress.setValue(value)
        self.overall_label.setText(f"{value}%")
        self.setWindowTitle(f"{value}% - XMEye NVR Tools")
        self._update_queue_status()

    def _update_queue_status(self):
        c = nvr_theme.colors()
        counts = {"Downloading": 0, "Converting": 0, "Queued": 0, "Failed": 0}
        for entry in self.queue_entries:
            status = entry.status
            if status.startswith("Converting"):
                status = "Converting"
            elif status in ("Cancelling", "Stopping"):
                status = "Downloading"
            if status in counts:
                counts[status] += 1
        dots = (("Downloading", c["accent"]), ("Converting", c["amber"]),
                ("Queued", c["faint"]), ("Failed", c["red"]))
        self.queue_stats_label.setText("&nbsp;&nbsp;&nbsp;".join(
            f'<span style="color:{color}">\u25cf</span> {counts[name]} {name.lower()}'
            for name, color in dots
        ))
        total = len(self.queue_entries)
        self.tabs.setTabText(1, f"Download queue  {total}" if total else "Download queue")
        speed = sum(entry.speed for entry in self.queue_entries if entry.key in self.active_downloads)
        remaining = sum(
            entry.item.recording.size_bytes * (100 - entry.progress) / 100
            for entry in self.queue_entries if entry.status in ("Queued", "Downloading")
        )
        self.speed_label.setText(f"{format_size(speed)}/s" if speed else "")
        self.eta_label.setText(
            f"~{format_duration(remaining / speed)} left" if speed and remaining else ""
        )
        running = self.queue_running
        state = "Queue running" if running else ("Queue paused" if total else "Queue empty")
        self.run_label.setText(
            f'<span style="color:{c["accent"] if running else c["faint"]}">\u25cf</span> {state}'
        )
        self.start_queue_button.setText("\u275a\u275a  Pause queue" if running else "\u25b6  Start queue")
        if bool(self.start_queue_button.property("primary")) == running:
            self.start_queue_button.setProperty("primary", not running)
            repolish(self.start_queue_button)

    def _toggle_queue(self):
        if self.queue_running:
            self._pause_queue()
        else:
            self._start_queue()
        self._update_queue_status()

    def _refresh_queue_table(self):
        selected_keys = {
            self.queue_entries[index.row()].key for index in self.queue_table.selectionModel().selectedRows()
            if index.row() < len(self.queue_entries)
        } if self.queue_table.selectionModel() else set()
        self.queue_table.setUpdatesEnabled(False)
        try:
            self.queue_table.clearContents()
            self.queue_table.setRowCount(len(self.queue_entries))
            self.queue_rows = {
                entry.key: row for row, entry in enumerate(self.queue_entries)
            }
            for row, entry in enumerate(self.queue_entries):
                for column, value in enumerate(self._queue_values(entry)):
                    self.queue_table.setItem(row, column, self._queue_item(column, value))
                if entry.key in selected_keys:
                    self.queue_table.selectRow(row)
        finally:
            self.queue_table.setUpdatesEnabled(True)
        self._update_overall_progress()

    @staticmethod
    def _queue_item(column, value):
        item = QTableWidgetItem(value)
        if column in (1, 2):
            item.setFont(mono_font())
        if column in (3, 4, 7):
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
        return item

    def _selected_queue_entries(self):
        rows = sorted({index.row() for index in self.queue_table.selectionModel().selectedRows()})
        return [self.queue_entries[row] for row in rows if row < len(self.queue_entries)]

    def _queue_context_menu(self, position):
        row = self.queue_table.rowAt(position.y())
        if row < 0 or row >= len(self.queue_entries):
            return
        self.queue_table.selectRow(row)
        entry = self.queue_entries[row]
        menu = QMenu(self)
        download_again = menu.addAction("Download again")
        stop = menu.addAction("Stop")
        delete_local = menu.addAction("Delete local file")
        menu.addSeparator()
        remove = menu.addAction("Remove from queue")
        paths = local_paths(entry.item, entry.output_dir)
        active = (
            entry.key in self.active_downloads
            or entry.key in self.active_conversions
            or any(waiting.key == entry.key for waiting, _ in self.waiting_conversion)
        )
        delete_local.setEnabled(
            not active and any(
                path.is_file() for path in (paths.raw, paths.mp4, paths.legacy_mkv)
            )
        )
        stop.setEnabled(entry.status in {
            "Queued", "Downloading", "Converting (waiting)", "Converting",
            "Cancelling", "Stopping",
        })
        remove.setEnabled(
            not active
        )
        chosen = menu.exec(self.queue_table.viewport().mapToGlobal(position))
        if chosen == download_again:
            self._download_again(entry)
        elif chosen == stop:
            self._stop_entry(entry)
        elif chosen == delete_local:
            self._delete_local_files(entry)
        elif chosen == remove:
            self._remove_queue_entry(entry)

    def _download_again(self, entry):
        entry.force_download = True
        if entry.key in self.active_downloads or entry.key in self.active_conversions:
            entry.retry_after_stop = True
            entry.cancel_status = "Cancelled"
            entry.status = "Cancelling"
            entry.cancel_event.set()
        else:
            self.waiting_conversion = [
                pair for pair in self.waiting_conversion if pair[0].key != entry.key
            ]
            entry.cancel_event.clear()
            entry.status = "Queued"
            entry.progress = 0
            entry.speed = 0
        self._update_queue_row(entry)
        self.queue_running = True
        self._log(f"Download again requested: {entry.item.recording.filename}")
        self._pump_queue()

    def _stop_entry(self, entry):
        entry.cancel_status = "Stopped"
        entry.retry_after_stop = False
        if entry.key in self.active_downloads or entry.key in self.active_conversions:
            entry.status = "Stopping"
            entry.cancel_event.set()
        else:
            self.waiting_conversion = [
                pair for pair in self.waiting_conversion if pair[0].key != entry.key
            ]
            entry.cancel_event.set()
            entry.status = "Stopped"
            entry.speed = 0
        self._log(f"Stop requested: {entry.item.recording.filename}")
        self._update_queue_row(entry)
        self._update_overall_progress()

    def _delete_local_files(self, entry):
        paths = local_paths(entry.item, entry.output_dir)
        existing = [
            path.resolve()
            for path in (paths.raw, paths.mp4, paths.legacy_mkv)
            if path.is_file()
        ]
        if not existing:
            return
        exact_paths = "\n".join(str(path) for path in existing)
        answer = QMessageBox.question(
            self,
            "Delete local file",
            f"Delete the following local file(s)?\n\n{exact_paths}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for path in existing:
            path.unlink()
            self._log(f"Deleted local file: {path}")
        self.results_model.refresh_local_status(Path(self.output_edit.text()).expanduser())

    def _remove_queue_entry(self, entry):
        self.queue_entries = [item for item in self.queue_entries if item is not entry]
        self._refresh_queue_table()

    def _remove_selected_queue(self):
        selected = set(id(entry) for entry in self._selected_queue_entries())
        blocked = {
            "Downloading", "Converting", "Converting (waiting)",
            "Cancelling", "Stopping",
        }
        self.queue_entries = [
            entry for entry in self.queue_entries
            if id(entry) not in selected or entry.status in blocked
        ]
        self._refresh_queue_table()

    def _clear_completed(self):
        self.queue_entries = [entry for entry in self.queue_entries if entry.status not in ("Done", "Cancelled")]
        self._refresh_queue_table()

    def _retry_failed(self):
        for entry in self.queue_entries:
            if entry.status in ("Failed", "Stopped", "Cancelled"):
                entry.status = "Queued"; entry.progress = 0; entry.cancel_event.clear()
                self._update_queue_row(entry)
        self._update_overall_progress()

    def _cancel_active(self):
        found = False
        for entry in self.queue_entries:
            if entry.key in self.active_downloads or entry.key in self.active_conversions:
                entry.cancel_status = "Cancelled"
                entry.retry_after_stop = False
                entry.status = "Cancelling"
                entry.cancel_event.set()
                found = True
        if found:
            self._log("Cancelling currently active operations; the queue remains running.")
        for entry in self.queue_entries:
            if entry.status == "Cancelling":
                self._update_queue_row(entry)
        self._update_overall_progress()

    def _pause_queue(self):
        self.queue_running = False
        self._log(
            "Queue paused: no new downloads will start; active downloads and "
            "all pending conversions will finish."
        )
        self._pump_conversions()
        self._update_overall_progress()

    def _apply_pool_limits(self):
        self.download_pool.setMaxThreadCount(self.downloads_spin.value())
        self.conversion_pool.setMaxThreadCount(self.conversions_spin.value())
        if self.queue_running:
            self._pump_queue()

    def _repair_file(self):
        source_text, _ = QFileDialog.getOpenFileName(
            self, "Choose raw XMEye recording", self.output_edit.text(), "XMEye recordings (*.xmeye);;All files (*)"
        )
        if not source_text:
            return
        source = Path(source_text)
        suggested = source.with_name(f"{source.stem}_repaired{source.suffix}")
        target_text, _ = QFileDialog.getSaveFileName(
            self, "Save repaired recording", str(suggested), "XMEye recordings (*.xmeye)"
        )
        if not target_text:
            return
        worker = RepairWorker(source, Path(target_text))
        worker.signals.finished.connect(self._repair_finished)
        worker.signals.failed.connect(self._repair_failed)
        self._log(f"Repairing: {source.name}")
        self.misc_pool.start(worker)

    def _repair_finished(self, path, summary):
        self._log(f"Repair complete: {path}. {summary}")
        if not self._closing:
            QMessageBox.information(self, "Repair complete", f"{summary}\n\nOutput: {path}")

    def _repair_failed(self, exc):
        self._log(f"Repair failed: {exc}")
        if not self._closing:
            QMessageBox.critical(self, "Repair failed", str(exc))

    def _log(self, message):
        c = nvr_theme.colors()
        stamp = datetime.now().strftime("%H:%M:%S")
        lower = message.lower()
        if "failed" in lower or "warning" in lower or "error" in lower:
            color = c["red"]
        elif "complete" in lower:
            color = c["green"]
        else:
            color = c["text2"]
        self.log_edit.append(
            f'<span style="color:{c["log_time"]}">{stamp}</span>&nbsp;&nbsp;'
            f'<span style="color:{color}">{html.escape(message)}</span>'
        )

    def closeEvent(self, event):
        self._save_settings()
        if self._close_ready:
            super().closeEvent(event)
            return
        pools = (
            self.search_pool,
            self.download_pool,
            self.conversion_pool,
            self.misc_pool,
        )
        active = (
            self.search_cancel is not None
            or bool(self.active_downloads)
            or bool(self.active_conversions)
            or any(pool.activeThreadCount() for pool in pools)
        )
        if not active:
            super().closeEvent(event)
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        self.queue_running = False
        for entry, _outcome in self.waiting_conversion:
            entry.cancel_status = "Stopped"
            entry.status = "Stopped"
            entry.cancel_event.set()
            self._update_queue_row(entry)
        self.waiting_conversion.clear()
        if self.search_cancel is not None:
            self.search_cancel.set()
        for entry in self.queue_entries:
            if entry.key in self.active_downloads or entry.key in self.active_conversions:
                entry.cancel_status = "Stopped"
                entry.status = "Stopping"
                entry.cancel_event.set()
                self._update_queue_row(entry)
        self.centralWidget().setEnabled(False)
        self.statusBar().showMessage("Stopping active operations before exit...")
        self._log("Stopping active operations before exit...")
        QTimer.singleShot(100, self._finish_close_when_idle)

    def _finish_close_when_idle(self):
        pools = (
            self.search_pool,
            self.download_pool,
            self.conversion_pool,
            self.misc_pool,
        )
        if (
            self.active_downloads
            or self.active_conversions
            or any(pool.activeThreadCount() for pool in pools)
        ):
            QTimer.singleShot(100, self._finish_close_when_idle)
            return
        self._close_ready = True
        self.close()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("XMEye NVR Tools")
    app.setStyle("Fusion")
    nvr_theme.apply(app, str(QSettings("xmeye-nvr-tools", "nvr-gui").value("theme", "dark")))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
