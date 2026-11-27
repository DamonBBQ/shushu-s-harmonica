from __future__ import annotations

from PySide6.QtCore import QPoint, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication, QHideEvent, QMouseEvent, QResizeEvent, QScreen
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSizePolicy,
    QSlider, QVBoxLayout, QWidget,
)

from .theme import MINI_WINDOW_STYLE, state_color

MIN_OPACITY = 35
MAX_OPACITY = 100
DEFAULT_OPACITY = 85


class _ElidedLabel(QLabel):
    def __init__(self):
        super().__init__()
        self._full_text = ''
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setAccessibleName(text)
        self._refresh_text()

    def _refresh_text(self) -> None:
        width = max(0, self.contentsRect().width())
        self.setText(self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, width))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_text()


class MiniWindow(QWidget):
    score_selected = Signal(int)
    play_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()
    restore_requested = Signal()
    opacity_changed = Signal(int)

    def __init__(self):
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
        super().__init__(None, flags)
        self.setObjectName('miniWindow')
        self.setWindowTitle('口琴迷你播放器')
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setFixedWidth(356)
        self._drag_offset: QPoint | None = None
        self._pause_action = False
        self._library_titles: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = _ElidedLabel()
        self._title.setObjectName('miniTitle')
        header.addWidget(self._title, 1)
        restore = self._button('主窗口', 'miniRestore')
        restore.setToolTip('返回主窗口')
        restore.clicked.connect(self.restore_requested)
        header.addWidget(restore)
        layout.addLayout(header)

        details = QHBoxLayout()
        details.setSpacing(9)
        self._status = QLabel()
        self._status.setObjectName('miniStatus')
        self._status.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._progress = _ElidedLabel()
        self._progress.setObjectName('miniProgress')
        self._progress.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        details.addWidget(self._status)
        details.addWidget(self._progress, 1)
        layout.addLayout(details)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self._choose = self._button('选曲', 'miniChoose')
        self._choose.setCheckable(True)
        self._choose.setToolTip('展开曲库')
        self._choose.clicked.connect(self._set_picker_visible)
        self._action = self._button('播放', 'miniAction')
        self._stop = self._button('停止', 'miniStop')
        self._action.clicked.connect(self._request_action)
        self._stop.clicked.connect(self.stop_requested)
        controls.addWidget(self._choose, 1)
        controls.addWidget(self._action, 2)
        controls.addWidget(self._stop, 1)
        layout.addLayout(controls)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        opacity_label = QLabel('透明度')
        opacity_label.setObjectName('miniOpacityLabel')
        opacity_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._opacity_value = QLabel()
        self._opacity_value.setObjectName('miniOpacityValue')
        self._opacity_value.setMinimumWidth(self._opacity_value.fontMetrics().horizontalAdvance('65%'))
        self._opacity_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._opacity_value.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._opacity_slider = QSlider(Qt.Horizontal)
        self._opacity_slider.setObjectName('miniOpacity')
        self._opacity_slider.setAccessibleName('悬浮窗透明度')
        self._opacity_slider.setToolTip('向右拖动更透明')
        self._opacity_slider.setFocusPolicy(Qt.NoFocus)
        self._opacity_slider.setLayoutDirection(Qt.LeftToRight)
        self._opacity_slider.setRange(0, MAX_OPACITY - MIN_OPACITY)
        self._opacity_slider.valueChanged.connect(self._on_transparency_changed)
        footer.addWidget(opacity_label)
        footer.addWidget(self._opacity_slider, 1)
        footer.addWidget(self._opacity_value)
        layout.addLayout(footer)

        self._library = QListWidget()
        self._library.setObjectName('miniScoreList')
        self._library.setAccessibleName('曲库')
        self._library.setFocusPolicy(Qt.NoFocus)
        self._library.verticalScrollBar().setFocusPolicy(Qt.NoFocus)
        self._library.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._library.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._library.setTextElideMode(Qt.ElideRight)
        self._library.itemClicked.connect(self._on_score_clicked)
        self._library.hide()
        layout.addWidget(self._library)

        self.setStyleSheet(MINI_WINDOW_STYLE)
        self.set_snapshot('', '空闲', '就绪', can_select=False,
                          can_play=False, can_pause=False, can_stop=False, state='idle')
        self.set_opacity(DEFAULT_OPACITY)

    @staticmethod
    def _button(label: str, name: str) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName(name)
        button.setFocusPolicy(Qt.NoFocus)
        return button

    def set_snapshot(self, title: str, status: str, progress: str, *, can_select: bool,
                     can_play: bool, can_pause: bool, can_stop: bool,
                     state: str) -> None:
        self._title.set_full_text(title or '未选曲谱')
        self._status.setText(status)
        self._status.setStyleSheet(f'color: {state_color(state)};')
        self._progress.set_full_text(progress)
        self._choose.setEnabled(can_select)
        if not can_select and not self._library.isHidden():
            self._set_picker_visible(False)
        self._stop.setEnabled(can_stop)
        self._pause_action = can_pause
        self._action.setEnabled(can_play or can_pause)
        action_text = '播放'
        if can_pause:
            action_text = '继续' if state == 'paused' else '暂停'
        self._action.setText(action_text)

    def set_library(self, titles: list[str], selected_row: int) -> None:
        if titles != self._library_titles:
            self._library_titles = titles.copy()
            self._library.clear()
            self._library.addItems(titles)
            if titles:
                self._library.ensurePolished()
                row_height = self._library.sizeHintForRow(0)
                self._library.setFixedHeight(min(168, row_height * min(5, len(titles)) + 14))
        self._library.setCurrentRow(selected_row)

    def _set_picker_visible(self, visible: bool) -> None:
        self._choose.setChecked(visible)
        self._choose.setText('收起曲库' if visible else '选曲')
        self._choose.setToolTip('收起曲库' if visible else '展开曲库')
        self._library.setVisible(visible)
        self.adjustSize()
        if visible:
            if self._library.currentItem() is not None:
                self._library.scrollToItem(self._library.currentItem())
            screen = self.screen()
            if screen is not None:
                self._move_onto_screen(self.pos(), screen)

    def _on_score_clicked(self, item: QListWidgetItem) -> None:
        row = self._library.row(item)
        self._set_picker_visible(False)
        self.score_selected.emit(row)

    def set_opacity(self, percent: int) -> None:
        value = min(MAX_OPACITY, max(MIN_OPACITY, int(percent)))
        transparency = MAX_OPACITY - value
        with QSignalBlocker(self._opacity_slider):
            self._opacity_slider.setValue(transparency)
        self._opacity_value.setText(f'{transparency}%')
        self.setWindowOpacity(value / 100)

    def _on_transparency_changed(self, percent: int) -> None:
        opacity = MAX_OPACITY - percent
        self.set_opacity(opacity)
        self.opacity_changed.emit(opacity)

    def _request_action(self) -> None:
        if self._pause_action:
            self.pause_requested.emit()
        else:
            self.play_requested.emit()

    def place(self, saved: QPoint | None, fallback: QPoint) -> None:
        self.adjustSize()
        self._drag_offset = None
        if saved is not None:
            screen = QGuiApplication.screenAt(saved)
            if screen is not None:
                self._move_onto_screen(saved, screen)
                return

        screen = QGuiApplication.screenAt(fallback) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            position = QPoint(area.x() + area.width() - self.width() - 16, area.top() + 16)
            self._move_onto_screen(position, screen)

    def _move_onto_screen(self, position: QPoint, screen: QScreen) -> None:
        area = screen.availableGeometry()
        max_x = max(area.left(), area.x() + area.width() - self.width())
        max_y = max(area.top(), area.y() + area.height() - self.height())
        x = min(max_x, max(area.left(), position.x()))
        y = min(max_y, max(area.top(), position.y()))
        self.move(x, y)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            anchor = event.globalPosition().toPoint()
            screen = QGuiApplication.screenAt(anchor) or self.screen()
            if screen is not None:
                self._move_onto_screen(anchor - self._drag_offset, screen)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        event.accept()
        self.restore_requested.emit()

    def hideEvent(self, event: QHideEvent) -> None:
        if not self._library.isHidden():
            self._set_picker_visible(False)
        super().hideEvent(event)
