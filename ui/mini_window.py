from __future__ import annotations

from PySide6.QtCore import QPoint, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication, QMouseEvent, QResizeEvent, QScreen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget

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
    previous_requested = Signal()
    next_requested = Signal()
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
        self._previous = self._button('上一首', 'miniPrevious')
        self._action = self._button('播放', 'miniAction')
        self._stop = self._button('停止', 'miniStop')
        self._next = self._button('下一首', 'miniNext')
        self._previous.clicked.connect(self.previous_requested)
        self._action.clicked.connect(self._request_action)
        self._stop.clicked.connect(self.stop_requested)
        self._next.clicked.connect(self.next_requested)
        for button in (self._previous, self._action, self._stop, self._next):
            controls.addWidget(button, 1)
        layout.addLayout(controls)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        opacity_label = QLabel('不透明度')
        opacity_label.setObjectName('miniOpacityLabel')
        opacity_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._opacity_value = QLabel()
        self._opacity_value.setObjectName('miniOpacityValue')
        self._opacity_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._opacity_value.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._opacity_slider = QSlider(Qt.Horizontal)
        self._opacity_slider.setObjectName('miniOpacity')
        self._opacity_slider.setAccessibleName('悬浮窗不透明度')
        self._opacity_slider.setFocusPolicy(Qt.NoFocus)
        self._opacity_slider.setRange(MIN_OPACITY, MAX_OPACITY)
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        footer.addWidget(opacity_label)
        footer.addWidget(self._opacity_slider, 1)
        footer.addWidget(self._opacity_value)
        layout.addLayout(footer)

        self.setStyleSheet('''
            QWidget#miniWindow {
                background-color: #171d1b; border: 1px solid #3e5147;
            }
            QLabel { color: #dce8e1; font-size: 12px; }
            QLabel#miniTitle { color: #f3f8f5; font-size: 15px; font-weight: 600; }
            QLabel#miniStatus { color: #9ed9af; font-weight: 600; }
            QLabel#miniProgress, QLabel#miniOpacityLabel, QLabel#miniOpacityValue { color: #b9c9bf; }
            QPushButton {
                color: #e6efe9; background-color: #26322b; border: 1px solid #3b4d42;
                border-radius: 5px; padding: 4px 7px; font-size: 12px; min-height: 16px;
            }
            QPushButton:hover { background-color: #344438; border-color: #607769; }
            QPushButton:pressed { background-color: #1f2b24; }
            QPushButton:disabled { color: #7b9084; background-color: #1e2722; border-color: #303e35; }
            QPushButton#miniAction:enabled { color: #122218; background-color: #9ad7ad; border-color: #b2e4c1; font-weight: 600; }
            QPushButton#miniAction:hover { background-color: #b2e4c1; }
            QPushButton#miniAction:pressed { background-color: #7ab890; }
            QPushButton#miniRestore { padding: 2px 7px; }
            QSlider::groove:horizontal { height: 4px; border-radius: 2px; background-color: #3c5145; }
            QSlider::sub-page:horizontal { border-radius: 2px; background-color: #74ae85; }
            QSlider::handle:horizontal { width: 12px; margin: -4px 0; border-radius: 6px; background-color: #c7ecd4; }
        ''')
        self.set_snapshot('', '空闲', '就绪', can_previous=False, can_next=False,
                          can_play=False, can_pause=False, can_stop=False, paused=False)
        self.set_opacity(DEFAULT_OPACITY)

    @staticmethod
    def _button(label: str, name: str) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName(name)
        button.setFocusPolicy(Qt.NoFocus)
        return button

    def set_snapshot(self, title: str, status: str, progress: str, *, can_previous: bool,
                     can_next: bool, can_play: bool, can_pause: bool, can_stop: bool,
                     paused: bool) -> None:
        self._title.set_full_text(title or '未选曲谱')
        self._status.setText(status)
        self._progress.set_full_text(progress)
        self._previous.setEnabled(can_previous)
        self._next.setEnabled(can_next)
        self._stop.setEnabled(can_stop)
        self._pause_action = can_pause
        self._action.setEnabled(can_play or can_pause)
        action_text = '播放'
        if can_pause:
            action_text = '继续' if paused else '暂停'
        self._action.setText(action_text)

    def set_opacity(self, percent: int) -> None:
        value = min(MAX_OPACITY, max(MIN_OPACITY, int(percent)))
        with QSignalBlocker(self._opacity_slider):
            self._opacity_slider.setValue(value)
        self._opacity_value.setText(f'{value}%')
        self.setWindowOpacity(value / 100)

    def _on_opacity_changed(self, percent: int) -> None:
        self.set_opacity(percent)
        self.opacity_changed.emit(percent)

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
