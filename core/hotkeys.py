from __future__ import annotations

import sys
import threading
from typing import Callable

HOTKEY_START_STOP = 1
HOTKEY_PAUSE = 2
HOTKEY_EMERGENCY = 3

HOTKEY_LABELS = {
    HOTKEY_START_STOP: 'Shift+F9',
    HOTKEY_PAUSE: 'Shift+F10',
    HOTKEY_EMERGENCY: 'Shift+F8',
}

_VK = {
    HOTKEY_START_STOP: 0x78,
    HOTKEY_PAUSE: 0x79,
    HOTKEY_EMERGENCY: 0x77,
}
_MOD_SHIFT = 0x0004
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


class GlobalHotkeys:
    def __init__(self, on_trigger: Callable[[int], None], on_status: Callable[[], None]):
        self._on_trigger = on_trigger
        self._on_status = on_status
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._registered: frozenset[int] | None = None
        self._closing = threading.Event()

    @property
    def available(self) -> bool:
        return sys.platform == 'win32'

    @property
    def registration_finished(self) -> bool:
        return self._registered is not None

    @property
    def failed(self) -> list[int]:
        registered = self._registered
        if registered is None:
            return []
        return [hotkey_id for hotkey_id in _VK if hotkey_id not in registered]

    @property
    def emergency_ready(self) -> bool:
        return not self._closing.is_set() and HOTKEY_EMERGENCY in (self._registered or ())

    def start(self) -> None:
        if not self.available:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        registered: set[int] = set()
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            if self._closing.is_set():
                return

            for hotkey_id, vk in _VK.items():
                if user32.RegisterHotKey(None, hotkey_id, _MOD_SHIFT | _MOD_NOREPEAT, vk):
                    registered.add(hotkey_id)
            self._registered = frozenset(registered)
            self._on_status()

            while not self._closing.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message == _WM_HOTKEY:
                    try:
                        self._on_trigger(int(msg.wParam))
                    except Exception:
                        pass
        finally:
            self._registered = frozenset()
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self._thread_id = 0
            if not self._closing.is_set():
                self._on_status()

    def stop(self) -> None:
        self._closing.set()
        thread_id = self._thread_id
        if self.available and thread_id:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=0.25)
