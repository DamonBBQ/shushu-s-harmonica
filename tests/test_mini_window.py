import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QListWidget, QMessageBox, QPushButton, QSlider

from core.engine import PlaybackOptions
from core.input_backend import DryRunBackend
from core.updater import RemoteScore, UpdateCheck, UpdateError, git_blob_sha
from ui import main_window


class MiniWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.scores_dir = root
        (root / 'a.txt').write_text('TITLE=第一首\nBPM=60\n1-- 2', encoding='utf-8')
        (root / 'b.txt').write_text('TITLE=第二首\nBPM=60\n3-- 4', encoding='utf-8')
        (root / 'c.txt').write_text('TITLE=第三首\nBPM=60\n5-- 6', encoding='utf-8')
        self.settings_path = str(root / 'settings.ini')
        context = patch.multiple(main_window, SCORES_DIR=root, QSettings=self.make_settings)
        context.start()
        self.addCleanup(context.stop)
        hotkey_start = patch.object(main_window.GlobalHotkeys, 'start')
        hotkey_start.start()
        self.addCleanup(hotkey_start.stop)
        self.window = self.open_window()

    def make_settings(self, *_):
        return QSettings(self.settings_path, QSettings.IniFormat)

    def open_window(self):
        window = main_window.MainWindow()
        self.addCleanup(self.close_window, window)
        window.show()
        self.wait_for(window.btn_play.isEnabled)
        return window

    def close_window(self, window):
        window.close()
        self.wait_for(lambda: not window.isVisible() and not window._mini.isVisible())

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('Timed out waiting for the Qt window to update')
            self.app.processEvents()
            time.sleep(0.005)

    def choose_song(self, row):
        mini = self.window._mini
        mini.findChild(QPushButton, 'miniChoose').click()
        library = mini.findChild(QListWidget, 'miniScoreList')
        self.assertTrue(library.isVisible())
        self.assertEqual(library.focusPolicy(), Qt.NoFocus)
        item = library.item(row)
        QTest.mouseClick(library.viewport(), Qt.LeftButton, pos=library.visualItemRect(item).center())
        self.assertFalse(library.isVisible())

    def test_switch_song_and_restore_opacity_and_position(self):
        mini = self.window._mini
        self.window.btn_mini.click()
        self.assertTrue(mini.isVisible())
        self.assertFalse(self.window.isVisible())
        self.assertTrue(mini.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertTrue(mini.windowFlags() & Qt.WindowDoesNotAcceptFocus)
        self.assertTrue(mini.testAttribute(Qt.WA_ShowWithoutActivating))

        self.choose_song(2)
        self.wait_for(lambda: self.window.btn_play.isEnabled())
        self.assertEqual(self.window.list_scores.currentRow(), 2)
        self.assertEqual(self.window.lbl_track.text(), '第三首')

        slider = mini.findChild(QSlider, 'miniOpacity')
        self.assertEqual(slider.value(), 15)
        slider.setValue(42)
        self.assertEqual(self.window.slider_mini_opacity.value(), 58)
        self.window.slider_mini_opacity.setValue(72)
        self.assertEqual(slider.value(), 28)
        self.assertAlmostEqual(mini.windowOpacity(), 0.72, delta=0.01)
        position = mini.screen().availableGeometry().topLeft() + QPoint(24, 24)
        mini.move(position)
        mini.restore_requested.emit()
        self.assertTrue(self.window.isVisible())
        self.close_window(self.window)

        reopened = self.open_window()
        reopened.btn_mini.click()
        self.assertEqual(reopened.slider_mini_opacity.value(), 72)
        self.assertEqual(reopened._mini.pos(), position)
        closed = QSignalSpy(reopened.closed)
        self.close_window(reopened)
        self.assertEqual(closed.count(), 1)

    def test_song_speed_and_transpose_are_remembered_per_song(self):
        window = self.window
        window.chk_auto_fit.setChecked(False)
        window.list_scores.setCurrentRow(0)
        window.slider_speed.setValue(80)
        window.spin_transpose.setValue(2)

        window.list_scores.setCurrentRow(1)
        self.assertEqual(window.slider_speed.value(), 100)
        self.assertEqual(window.spin_transpose.value(), 0)
        window.slider_speed.setValue(120)
        window.spin_transpose.setValue(-3)

        window.list_scores.setCurrentRow(0)
        self.assertEqual(window.slider_speed.value(), 80)
        self.assertEqual(window.spin_transpose.value(), 2)
        self.wait_for(lambda: window._plan is not None)
        self.assertEqual(window._plan[2], 2)

        self.close_window(window)
        reopened = self.open_window()
        self.assertEqual(reopened.slider_speed.value(), 80)
        self.assertEqual(reopened.spin_transpose.value(), 2)
        reopened.list_scores.setCurrentRow(1)
        self.assertEqual(reopened.slider_speed.value(), 120)
        self.assertEqual(reopened.spin_transpose.value(), -3)

    def test_song_preferences_do_not_transfer_between_local_and_git(self):
        window = self.window
        window.list_scores.setCurrentRow(0)
        window.slider_speed.setValue(80)

        manifest = self.scores_dir / '.git-scores.json'
        manifest.write_text(json.dumps({'version': 1, 'scores': ['a.txt']}), encoding='utf-8')
        window._refresh_scores()
        self.assertEqual(window.slider_speed.value(), 100)

        window.slider_speed.setValue(120)
        manifest.write_text(json.dumps({'version': 1, 'scores': []}), encoding='utf-8')
        window._refresh_scores()
        self.assertEqual(window.slider_speed.value(), 80)

    def test_local_shortcut_defers_to_registered_global_shortcut(self):
        window = self.window
        with patch.object(window._hotkeys, 'is_registered', return_value=True):
            window._mini_shortcut.activated.emit()
            self.assertTrue(window.isVisible())
            window._dispatch_hotkey(main_window.HOTKEY_MINI_WINDOW)
            self.assertTrue(window._mini.isVisible())
            window._dispatch_hotkey(main_window.HOTKEY_MINI_WINDOW)
            self.assertTrue(window.isVisible())
        with patch.object(window._hotkeys, 'is_registered', return_value=False):
            window._mini_shortcut.activated.emit()
            self.assertTrue(window._mini.isVisible())

    def test_playback_and_pause_survive_switching_windows(self):
        window = self.window
        window.btn_mini.click()
        action = window._mini.findChild(QPushButton, 'miniAction')
        with patch.object(main_window, 'PlaybackOptions', side_effect=self.without_countdown):
            action.click()
        self.wait_for(lambda: window._state == 'playing' and window.track_progress.value() > 0)
        self.assertEqual(window.track_progress.maximum(), 2)
        player = window._player
        self.assertTrue(window._mini.findChild(QPushButton, 'miniChoose').isEnabled())
        action.click()
        self.wait_for(lambda: window._state == 'paused')
        window._mini.restore_requested.emit()
        self.assertTrue(window.isVisible())
        self.assertIs(window._player, player)
        self.assertEqual(window._state, 'paused')
        window.btn_mini.click()
        self.assertEqual(action.text(), '继续')
        window._mini.findChild(QPushButton, 'miniStop').click()
        self.wait_for(lambda: window._state == 'idle')
        self.assertEqual(window.track_progress.value(), 0)

    def test_song_selection_waits_for_previous_playback_to_release_input(self):
        stopping = Event()
        release = Event()
        self.addCleanup(release.set)

        class TestBackend(DryRunBackend):
            def release_all(self):
                stopping.set()
                release.wait(2)

        window = self.window
        window.btn_mini.click()
        action = window._mini.findChild(QPushButton, 'miniAction')
        with (patch.object(main_window, 'PlaybackOptions', side_effect=self.without_countdown),
              patch.object(main_window, 'DryRunBackend', TestBackend)):
            action.click()
        self.wait_for(lambda: window._state == 'playing' and window.track_progress.value() > 0)
        self.choose_song(0)
        self.assertFalse(stopping.is_set())
        self.choose_song(2)
        self.wait_for(stopping.is_set)
        self.assertEqual(window.list_scores.currentRow(), 0)
        self.assertFalse(window.btn_play.isEnabled())
        release.set()
        self.wait_for(lambda: window._state == 'idle' and window.btn_play.isEnabled())
        self.assertEqual(window.list_scores.currentRow(), 2)
        self.assertFalse(window._player.active)

    def test_check_updates_reports_latest_without_blocking_ui(self):
        check = UpdateCheck(updates=(), total=3)
        with (patch.object(main_window.ScoreUpdater, 'check_for_updates', return_value=check),
              patch.object(main_window.QMessageBox, 'information') as information):
            self.window.btn_update.click()
            self.wait_for(lambda: not self.window._updating)

        self.assertTrue(self.window.btn_update.isEnabled())
        self.assertIn('已是最新', self.window.lbl_update.text())
        information.assert_called_once()

    def test_update_network_failure_restores_controls(self):
        with (patch.object(main_window.ScoreUpdater, 'check_for_updates',
                           side_effect=UpdateError('无法连接 GitHub 更新服务器：offline')),
              patch.object(main_window.QMessageBox, 'warning') as warning):
            self.window.btn_update.click()
            self.wait_for(lambda: not self.window._updating)

        self.assertTrue(self.window.btn_update.isEnabled())
        self.assertEqual(self.window.lbl_update.text(), '更新失败')
        warning.assert_called_once()

    def test_update_check_reports_local_name_conflicts(self):
        check = UpdateCheck(updates=(), total=4, local_conflicts=('d.txt',))
        with (patch.object(main_window.ScoreUpdater, 'check_for_updates', return_value=check),
              patch.object(main_window.QMessageBox, 'information') as information):
            self.window.btn_update.click()
            self.wait_for(lambda: not self.window._updating)

        self.assertIn('保留 1 个本地同名曲目', self.window.lbl_update.text())
        self.assertIn('本地同名曲谱', information.call_args.args[2])

    def test_update_install_refreshes_library_and_keeps_existing_scores(self):
        data = 'TITLE=第四首\nBPM=60\n7-- 1\n'.encode('utf-8')
        remote = RemoteScore(name='d.txt', blob_sha=git_blob_sha(data), size=len(data))
        check = UpdateCheck(updates=(remote,), total=4)
        with (patch.object(main_window.ScoreUpdater, 'check_for_updates', return_value=check),
              patch.object(main_window.ScoreUpdater, '_download', return_value=data),
              patch.object(main_window.QMessageBox, 'question',
                           return_value=QMessageBox.StandardButton.Yes),
              patch.object(main_window.QMessageBox, 'information') as information):
            self.window.btn_update.click()
            self.wait_for(lambda: not self.window._updating and self.window.list_scores.count() == 4)

        self.assertEqual(self.window.list_scores.count(), 4)
        self.assertTrue((main_window.SCORES_DIR / 'd.txt').is_file())
        self.assertIn('曲库已更新', self.window.lbl_update.text())
        self.assertTrue(self.window.btn_update.isEnabled())
        information.assert_called_once()

    def test_frozen_update_installs_complete_application_package(self):
        release = main_window.ApplicationRelease(
            version='1.1.0',
            tag='v1.1.0',
            asset_name='HarmonicaPlayer-1.1.0-windows-x64.zip',
            download_url='https://github.com/example/releases/download/v1.1.0/update.zip',
            size=10,
            sha256='0' * 64,
        )
        prepared = Mock(spec=main_window.PreparedUpdate)
        with (patch.object(main_window.sys, 'frozen', True, create=True),
              patch.object(main_window.ApplicationUpdater, 'check_for_update', return_value=release),
              patch.object(main_window.ApplicationUpdater, 'prepare_update', return_value=prepared) as prepare,
              patch.object(main_window.ScoreUpdater, 'check_for_updates') as check_scores,
              patch.object(main_window.QMessageBox, 'question',
                           return_value=QMessageBox.StandardButton.Yes),
              patch.object(self.window, 'close') as close):
            self.window.btn_update.click()
            self.wait_for(lambda: prepared.launch.called and close.called)

        prepare.assert_called_once()
        check_scores.assert_not_called()
        prepared.launch.assert_called_once_with()
        prepared.cleanup.assert_not_called()
        self.assertFalse(self.window._updating)

    def test_cancelling_application_update_restores_controls(self):
        release = main_window.ApplicationRelease(
            version='1.1.0', tag='v1.1.0', asset_name='update.zip',
            download_url='https://github.com/example/update.zip', size=10, sha256='0' * 64,
        )
        with (patch.object(main_window.sys, 'frozen', True, create=True),
              patch.object(main_window.ApplicationUpdater, 'check_for_update', return_value=release),
              patch.object(main_window.ApplicationUpdater, 'prepare_update') as prepare,
              patch.object(main_window.QMessageBox, 'question',
                           return_value=QMessageBox.StandardButton.No)):
            self.window.btn_update.click()
            self.wait_for(lambda: not self.window._updating)

        prepare.assert_not_called()
        self.assertTrue(self.window.btn_update.isEnabled())
        self.assertIn('已取消', self.window.lbl_update.text())

    def test_application_package_failure_restores_controls(self):
        release = main_window.ApplicationRelease(
            version='1.1.0', tag='v1.1.0', asset_name='update.zip',
            download_url='https://github.com/example/update.zip', size=10, sha256='0' * 64,
        )
        with (patch.object(main_window.sys, 'frozen', True, create=True),
              patch.object(main_window.ApplicationUpdater, 'check_for_update', return_value=release),
              patch.object(main_window.ApplicationUpdater, 'prepare_update',
                           side_effect=UpdateError('校验失败')),
              patch.object(main_window.QMessageBox, 'question',
                           return_value=QMessageBox.StandardButton.Yes),
              patch.object(main_window.QMessageBox, 'warning') as warning):
            self.window.btn_update.click()
            self.wait_for(lambda: not self.window._updating)

        self.assertTrue(self.window.btn_update.isEnabled())
        self.assertEqual(self.window.lbl_update.text(), '更新失败')
        warning.assert_called_once()

    @staticmethod
    def without_countdown(**options):
        options['countdown'] = 0
        return PlaybackOptions(**options)
