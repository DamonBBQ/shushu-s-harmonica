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
    def __init__(self, on_trigger: Callable[[int], None]):
        self._on_trigger = on_trigger
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self.failed: list[int] = []

    @property
    def available(self) -> bool:
        return sys.platform == 'win32'

    def start(self) -> None:
        if not self.available:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        registered = []
        for hotkey_id, vk in _VK.items():
            if user32.RegisterHotKey(None, hotkey_id, _MOD_SHIFT | _MOD_NOREPEAT, vk):
                registered.append(hotkey_id)
            else:
                self.failed.append(hotkey_id)

        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message == _WM_HOTKEY:
                    try:
                        self._on_trigger(int(msg.wParam))
                    except Exception:
                        pass
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)

    def stop(self) -> None:
        if not self.available or not self._thread_id:
            return
        import ctypes
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)