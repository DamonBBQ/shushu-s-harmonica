from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .input_backend import InputBackend
from .mapping import DEFAULT_GAP_MS, MIN_HOLD_MS, PREP_MS, PlayNote

_GAP = DEFAULT_GAP_MS / 1000.0
_PREP = PREP_MS / 1000.0
_MIN_HOLD = MIN_HOLD_MS / 1000.0
_MIN_SLOT = _PREP + _MIN_HOLD + _GAP
_TIME_EPSILON = 1e-9
_POLL_INTERVAL = 0.005


@dataclass
class PlaybackOptions:
    speed: float = 1.0
    loop: bool = False
    countdown: float = 3.0
    require_foreground: bool = True
    score_length: float | None = None


class _Stopped(Exception):
    pass


class _Paused(Exception):
    def __init__(self, state: str, observed_at: float):
        self.state = state
        self.observed_at = observed_at


class Player:
    def __init__(self, backend: InputBackend,
                 on_state: Callable[[str], None],
                 on_log: Callable[[str], None],
                 foreground_ok: Callable[[], bool] | None = None,
                 on_progress: Callable[[int, int, PlayNote], None] | None = None):
        self._backend = backend
        self._on_state = on_state
        self._on_log = on_log
        self._foreground_ok = foreground_ok or (lambda: True)
        self._on_progress = on_progress
        self._thread: threading.Thread | None = None
        self._play_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._state = 'idle'
        self._t0 = 0.0

    @property
    def active(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def play(self, notes: Sequence[PlayNote], opts: PlaybackOptions | None = None) -> bool:
        with self._play_lock:
            if self.active or not notes:
                return False
            self._stop_event.clear()
            self._resume_event.set()
            opts = opts or PlaybackOptions()
            self._thread = threading.Thread(target=self._run, args=(list(notes), opts), daemon=True)
            self._thread.start()
        return True

    def request_stop(self) -> None:
        """Ask the worker to stop without waiting for input or cleanup."""
        self._stop_event.set()

    def stop(self, timeout: float = 0.5) -> bool:
        """Request a stop and report whether the worker has exited."""
        self.request_stop()
        thread = self._thread
        if thread and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=timeout)
        return thread is None or not thread.is_alive()

    def pause(self) -> None:
        if self.active:
            self._resume_event.clear()

    def resume(self) -> None:
        self._resume_event.set()

    def toggle_pause(self) -> None:
        if not self.active:
            return
        if self._resume_event.is_set():
            self.pause()
        else:
            self.resume()

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._on_state(state)

    def _run(self, notes: Sequence[PlayNote], opts: PlaybackOptions) -> None:
        try:
            self._check_stop()
            speed = opts.speed
            if not math.isfinite(speed) or speed <= 0:
                raise ValueError('播放速度必须为有限正数')
            score_end = max(note.start + note.duration for note in notes)
            if opts.score_length is not None:
                score_end = max(score_end, opts.score_length)
            total = len(notes)

            self._countdown(max(opts.countdown, 0.0))
            self._backend.begin_session()
            while True:
                self._t0 = time.perf_counter()
                self._set_state('playing')
                missed = 0
                for index, note in enumerate(notes):
                    start = note.start / speed
                    end = (note.start + note.duration) / speed
                    if index + 1 < total:
                        end = min(end, notes[index + 1].start / speed)
                    self._wait_on_timeline(start, opts)
                    if self._on_progress:
                        self._on_progress(index + 1, total, note)
                    if not self._play_on_timeline(start, end, note, opts):
                        missed += 1

                if missed:
                    self._on_log(f'已跳过 {missed} 个可用演奏时间不足的音符')
                self._wait_on_timeline(score_end / speed + (0.8 if opts.loop else 0.0), opts)
                if not opts.loop:
                    break
            self._check_stop()
            self._on_log('演奏结束')
            self._set_state('finished')
        except _Stopped:
            self._on_log('已停止演奏')
        except Exception as exc:
            self._on_log(f'播放出错：{exc}')
        finally:
            try:
                self._backend.release_all()
            except Exception as exc:
                self._on_log(f'释放输入失败：{exc}')
            try:
                self._backend.end_session()
            except Exception as exc:
                self._on_log(f'关闭输入会话失败：{exc}')
            with self._play_lock:
                self._thread = None
                self._set_state('idle')

    def _check_stop(self) -> None:
        if self._stop_event.is_set():
            raise _Stopped

    def _countdown(self, seconds: float) -> None:
        if seconds > 0:
            self._set_state('countdown')
            self._on_log(f'倒计时 {seconds:.0f} 秒，请切回游戏并打开口琴界面')
        end = time.perf_counter() + seconds
        while True:
            self._check_stop()
            remaining = end - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.02))

    def _pause_state(self, opts: PlaybackOptions) -> str | None:
        if not self._resume_event.is_set():
            return 'paused'
        if opts.require_foreground and not self._foreground_ok():
            return 'waiting_game'
        return None

    def _check_playing(self, opts: PlaybackOptions) -> None:
        self._check_stop()
        state = self._pause_state(opts)
        # Resolving foreground ownership may have allowed another thread to stop or pause.
        self._check_stop()
        if not self._resume_event.is_set():
            state = 'paused'
        if state is not None:
            raise _Paused(state, time.perf_counter())

    def _wait_until(self, deadline: float, opts: PlaybackOptions) -> None:
        while True:
            self._check_playing(opts)
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(remaining, _POLL_INTERVAL))

    def _wait_for_resume(self, pause: _Paused, opts: PlaybackOptions) -> float:
        state = pause.state
        while state is not None:
            self._check_stop()
            if state == 'waiting_game' and state != self._state:
                self._on_log('游戏窗口不在前台，已暂停（切回后自动继续）')
            self._set_state(state)
            time.sleep(0.01)
            state = self._pause_state(opts)
        self._check_stop()
        resumed_at = time.perf_counter()
        self._set_state('playing')
        return resumed_at

    def _wait_on_timeline(self, music_time: float, opts: PlaybackOptions) -> None:
        while True:
            try:
                self._wait_until(self._t0 + music_time, opts)
                return
            except _Paused as pause:
                self._t0 += self._wait_for_resume(pause, opts) - pause.observed_at

    def _play_on_timeline(self, start: float, end: float, note: PlayNote,
                          opts: PlaybackOptions) -> bool:
        if end - start + _TIME_EPSILON < _MIN_SLOT:
            return False
        while True:
            try:
                return self._play_note(self._t0 + end, note, opts)
            except _Paused as pause:
                # Replaying the current note preserves its attack and its place in the score.
                self._t0 = self._wait_for_resume(pause, opts) - start

    def _play_note(self, slot_end: float, note: PlayNote, opts: PlaybackOptions) -> bool:
        if time.perf_counter() + _PREP + _MIN_HOLD > slot_end + _TIME_EPSILON:
            return False
        backend = self._backend
        buttons = []
        if note.shift:
            buttons.append('left' if note.shift < 0 else 'right')
        if note.sharp:
            buttons.append('middle')

        pressed_buttons: list[str] = []
        key_pressed = False
        try:
            for button in buttons:
                self._check_playing(opts)
                backend.mouse_down(button)
                pressed_buttons.append(button)

            self._wait_until(time.perf_counter() + _PREP, opts)
            self._check_playing(opts)
            if time.perf_counter() + _MIN_HOLD > slot_end + _TIME_EPSILON:
                return False
            self._check_stop()
            backend.key_down(note.key_index)
            key_pressed = True

            # Use the scheduled tail gap to absorb a late wakeup without moving the next onset.
            release_at = max(slot_end - _GAP, time.perf_counter() + _MIN_HOLD)
            self._wait_until(min(release_at, slot_end), opts)
            return True
        finally:
            self._release_note(note.key_index if key_pressed else None, pressed_buttons)

    def _release_note(self, key: int | None, buttons: list[str]) -> None:
        failures = []
        if key is not None:
            try:
                self._backend.key_up(key)
            except Exception as exc:
                failures.append(f'松开按键失败：{exc}')
        for button in reversed(buttons):
            try:
                self._backend.mouse_up(button)
            except Exception as exc:
                failures.append(f'松开鼠标键失败：{exc}')
        if failures:
            raise RuntimeError('，'.join(failures))
