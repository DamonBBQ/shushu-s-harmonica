import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import URLError

import mido

from core.mapping import build_play_notes, to_action
from core.score import ScoreEvent, load_midi, load_score, load_txt
from core.updater import RemoteScore, ScoreUpdater, UpdateError, git_blob_sha


class ScoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def load_text(self, text):
        path = self.directory / 'example.txt'
        path.write_text(text, encoding='utf-8-sig')
        return load_txt(path)

    def load_midi_file(self, midi):
        path = self.directory / 'example.mid'
        midi.save(path)
        return load_midi(path)

    def test_bundled_scores(self):
        root = Path(__file__).resolve().parents[1]
        fixture = Path(__file__).resolve().parent / 'fixtures' / 'sample.mid'
        for path, count in ((fixture, 42), (root / 'scores' / '小星星.txt', 56)):
            with self.subTest(name=path.name):
                score = load_score(path)
                self.assertEqual(len(score.events), count)
                self.assertEqual(len(build_play_notes(score.events)[0]), count)

    def test_basic_txt_retains_rests_and_extensions(self):
        score = self.load_text('TITLE=测试\nBPM=120\n0 | ^#1-- - // 延长\n i | 0--\n')
        self.assertEqual(score.name, '测试')
        self.assertEqual(
            [(event.pitch, event.start, event.duration) for event in score.events],
            [(73, 0.5, 2.0), (72, 2.5, 0.5)],
        )
        self.assertEqual(score.length, 4.5)

    def test_txt_grid_subdivides_notes_rests_and_extensions(self):
        score = self.load_text('BPM=120\nGRID=4\n5- - | 0 - ^1--- 0\n')
        self.assertEqual(
            [(event.pitch, event.start, event.duration) for event in score.events],
            [(67, 0.0, 0.375), (72, 0.625, 0.5)],
        )
        self.assertEqual(score.length, 1.25)

    def test_invalid_txt_reports_line_and_token(self):
        cases = (
            ('BPM=120\n1 9 2', r'第 2 行.*9'),
            ('BPM=120\n1 mistake 2', r'第 2 行.*mistake'),
            ('1\nBPM=inf', r'第 2 行.*BPM'),
            ('1\nBPM=nan', r'第 2 行.*BPM'),
            ('1\nGRID=2.5', r'第 2 行.*GRID'),
            ('\n@format example/1\n1', r'第 2 行.*@format'),
            ('示例 — 三角洲口琴谱\n键位标记 + - #\n1', r'第 2 行.*键位文本谱'),
        )
        for text, match in cases:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, match):
                self.load_text(text)

    def test_midi_pairs_by_track_and_shares_tempo_with_note_tracks(self):
        midi = mido.MidiFile(type=1, ticks_per_beat=1000)
        tempo = midi.add_track()
        melody = midi.add_track()
        second = midi.add_track()
        tempo.append(mido.MetaMessage('set_tempo', tempo=1_000_000, time=200))
        tempo.append(mido.MetaMessage('end_of_track', time=400))
        melody.append(mido.Message('note_on', channel=0, note=60, time=0))
        melody.append(mido.Message('note_off', channel=0, note=60, time=240))
        second.append(mido.Message('note_on', channel=0, note=60, time=160))
        second.append(mido.Message('note_on', channel=0, note=60, velocity=0, time=320))

        score = self.load_midi_file(midi)
        self.assertEqual(len(score.events), 2)
        self.assertAlmostEqual(score.events[0].duration, 0.14)
        self.assertAlmostEqual(score.events[1].start, 0.08)
        self.assertAlmostEqual(score.events[1].duration, 0.30)
        self.assertAlmostEqual(score.length, 0.5)
        notes, _, dropped = build_play_notes(score.events)
        self.assertEqual(dropped, 0)
        self.assertAlmostEqual(notes[0].duration, 0.08)
        self.assertAlmostEqual(notes[1].duration, 0.30)

    def test_chooses_next_chord_pitch_when_highest_note_is_too_short(self):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = midi.add_track()
        track.extend((
            mido.Message('note_on', note=60, time=0),
            mido.Message('note_on', note=72, time=0),
            mido.Message('note_off', note=72, time=100),
            mido.Message('note_off', note=60, time=500),
        ))
        score = self.load_midi_file(midi)
        for speed, pitch in ((1.0, 60), (0.5, 72)):
            with self.subTest(speed=speed):
                notes, _, dropped = build_play_notes(score.events, speed=speed)
                self.assertEqual(dropped, 0)
                self.assertEqual(len(notes), 1)
                note = notes[0]
                self.assertEqual((note.key_index, note.shift, note.sharp), to_action(pitch))


