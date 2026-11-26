import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from core.hotkeys import (
    HOTKEY_EMERGENCY,
    HOTKEY_MINI_WINDOW,
    HOTKEY_PAUSE,
    HOTKEY_START_STOP,
    GlobalHotkeys,
)


class GlobalHotkeysTests(unittest.TestCase):
    def run_listener(self, *, unavailable=(), messages=()):
        status = []
        triggered = []
        listener = GlobalHotkeys(triggered.append, lambda: status.append((
            listener.registration_finished,
            listener.emergency_ready,
            listener.is_registered(HOTKEY_MINI_WINDOW),
            listener.failed,
        )))
        self.assertFalse(listener.is_registered(HOTKEY_MINI_WINDOW))

        user32 = Mock()
        user32.RegisterHotKey.side_effect = lambda _hwnd, hotkey_id, _mods, _vk: hotkey_id not in unavailable
        pending = iter(messages)

        def get_message(message, *_args):
            hotkey_id = next(pending, None)
            if hotkey_id is None:
                return 0
            message._obj.message = 0x0312
            message._obj.wParam = hotkey_id
            return 1

        user32.GetMessageW.side_effect = get_message
        kernel32 = SimpleNamespace(GetCurrentThreadId=lambda: 123)
        with patch.object(ctypes, 'windll', SimpleNamespace(user32=user32, kernel32=kernel32), create=True):
            listener._run()
        return status, triggered, user32

    def test_f11_uses_plain_nonrepeating_registration_and_dispatches(self):
        status, triggered, user32 = self.run_listener(messages=(HOTKEY_MINI_WINDOW,))

        self.assertCountEqual(user32.RegisterHotKey.call_args_list, [
            call(None, HOTKEY_START_STOP, 0x4004, 0x78),
            call(None, HOTKEY_PAUSE, 0x4004, 0x79),
            call(None, HOTKEY_EMERGENCY, 0x4004, 0x77),
            call(None, HOTKEY_MINI_WINDOW, 0x4000, 0x7A),
        ])
        self.assertEqual(status[0], (True, True, True, []))
        self.assertEqual(status[-1][:3], (True, False, False))
        self.assertEqual(triggered, [HOTKEY_MINI_WINDOW])
        user32.UnregisterHotKey.assert_any_call(None, HOTKEY_MINI_WINDOW)

    def test_f11_conflict_keeps_emergency_hotkey_ready(self):
        status, triggered, user32 = self.run_listener(
            unavailable=(HOTKEY_MINI_WINDOW,), messages=(HOTKEY_EMERGENCY,),
        )

        self.assertEqual(status[0], (True, True, False, [HOTKEY_MINI_WINDOW]))
        self.assertEqual(triggered, [HOTKEY_EMERGENCY])
        self.assertNotIn(call(None, HOTKEY_MINI_WINDOW), user32.UnregisterHotKey.call_args_list)


if __name__ == '__main__':
    unittest.main()
