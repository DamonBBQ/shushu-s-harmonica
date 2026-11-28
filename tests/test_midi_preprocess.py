import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import mido

from core.midi_preprocess import (
    MIDI_PREPROCESS_VERSION,
    MidiPreprocessOptions,
    preprocess_midi,
    sha256_file,
)
from core.score import load_midi


class MidiPreprocessTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def save(self, midi, name='example.mid'):
        path = self.directory / name
        midi.save(path)
        return path

    @staticmethod
    def absolute_events(track, events):
        tick = 0
        for event_tick, _order, message in sorted(events, key=lambda item: (item[0], item[1])):
            message = message.copy(time=event_tick - tick)
            track.append(message)
            tick = event_tick

    def test_type_zero_splits_channels_and_ignores_percussion(self):
        midi = mido.MidiFile(type=0, ticks_per_beat=1000)
        track = midi.add_track()
        events = [
            (0, 0, mido.Message('program_change', channel=0, program=73)),
            (0, 0, mido.Message('program_change', channel=1, program=32)),
            (0, 0, mido.Message('program_change', channel=9, program=0)),
        ]
        for index, pitch in enumerate((72, 74, 76, 79, 81, 79, 76, 74)):
            start = index * 200
            events.extend((
                (start, 2, mido.Message('note_on', channel=0, note=pitch, velocity=90)),
                (start + 160, 1, mido.Message('note_off', channel=0, note=pitch)),
                (start, 2, mido.Message('note_on', channel=1, note=36, velocity=80)),
                (start + 180, 1, mido.Message('note_off', channel=1, note=36)),
                (start, 2, mido.Message('note_on', channel=9, note=40, velocity=100)),
                (start + 100, 1, mido.Message('note_off', channel=9, note=40)),
            ))
        self.absolute_events(track, events)
        path = self.save(midi)

        result = preprocess_midi(path)

        self.assertEqual({candidate.channel for candidate in result.candidates}, {0, 1})
        self.assertTrue(all(candidate.track == 0 for candidate in result.candidates))
        self.assertEqual(result.selected.channel, 0)
        self.assertEqual([event.pitch for event in result.events], [72, 74, 76, 79, 81, 79, 76, 74])

    def test_selects_monophonic_melody_instead_of_piano_accompaniment(self):
        midi = mido.MidiFile(type=1, ticks_per_beat=1000)
        piano = midi.add_track()
        flute = midi.add_track()
        piano.append(mido.MetaMessage('track_name', name='Piano accompaniment'))
        piano.append(mido.Message('program_change', channel=0, program=0))
        flute.append(mido.MetaMessage('track_name', name='Lead melody'))
        flute.append(mido.Message('program_change', channel=0, program=73))
        piano_events = []
        flute_events = []
        for index, pitch in enumerate((72, 74, 76, 79, 81, 79, 76, 74, 72, 74)):
            start = index * 400
            for chord_pitch in (pitch - 12, pitch - 5, pitch):
                piano_events.extend((
                    (start, 2, mido.Message('note_on', note=chord_pitch, velocity=70)),
                    (start + 300, 1, mido.Message('note_off', note=chord_pitch)),
                ))
            flute_events.extend((
                (start, 2, mido.Message('note_on', note=pitch, velocity=100)),
                (start + 320, 1, mido.Message('note_off', note=pitch)),
            ))
        self.absolute_events(piano, piano_events)
        self.absolute_events(flute, flute_events)
        path = self.save(midi)
        original = path.read_bytes()

        score = load_midi(path)
        result = score.midi_analysis

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(result.selected.track, 1)
        self.assertEqual(result.selected.channel, 0)
        self.assertEqual(result.confidence_level, 'high')
        self.assertEqual([event.pitch for event in score.events], [72, 74, 76, 79, 81, 79, 76, 74, 72, 74])
        self.assertTrue(all(not event.alternatives for event in score.events))
        self.assertTrue(all(event.pitch >= 72 for event in score.events))

    def test_manual_override_and_legacy_mode_are_isolated(self):
        midi = mido.MidiFile(type=1, ticks_per_beat=1000)
        first = midi.add_track()
        second = midi.add_track()
        for index in range(12):
            start = index * 300
            first.extend((
                mido.Message('note_on', note=60 + index % 4, time=300 if index else 0),
                mido.Message('note_off', note=60 + index % 4, time=200),
            ))
            second.extend((
                mido.Message('note_on', note=72 + index % 5, time=300 if index else 0),
                mido.Message('note_off', note=72 + index % 5, time=220),
            ))
        path = self.save(midi)

        manual = preprocess_midi(path, MidiPreprocessOptions(mode='manual', track=0, channel=0))
        legacy = preprocess_midi(path, MidiPreprocessOptions(mode='legacy'))

        self.assertEqual(manual.selected.track, 0)
        self.assertEqual(manual.confidence_level, 'manual')
        self.assertTrue(all(event.pitch < 72 for event in manual.events))
        self.assertIsNone(legacy.selected)
        self.assertTrue(legacy.fallback_used)
        self.assertEqual(legacy.confidence_level, 'legacy')
        self.assertTrue(any(event.alternatives for event in legacy.events))

    def test_shared_tempo_map_uses_absolute_ticks(self):
        midi = mido.MidiFile(type=1, ticks_per_beat=1000)
        tempo = midi.add_track()
        melody = midi.add_track()
        tempo.append(mido.MetaMessage('set_tempo', tempo=500_000, time=0))
        tempo.append(mido.MetaMessage('set_tempo', tempo=1_000_000, time=100))
        melody.extend((
            mido.Message('note_on', note=72, time=0),
            mido.Message('note_off', note=72, time=100),
            mido.Message('note_on', note=74, time=0),
            mido.Message('note_off', note=74, time=100),
        ))
        path = self.save(midi)

        result = preprocess_midi(path)

        self.assertAlmostEqual(result.events[0].duration, 0.05)
        self.assertAlmostEqual(result.events[1].start, 0.05)
        self.assertAlmostEqual(result.events[1].duration, 0.10)

    def test_same_tick_tempo_events_keep_file_order(self):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = midi.add_track()
        track.extend((
            mido.MetaMessage('set_tempo', tempo=1_000_000, time=0),
            mido.MetaMessage('set_tempo', tempo=500_000, time=0),
            mido.Message('note_on', note=72, time=0),
            mido.Message('note_off', note=72, time=1000),
        ))
        path = self.save(midi)

        result = preprocess_midi(path)

        self.assertAlmostEqual(result.events[0].duration, 0.5)

    def test_short_notes_are_repaired_only_when_space_remains(self):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = midi.add_track()
        track.extend((
            mido.Message('note_on', note=60, time=0),
            mido.Message('note_off', note=60, time=20),
            mido.Message('note_on', note=62, time=180),
            mido.Message('note_off', note=62, time=20),
            mido.Message('note_on', note=64, time=10),
            mido.Message('note_off', note=64, time=20),
            mido.Message('note_on', note=65, time=170),
            mido.Message('note_off', note=65, time=270),
        ))
        path = self.save(midi)

        repaired = preprocess_midi(path)
        kept = preprocess_midi(path, MidiPreprocessOptions(short_note_strategy='keep'))

        self.assertEqual(repaired.short_note_count, 3)
        self.assertEqual(repaired.grace_note_count, 3)
        self.assertEqual(repaired.repaired_short_notes, 2)
        self.assertEqual(repaired.irreparable_short_notes, 1)
        self.assertAlmostEqual(repaired.events[0].duration, 0.08)
        self.assertAlmostEqual(repaired.events[1].duration, 0.01)
        self.assertAlmostEqual(repaired.events[2].duration, 0.08)
        self.assertEqual(kept.repaired_short_notes, 0)
        self.assertAlmostEqual(kept.events[0].duration, 0.01)

    def test_repaired_final_short_note_extends_length_and_uses_cache(self):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = midi.add_track()
        track.extend((
            mido.Message('note_on', note=72, time=0),
            mido.Message('note_off', note=72, time=20),
        ))
        path = self.save(midi)
        cache_dir = self.directory / 'cache'

        first = preprocess_midi(path, cache_dir=cache_dir)
        cached = preprocess_midi(path, cache_dir=cache_dir)

        self.assertEqual(first.repaired_short_notes, 1)
        self.assertAlmostEqual(first.events[0].duration, 0.08)
        self.assertAlmostEqual(first.length, 0.08)
        self.assertFalse(first.cache_hit)
        self.assertTrue(cached.cache_hit)
        self.assertEqual(cached.events, first.events)
        self.assertEqual(cached.length, first.length)

    def test_cache_uses_content_options_and_algorithm_version(self):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = midi.add_track()
        for index, pitch in enumerate((72, 74, 76, 79)):
            track.extend((
                mido.Message('note_on', note=pitch, time=300 if index else 0),
                mido.Message('note_off', note=pitch, time=240),
            ))
        path = self.save(midi)
        cache_dir = self.directory / 'cache'

        first = preprocess_midi(path, cache_dir=cache_dir)
        second = preprocess_midi(path, cache_dir=cache_dir)
        kept = preprocess_midi(path, MidiPreprocessOptions(short_note_strategy='keep'), cache_dir=cache_dir)

        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.events, second.events)
        self.assertFalse(kept.cache_hit)
        self.assertEqual(len(list(cache_dir.glob('*.json'))), 2)

        track[0].velocity = 90
        path = self.save(midi)
        changed = preprocess_midi(path, cache_dir=cache_dir)
        self.assertFalse(changed.cache_hit)
        self.assertNotEqual(changed.source_sha256, first.source_sha256)
        self.assertEqual(len(list(cache_dir.glob('*.json'))), 3)

        with patch('core.midi_preprocess.MIDI_PREPROCESS_VERSION', '3'):
            upgraded = preprocess_midi(path, cache_dir=cache_dir)
        self.assertFalse(upgraded.cache_hit)
        self.assertEqual(upgraded.algorithm_version, '3')
        self.assertEqual(len(list(cache_dir.glob('*.json'))), 4)

    def test_parse_uses_the_same_snapshot_as_its_content_hash(self):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = midi.add_track()
        track.extend((
            mido.Message('note_on', note=72, time=0),
            mido.Message('note_off', note=72, time=400),
        ))
        path = self.save(midi)
        original_hash = sha256_file(path)
        original_loader = mido.MidiFile

        def replace_before_parse(*args, **kwargs):
            replacement = original_loader(ticks_per_beat=1000)
            replacement.add_track().extend((
                mido.Message('note_on', note=60, time=0),
                mido.Message('note_off', note=60, time=400),
            ))
            replacement.save(path)
            return original_loader(*args, **kwargs)

        with patch('core.midi_preprocess.mido.MidiFile', side_effect=replace_before_parse):
            snapshot_result = preprocess_midi(path)

        self.assertEqual([event.pitch for event in snapshot_result.events], [72])
        self.assertEqual(snapshot_result.source_sha256, original_hash)
        changed_result = preprocess_midi(path)
        self.assertEqual([event.pitch for event in changed_result.events], [60])
        self.assertNotEqual(changed_result.source_sha256, snapshot_result.source_sha256)

    def test_stale_source_hash_cannot_return_an_old_cache(self):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = midi.add_track()
        track.extend((
            mido.Message('note_on', note=72, time=0),
            mido.Message('note_off', note=72, time=400),
        ))
        path = self.save(midi)
        cache_dir = self.directory / 'cache'
        original = preprocess_midi(path, cache_dir=cache_dir)
        replacement = mido.MidiFile(ticks_per_beat=1000)
        replacement.add_track().extend((
            mido.Message('note_on', note=60, time=0),
            mido.Message('note_off', note=60, time=400),
        ))
        replacement.save(path)

        with self.assertRaisesRegex(ValueError, '文件已变化'):
            preprocess_midi(path, cache_dir=cache_dir, source_sha256=original.source_sha256)
        changed = preprocess_midi(path, cache_dir=cache_dir)
        self.assertFalse(changed.cache_hit)
        self.assertEqual([event.pitch for event in changed.events], [60])

    def test_semantically_invalid_cache_is_rebuilt(self):
        midi = mido.MidiFile(type=1, ticks_per_beat=1000)
        for base in (60, 72):
            track = midi.add_track()
            for index, pitch in enumerate((base, base + 2, base + 4, base + 7)):
                track.extend((
                    mido.Message('note_on', note=pitch, time=300 if index else 0),
                    mido.Message('note_off', note=pitch, time=240),
                ))
        path = self.save(midi)
        options = MidiPreprocessOptions(mode='manual', track=0, channel=0)
        cache_dir = self.directory / 'cache'
        first = preprocess_midi(path, options, cache_dir=cache_dir)
        cache_file = next(cache_dir.glob('*.json'))
        payload = json.loads(cache_file.read_text(encoding='utf-8'))

        mutations = (
            lambda value: value.update(events=[]),
            lambda value: value['options'].update(mode='legacy'),
            lambda value: value['selected'].update(track=1),
            lambda value: value['candidates'].reverse(),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                broken = json.loads(json.dumps(payload))
                mutate(broken)
                cache_file.write_text(json.dumps(broken), encoding='utf-8')
                rebuilt = preprocess_midi(path, options, cache_dir=cache_dir)
                self.assertFalse(rebuilt.cache_hit)
                self.assertEqual(rebuilt.events, first.events)
                self.assertEqual(rebuilt.selected, first.selected)
                cached = preprocess_midi(path, options, cache_dir=cache_dir)
                self.assertTrue(cached.cache_hit)
                payload = json.loads(cache_file.read_text(encoding='utf-8'))

    def test_corrupt_cache_is_rebuilt_and_legacy_alternatives_survive(self):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = midi.add_track()
        track.extend((
            mido.Message('note_on', note=60, time=0),
            mido.Message('note_on', note=72, time=0),
            mido.Message('note_off', note=72, time=100),
            mido.Message('note_off', note=60, time=400),
        ))
        path = self.save(midi)
        cache_dir = self.directory / 'cache'
        options = MidiPreprocessOptions(mode='legacy')
        first = preprocess_midi(path, options, cache_dir=cache_dir)
        cache_file = next(cache_dir.glob('*.json'))
        cache_file.write_text('{broken', encoding='utf-8')

        rebuilt = preprocess_midi(path, options, cache_dir=cache_dir)
        cached = preprocess_midi(path, options, cache_dir=cache_dir)

        self.assertFalse(rebuilt.cache_hit)
        self.assertTrue(cached.cache_hit)
        self.assertEqual(first.events, cached.events)
        self.assertTrue(cached.events[0].alternatives)

    def test_similar_candidates_report_low_confidence(self):
        midi = mido.MidiFile(type=1, ticks_per_beat=1000)
        for offset in (0, 1):
            track = midi.add_track()
            for index, pitch in enumerate((72, 74, 76, 79, 81, 79, 76, 74)):
                track.extend((
                    mido.Message('note_on', note=pitch + offset, time=300 if index else 0),
                    mido.Message('note_off', note=pitch + offset, time=240),
                ))
        path = self.save(midi)

        result = preprocess_midi(path)

        self.assertNotEqual(result.confidence_level, 'high')
        self.assertLess(result.score_margin, 0.05)
        self.assertEqual(len(result.candidates), 2)

    def test_percussion_only_file_is_rejected(self):
        midi = mido.MidiFile(ticks_per_beat=1000)
        track = midi.add_track()
        track.extend((
            mido.Message('note_on', channel=9, note=36, time=0),
            mido.Message('note_off', channel=9, note=36, time=100),
        ))
        path = self.save(midi)

        with self.assertRaisesRegex(ValueError, '没有可用音符'):
            preprocess_midi(path)

    def test_content_hash_is_stable(self):
        path = self.directory / 'hash.mid'
        path.write_bytes(b'MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60')
        self.assertEqual(sha256_file(path), sha256_file(path))
        self.assertEqual(len(sha256_file(path)), 64)
        self.assertEqual(MIDI_PREPROCESS_VERSION, '2')


if __name__ == '__main__':
    unittest.main()
