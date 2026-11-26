from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .input_backend import InputBackend
from .mapping import DEFAULT_GAP_MS, MIN_HOLD_MS, PREP_MS, PlayNote

_GAP = DEFAULT_GAP_MS / 1000.0
_PREP = PREP_MS / 1000.0
_MIN_HOLD = MIN_HOLD_MS / 1000.0


@dataclass
class PlaybackOptions:
    speed: float = 1.0
    loop: bool = False
    countdown: float = 3.0
    require_foreground: bool = True


class _Stopped(Exception):
    pass


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
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._state = 'idle'
        self._t0 = 0.0
        self._paused_total = 0.0
        self._pause_begin: float | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def play(self, notes: Sequence[PlayNote], opts: PlaybackOptions | None = None) -> bool:
        if self.active or not notes:
            return False
        self._stop_event.clear()
        self._resume_event.set()
        opts = opts or PlaybackOptions()
        self._thread = threading.Thread(target=self._run, args=(list(notes), opts), daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self._resume_event.set()
        try:
            self._backend.release_all()
        except Exception:
            pass
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)

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
            speed = max(opts.speed, 0.25)
            if not self._countdown(max(opts.countdown, 0.0)):
                raise _Stopped
            total = len(notes)
            while True:
                self._t0 = time.perf_counter()
                self._paused_total = 0.0
                self._pause_begin = None
                self._set_state('playing')
                loop_end = 0.0
                for index, note in enumerate(notes, 1):
                    start = note.start / speed
                    duration = note.duration / speed
                    if not self._wait_until(start, opts):
                        raise _Stopped
                    if self._on_progress:
                        self._on_progress(index, total, note)
                    if not self._play_note(start, duration, note, opts):
                        raise _Stopped
                    loop_end = start + duration
                if not opts.loop:
                    break
                if not self._wait_until(loop_end + 0.8, opts):
                    raise _Stopped
            self._on_log('演奏结束')
            self._set_state('finished')
        except _Stopped:
            self._on_log('已停止演奏')
        except Exception as exc:
            self._on_log(f'播放出错：{exc}')
        finally:
            try:
                self._backend.release_all()
            except Exception:
                pass
            self._set_state('idle')
            self._thread = None

    def _countdown(self, seconds: float) -> bool:
        if seconds <= 0:
            return not self._stop_event.is_set()
        self._set_state('countdown')
        self._on_log(f'倒计时 {seconds:.0f} 秒，请切回游戏并打开口琴界面')
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            if self._stop_event.is_set():
                return False
            time.sleep(0.02)
        return True

    def _wait_until(self, music_time: float, opts: PlaybackOptions) -> bool:
        while True:
            if self._stop_event.is_set():
                return False
            if not self._check_running(opts):
                continue
            target = self._t0 + self._paused_total + music_time
            remaining = target - time.perf_counter()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.005))

    def _check_running(self, opts: PlaybackOptions) -> bool:
        user_paused = not self._resume_event.is_set()
        foreground_ok = True
        if opts.require_foreground and not user_paused:
            foreground_ok = self._foreground_ok()
        if user_paused or not foreground_ok:
            if self._pause_begin is None:
                self._pause_begin = time.perf_counter()
                try:
                    self._backend.release_all()
                except Exception:
                    pass
                if not user_paused:
                    self._on_log('游戏窗口不在前台，已暂停（切回后自动继续）')
            self._set_state('paused' if user_paused else 'waiting_game')
            time.sleep(0.01)
            return False
        if self._pause_begin is not None:
            self._paused_total += time.perf_counter() - self._pause_begin
            self._pause_begin = None
            self._set_state('playing')
        return True

    def _play_note(self, start: float, duration: float, note: PlayNote,
                   opts: PlaybackOptions) -> bool:
        backend = self._backend
        buttons: list[str] = []
        key_pressed = False
        try:
            if note.shift < 0:
                backend.mouse_down('left')
                buttons.append('left')
            elif note.shift > 0:
                backend.mouse_down('right')
                buttons.append('right')
            if note.sharp:
                backend.mouse_down('middle')
                buttons.append('middle')

            prep_end = time.perf_counter() + _PREP
            while time.perf_counter() < prep_end:
                if self._stop_event.is_set():
                    return False
                time.sleep(0.002)

            backend.key_down(note.key_index)
            key_pressed = True

            now_music = time.perf_counter() - self._t0 - self._paused_total
            release_at = max(start + duration - _GAP, now_music + _MIN_HOLD)
            while True:
                if self._stop_event.is_set():
                    return False
                if not self._check_running(opts):
                    return True
                remaining = self._t0 + self._paused_total + release_at - time.perf_counter()
                if remaining <= 0:
                    return True
                time.sleep(min(remaining, 0.005))
        finally:
            if key_pressed:
                try:
                    backend.key_up(note.key_index)
                except Exception as exc:
                    self._on_log(f'松开按键失败：{exc}')
            for button in reversed(buttons):
                try:
                    backend.mouse_up(button)
                except Exception as exc:
                    self._on_log(f'松开鼠标键失败：{exc}')