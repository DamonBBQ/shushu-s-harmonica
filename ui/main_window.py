from __future__ import annotations

import json
import queue
import shutil
import sys
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QSettings, QSignalBlocker, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.app_updater import ApplicationRelease, ApplicationUpdater, PreparedUpdate
from core.engine import PlaybackOptions, Player
from core.hotkeys import (
    HOTKEY_EMERGENCY,
    HOTKEY_LABELS,
    HOTKEY_MINI_WINDOW,
    HOTKEY_PAUSE,
    HOTKEY_START_STOP,
    GlobalHotkeys,
)
from core.input_backend import (
    DryRunBackend,
    create_backend,
    foreground_process_name,
    is_process_elevated,
)
from core.mapping import build_play_notes, describe_action
from core.midi_preprocess import (
    MIDI_PREPROCESS_VERSION,
    MidiPreprocessOptions,
    preprocess_midi,
    sha256_file,
)
from core.score import load_score
from core.updater import ScoreUpdater, UpdateError
from core.version import __version__
from ui.mini_window import DEFAULT_OPACITY, MAX_OPACITY, MIN_OPACITY, MiniWindow
from ui.theme import FOREGROUND_MATCH, FOREGROUND_MUTED, MAIN_WINDOW_STYLE, state_color

if getattr(sys, 'frozen', False):
    SCORES_DIR = Path(sys.executable).resolve().parent / 'scores'
else:
    SCORES_DIR = Path(__file__).resolve().parent.parent / 'scores'

_ACTIVE_STATES = ('countdown', 'playing', 'paused', 'waiting_game')
_SCORE_SUFFIXES = ('.mid', '.midi', '.txt')
_SONG_PREFERENCES_KEY = 'playback/songPreferences'
_DEFAULT_SPEED_PERCENT = 100
_DEFAULT_TRANSPOSE = 0

_STATE_TEXT = {
    'idle': '空闲',
    'countdown': '准备中…',
    'playing': '演奏中',
    'paused': '已暂停（Shift+F10 继续）',
    'waiting_game': '等待游戏窗口…',
    'finished': '已完成',
}


class _Bridge(QObject):
    hotkey = Signal(int)
    hotkey_status = Signal()
    state = Signal(str)
    log = Signal(str)
    progress = Signal(int, int, str)
    score_read = Signal(object, object, object)
    midi_preprocessed = Signal(object, object, object)
    update_progress = Signal(int, int, str)
    app_update_checked = Signal(object)
    app_update_ready = Signal(object)
    update_checked = Signal(object)
    update_installed = Signal(object)
    update_failed = Signal(str)