class PlaybackPlanTests(unittest.TestCase):
    def test_speed_uses_wall_time_and_keeps_source_timestamps(self):
        quick = [ScoreEvent(60, 0, 0.08), ScoreEvent(62, 0.08, 0.08)]
        self.assertEqual(build_play_notes(quick, speed=2), ([], 0, 2))

        short = [ScoreEvent(60, 0, 0.06), ScoreEvent(62, 0.06, 0.06)]
        notes, _, dropped = build_play_notes(short, speed=0.5)
        self.assertEqual(dropped, 0)
        self.assertEqual([(note.start, note.duration) for note in notes], [(0, 0.06), (0.06, 0.06)])

    def test_overlapping_note_must_fit_before_next_onset(self):
        events = [ScoreEvent(60, 0, 0.5), ScoreEvent(62, 0.05, 0.3)]
        notes, _, dropped = build_play_notes(events)
        self.assertEqual(dropped, 1)
        self.assertEqual([note.start for note in notes], [0.05])

        notes, _, dropped = build_play_notes(events, speed=0.5)
        self.assertEqual(dropped, 0)
        self.assertAlmostEqual(notes[0].duration, 0.05)

    def test_exact_duration_boundary_survives_float_rounding(self):
        events = [ScoreEvent(60, 10.0, 0.072), ScoreEvent(62, 10.072, 0.5)]
        notes, _, dropped = build_play_notes(events)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(notes), 2)

    def test_auto_fit_accounts_for_manual_offset(self):
        events = [ScoreEvent(48, 0, 0.5), ScoreEvent(85, 0.5, 0.5)]
        _, corrected, _ = build_play_notes(events, transpose=12)
        _, manual_only, _ = build_play_notes(events, transpose=12, auto_fit=False)
        self.assertEqual(corrected, 0)
        self.assertEqual(manual_only, 12)


class ScoreUpdaterTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.scores_dir = Path(directory.name)
        self.updater = ScoreUpdater(self.scores_dir)

    @staticmethod
    def remote(name: str, data: bytes) -> RemoteScore:
        return RemoteScore(name=name, blob_sha=git_blob_sha(data), size=len(data))

    def write_manifest(self, names):
        path = self.scores_dir / '.git-scores.json'
        path.write_text(json.dumps({'version': 1, 'scores': list(names)}), encoding='utf-8')

    def test_bundled_git_manifest_matches_score_files(self):
        scores = Path(__file__).resolve().parents[1] / 'scores'
        manifest = json.loads((scores / '.git-scores.json').read_text(encoding='utf-8'))
        actual = {path.name for path in scores.iterdir()
                  if path.suffix.lower() in ('.mid', '.midi', '.txt')}
        self.assertEqual(manifest['version'], 1)
        self.assertEqual(set(manifest['scores']), actual)

    def test_check_detects_only_changed_or_missing_scores(self):
        same_data = b'TITLE=same\nBPM=60\n1 2\n'
        old_data = b'TITLE=changed\nBPM=60\n1\n'
        new_data = b'TITLE=changed\nBPM=60\n1 2\n'
        added_data = b'TITLE=added\nBPM=60\n3 4\n'
        (self.scores_dir / 'same.txt').write_bytes(same_data)
        (self.scores_dir / 'changed.txt').write_bytes(old_data)
        (self.scores_dir / 'custom.txt').write_bytes(b'TITLE=custom\nBPM=60\n5 6\n')
        self.write_manifest(['same.txt', 'changed.txt'])
        remote = (
            self.remote('same.txt', same_data),
            self.remote('added.txt', added_data),
            self.remote('changed.txt', new_data),
        )

        with patch('core.updater._fetch_remote_scores', return_value=remote):
            check = self.updater.check_for_updates()

        self.assertEqual(check.total, 3)
        self.assertEqual([item.name for item in check.updates], ['added.txt', 'changed.txt'])

    def test_windows_crlf_checkout_matches_git_blob(self):
        data = b'TITLE=same\nBPM=60\n1 2\n'
        (self.scores_dir / 'same.txt').write_bytes(data.replace(b'\n', b'\r\n'))
        self.write_manifest(['same.txt'])
        remote = (self.remote('same.txt', data),)

        with patch('core.updater._fetch_remote_scores', return_value=remote):
            check = self.updater.check_for_updates()

        self.assertEqual(check.updates, ())

    def test_network_failure_is_reported_without_touching_scores(self):
        original = b'TITLE=local\nBPM=60\n1 2\n'
        (self.scores_dir / 'local.txt').write_bytes(original)

        with patch('core.updater.urlopen', side_effect=URLError('offline')):
            with self.assertRaisesRegex(UpdateError, '无法连接 GitHub'):
                self.updater.check_for_updates()

        self.assertEqual((self.scores_dir / 'local.txt').read_bytes(), original)

    def test_install_backs_up_replaced_scores_and_keeps_custom_files(self):
        old_data = b'TITLE=changed\nBPM=60\n1\n'
        new_data = b'TITLE=changed\nBPM=60\n1 2\n'
        added_data = b'TITLE=added\nBPM=60\n3 4\n'
        custom_data = b'TITLE=custom\nBPM=60\n5 6\n'
        (self.scores_dir / 'changed.txt').write_bytes(old_data)
        (self.scores_dir / 'custom.txt').write_bytes(custom_data)
        self.write_manifest(['changed.txt'])
        updates = (
            self.remote('changed.txt', new_data),
            self.remote('added.txt', added_data),
        )
        progress = []

        with patch.object(self.updater, '_download', side_effect=[new_data, added_data]):
            result = self.updater.install_updates(updates, lambda *args: progress.append(args))

        self.assertEqual((self.scores_dir / 'changed.txt').read_bytes(), new_data)
        self.assertEqual((self.scores_dir / 'added.txt').read_bytes(), added_data)
        self.assertEqual((self.scores_dir / 'custom.txt').read_bytes(), custom_data)
        self.assertEqual([path.name for path in result.installed], ['changed.txt', 'added.txt'])
        self.assertIsNotNone(result.backup_dir)
        self.assertEqual((result.backup_dir / 'changed.txt').read_bytes(), old_data)
        self.assertEqual(progress, [(1, 2, 'changed.txt'), (2, 2, 'added.txt')])
        manifest = json.loads((self.scores_dir / '.git-scores.json').read_text(encoding='utf-8'))
        self.assertEqual(set(manifest['scores']), {'changed.txt', 'added.txt'})

    def test_checksum_failure_does_not_replace_any_scores(self):
        old_data = b'TITLE=changed\nBPM=60\n1\n'
        valid_data = b'TITLE=added\nBPM=60\n3 4\n'
        broken_data = b'TITLE=changed\nBPM=60\n1 2\n'
        (self.scores_dir / 'changed.txt').write_bytes(old_data)
        self.write_manifest(['changed.txt'])
        updates = (
            self.remote('added.txt', valid_data),
            self.remote('changed.txt', broken_data),
        )

        with patch('core.updater._read_url', side_effect=[valid_data, broken_data[:-1] + b'X']):
            with self.assertRaisesRegex(UpdateError, '校验值不匹配'):
                self.updater.install_updates(updates)

        self.assertEqual((self.scores_dir / 'changed.txt').read_bytes(), old_data)
        self.assertFalse((self.scores_dir / 'added.txt').exists())

    def test_unparseable_download_does_not_replace_existing_score(self):
        old_data = b'TITLE=changed\nBPM=60\n1\n'
        broken_data = b'not a supported score\n'
        (self.scores_dir / 'changed.txt').write_bytes(old_data)
        self.write_manifest(['changed.txt'])

        with patch.object(self.updater, '_download', return_value=broken_data):
            with self.assertRaisesRegex(UpdateError, '无法解析'):
                self.updater.install_updates((self.remote('changed.txt', broken_data),))

        self.assertEqual((self.scores_dir / 'changed.txt').read_bytes(), old_data)

    def test_replace_failure_rolls_back_earlier_files(self):
        first_old = b'TITLE=first\nBPM=60\n1\n'
        first_new = b'TITLE=first\nBPM=60\n1 2\n'
        second_new = b'TITLE=second\nBPM=60\n3 4\n'
        (self.scores_dir / 'first.txt').write_bytes(first_old)
        self.write_manifest(['first.txt', 'second.txt'])
        updates = (
            self.remote('first.txt', first_new),
            self.remote('second.txt', second_new),
        )
        real_replace = os.replace

        def fail_second(source, target):
            if Path(target).name == 'second.txt':
                raise OSError('file is locked')
            return real_replace(source, target)

        with (patch.object(self.updater, '_download', side_effect=[first_new, second_new]),
              patch('core.updater.os.replace', side_effect=fail_second)):
            with self.assertRaisesRegex(UpdateError, '安装曲库更新失败'):
                self.updater.install_updates(updates)

        self.assertEqual((self.scores_dir / 'first.txt').read_bytes(), first_old)
        self.assertFalse((self.scores_dir / 'second.txt').exists())
        manifest = json.loads((self.scores_dir / '.git-scores.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['scores'], ['first.txt', 'second.txt'])

    def test_local_same_name_is_reported_and_never_overwritten(self):
        local_data = b'TITLE=local version\nBPM=60\n5 6\n'
        remote_data = b'TITLE=git version\nBPM=60\n1 2\n'
        (self.scores_dir / 'shared.txt').write_bytes(local_data)
        remote = (self.remote('shared.txt', remote_data),)

        with patch('core.updater._fetch_remote_scores', return_value=remote):
            check = self.updater.check_for_updates()

        self.assertEqual(check.updates, ())
        self.assertEqual(check.local_conflicts, ('shared.txt',))
        self.assertEqual((self.scores_dir / 'shared.txt').read_bytes(), local_data)

    def test_new_git_score_is_added_to_manifest(self):
        data = b'TITLE=new git score\nBPM=60\n1 2\n'
        remote = self.remote('new.txt', data)

        with patch.object(self.updater, '_download', return_value=data):
            self.updater.install_updates((remote,))

        manifest = json.loads((self.scores_dir / '.git-scores.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['scores'], ['new.txt'])

    def test_same_name_added_during_download_aborts_install(self):
        managed = self.scores_dir / 'managed.txt'
        local = self.scores_dir / 'shared.txt'
        old_data = b'TITLE=managed\n1\n'
        local_data = b'TITLE=local\n5 6\n'
        new_data = b'TITLE=remote\n2 3\n'
        managed.write_bytes(old_data)
        self.write_manifest(['managed.txt'])
        updates = tuple(self.remote(name, new_data) for name in ('managed.txt', 'shared.txt'))

        def download(update):
            if update.name == local.name:
                local.write_bytes(local_data)
            return new_data

        with patch.object(self.updater, '_download', side_effect=download):
            with self.assertRaisesRegex(UpdateError, '本地同名曲谱 shared.txt 已保留'):
                self.updater.install_updates(updates)

        self.assertEqual(managed.read_bytes(), old_data)
        self.assertEqual(local.read_bytes(), local_data)
        self.assertEqual(self.updater._load_managed_scores(), {'managed.txt'})

    def test_corrupt_manifest_stops_update_before_download(self):
        original = b'TITLE=local\nBPM=60\n1 2\n'
        (self.scores_dir / 'song.txt').write_bytes(original)
        (self.scores_dir / '.git-scores.json').write_text('{broken', encoding='utf-8')
        remote = (self.remote('song.txt', b'TITLE=git\nBPM=60\n3 4\n'),)

        with patch.object(self.updater, '_download') as download:
            with self.assertRaisesRegex(UpdateError, '清单格式损坏'):
                self.updater.install_updates(remote)

        download.assert_not_called()
        self.assertEqual((self.scores_dir / 'song.txt').read_bytes(), original)
