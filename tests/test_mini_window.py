import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import mido

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QListWidget, QMessageBox, QPushButton, QSlider

from core.engine import PlaybackOptions
from core.input_backend import DryRunBackend
from core.midi_preprocess import MidiPreprocessOptions
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

    @staticmethod
    def write_midi(path, variation=0):
        midi = mido.MidiFile(type=1, ticks_per_beat=1000)
        piano = midi.add_track()
        melody = midi.add_track()
        piano.append(mido.MetaMessage('track_name', name='Piano accompaniment'))
        piano.append(mido.Message('program_change', program=0))
        melody.append(mido.MetaMessage('track_name', name='Lead melody'))
        melody.append(mido.Message('program_change', program=73))
        for index, pitch in enumerate((72, 74, 76, 79, 81, 79, 76, 74)):
            start = index * 400
            piano.extend((
                mido.Message('note_on', note=pitch - 12 + variation, time=400 if index else 0),
                mido.Message('note_off', note=pitch - 12 + variation, time=300),
            ))
            melody.extend((
                mido.Message('note_on', note=pitch + variation, time=400 if index else 0),
                mido.Message('note_off', note=pitch + variation, time=320),
            ))
        midi.save(path)

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

    def test_midi_import_preprocesses_copy_without_touching_source_or_scores_cache(self):
        incoming = self.scores_dir / 'incoming'
        incoming.mkdir()
        source = incoming / 'imported.mid'
        self.write_midi(source)
        original = source.read_bytes()
        cache = TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.window._midi_cache_dir = Path(cache.name)

        self.window._import_scores([source])
        destination = self.scores_dir / source.name
        self.wait_for(lambda: self.window._plan is not None
                      and self.window._plan[0].midi_analysis is not None
                      and 'MIDI 预处理完成' in self.window.txt_log.toPlainText())

        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(self.window._plan[0].midi_analysis.selected.track, 1)
        self.assertTrue(self.window.btn_midi.isEnabled())
        self.assertIn('声部', self.window.lbl_info.text())
        self.assertTrue(list(Path(cache.name).glob('*.json')))
        self.assertFalse(list(self.scores_dir.rglob('*.json')))

        self.write_midi(source, variation=1)
        self.window._import_scores([source])
        self.assertEqual(destination.read_bytes(), original)
        self.assertIn('未覆盖', self.window.txt_log.toPlainText())

    def test_import_does_not_overwrite_when_existence_check_is_stale(self):
        incoming = self.scores_dir / 'incoming'
        incoming.mkdir()
        source = incoming / 'race.txt'
        source.write_text('TITLE=新文件\nBPM=60\n1-- 2', encoding='utf-8')
        destination = self.scores_dir / source.name
        destination.write_text('TITLE=本地文件\nBPM=60\n3-- 4', encoding='utf-8')
        original = destination.read_bytes()
        original_exists = Path.exists

        def stale_exists(path):
            return False if path == destination else original_exists(path)

        with patch.object(Path, 'exists', stale_exists):
            self.window._import_scores([source])

        self.assertEqual(destination.read_bytes(), original)
        self.assertIn('未覆盖', self.window.txt_log.toPlainText())

    def test_failed_import_removes_partial_destination(self):
        incoming = self.scores_dir / 'incoming'
        incoming.mkdir()
        source = incoming / 'partial.txt'
        source.write_text('TITLE=新文件\nBPM=60\n1-- 2', encoding='utf-8')
        destination = self.scores_dir / source.name

        with patch.object(main_window.shutil, 'copyfileobj', side_effect=OSError('disk full')):
            self.window._import_scores([source])

        self.assertFalse(destination.exists())
        self.assertIn('disk full', self.window.txt_log.toPlainText())

    def test_speed_and_transpose_reuse_cached_source_hash(self):
        path = self.scores_dir / 'hash-reuse.mid'
        self.write_midi(path)
        cache = TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.window._midi_cache_dir = Path(cache.name)
        self.window._refresh_scores(path)
        self.wait_for(lambda: self.window._plan is not None
                      and self.window._plan[0].midi_analysis is not None)

        with patch.object(main_window, 'sha256_file', side_effect=AssertionError('重复计算哈希')) as digest:
            self.window.slider_speed.setValue(80)
            self.window.spin_transpose.setValue(2)
            self.app.processEvents()

        digest.assert_not_called()
        self.assertIsNotNone(self.window._plan)
        self.assertEqual(self.window.slider_speed.value(), 80)
        self.assertEqual(self.window.spin_transpose.value(), 2)

    def test_midi_manual_override_is_remembered_and_reset_when_content_changes(self):
        path = self.scores_dir / 'melody.mid'
        self.write_midi(path)
        cache = TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.window._midi_cache_dir = Path(cache.name)
        self.window._refresh_scores(path)
        self.wait_for(lambda: self.window._plan is not None
                      and self.window._plan[0].midi_analysis is not None)
        original_hash = self.window._plan[0].midi_analysis.source_sha256
        self.assertEqual(self.window._plan[0].midi_analysis.selected.track, 1)

        self.window._apply_midi_options(MidiPreprocessOptions(mode='manual', track=0, channel=0))
        self.wait_for(lambda: self.window._plan is not None
                      and self.window._plan[0].midi_analysis.options.mode == 'manual')
        self.assertEqual(self.window._plan[0].midi_analysis.selected.track, 0)
        self.window.list_scores.setCurrentRow(0)
        self.wait_for(lambda: self.window._plan is not None and self.window._plan[0].name == '第一首')
        melody_row = next(row for row in range(self.window.list_scores.count())
                          if Path(self.window.list_scores.item(row).data(Qt.UserRole)) == path)
        self.window.list_scores.setCurrentRow(melody_row)
        self.wait_for(lambda: self.window._plan is not None
                      and self.window._plan[0].midi_analysis.options.mode == 'manual')

        self.close_window(self.window)
        reopened = self.open_window()
        reopened._midi_cache_dir = Path(cache.name)
        reopened.list_scores.setCurrentRow(melody_row)
        self.wait_for(lambda: reopened._plan is not None
                      and reopened._plan[0].midi_analysis.options.mode == 'manual')
        self.assertEqual(reopened._plan[0].midi_analysis.selected.track, 0)

        self.write_midi(path, variation=1)
        reopened._refresh_scores()
        self.wait_for(lambda: reopened._plan is not None
                      and reopened._plan[0].midi_analysis.source_sha256 != original_hash
                      and reopened._plan[0].midi_analysis.options.mode == 'auto')
        self.assertEqual(reopened._plan[0].midi_analysis.selected.track, 1)

    def test_midi_voice_dialog_lists_candidates_and_switches_modes(self):
        path = self.scores_dir / 'voices.mid'
        self.write_midi(path)
        cache = TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.window._midi_cache_dir = Path(cache.name)
        self.window._refresh_scores(path)
        self.wait_for(lambda: self.window._plan is not None
                      and self.window._plan[0].midi_analysis is not None)

        with patch.object(main_window.QDialog, 'exec', return_value=0):
            self.window._open_midi_voices()
        dialog = self.window.findChild(main_window.QDialog, 'midiVoicesDialog')
        self.assertIsNotNone(dialog)
        table = dialog.findChild(main_window.QTableWidget, 'midiCandidateTable')
        self.assertEqual(table.rowCount(), 2)
        dialog.findChild(QPushButton, 'midiLegacy').click()
        self.wait_for(lambda: self.window._plan is not None
                      and self.window._plan[0].midi_analysis.options.mode == 'legacy')
        self.assertEqual(self.window._plan[0].midi_analysis.confidence_level, 'legacy')
        dialog.close()
        dialog.deleteLater()
        self.app.processEvents()

        with patch.object(main_window.QDialog, 'exec', return_value=0):
            self.window._open_midi_voices()
        dialog = self.window.findChild(main_window.QDialog, 'midiVoicesDialog')
        dialog.findChild(QPushButton, 'midiAuto').click()
        self.wait_for(lambda: self.window._plan is not None
                      and self.window._plan[0].midi_analysis.options.mode == 'auto')
        self.assertEqual(self.window._plan[0].midi_analysis.selected.track, 1)
        dialog.close()
        dialog.deleteLater()
        self.app.processEvents()

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
