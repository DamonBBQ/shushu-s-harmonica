import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import mido

from core.mapping import build_play_notes, to_action
from core.score import ScoreEvent, load_midi, load_score, load_txt


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
        scores = Path(__file__).resolve().parents[1] / 'scores'
        for name, count in (('twinkle.mid', 42), ('小星星.txt', 56)):
            with self.subTest(name=name):
                score = load_score(scores / name)
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

    def test_invalid_txt_reports_line_and_token(self):
        cases = (
            ('BPM=120\n1 9 2', r'第 2 行.*9'),
            ('BPM=120\n1 mistake 2', r'第 2 行.*mistake'),
            ('1\nBPM=inf', r'第 2 行.*BPM'),
            ('1\nBPM=nan', r'第 2 行.*BPM'),
            ('\n@format HarmonicaPlayer/1\n1', r'第 2 行.*@format'),
            ('示例 — 三角洲口琴谱\n键位标记 + - #\n1', r'第 2 行.*Dr-hydra'),
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
