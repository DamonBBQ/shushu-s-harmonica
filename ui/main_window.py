from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.engine import PlaybackOptions, Player
from core.hotkeys import (
    HOTKEY_EMERGENCY,
    HOTKEY_LABELS,
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
from core.score import load_score

if getattr(sys, 'frozen', False):
    SCORES_DIR = Path(sys.executable).resolve().parent / 'scores'
else:
    SCORES_DIR = Path(__file__).resolve().parent.parent / 'scores'

_ACTIVE_STATES = ('countdown', 'playing', 'paused', 'waiting_game')

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
    state = Signal(str)
    log = Signal(str)
    progress = Signal(int, int, str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('三角洲行动 · 口琴自动演奏器')
        self.resize(920, 620)
        self.setMinimumSize(860, 560)

        self._bridge = _Bridge()
        self._bridge.hotkey.connect(self._on_hotkey)
        self._bridge.state.connect(self._on_state)
        self._bridge.log.connect(self._append_log)
        self._bridge.progress.connect(self._on_progress)

        self._player: Player | None = None
        self._state = 'idle'

        self._build_ui()
        self._apply_theme()
        self._refresh_scores()
        self._on_state('idle')

        self._hotkeys = GlobalHotkeys(self._bridge.hotkey.emit)
        self._hotkeys.start()
        QTimer.singleShot(700, self._report_hotkey_status)

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
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 10)
        root.setSpacing(10)

        title = QLabel('三角洲行动 · 口琴自动演奏器')
        title.setObjectName('title')
        subtitle = QLabel('MIDI / 简谱 TXT → 自动模拟 Z X C V B N M , 与鼠标升降调键')
        subtitle.setObjectName('subtitle')
        root.addWidget(title)
        root.addWidget(subtitle)

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(QLabel('曲库（scores 目录）'))
        self.list_scores = QListWidget()
        self.list_scores.currentItemChanged.connect(lambda *_: self._update_info())
        self.list_scores.itemDoubleClicked.connect(lambda *_: self._start_play())
        left.addWidget(self.list_scores, 1)
        row = QHBoxLayout()
        self.btn_add = QPushButton('添加曲谱…')
        self.btn_add.clicked.connect(self._add_score)
        self.btn_refresh = QPushButton('刷新')
        self.btn_refresh.clicked.connect(self._refresh_scores)
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_refresh)
        left.addLayout(row)
        self.lbl_info = QLabel('选择一个曲谱')
        self.lbl_info.setObjectName('info')
        self.lbl_info.setWordWrap(True)
        left.addWidget(self.lbl_info)
        left.addStretch(0)
        body.addLayout(left, 5)

        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 6)

        controls = QGroupBox('演奏控制')
        grid = QGridLayout(controls)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.btn_play = QPushButton('播放')
        self.btn_play.setObjectName('primary')
        self.btn_play.clicked.connect(self._start_play)
        self.btn_pause = QPushButton('暂停')
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_stop = QPushButton('停止')
        self.btn_stop.clicked.connect(self._stop)
        grid.addWidget(self.btn_play, 0, 0)
        grid.addWidget(self.btn_pause, 0, 1)
        grid.addWidget(self.btn_stop, 0, 2)

        grid.addWidget(QLabel('速度'), 1, 0)
        speed_row = QHBoxLayout()
        self.slider_speed = QSlider(Qt.Horizontal)
        self.slider_speed.setRange(50, 200)
        self.slider_speed.setValue(100)
        self.lbl_speed = QLabel('100%')
        self.slider_speed.valueChanged.connect(lambda v: self.lbl_speed.setText(f'{v}%'))
        speed_row.addWidget(self.slider_speed)
        speed_row.addWidget(self.lbl_speed)
        grid.addLayout(speed_row, 1, 1, 1, 2)

        grid.addWidget(QLabel('移调'), 2, 0)
        self.spin_transpose = QSpinBox()
        self.spin_transpose.setRange(-12, 12)
        self.spin_transpose.setSuffix(' 半音')
        self.spin_transpose.valueChanged.connect(lambda *_: self._update_info())
        grid.addWidget(self.spin_transpose, 2, 1)
        flags = QHBoxLayout()
        self.chk_auto_fit = QCheckBox('自动移调适配音域')
        self.chk_auto_fit.setChecked(True)
        self.chk_auto_fit.toggled.connect(lambda *_: self._update_info())
        self.chk_loop = QCheckBox('循环')
        flags.addWidget(self.chk_auto_fit)
        flags.addWidget(self.chk_loop)
        grid.addLayout(flags, 2, 2)

        self.lbl_progress = QLabel('就绪')
        self.lbl_progress.setObjectName('progress')
        grid.addWidget(self.lbl_progress, 3, 0, 1, 3)
        hint = QLabel('热键：Shift+F9 播放/停止 · Shift+F10 暂停/继续 · Shift+F8 急停')
        hint.setObjectName('hint')
        grid.addWidget(hint, 4, 0, 1, 3)
        right.addWidget(controls)

        safety = QGroupBox('安全设置')
        sgrid = QGridLayout(safety)
        sgrid.setHorizontalSpacing(10)
        sgrid.setVerticalSpacing(8)
        self.chk_foreground = QCheckBox('仅在游戏窗口位于前台时演奏')
        self.chk_foreground.setChecked(True)
        self.chk_foreground.setToolTip('窗口失焦时自动暂停，切回游戏后自动继续')
        sgrid.addWidget(self.chk_foreground, 0, 0, 1, 2)
        sgrid.addWidget(QLabel('游戏进程关键字'), 1, 0)
        self.edit_process = QLineEdit('DeltaForce')
        self.edit_process.setToolTip('前台进程名包含此关键字即视为游戏窗口')
        sgrid.addWidget(self.edit_process, 1, 1)
        self.chk_dry = QCheckBox('仅测试（不发送按键，只走时间轴）')
        sgrid.addWidget(self.chk_dry, 2, 0, 1, 2)
        if sys.platform != 'win32':
            self.chk_dry.setChecked(True)
            self.chk_dry.setEnabled(False)
        right.addWidget(safety)

        log_box = QGroupBox('日志')
        log_layout = QVBoxLayout(log_box)
        self.txt_log = QPlainTextEdit()
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

    def _apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #1b1d21; color: #e6e6e6; font-size: 13px; }
            QLabel#title { font-size: 20px; font-weight: 600; color: #f2f4f8; }
            QLabel#subtitle, QLabel#hint, QLabel#info { color: #9aa0a6; font-size: 12px; }
            QLabel#progress { color: #c8cdd4; font-weight: 600; padding-top: 2px; }
            QLabel#state { color: #e6e6e6; padding-left: 4px; }
            QGroupBox { border: 1px solid #33373d; border-radius: 8px; margin-top: 14px; padding: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #b7bec7; }
            QListWidget, QPlainTextEdit, QLineEdit, QSpinBox {
                background-color: #26292e; border: 1px solid #3a3f46; border-radius: 6px;
                padding: 4px 6px; selection-background-color: #2f6f3e;
            }
            QPushButton {
                background-color: #34383f; border: 1px solid #454b54; border-radius: 6px;
                padding: 7px 16px;
            }
            QPushButton:hover { background-color: #3d424a; }
            QPushButton:pressed { background-color: #2e3238; }
            QPushButton:disabled { color: #6d727a; background-color: #26292e; }
            QPushButton#primary { background-color: #2e7d46; border-color: #3c9a58; }
            QPushButton#primary:hover { background-color: #358f51; }
            QPushButton#primary:disabled { background-color: #2a3a2f; border-color: #2f4436; color: #6f8a79; }
            QSlider::groove:horizontal { height: 4px; background: #3a3f46; border-radius: 2px; }
            QSlider::handle:horizontal { width: 14px; margin: -6px 0; border-radius: 7px; background: #5aa874; }
            QCheckBox { spacing: 6px; }
            QStatusBar { border-top: 1px solid #2a2d31; }
            QStatusBar QLabel { padding: 2px 8px; }
        """)

    def _refresh_scores(self):
        self.list_scores.clear()
        if SCORES_DIR.is_dir():
            files = sorted(
                (p for p in SCORES_DIR.iterdir()
                 if p.suffix.lower() in ('.mid', '.midi', '.txt')),
                key=lambda p: p.name.lower())
            for path in files:
                item = QListWidgetItem(path.stem)
                item.setData(Qt.UserRole, str(path))
                item.setToolTip(str(path))
                self.list_scores.addItem(item)
        if self.list_scores.count():
            self.list_scores.setCurrentRow(0)
        else:
            self.lbl_info.setText('scores 目录还没有曲谱，点击「添加曲谱…」导入 MIDI 或简谱 TXT')

    def _selected_path(self) -> Path | None:
        item = self.list_scores.currentItem()
        if item is None:
            return None
        return Path(item.data(Qt.UserRole))

    def _update_info(self):
        path = self._selected_path()
        if path is None:
            self.lbl_info.setText('选择一个曲谱')
            return
        try:
            score = load_score(path)
            _, applied, dropped = build_play_notes(
                score.events, self.spin_transpose.value(), self.chk_auto_fit.isChecked())
        except Exception as exc:
            self.lbl_info.setText(f'无法解析：{exc}')
            return
        parts = [f'音符 {len(score.events)}', f'时长 {score.length:.1f}s', f'移调 {applied:+d} 半音']
        if dropped:
            parts.append(f'过短跳过 {dropped}')
        self.lbl_info.setText(' · '.join(parts))

    def _add_score(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '选择曲谱', '', '曲谱文件 (*.mid *.midi *.txt)')
        if not path:
            return
        src = Path(path)
        SCORES_DIR.mkdir(parents=True, exist_ok=True)
        dest = SCORES_DIR / src.name
        try:
            if src.resolve() != dest.resolve():
                if dest.exists():
                    self._append_log(f'曲库中已存在同名文件：{dest.name}，未覆盖')
                else:
                    shutil.copy2(src, dest)
                    self._append_log(f'已添加曲谱：{dest.name}')
        except OSError as exc:
            self._append_log(f'添加曲谱失败：{exc}')
            return
        self._refresh_scores()
        for row in range(self.list_scores.count()):
            item = self.list_scores.item(row)
            if Path(item.data(Qt.UserRole)).name == dest.name:
                self.list_scores.setCurrentRow(row)
                break

    def _start_play(self):
        if self._player and self._player.active:
            return
        path = self._selected_path()
        if path is None:
            self._append_log('请先在曲库中选择一个曲谱')
            return
        try:
            score = load_score(path)
            notes, applied, dropped = build_play_notes(
                score.events, self.spin_transpose.value(), self.chk_auto_fit.isChecked())
        except Exception as exc:
            self._append_log(f'曲谱解析失败：{exc}')
            return
        if not notes:
            self._append_log('曲谱中没有可演奏的音符（可能都短于游戏最小时长）')
            return

        dry = self.chk_dry.isChecked() or sys.platform != 'win32'
        if not dry and HOTKEY_EMERGENCY in self._hotkeys.failed:
            self._append_log('急停热键 Shift+F8 注册失败，为避免无法停止，已阻止演奏')
            return

        if dry:
            backend = DryRunBackend()
            self._append_log('仅测试模式：只走时间轴，不会发送任何按键')
        else:
            try:
                backend = create_backend(self._bridge.log.emit)
            except Exception as exc:
                self._append_log(f'初始化输入后端失败：{exc}')
                return
            self._append_log('已启用 Windows 输入后端（SendInput）')

        require_fg = self.chk_foreground.isChecked() and not dry
        keyword = self.edit_process.text().strip()
        if require_fg and not keyword:
            require_fg = False
            self._append_log('游戏进程关键字为空，已临时关闭前台检测')
        cache = [0.0, True]

        def foreground_ok() -> bool:
            if not require_fg:
                return True
            now = time.perf_counter()
            if now - cache[0] >= 0.15:
                cache[0] = now
                cache[1] = keyword.lower() in foreground_process_name().lower()
            return cache[1]

        def on_progress(index: int, total: int, note) -> None:
            self._bridge.progress.emit(index, total, describe_action(note))

        self._player = Player(
            backend,
            self._bridge.state.emit,
            self._bridge.log.emit,
            foreground_ok=foreground_ok,
            on_progress=on_progress,
        )
        opts = PlaybackOptions(
            speed=self.slider_speed.value() / 100.0,
            loop=self.chk_loop.isChecked(),
            countdown=3.0,
            require_foreground=require_fg,
        )
        summary = f'开始演奏《{score.name}》 · {len(notes)} 个音 · 移调 {applied:+d} 半音 · 速度 {self.slider_speed.value()}%'
        if dropped:
            summary += f' · 过短跳过 {dropped} 个'
        self._append_log(summary)
        if not self._player.play(notes, opts):
            self._append_log('启动失败：播放器仍在退出中，请稍后再试')

    def _toggle_pause(self):
        if self._player and self._state in ('playing', 'waiting_game', 'paused'):
            self._player.toggle_pause()

    def _stop(self):
        if self._player:
            self._player.stop()

    def _on_hotkey(self, hotkey_id: int):
        if hotkey_id == HOTKEY_START_STOP:
            if self._state in _ACTIVE_STATES:
                self._stop()
            else:
                self._start_play()
        elif hotkey_id == HOTKEY_PAUSE:
            self._toggle_pause()
        elif hotkey_id == HOTKEY_EMERGENCY:
            if self._state in _ACTIVE_STATES:
                self._append_log('急停')
            self._stop()

    def _on_state(self, state: str):
        self._state = state
        self.lbl_state.setText(_STATE_TEXT.get(state, state))
        active = state in _ACTIVE_STATES
        self.btn_play.setEnabled(not active)
        self.btn_stop.setEnabled(active)
        self.btn_pause.setEnabled(state in ('playing', 'waiting_game', 'paused'))
        self.btn_pause.setText('继续' if state == 'paused' else '暂停')
        if state == 'playing':
            self.lbl_state.setStyleSheet('color: #7bd88f; font-weight: 600;')
        elif state in ('paused', 'waiting_game'):
            self.lbl_state.setStyleSheet('color: #e0b356; font-weight: 600;')
        elif state == 'finished':
            self.lbl_state.setStyleSheet('color: #7bd88f;')
            self.lbl_progress.setText('演奏完成')
        else:
            self.lbl_state.setStyleSheet('color: #9aa0a6;')
            if state == 'idle' and self.lbl_progress.text() == '':
                self.lbl_progress.setText('就绪')

    def _on_progress(self, index: int, total: int, action: str):
        self.lbl_progress.setText(f'第 {index}/{total} 音 · 按 {action}')

    def _refresh_fg_label(self):
        if sys.platform != 'win32':
            self.lbl_fg.setText('前台进程: (仅 Windows 可检测)')
            self.lbl_fg.setStyleSheet('color: #9aa0a6;')
            return
        name = foreground_process_name() or '-'
        keyword = self.edit_process.text().strip().lower()
        matched = bool(keyword) and keyword in name.lower()
        self.lbl_fg.setText(f'前台进程: {name}' + ('（匹配）' if matched else ''))
        self.lbl_fg.setStyleSheet('color: #7bd88f;' if matched else 'color: #9aa0a6;')

    def _report_hotkey_status(self):
        if not self._hotkeys.available:
            return
        missing = [HOTKEY_LABELS[h] for h in self._hotkeys.failed if h in HOTKEY_LABELS]
        if missing:
            self._append_log('热键注册失败（可能被其他程序占用）：' + '、'.join(missing))
        else:
            self._append_log('全局热键就绪：Shift+F9 播放/停止，Shift+F10 暂停/继续，Shift+F8 急停')

    def _append_log(self, message: str):
        stamp = time.strftime('%H:%M:%S')
        self.txt_log.appendPlainText(f'[{stamp}] {message}')

    def closeEvent(self, event):
        try:
            if self._player:
                self._player.stop()
            self._hotkeys.stop()
        finally:
            super().closeEvent(event)