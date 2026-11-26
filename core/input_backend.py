from __future__ import annotations

import os
import sys
from typing import Callable

KEY_SCANCODES = (0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33)


class InputBackend:
    name = 'base'

    def begin_session(self) -> None:
        pass

    def end_session(self) -> None:
        pass

    def key_down(self, key_index: int) -> None:
        raise NotImplementedError

    def key_up(self, key_index: int) -> None:
        raise NotImplementedError

    def mouse_down(self, button: str) -> None:
        raise NotImplementedError

    def mouse_up(self, button: str) -> None:
        raise NotImplementedError

    def release_all(self) -> None:
        pass

    def foreground_matches(self, keyword: str) -> bool:
        return True


class DryRunBackend(InputBackend):
    name = 'dry-run'

    def key_down(self, key_index: int) -> None:
        pass

    def key_up(self, key_index: int) -> None:
        pass

    def mouse_down(self, button: str) -> None:
        pass

    def mouse_up(self, button: str) -> None:
        pass


if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _winmm = ctypes.WinDLL('winmm')

    _ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ('dx', wintypes.LONG),
            ('dy', wintypes.LONG),
            ('mouseData', wintypes.DWORD),
            ('dwFlags', wintypes.DWORD),
            ('time', wintypes.DWORD),
            ('dwExtraInfo', _ULONG_PTR),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ('wVk', wintypes.WORD),
            ('wScan', wintypes.WORD),
            ('dwFlags', wintypes.DWORD),
            ('time', wintypes.DWORD),
            ('dwExtraInfo', _ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ('uMsg', wintypes.DWORD),
            ('wParamL', wintypes.WORD),
            ('wParamH', wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [('mi', _MOUSEINPUT), ('ki', _KEYBDINPUT), ('hi', _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [('type', wintypes.DWORD), ('u', _INPUTUNION)]

    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    _user32.GetForegroundWindow.argtypes = ()
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _INPUT_KEYBOARD = 1
    _INPUT_MOUSE = 0
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_SCANCODE = 0x0008
    _MOUSE_FLAGS = {
        'left': (0x0002, 0x0004),
        'right': (0x0008, 0x0010),
        'middle': (0x0020, 0x0040),
    }

    def _send(inp: _INPUT) -> None:
        if _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) != 1:
            code = ctypes.get_last_error()
            raise RuntimeError(
                f'输入发送失败（错误码 {code}）。请检查：本工具与游戏权限是否一致（必要时以管理员运行）；'
                '是否已切换到英文输入法；游戏是否使用无边框窗口。')

    class Win32Backend(InputBackend):
        name = 'win32'

        def __init__(self):
            self._held_keys: set[int] = set()
            self._held_buttons: set[str] = set()
            self._high_resolution = False
            self._target_window: tuple[int, int] | None = None
            self._checked_window: tuple[int, int] | None = None
            self._checked_name = ''

        def begin_session(self) -> None:
            self._target_window = None
            self._checked_window = None
            self._checked_name = ''
            self._high_resolution = _winmm.timeBeginPeriod(1) == 0

        def end_session(self) -> None:
            if self._high_resolution:
                _winmm.timeEndPeriod(1)
                self._high_resolution = False

        def _key(self, key_index: int, up: bool) -> None:
            flags = _KEYEVENTF_SCANCODE | (_KEYEVENTF_KEYUP if up else 0)
            inp = _INPUT(
                type=_INPUT_KEYBOARD,
                u=_INPUTUNION(ki=_KEYBDINPUT(0, KEY_SCANCODES[key_index], flags, 0, 0)))
            _send(inp)

        def key_down(self, key_index: int) -> None:
            self._key(key_index, False)
            self._held_keys.add(key_index)

        def key_up(self, key_index: int) -> None:
            if key_index not in self._held_keys:
                return
            self._key(key_index, True)
            self._held_keys.discard(key_index)

        def _button(self, button: str, up: bool) -> None:
            down_flag, up_flag = _MOUSE_FLAGS[button]
            inp = _INPUT(
                type=_INPUT_MOUSE,
                u=_INPUTUNION(mi=_MOUSEINPUT(0, 0, 0, up_flag if up else down_flag, 0, 0)))
            _send(inp)

        def mouse_down(self, button: str) -> None:
            self._button(button, False)
            self._held_buttons.add(button)

        def mouse_up(self, button: str) -> None:
            if button not in self._held_buttons:
                return
            self._button(button, True)
            self._held_buttons.discard(button)

        def release_all(self) -> None:
            errors = []
            for key_index in tuple(self._held_keys):
                try:
                    self.key_up(key_index)
                except Exception as exc:
                    errors.append(str(exc))
            for button in tuple(self._held_buttons):
                try:
                    self.mouse_up(button)
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise RuntimeError(errors[0])

        def foreground_matches(self, keyword: str) -> bool:
            keyword = keyword.strip().lower()
            window = _foreground_window()
            if window is None or not keyword:
                return False
            if self._target_window is not None:
                return window == self._target_window
            if window != self._checked_window:
                self._checked_name = _process_name(window[1]).lower()
                self._checked_window = window if self._checked_name else None
            if keyword in self._checked_name:
                self._target_window = window
                return True
            return False

    def _foreground_window() -> tuple[int, int] | None:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return (hwnd, pid.value) if pid.value else None

    def _process_name(pid: int) -> str:
        process = _kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return ''
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if _kernel32.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return ''
        finally:
            _kernel32.CloseHandle(process)

    def foreground_process_name() -> str:
        window = _foreground_window()
        return _process_name(window[1]) if window else ''

    def is_process_elevated() -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

else:
    class Win32Backend(InputBackend):
        name = 'win32'

    def foreground_process_name() -> str:
        return ''

    def is_process_elevated() -> bool:
        return False


def create_backend(log: Callable[[str], None] | None = None) -> InputBackend:
    if sys.platform == 'win32':
        return Win32Backend()
    if log:
        log('当前系统不是 Windows，已切换为试运行模式（不发送按键）')
    return DryRunBackend()