class MainWindow(QMainWindow):
    closed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle('三角洲行动 · 口琴自动演奏器')
        self.resize(1000, 680)
        self.setMinimumSize(852, 630)

        self._bridge = _Bridge()
        self._bridge.hotkey.connect(self._on_hotkey)
        self._bridge.hotkey_status.connect(self._report_hotkey_status)
        self._bridge.state.connect(self._on_state)
        self._bridge.log.connect(self._append_log)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.score_read.connect(self._on_score_read)
        self._bridge.midi_preprocessed.connect(self._on_midi_preprocessed)
        self._bridge.update_progress.connect(self._on_update_progress)
        self._bridge.app_update_checked.connect(self._on_app_update_checked)
        self._bridge.app_update_ready.connect(self._on_app_update_ready)
        self._bridge.update_checked.connect(self._on_update_checked)
        self._bridge.update_installed.connect(self._on_update_installed)
        self._bridge.update_failed.connect(self._on_update_failed)

        self._player: Player | None = None
        self._player_lock = threading.Lock()
        self._real_playback = False
        self._state = 'idle'
        self._closing = False
        self._updating = False
        self._score_loading = False
        self._score_request: tuple | None = None
        self._score_cache = OrderedDict()
        self._source_hashes: dict[Path, tuple[tuple[int, int], str]] = {}
        self._midi_cache_dir = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)) / 'midi-preprocess'
        self._preprocess_queue: queue.Queue[Path | None] = queue.Queue()
        self._preprocess_worker: threading.Thread | None = None
        self._low_confidence_notified: set[str] = set()
        self._plan = None
        self._pending_mini_score: Path | None = None
        self._settings = QSettings('DamonBBQ', 'shushu-s-harmonica')
        self._managed_scores: set[str] = set()

        self._build_ui()
        self._build_mini_window()
        self.setStyleSheet(MAIN_WINDOW_STYLE)
        self.setAcceptDrops(True)
        self._refresh_scores()
        self._on_state('idle')

        self._hotkeys = GlobalHotkeys(self._dispatch_hotkey, self._hotkey_status_changed)
        self._hotkeys.start()

        if sys.platform != 'win32':
            self._append_log('当前系统不是 Windows：仅支持「仅测试」模式，全局热键不可用')
            self._append_log('在 Windows 上游玩三角洲的电脑上运行，才能发送真实按键')
        else:
            self._append_log('运行权限：' + ('管理员' if is_process_elevated() else '普通用户') + '（需与游戏权限一致）')
            self._append_log('游戏建议使用无边框窗口；若按键无效，请切换英文输入法并尝试以管理员运行')

        self._fg_timer = QTimer(self)
        self._fg_timer.setInterval(1000)
        self._fg_timer.timeout.connect(self._refresh_fg_label)
        self._fg_timer.start()
        self._refresh_fg_label()

    def _build_ui(self):
        central = QWidget()
        central.setObjectName('workspace')
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName('header')
        title_row = QHBoxLayout(header)
        title_row.setContentsMargins(16, 12, 16, 12)
        title_row.setSpacing(16)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        title = QLabel('口琴自动演奏器')
        title.setObjectName('title')
        subtitle = QLabel(f'三角洲行动 · MIDI 与简谱 TXT · v{__version__}')
        subtitle.setObjectName('subtitle')
        titles.addWidget(title)
        titles.addWidget(subtitle)
        title_row.addLayout(titles, 1)
        self.btn_mini = QPushButton('迷你浮窗  F11')
        self.btn_mini.setObjectName('miniLauncher')
        self.btn_mini.setToolTip('切换到置顶浮窗')
        self.btn_mini.clicked.connect(self._toggle_mini)
        title_row.addWidget(self.btn_mini)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        library = QGroupBox('曲库')
        library.setObjectName('libraryCard')
        left = QVBoxLayout(library)
        left.setSpacing(10)
        self.list_scores = QListWidget()
        self.list_scores.setObjectName('scoreList')
        self.list_scores.setAlternatingRowColors(True)
        self.list_scores.currentItemChanged.connect(self._on_score_selected)
        self.list_scores.itemDoubleClicked.connect(lambda *_: self._start_play())
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_add = QPushButton('添加曲谱…')
        self.btn_add.setObjectName('importButton')
        self.btn_add.clicked.connect(self._add_score)
        self.btn_refresh = QPushButton('刷新')
        self.btn_refresh.clicked.connect(lambda: self._refresh_scores())
        self.btn_update = QPushButton('检查更新')
        self.btn_update.setObjectName('updateButton')
        self.btn_update.setToolTip('优先更新完整程序与功能；没有新程序版本时更新内置曲谱，本地曲目保留')
        self.btn_update.clicked.connect(self._check_for_updates)
        row.addWidget(self.btn_add, 1)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_update)
        left.addLayout(row)
        self.lbl_update = QLabel('')
        self.lbl_update.setObjectName('hint')
        self.lbl_update.setWordWrap(True)
        left.addWidget(self.lbl_update)
        left.addWidget(self.list_scores, 1)
        details = QFrame()
        details.setObjectName('trackSummary')
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(10, 9, 10, 9)
        self.lbl_info = QLabel('选择一个曲谱')
        self.lbl_info.setObjectName('info')
        self.lbl_info.setWordWrap(True)
        details_layout.addWidget(self.lbl_info)
        self.lbl_midi_hint = QLabel('')
        self.lbl_midi_hint.setObjectName('hint')
        self.lbl_midi_hint.setWordWrap(True)
        details_layout.addWidget(self.lbl_midi_hint)
        self.btn_midi = QPushButton('MIDI 声部…')
        self.btn_midi.setObjectName('midiVoicesButton')
        self.btn_midi.setToolTip('查看自动识别的旋律声部、置信度，或手动指定轨道与通道')
        self.btn_midi.clicked.connect(self._open_midi_voices)
        details_layout.addWidget(self.btn_midi)
        left.addWidget(details)
        body.addWidget(library, 4)

        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 7)

        controls = QGroupBox('演奏控制')
        controls.setObjectName('playerCard')
        grid = QGridLayout(controls)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnMinimumWidth(0, 84)
        grid.setColumnMinimumWidth(1, 92)
        grid.setColumnStretch(2, 1)

        playback = QFrame()
        playback.setObjectName('playbackSummary')
        playback_layout = QVBoxLayout(playback)
        playback_layout.setContentsMargins(12, 9, 12, 10)
        playback_layout.setSpacing(7)
        playback_header = QHBoxLayout()
        playback_header.setSpacing(12)
        self.lbl_track = QLabel('未选曲谱')
        self.lbl_track.setObjectName('trackTitle')
        self.lbl_track.setTextFormat(Qt.PlainText)
        self.lbl_track.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        playback_header.addWidget(self.lbl_track, 1)
        self.lbl_progress = QLabel('就绪')
        self.lbl_progress.setObjectName('progress')
        playback_header.addWidget(self.lbl_progress)
        playback_layout.addLayout(playback_header)
        self.track_progress = QProgressBar()
        self.track_progress.setObjectName('trackProgress')
        self.track_progress.setAccessibleName('演奏进度')
        self.track_progress.setRange(0, 1)
        self.track_progress.setValue(0)
        self.track_progress.setTextVisible(False)
        self.track_progress.setFixedHeight(5)
        playback_layout.addWidget(self.track_progress)
        grid.addWidget(playback, 0, 0, 1, 3)

        self.btn_play = QPushButton('播放')
        self.btn_play.setObjectName('primary')
        self.btn_play.clicked.connect(self._start_play)
        self.btn_pause = QPushButton('暂停')
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_stop = QPushButton('停止')
        self.btn_stop.setObjectName('stopButton')
        self.btn_stop.clicked.connect(self._stop)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        for button in (self.btn_play, self.btn_pause, self.btn_stop):
            actions.addWidget(button, 1)
        grid.addLayout(actions, 1, 0, 1, 3)

        speed_label = QLabel('速度')
        grid.addWidget(speed_label, 2, 0)
        speed_row = QHBoxLayout()
        speed_row.setSpacing(12)
        self.slider_speed = QSlider(Qt.Horizontal)
        self.slider_speed.setRange(50, 200)
        self.slider_speed.setValue(_DEFAULT_SPEED_PERCENT)
        self.slider_speed.setAccessibleName(speed_label.text())
        self.slider_speed.setToolTip('速度按当前曲目自动保存，下次选择时恢复')
        self.lbl_speed = QLabel(f'{_DEFAULT_SPEED_PERCENT}%')
        self.lbl_speed.setMinimumWidth(self.lbl_speed.fontMetrics().horizontalAdvance('200%'))
        self.lbl_speed.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.slider_speed.valueChanged.connect(self._on_speed_changed)
        speed_row.addWidget(self.slider_speed)
        speed_row.addWidget(self.lbl_speed)
        grid.addLayout(speed_row, 2, 1, 1, 2)

        transpose_label = QLabel('移调')
        grid.addWidget(transpose_label, 3, 0)
        self.spin_transpose = QSpinBox()
        self.spin_transpose.setRange(-12, 12)
        self.spin_transpose.setValue(_DEFAULT_TRANSPOSE)
        self.spin_transpose.setSuffix(' 半音')
        self.spin_transpose.setToolTip('手动移调按当前曲目自动保存，下次选择时恢复')
        self.spin_transpose.valueChanged.connect(self._on_transpose_changed)
        transpose_label.setBuddy(self.spin_transpose)
        self.spin_transpose.setAccessibleName(transpose_label.text())
        grid.addWidget(self.spin_transpose, 3, 1)
        flags = QHBoxLayout()
        flags.setSpacing(12)
        self.chk_auto_fit = QCheckBox('自动移调适配音域')
        self.chk_auto_fit.setChecked(True)
        self.chk_auto_fit.toggled.connect(lambda *_: self._update_info())
        self.chk_loop = QCheckBox('循环')
        flags.addWidget(self.chk_auto_fit)
        flags.addWidget(self.chk_loop)
        flags.addStretch(1)
        grid.addLayout(flags, 3, 2)

        opacity_label = QLabel('浮窗不透明度')
        self.slider_mini_opacity = QSlider(Qt.Horizontal)
        self.slider_mini_opacity.setRange(MIN_OPACITY, MAX_OPACITY)
        self.slider_mini_opacity.setAccessibleName(opacity_label.text())
        opacity_label.setBuddy(self.slider_mini_opacity)
        self.lbl_mini_opacity = QLabel('')
        self.lbl_mini_opacity.setMinimumWidth(self.lbl_mini_opacity.fontMetrics().horizontalAdvance('100%'))
        self.lbl_mini_opacity.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        opacity_row = QHBoxLayout()
        opacity_row.setSpacing(12)
        opacity_row.addWidget(self.slider_mini_opacity, 1)
        opacity_row.addWidget(self.lbl_mini_opacity)
        grid.addWidget(opacity_label, 4, 0)
        grid.addLayout(opacity_row, 4, 1, 1, 2)
        hint = QLabel('Shift+F9 播放/停止  ·  Shift+F10 暂停/继续  ·  Shift+F8 急停')
        hint.setObjectName('hint')
        hint.setWordWrap(True)
        grid.addWidget(hint, 5, 0, 1, 3)
        right.addWidget(controls)

        safety = QGroupBox('前台与输入')
        safety.setObjectName('safetyCard')
        sgrid = QGridLayout(safety)
        sgrid.setHorizontalSpacing(10)
        sgrid.setVerticalSpacing(8)
        self.chk_foreground = QCheckBox('仅在游戏窗口位于前台时演奏')
        self.chk_foreground.setChecked(True)
        self.chk_foreground.setToolTip('窗口失焦时自动暂停，切回游戏后自动继续')
        sgrid.addWidget(self.chk_foreground, 0, 0, 1, 2)
        process_label = QLabel('游戏进程关键字')
        sgrid.addWidget(process_label, 1, 0)
        self.edit_process = QLineEdit('DeltaForce')
        self.edit_process.setToolTip('前台进程名包含此关键字即视为游戏窗口')
        process_label.setBuddy(self.edit_process)
        sgrid.addWidget(self.edit_process, 1, 1)
        self.chk_dry = QCheckBox('仅测试（不发送按键，只走时间轴）')
        self.chk_dry.setChecked(True)
        sgrid.addWidget(self.chk_dry, 2, 0, 1, 2)
        right.addWidget(safety)

        log_box = QGroupBox('运行日志')
        log_box.setObjectName('logCard')
        log_layout = QVBoxLayout(log_box)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setObjectName('logText')
        self.txt_log.setMinimumHeight(54)
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(800)
        log_layout.addWidget(self.txt_log)
        right.addWidget(log_box, 1)

        self.lbl_state = QLabel('空闲')
        self.lbl_state.setObjectName('state')
        self.statusBar().addWidget(self.lbl_state, 1)
        self.lbl_fg = QLabel('前台进程: -')
        self.statusBar().addPermanentWidget(self.lbl_fg)
        self.lbl_perm = QLabel('')
        self.statusBar().addPermanentWidget(self.lbl_perm)
        if sys.platform == 'win32':
            self.lbl_perm.setText('权限：' + ('管理员' if is_process_elevated() else '普通用户'))
        else:
            self.lbl_perm.setText('开发模式（仅测试）')

    def _build_mini_window(self):
        self._mini = MiniWindow()
        self._mini.score_selected.connect(self._select_mini_score)
        self._mini.play_requested.connect(self._start_play)
        self._mini.pause_requested.connect(self._toggle_pause)
        self._mini.stop_requested.connect(self._stop)
        self._mini.restore_requested.connect(self._restore_main_from_mini)
        self._mini.opacity_changed.connect(self._set_mini_opacity)
        self.slider_mini_opacity.valueChanged.connect(self._set_mini_opacity)

        try:
            opacity = int(self._settings.value('miniWindow/opacity', DEFAULT_OPACITY))
        except (TypeError, ValueError):
            opacity = DEFAULT_OPACITY
        self._set_mini_opacity(min(MAX_OPACITY, max(MIN_OPACITY, opacity)))

        self._mini_shortcut = QShortcut(QKeySequence(Qt.Key_F11), self)
        self._mini_shortcut.setAutoRepeat(False)
        self._mini_shortcut.activated.connect(self._on_local_mini_shortcut)

    def _set_mini_opacity(self, opacity: int):
        self._mini.set_opacity(opacity)
        with QSignalBlocker(self.slider_mini_opacity):
            self.slider_mini_opacity.setValue(opacity)
        self.lbl_mini_opacity.setText(f'{opacity}%')
        self._settings.setValue('miniWindow/opacity', opacity)

    def _on_local_mini_shortcut(self):
        if not self._hotkeys.is_registered(HOTKEY_MINI_WINDOW):
            self._toggle_mini()

    def _toggle_mini(self):
        if self._closing or self._updating:
            return
        if self._mini.isVisible():
            self._restore_main()
        else:
            self._update_mini()
            self._place_mini()
            self._mini.show()
            self.hide()

    def _place_mini(self):
        saved = self._settings.value('miniWindow/position')
        self._mini.place(saved if isinstance(saved, QPoint) else None, QCursor.pos())

    def _save_mini_position(self):
        if self._mini.isVisible():
            self._settings.setValue('miniWindow/position', self._mini.pos())

    def _restore_main_from_mini(self):
        activate = self._state not in _ACTIVE_STATES and not (self._player and self._player.active)
        self._restore_main(activate=activate)

    def _restore_main(self, *, activate: bool = False):
        if self._closing:
            return
        self._save_mini_position()
        self.setAttribute(Qt.WA_ShowWithoutActivating, not activate)
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.show()
        self._mini.hide()
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        if activate:
            self.raise_()
            self.activateWindow()

    def _refresh_scores(self, selected: Path | None = None):
        selected = selected or self._selected_path()
        try:
            self._managed_scores = ScoreUpdater(SCORES_DIR).managed_score_names()
        except UpdateError as exc:
            self._managed_scores = set()
            self._append_log(f'读取 Git 曲目清单失败，播放偏好按本地曲目处理：{exc}')
        with QSignalBlocker(self.list_scores):
            self.list_scores.clear()
            try:
                files = sorted(
                    (path for path in SCORES_DIR.iterdir()
                     if path.suffix.lower() in _SCORE_SUFFIXES and path.is_file()),
                    key=lambda path: path.name.lower()) if SCORES_DIR.is_dir() else []
            except OSError as exc:
                self._append_log(f'读取曲库失败：{exc}')
                files = []
            selected_row = 0
            for path in files:
                if path == selected:
                    selected_row = self.list_scores.count()
                item = QListWidgetItem(path.stem)
                item.setData(Qt.UserRole, str(path))
                item.setToolTip(str(path))
                self.list_scores.addItem(item)
            if files:
                self.list_scores.setCurrentRow(selected_row)
        self._sync_mini_library()
        self._restore_song_preferences()
        self._update_info()

    def _check_for_updates(self):
        if (self._closing or self._updating or self._state in _ACTIVE_STATES
                or (self._player and self._player.active)):
            return
        self._updating = True
        self.lbl_update.setText('正在检查程序与曲库更新…')
        self._append_log('正在检查在线更新…')
        self._update_controls()
        threading.Thread(target=self._run_update_check, daemon=True).start()

    def _run_update_check(self):
        try:
            release = None
            if getattr(sys, 'frozen', False):
                release = ApplicationUpdater().check_for_update()
            if release is not None:
                self._bridge.app_update_checked.emit(release)
            else:
                check = ScoreUpdater(SCORES_DIR).check_for_updates()
                self._bridge.update_checked.emit(check)
        except Exception as exc:
            self._bridge.update_failed.emit(str(exc))

    def _on_app_update_checked(self, release: ApplicationRelease):
        if self._closing:
            return
        answer = QMessageBox.question(
            self,
            '发现程序更新',
            f'发现新版本 v{release.version}，是否下载完整更新包？\n\n'
            '更新包含程序功能、运行依赖和内置曲谱。安装时会保留本地曲谱与个人设置，'
            '并在程序退出后自动替换、重新启动。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._updating = False
            self.lbl_update.setText(f'发现程序 v{release.version}，已取消')
            self._append_log(f'已取消程序更新 v{release.version}')
            self._update_controls()
            return
        self.lbl_update.setText(f'正在下载程序 v{release.version}…')
        self._append_log(f'开始下载完整程序更新包：{release.asset_name}')
        threading.Thread(target=self._run_app_update_install, args=(release,), daemon=True).start()

    def _run_app_update_install(self, release: ApplicationRelease):
        def report(done: int, total: int, name: str):
            self._bridge.update_progress.emit(done, total, name)

        try:
            prepared = ApplicationUpdater().prepare_update(
                release,
                Path(sys.executable).resolve().parent,
                SCORES_DIR,
                report,
            )
        except Exception as exc:
            self._bridge.update_failed.emit(str(exc))
        else:
            self._bridge.app_update_ready.emit(prepared)

    def _on_app_update_ready(self, prepared: PreparedUpdate):
        if self._closing:
            prepared.cleanup()
            return
        answer = QMessageBox.question(
            self,
            '安装程序更新',
            '完整更新包已下载并通过 SHA-256 校验。是否立即安装？\n\n'
            '程序将关闭并自动替换，随后重新启动。本地曲谱和个人设置会保留；'
            '更新失败时会自动恢复原程序目录。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            prepared.cleanup()
            self._updating = False
            self.lbl_update.setText('更新包已下载，已取消安装')
            self._append_log('已取消安装程序更新，临时文件已清理')
            self._update_controls()
            return
        try:
            prepared.launch()
        except Exception as exc:
            prepared.cleanup()
            self._on_update_failed(f'启动程序更新失败：{exc}')
            return
        self._settings.sync()
        self._updating = False
        self._closing = True
        self.lbl_update.setText('更新包已就绪，正在退出并安装…')
        self._append_log('正在退出程序，Windows 更新助手将替换文件并重新启动')
        self._update_controls()
        self.close()

    def _on_update_checked(self, check):
        if self._closing:
            return
        count = len(check.updates)
        conflict_count = len(check.local_conflicts)
        if conflict_count:
            self._append_log('保留本地同名曲谱：' + '、'.join(check.local_conflicts))
        if not count:
            self._updating = False
            suffix = f'；保留 {conflict_count} 个本地同名曲目' if conflict_count else ''
            self.lbl_update.setText(f'已是最新曲库（共检查 {check.total} 个文件）{suffix}')
            self._append_log('在线曲库检查完成：已是最新')
            self._update_controls()
            message = '当前曲库已是最新。'
            if conflict_count:
                message += f'\n另有 {conflict_count} 个本地同名曲谱不在 Git 清单中，已保留不覆盖。'
            QMessageBox.information(self, '检查更新', message)
            return

        conflict_hint = ''
        if conflict_count:
            conflict_hint = f'\n\n另有 {conflict_count} 个本地同名曲谱不在 Git 清单中，将保留不覆盖。'
        answer = QMessageBox.question(
            self,
            '发现曲库更新',
            f'发现 {count} 个新增或更新的曲谱，是否下载并安装？\n\n'
            'Git 清单内的同名谱会被更新，原文件会备份；本地曲目不会被覆盖。'
            f'{conflict_hint}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._updating = False
            self.lbl_update.setText(f'发现 {count} 个更新，已取消')
            self._append_log(f'已取消曲库更新（发现 {count} 个文件）')
            self._update_controls()
            return

        self.lbl_update.setText(f'准备下载 {count} 个更新…')
        self._append_log(f'开始更新曲库：{count} 个文件')
        threading.Thread(target=self._run_update_install, args=(check.updates,), daemon=True).start()

    def _run_update_install(self, updates):
        def report(done: int, total: int, name: str):
            self._bridge.update_progress.emit(done, total, name)

        try:
            result = ScoreUpdater(SCORES_DIR).install_updates(updates, report)
        except Exception as exc:
            self._bridge.update_failed.emit(str(exc))
        else:
            self._bridge.update_installed.emit(result)

    def _on_update_progress(self, done: int, total: int, name: str):
        if name.endswith('.zip'):
            self.lbl_update.setText(
                f'正在下载并校验程序更新包：{done / 1048576:.1f}/{total / 1048576:.1f} MB')
        else:
            self.lbl_update.setText(f'正在下载并校验 {done}/{total}：{name}')

    def _on_update_installed(self, result):
        if self._closing:
            return
        self._updating = False
        self._score_cache.clear()
        self._source_hashes.clear()
        count = len(result.installed)
        self.lbl_update.setText(f'曲库已更新（{count} 个文件）')
        self._append_log(f'曲库更新完成：{count} 个文件')
        if result.backup_dir is not None:
            self._append_log(f'被替换的原文件备份在：{result.backup_dir}')
        self._refresh_scores()
        self._update_controls()
        QMessageBox.information(self, '更新完成', f'曲库已更新，共处理 {count} 个文件。')

    def _on_update_failed(self, message: str):
        if self._closing:
            return
        self._updating = False
        self.lbl_update.setText('更新失败')
        self._append_log(f'更新失败：{message}')
        self._update_controls()
        QMessageBox.warning(self, '更新失败', message)

    def _selected_path(self) -> Path | None:
        item = self.list_scores.currentItem()
        if item is None:
            return None
        return Path(item.data(Qt.UserRole))

    def _sync_mini_library(self):
        paths = [Path(self.list_scores.item(row).data(Qt.UserRole))
                 for row in range(self.list_scores.count())]
        stems = Counter(path.stem.casefold() for path in paths)
        titles = [path.name if stems[path.stem.casefold()] > 1 else path.stem for path in paths]
        self._mini.set_library(titles, self.list_scores.currentRow())

    def _select_mini_score(self, row: int):
        if self._closing or not 0 <= row < self.list_scores.count():
            return
        target = Path(self.list_scores.item(row).data(Qt.UserRole))
        if self._pending_mini_score is not None:
            self._pending_mini_score = target
            return
        if row == self.list_scores.currentRow():
            return
        if self._state in _ACTIVE_STATES or (self._player and self._player.active):
            self._pending_mini_score = target
            self._stop()
            self._update_controls()
            return
        self.list_scores.setCurrentRow(row)

    def _apply_pending_mini_score(self):
        target = self._pending_mini_score
        self._pending_mini_score = None
        if target is None or self._closing:
            return
        for row in range(self.list_scores.count()):
            if Path(self.list_scores.item(row).data(Qt.UserRole)) == target:
                self.list_scores.setCurrentRow(row)
                return

    def _on_score_selected(self):
        self.lbl_progress.setText('就绪')
        self.track_progress.setValue(0)
        self._sync_mini_library()
        self._restore_song_preferences()
        self._update_info()

    def _update_info(self):
        self._plan = None
        try:
            self._prepare_score()
        finally:
            self._update_controls()

    def _prepare_score(self):
        if self._closing:
            return
        path = self._selected_path()
        if path is None:
            self.lbl_info.setText('曲库为空，可添加或拖入 MIDI 和简谱 TXT')
            return
        try:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            cached_hash = self._source_hashes.get(path)
            if cached_hash is not None and cached_hash[0] == signature:
                source_hash = cached_hash[1]
            else:
                source_hash = sha256_file(path)
                self._source_hashes[path] = (signature, source_hash)
        except OSError as exc:
            self.lbl_info.setText(f'无法读取曲谱：{exc}')
            return
        if path.suffix.lower() in ('.mid', '.midi'):
            options = self._midi_options_for(path, source_hash)
            algorithm = MIDI_PREPROCESS_VERSION
            fingerprint = options.fingerprint
        else:
            options = None
            algorithm = 'txt'
            fingerprint = ''
        key = (path, source_hash, algorithm, fingerprint)
        if key not in self._score_cache:
            self.lbl_info.setText('正在解析曲谱…')
            self.lbl_midi_hint.setText('')
            if not self._score_loading or self._score_request != key:
                self._score_loading = True
                self._score_request = key
                threading.Thread(target=self._read_score, args=(key, options), daemon=True).start()
            return
        self._score_cache.move_to_end(key)
        score, error = self._score_cache[key]
        if error is not None:
            self.lbl_info.setText(f'无法解析：{error}')
            self.lbl_midi_hint.setText('')
            return
        speed = self.slider_speed.value() / 100.0
        try:
            notes, applied, dropped = build_play_notes(
                score.events, self.spin_transpose.value(), self.chk_auto_fit.isChecked(), speed=speed)
        except (ValueError, ArithmeticError) as exc:
            self.lbl_info.setText(f'无法解析：{exc}')
            self.lbl_midi_hint.setText('')
            return
        self._plan = (score, notes, applied, dropped)
        parts = [f'可演奏 {len(notes)}/{len(score.events)} 音',
                 f'时长 {score.length / speed:.1f}s', f'移调 {applied:+d} 半音']
        if dropped:
            parts.append(f'过短跳过 {dropped}')
        analysis = score.midi_analysis
        if analysis is not None:
            summary = self._midi_summary(analysis)
            if summary:
                parts.append(summary)
            self.lbl_midi_hint.setText(self._midi_hint(analysis))
            self._notify_midi_confidence(analysis)
        else:
            self.lbl_midi_hint.setText('')
        self.lbl_info.setText(' · '.join(parts))

    def _read_score(self, key, options):
        try:
            score, error = load_score(
                key[0], midi_options=options, cache_dir=self._midi_cache_dir,
                source_sha256=key[1],
            ), None
        except Exception as exc:
            score, error = None, str(exc)
        if not self._closing:
            self._bridge.score_read.emit(key, score, error)

    def _on_score_read(self, key, score, error):
        if self._closing:
            return
        self._score_cache[key] = (score, error)
        self._score_cache.move_to_end(key)
        if len(self._score_cache) > 8:
            self._score_cache.popitem(last=False)
        if key == self._score_request:
            self._score_request = None
            self._score_loading = False
        if error is not None:
            self._source_hashes.pop(key[0], None)
        self._update_info()

    def _song_preference_id(self, path: Path) -> str:
        source = 'git' if path.name in self._managed_scores else 'local'
        return f'{source}:{path.name}'

    def _load_song_preferences(self) -> dict[str, dict[str, object]]:
        raw = self._settings.value(_SONG_PREFERENCES_KEY, '')
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict) or payload.get('version') != 1:
            return {}
        songs = payload.get('songs')
        if not isinstance(songs, dict):
            return {}
        preferences: dict[str, dict[str, object]] = {}
        for song_id, values in songs.items():
            if not isinstance(song_id, str) or not isinstance(values, dict):
                continue
            transpose = values.get('transpose', _DEFAULT_TRANSPOSE)
            speed = values.get('speed_percent', _DEFAULT_SPEED_PERCENT)
            if (type(transpose) is not int
                    or not self.spin_transpose.minimum() <= transpose <= self.spin_transpose.maximum()):
                transpose = _DEFAULT_TRANSPOSE
            if (type(speed) is not int
                    or not self.slider_speed.minimum() <= speed <= self.slider_speed.maximum()):
                speed = _DEFAULT_SPEED_PERCENT
            entry: dict[str, object] = {'transpose': transpose, 'speed_percent': speed}
            midi_values = values.get('midi_preprocess')
            if isinstance(midi_values, dict):
                source_hash = midi_values.get('source_sha256')
                if isinstance(source_hash, str) and len(source_hash) == 64:
                    try:
                        options = MidiPreprocessOptions.from_dict(midi_values)
                    except ValueError:
                        pass
                    else:
                        entry['midi_preprocess'] = {
                            **options.to_dict(),
                            'source_sha256': source_hash,
                            'track_name': str(midi_values.get('track_name', '')),
                        }
            preferences[song_id] = entry
        return preferences

    def _restore_song_preferences(self):
        path = self._selected_path()
        preference = self._load_song_preferences().get(self._song_preference_id(path), {}) if path else {}
        speed = preference.get('speed_percent', _DEFAULT_SPEED_PERCENT)
        transpose = preference.get('transpose', _DEFAULT_TRANSPOSE)
        with QSignalBlocker(self.slider_speed), QSignalBlocker(self.spin_transpose):
            self.slider_speed.setValue(speed)
            self.spin_transpose.setValue(transpose)
        self.lbl_speed.setText(f'{speed}%')

    def _save_song_preferences(self):
        path = self._selected_path()
        if path is None:
            return
        preferences = self._load_song_preferences()
        entry = preferences.setdefault(self._song_preference_id(path), {})
        entry.update({
            'transpose': self.spin_transpose.value(),
            'speed_percent': self.slider_speed.value(),
        })
        self._write_song_preferences(preferences)

    def _write_song_preferences(self, preferences: dict[str, dict[str, object]]):
        payload = {'version': 1, 'songs': preferences}
        self._settings.setValue(
            _SONG_PREFERENCES_KEY,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        )

    def _midi_options_for(self, path: Path, source_hash: str) -> MidiPreprocessOptions:
        preference = self._load_song_preferences().get(self._song_preference_id(path), {})
        midi = preference.get('midi_preprocess')
        if not isinstance(midi, dict) or midi.get('source_sha256') != source_hash:
            return MidiPreprocessOptions()
        return MidiPreprocessOptions.from_dict(midi)

    def _save_midi_preference(self, options: MidiPreprocessOptions, source_hash: str, track_name: str = ''):
        path = self._selected_path()
        if path is None:
            return
        preferences = self._load_song_preferences()
        entry = preferences.setdefault(self._song_preference_id(path), {
            'transpose': self.spin_transpose.value(),
            'speed_percent': self.slider_speed.value(),
        })
        entry['midi_preprocess'] = {
            **options.to_dict(),
            'source_sha256': source_hash,
            'track_name': track_name,
        }
        self._write_song_preferences(preferences)

    @staticmethod
    def _midi_summary(analysis) -> str:
        if analysis.selected is None:
            return '旧版合并声部'
        levels = {'high': '高', 'medium': '中', 'low': '低', 'manual': '手动'}
        level = levels.get(analysis.confidence_level, '中')
        selected = analysis.selected
        parts = [f'声部 第{selected.track + 1}轨/通道{selected.channel + 1}']
        if selected.track_name:
            parts.append(selected.track_name)
        parts.append(f'置信度{level}')
        if analysis.repaired_short_notes:
            parts.append(f'修复短音 {analysis.repaired_short_notes}')
        if analysis.irreparable_short_notes:
            parts.append(f'密集短音 {analysis.irreparable_short_notes}')
        return ' · '.join(parts)

    @staticmethod
    def _midi_hint(analysis) -> str:
        if analysis.confidence_level == 'legacy':
            return '旧版模式会合并所有非打击乐声部；如声音杂乱，请改回自动识别或手动选声部。'
        if analysis.confidence_level == 'low':
            hint = '自动声部识别置信度较低，建议打开「MIDI 声部」确认旋律。'
        elif analysis.confidence_level == 'medium':
            hint = '已自动选择旋律候选，如有疑问可打开「MIDI 声部」调整。'
        elif analysis.confidence_level == 'manual':
            hint = '手动声部按当前文件内容保存；文件变化后会自动重新识别。'
        else:
            hint = ''
        if analysis.irreparable_short_notes:
            hint += f' 有 {analysis.irreparable_short_notes} 个短音因下一音过密无法延长。'
        return hint.strip()

    def _notify_midi_confidence(self, analysis):
        if analysis.options.mode != 'auto' or analysis.confidence_level != 'low':
            return
        if analysis.source_sha256 in self._low_confidence_notified:
            return
        self._low_confidence_notified.add(analysis.source_sha256)
        self._append_log(f'《{analysis.name}》自动声部识别置信度较低，可点击「MIDI 声部」手动确认')

    def _on_speed_changed(self, value: int):
        self.lbl_speed.setText(f'{value}%')
        self._save_song_preferences()
        self._update_info()

    def _on_transpose_changed(self, _value: int):
        self._save_song_preferences()
        self._update_info()

    def _current_midi_analysis(self):
        if self._plan is None:
            return None
        return self._plan[0].midi_analysis

    def _open_midi_voices(self):
        analysis = self._current_midi_analysis()
        if analysis is None or self._closing or self._updating:
            return
        dialog = QDialog(self)
        dialog.setObjectName('midiVoicesDialog')
        dialog.setWindowTitle(f'MIDI 声部 · {analysis.name}')
        dialog.resize(840, 430)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        info = QLabel('')
        info.setObjectName('hint')
        info.setWordWrap(True)
        layout.addWidget(info)
        table = QTableWidget(len(analysis.candidates), 8)
        table.setObjectName('midiCandidateTable')
        table.setHorizontalHeaderLabels(('轨道', '通道', '名称', '乐器', '音符', '音域', '最大复调', '评分'))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, candidate in enumerate(analysis.candidates):
            values = (
                str(candidate.track + 1), str(candidate.channel + 1),
                candidate.track_name or '-', candidate.instrument, str(candidate.note_count),
                f'{candidate.pitch_low}–{candidate.pitch_high}', str(candidate.max_polyphony),
                f'{candidate.score:.1f}',
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (0, 1, 4, 6, 7):
                    item.setTextAlignment(Qt.AlignCenter)
                table.setItem(row, column, item)
            if analysis.selected is not None and (
                candidate.track == analysis.selected.track and candidate.channel == analysis.selected.channel
            ):
                table.selectRow(row)
        if analysis.selected is None and analysis.candidates:
            table.selectRow(0)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table, 1)
        levels = {'high': '高', 'medium': '中', 'low': '低', 'manual': '手动', 'legacy': '旧版'}
        level = levels.get(analysis.confidence_level, '中')
        if analysis.selected is None:
            current = '旧版合并所有非打击乐声部'
        else:
            current = f'第{analysis.selected.track + 1}轨 / 通道{analysis.selected.channel + 1}'
        info.setText(
            f'当前：{current} · 置信度{level} {analysis.confidence:.0%} · '
            f'候选分差 {analysis.score_margin:.1%}。轨道编号从 1 开始，通道 10 为打击乐并已排除。'
        )
        options_row = QHBoxLayout()
        options_row.addWidget(QLabel('短音处理'))
        strategy = QComboBox()
        strategy.setObjectName('midiShortNoteStrategy')
        strategy.addItem('自动修复可延长的短音', 'repair')
        strategy.addItem('保留原始短音', 'keep')
        strategy.setCurrentIndex(0 if analysis.options.short_note_strategy == 'repair' else 1)
        options_row.addWidget(strategy)
        options_row.addStretch(1)
        layout.addLayout(options_row)
        buttons = QHBoxLayout()
        auto_button = QPushButton('恢复自动识别')
        auto_button.setObjectName('midiAuto')
        use_button = QPushButton('使用所选声部')
        use_button.setObjectName('midiUseSelected')
        legacy_button = QPushButton('旧版合并')
        legacy_button.setObjectName('midiLegacy')
        close_button = QPushButton('关闭')
        close_button.setObjectName('midiClose')
        for button in (auto_button, use_button, legacy_button, close_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        def use_selected():
            row = table.currentRow()
            if not 0 <= row < len(analysis.candidates):
                return
            candidate = analysis.candidates[row]
            options = MidiPreprocessOptions(
                mode='manual', track=candidate.track, channel=candidate.channel,
                short_note_strategy=strategy.currentData(),
            )
            self._apply_midi_options(options, candidate.track_name, dialog)

        auto_button.clicked.connect(lambda: self._apply_midi_options(
            MidiPreprocessOptions(mode='auto', short_note_strategy=strategy.currentData()), '', dialog,
        ))
        use_button.clicked.connect(use_selected)
        legacy_button.clicked.connect(lambda: self._apply_midi_options(
            MidiPreprocessOptions(mode='legacy', short_note_strategy=strategy.currentData()), '', dialog,
        ))
        close_button.clicked.connect(dialog.reject)
        dialog.exec()

    def _apply_midi_options(self, options: MidiPreprocessOptions, track_name: str = '', dialog=None):
        analysis = self._current_midi_analysis()
        if analysis is None:
            if dialog is not None:
                dialog.reject()
            return
        path = self._selected_path()
        self._save_midi_preference(options, analysis.source_sha256, track_name)
        self._low_confidence_notified.discard(analysis.source_sha256)
        if path is not None:
            for key in list(self._score_cache):
                if key[0] == path:
                    del self._score_cache[key]
        mode_names = {'auto': '自动识别', 'manual': '手动声部', 'legacy': '旧版合并'}
        self._append_log(f'《{analysis.name}》MIDI 处理已切换为{mode_names[options.mode]}')
        if dialog is not None:
            dialog.accept()
        self._update_info()

    def _add_score(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, '选择曲谱', '', '曲谱文件 (*.mid *.midi *.txt)')
        self._import_scores([Path(path) for path in paths])

    def _import_scores(self, paths: list[Path]):
        if not paths or self._closing or self._updating or self._state in _ACTIVE_STATES:
            return
        try:
            SCORES_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._append_log(f'无法创建曲库目录：{exc}')
            return
        selected = None
        midi_paths: list[Path] = []
        for src in paths:
            if src.suffix.lower() not in _SCORE_SUFFIXES or not src.is_file():
                continue
            dest = SCORES_DIR / src.name
            created = False
            try:
                if src.resolve() == dest.resolve():
                    selected = dest
                else:
                    with src.open('rb') as source:
                        try:
                            target = dest.open('xb')
                        except FileExistsError:
                            self._append_log(f'曲库中已存在同名文件：{dest.name}，未覆盖')
                            selected = dest
                        else:
                            created = True
                            with target:
                                shutil.copyfileobj(source, target)
                    if created:
                        shutil.copystat(src, dest)
                        self._append_log(f'已添加曲谱：{dest.name}')
                        selected = dest
                if dest.suffix.lower() in ('.mid', '.midi'):
                    midi_paths.append(dest)
            except OSError as exc:
                if created:
                    try:
                        dest.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._append_log(f'添加曲谱失败：{exc}')
        if selected is not None:
            self._refresh_scores(selected)
        self._queue_midi_preprocess(midi_paths)

    def _queue_midi_preprocess(self, paths: list[Path]):
        if not paths or self._closing:
            return
        for path in paths:
            self._preprocess_queue.put(path)
        if self._preprocess_worker is None or not self._preprocess_worker.is_alive():
            self._preprocess_worker = threading.Thread(
                target=self._preprocess_imported_scores, daemon=True,
            )
            self._preprocess_worker.start()

    def _preprocess_imported_scores(self):
        while True:
            path = self._preprocess_queue.get()
            try:
                if path is None:
                    return
                if self._closing:
                    continue
                try:
                    analysis = preprocess_midi(path, cache_dir=self._midi_cache_dir)
                    error = None
                except Exception as exc:
                    analysis, error = None, str(exc)
                if not self._closing:
                    self._bridge.midi_preprocessed.emit(path, analysis, error)
            finally:
                self._preprocess_queue.task_done()

    def _on_midi_preprocessed(self, path, analysis, error):
        if self._closing:
            return
        for key in list(self._score_cache):
            if key[0] == path:
                del self._score_cache[key]
        if error is not None:
            self._append_log(f'MIDI 预处理失败：{path.name}：{error}')
        else:
            summary = self._midi_summary(analysis)
            self._append_log(f'MIDI 预处理完成：{path.name} · {summary}')
        if self._selected_path() == path:
            self._update_info()

    def dragEnterEvent(self, event):
        if not self._closing and not self._updating and self._state not in _ACTIVE_STATES:
            if any(url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in _SCORE_SUFFIXES
                   for url in event.mimeData().urls()):
                event.acceptProposedAction()

    def dropEvent(self, event):
        if not self._closing and not self._updating and self._state not in _ACTIVE_STATES:
            self._import_scores([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])
            event.acceptProposedAction()

    def _start_play(self):
        if (self._closing or self._updating or self._pending_mini_score is not None
                or self._state in _ACTIVE_STATES or (self._player and self._player.active)):
            return
        self._update_info()
        if self._plan is None:
            self._append_log(self.lbl_info.text())
            return
        score, notes, applied, dropped = self._plan
        if not notes:
            self._append_log('当前速度下没有可演奏的音符，请降低速度或检查曲谱')
            return

        dry = self.chk_dry.isChecked() or sys.platform != 'win32'
        require_fg = self.chk_foreground.isChecked() and not dry
        keyword = self.edit_process.text().strip()
        if require_fg and not keyword:
            self._append_log('启用前台保护时需要填写游戏进程关键字')
            return

        def on_progress(index: int, total: int, note) -> None:
            self._bridge.progress.emit(index, total, describe_action(note))

        opts = PlaybackOptions(
            speed=self.slider_speed.value() / 100.0,
            loop=self.chk_loop.isChecked(),
            countdown=3.0,
            require_foreground=require_fg,
            score_length=score.length,
        )
        with self._player_lock:
            if not dry and not self._hotkeys.emergency_ready:
                reason = '急停热键 Shift+F8 不可用' if self._hotkeys.registration_finished else '正在注册急停热键'
                self._append_log(f'{reason}，可先使用「仅测试」模式')
                return
            try:
                backend = DryRunBackend() if dry else create_backend(self._bridge.log.emit)
            except Exception as exc:
                self._append_log(f'初始化输入后端失败：{exc}')
                return
            self._player = Player(
                backend,
                self._bridge.state.emit,
                self._bridge.log.emit,
                foreground_ok=lambda: backend.foreground_matches(keyword),
                on_progress=on_progress,
            )
            self._real_playback = not dry
            if not self._player.play(notes, opts):
                self._real_playback = False
                self._append_log('启动失败：播放器仍在退出中，请稍后再试')
                return

        self.lbl_progress.setText('准备演奏…')
        self._on_state('countdown')
        self._append_log('仅测试模式：只走时间轴' if dry else '已启用 Windows 输入后端（SendInput）')
        summary = f'开始演奏《{score.name}》 · {len(notes)} 个音 · 移调 {applied:+d} 半音 · 速度 {self.slider_speed.value()}%'
        if dropped:
            summary += f' · 过短跳过 {dropped} 个'
        self._append_log(summary)

    def _toggle_pause(self):
        if self._player and self._state in ('playing', 'waiting_game', 'paused'):
            self._player.toggle_pause()

    def _stop(self):
        if self._player:
            self._player.request_stop()

    def _dispatch_hotkey(self, hotkey_id: int):
        if hotkey_id == HOTKEY_EMERGENCY:
            with self._player_lock:
                if self._player and self._player.active:
                    self._player.request_stop()
                    self._bridge.log.emit('急停')
        else:
            self._bridge.hotkey.emit(hotkey_id)

    def _hotkey_status_changed(self):
        with self._player_lock:
            if (self._real_playback and self._player and self._player.active
                    and not self._hotkeys.emergency_ready):
                self._player.request_stop()
                self._bridge.log.emit('急停热键已失效，正在停止演奏')
        self._bridge.hotkey_status.emit()

    def _on_hotkey(self, hotkey_id: int):
        if self._closing:
            return
        if hotkey_id == HOTKEY_START_STOP:
            if self._state in _ACTIVE_STATES:
                self._stop()
            else:
                self._start_play()
        elif hotkey_id == HOTKEY_PAUSE:
            self._toggle_pause()
        elif hotkey_id == HOTKEY_MINI_WINDOW:
            self._toggle_mini()

    def _update_controls(self):
        active = self._state in _ACTIVE_STATES or (self._player is not None and self._player.active)
        configurable = (not active and not self._closing and not self._updating
                        and self._pending_mini_score is None)
        self.btn_play.setEnabled(configurable and self._plan is not None and bool(self._plan[1]))
        self.btn_stop.setEnabled(active and not self._closing)
        self.btn_pause.setEnabled(not self._closing and self._state in ('playing', 'waiting_game', 'paused'))
        for widget in (self.list_scores, self.btn_add, self.btn_refresh, self.btn_update,
                       self.slider_speed, self.spin_transpose, self.chk_auto_fit, self.chk_loop,
                       self.chk_foreground, self.edit_process):
            widget.setEnabled(configurable)
        self.chk_dry.setEnabled(configurable and sys.platform == 'win32')
        self.btn_midi.setEnabled(configurable and self._current_midi_analysis() is not None)
        self.btn_mini.setEnabled(not self._closing and not self._updating)
        self.slider_mini_opacity.setEnabled(not self._closing)
        self._update_mini()

    def _update_mini(self):
        item = self.list_scores.currentItem()
        title = self._plan[0].name if self._plan is not None else (item.text() if item else '')
        self.lbl_track.setText(title or '未选曲谱')
        self.lbl_track.setToolTip(title)
        progress = self.lbl_progress.text() if self._plan is not None else self.lbl_info.text()
        if self._pending_mini_score is not None:
            progress = '正在切换曲目…'
        self._mini.set_snapshot(
            title, self.lbl_state.text(), progress,
            can_select=not self._closing and self.list_scores.count() > 0,
            can_play=self.btn_play.isEnabled(),
            can_pause=self.btn_pause.isEnabled(),
            can_stop=self.btn_stop.isEnabled(),
            state=self._state,
        )

    def _on_state(self, state: str):
        previous = self._state
        self._state = state
        self.lbl_state.setText(_STATE_TEXT.get(state, state))
        self.lbl_state.setStyleSheet(f'color: {state_color(state)};')
        self.btn_pause.setText('继续' if state == 'paused' else '暂停')
        if state == 'finished':
            self.lbl_progress.setText('演奏完成')
            self.track_progress.setValue(self.track_progress.maximum())
        elif state == 'countdown':
            self.track_progress.setValue(0)
        elif state == 'idle' and previous != 'finished':
            self.lbl_progress.setText('就绪')
            self.track_progress.setValue(0)
        if state == 'idle':
            self._apply_pending_mini_score()
        self._update_controls()

    def _on_progress(self, index: int, total: int, action: str):
        self.lbl_progress.setText(f'第 {index}/{total} 音 · 按 {action}')
        self.track_progress.setRange(0, total)
        self.track_progress.setValue(index)
        self._update_mini()

    def _refresh_fg_label(self):
        if sys.platform != 'win32':
            self.lbl_fg.setText('前台进程: (仅 Windows 可检测)')
            self.lbl_fg.setStyleSheet(f'color: {FOREGROUND_MUTED};')
            return
        name = foreground_process_name() or '-'
        keyword = self.edit_process.text().strip().lower()
        matched = bool(keyword) and keyword in name.lower()
        self.lbl_fg.setText(f'前台进程: {name}' + ('（关键字匹配）' if matched else ''))
        self.lbl_fg.setStyleSheet(f'color: {FOREGROUND_MATCH if matched else FOREGROUND_MUTED};')

    def _report_hotkey_status(self):
        if self._closing or not self._hotkeys.registration_finished:
            return
        missing = [HOTKEY_LABELS[h] for h in self._hotkeys.failed if h in HOTKEY_LABELS]
        if missing:
            self._append_log('热键注册失败（可能被其他程序占用）：' + '、'.join(missing))
            if HOTKEY_MINI_WINDOW in self._hotkeys.failed:
                self._append_log('可在本工具窗口内按 F11 或点击「迷你浮窗」打开小窗')
        else:
            self._append_log('全局热键就绪：Shift+F9 播放/停止，Shift+F10 暂停/继续，Shift+F8 急停，F11 浮窗')

    def _append_log(self, message: str):
        stamp = time.strftime('%H:%M:%S')
        self.txt_log.appendPlainText(f'[{stamp}] {message}')

    def closeEvent(self, event):
        if self._updating:
            self._append_log('更新尚未结束，请稍候再关闭程序')
            event.ignore()
            return
        if not self._closing:
            self._closing = True
            self._update_controls()
        if self._player and not self._player.stop(timeout=0):
            self.lbl_state.setText('正在停止演奏…')
            self._update_mini()
            event.ignore()
            QTimer.singleShot(50, self.close)
            return
        self._save_mini_position()
        self._mini.hide()
        self._preprocess_queue.put(None)
        self._settings.sync()
        self._hotkeys.stop()
        super().closeEvent(event)
        self.closed.emit()
