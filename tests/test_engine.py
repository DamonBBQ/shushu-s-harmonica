import threading
import unittest
from unittest.mock import patch

from core import engine
from core.input_backend import DryRunBackend
from core.mapping import PlayNote


class Clock:
    def __init__(self):
        self.now = 0.0
        self.actions = []

    def perf_counter(self):
        return self.now

    def call_at(self, when, action):
        self.actions.append((when, action))
        self.actions.sort(key=lambda item: item[0])

    def sleep(self, seconds):
        wake = self.now + seconds
        while self.actions and self.actions[0][0] <= wake:
            when, action = self.actions.pop(0)
            self.now = max(self.now, when)
            action()
        self.now = max(self.now, wake)


class RecordingBackend(DryRunBackend):
    def __init__(self, clock):
        self.clock = clock
        self.events = []
        self.thread_ids = []
        self.on_mouse_down = None

    def record(self, action, value=None):
        self.events.append((round(self.clock.now, 3), action, value))
        self.thread_ids.append(threading.get_ident())

    def begin_session(self):
        self.record('begin')

    def end_session(self):
        self.record('end')

    def key_down(self, key):
        self.record('key_down', key)

    def key_up(self, key):
        self.record('key_up', key)

    def mouse_down(self, button):
        self.record('mouse_down', button)
        if self.on_mouse_down is not None:
            self.on_mouse_down(button)

    def mouse_up(self, button):
        self.record('mouse_up', button)

    def release_all(self):
        self.record('release_all')


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.backend = RecordingBackend(self.clock)
        self.states = []
        self.logs = []
        self.idle = threading.Event()
        self.focus = True
        self.player = None

    def on_state(self, state):
        self.states.append((round(self.clock.now, 3), state, self.player.active))
        if state == 'idle':
            self.idle.set()

    def create_player(self, *, on_progress=None):
        self.player = engine.Player(
            self.backend,
            self.on_state,
            self.logs.append,
            foreground_ok=lambda: self.focus,
            on_progress=on_progress,
        )

    def play(self, notes, **options):
        with patch.object(engine, 'time', self.clock):
            self.assertTrue(self.player.play(notes, engine.PlaybackOptions(countdown=0, **options)))
            try:
                self.assertTrue(self.idle.wait(1), 'worker did not return to idle')
            finally:
                self.assertTrue(self.player.stop())
        self.assertFalse(any(log.startswith('播放出错') for log in self.logs), self.logs)

    def pressed_keys(self):
        return [(at, value) for at, action, value in self.backend.events if action == 'key_down']

    def test_stop_between_progress_and_dispatch(self):
        at_progress = threading.Event()
        allow_dispatch = threading.Event()

        def progress(*_args):
            at_progress.set()
            if not allow_dispatch.wait(1):
                raise RuntimeError('test did not release progress callback')

        self.create_player(on_progress=progress)
        with patch.object(engine, 'time', self.clock):
            self.assertTrue(self.player.play([PlayNote(0, 0.2, 0, -1)], engine.PlaybackOptions(countdown=0)))
            try:
                self.assertTrue(at_progress.wait(1))
                self.player.request_stop()
            finally:
                allow_dispatch.set()
                self.assertTrue(self.player.stop())

        self.assertEqual([action for _, action, _ in self.backend.events], ['begin', 'release_all', 'end'])
        self.assertEqual(len(set(self.backend.thread_ids)), 1)
        self.assertNotEqual(self.backend.thread_ids[0], threading.get_ident())
        self.assertIn('已停止演奏', self.logs)
        self.assertEqual(self.states[-1], (0.0, 'idle', False))

    def test_foreground_loss_during_prep_releases_and_replays(self):
        self.create_player()
        self.clock.call_at(0.006, lambda: setattr(self, 'focus', False))
        self.clock.call_at(0.1, lambda: setattr(self, 'focus', True))
        self.play([PlayNote(0, 0.18, 0, 1)])

        self.assertEqual(self.backend.events[1:3], [
            (0.0, 'mouse_down', 'right'),
            (0.01, 'mouse_up', 'right'),
        ])
        self.assertIn((0.01, 'waiting_game', True), self.states)
        self.assertEqual(len(self.pressed_keys()), 1)
        self.assertGreaterEqual(self.pressed_keys()[0][0], 0.112)
        self.assertEqual(self.backend.events.count((0.01, 'mouse_up', 'right')), 1)

    def test_focus_is_rechecked_between_modifiers(self):
        self.create_player()
        self.backend.on_mouse_down = lambda _: setattr(self, 'focus', False)
        self.clock.call_at(0.03, self.player.request_stop)
        self.play([PlayNote(0, 0.18, 0, 1, True)])

        self.assertEqual(self.backend.events[1:3], [
            (0.0, 'mouse_down', 'right'),
            (0.0, 'mouse_up', 'right'),
        ])
        self.assertEqual(len(self.backend.events), 5)

    def test_pause_last_note_and_replay_on_resume(self):
        self.create_player()
        self.clock.call_at(0.032, self.player.pause)
        self.clock.call_at(0.2, self.player.resume)
        self.play([PlayNote(0, 0.8, 0)])

        self.assertEqual([state for _, state, _ in self.states], [
            'playing', 'paused', 'playing', 'finished', 'idle',
        ])
        self.assertEqual(self.pressed_keys()[0], (0.012, 0))
        self.assertEqual(len(self.pressed_keys()), 2)
        self.assertGreaterEqual(self.pressed_keys()[1][0], 0.212)
        self.assertGreaterEqual(self.states[-2][0], 1.0)

    def test_late_note_is_skipped_without_moving_later_notes(self):
        def late_callback(index, *_args):
            if index == 1:
                self.clock.now += 0.05

        self.create_player(on_progress=late_callback)
        self.play([PlayNote(0, 0.09, 0), PlayNote(0.1, 0.1, 1), PlayNote(0.2, 0.12, 2)])

        self.assertEqual(self.pressed_keys(), [(0.112, 1), (0.212, 2)])
        self.assertIn('已跳过 1 个可用演奏时间不足的音符', self.logs)

    def test_finish_waits_for_trailing_rest_at_selected_speed(self):
        self.create_player()
        self.play([PlayNote(0, 0.16, 0)], speed=2, score_length=0.4)

        self.assertEqual(self.pressed_keys(), [(0.012, 0)])
        self.assertEqual(self.states[-2], (0.2, 'finished', True))

    def test_loop_waits_for_trailing_rest_and_intermission(self):
        self.create_player()
        self.clock.call_at(1.3, self.player.request_stop)
        self.play([PlayNote(0, 0.08, 0)], loop=True, score_length=0.4)

        self.assertEqual(self.pressed_keys(), [(0.012, 0), (1.212, 0)])


if __name__ == '__main__':
    unittest.main()
