from __future__ import annotations

import hashlib
import io
import json
import math
import os
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import mido

MIDI_PREPROCESS_VERSION = '2'
_CHORD_WINDOW = 0.03
_SHORT_NOTE_THRESHOLD = 0.072
_SHORT_NOTE_TARGET = 0.080
_SHORT_NOTE_GAP = 0.010
_GRACE_NOTE_THRESHOLD = 0.025

_INSTRUMENT_NAMES = {
    0: '钢琴', 1: '亮钢琴', 2: '电钢琴', 3: 'honky-tonk 钢琴', 4: '电钢琴 1',
    5: '电钢琴 2', 6: '拨弦古钢琴', 7: ' clavichord', 8: 'celesta', 9: 'glockenspiel',
    10: '音乐盒', 11: 'vibraphone', 12: 'marimba', 13: 'xylophone', 14: '管钟',
    15: 'dulcimer', 16: 'Hammond 风琴', 17: '打击风琴', 18: '摇滚风琴', 19: '教堂风琴',
    20: '簧管风琴', 21: '手风琴', 22: '口琴', 23: '探戈手风琴', 24: '木吉他',
    25: '钢丝弦吉他', 26: '爵士吉他', 27: '清音吉他', 28: '闷音吉他', 29: '过载吉他',
    30: '失真吉他', 31: '吉他泛音', 32: '原声贝斯', 33: '指弹贝斯', 34: '拨片贝斯',
    35: '无品格贝斯', 36: 'slap 贝斯 1', 37: 'slap 贝斯 2', 38: '合成贝斯 1',
    39: '合成贝斯 2', 40: '小提琴', 41: '中提琴', 42: '大提琴', 43: '低音提琴',
    44: '颤音弦乐', 45: '拨弦弦乐', 46: '竖琴', 47: '定音鼓', 48: '弦乐合奏',
    49: '慢弦乐', 50: '合成弦乐 1', 51: '合成弦乐 2', 52: '合唱团', 53: '人声合唱',
    54: '人声', 55: '合成人声', 56: '小号', 57: '长号', 58: '大号', 59: '闷音小号',
    60: '法国号', 61: '铜管合奏', 62: '合成铜管 1', 63: '合成铜管 2', 64: '中音萨克斯',
    65: '次中音萨克斯', 66: ' alto 萨克斯', 67: '低音萨克斯', 68: '单簧管', 69: '双簧管',
    70: '英国管', 71: '巴松', 72: '单簧管', 73: '长笛', 74: '短笛', 75: '排箫',
    76: '竖笛', 77: '排笛', 78: '口哨', 79: 'ocarina', 80: '方波主音', 81: '锯齿波主音',
    82: 'calliope 主音', 83: 'chiff 主音', 84: 'charang 主音', 85: '人声主音',
    86: '五度主音', 87: '贝斯加主音', 88: 'new age 主音', 89: 'warm 主音', 90: 'polysynth 主音',
    91: 'choir 主音', 92: 'bowed glass 主音', 93: 'metal 主音', 94: 'halo 主音',
    95: 'sweep 主音', 96: '雨声', 97: 'soundtrack', 98: 'crystal', 99: 'atmosphere',
    100: 'brightness', 101: 'goblins', 102: 'echoes', 103: 'sci-fi', 104: 'sitar',
    105: '班卓琴', 106: '三味线', 107: 'koto', 108: 'kalimba', 109: 'bagpipe',
    110: 'fiddle', 111: 'shanai', 112: 'tinkle bell', 113: 'agogo', 114: 'steel drums',
    115: 'woodblock', 116: 'taiko drum', 117: 'melodic tom', 118: 'synth drum', 119: 'reverse cymbal',
    120: 'guitar fret noise', 121: 'breath noise', 122: 'seashore', 123: 'bird tweet',
    124: 'telephone ring', 125: 'helicopter', 126: 'applause', 127: 'gunshot',
}


@dataclass(frozen=True)
class MidiNoteEvent:
    pitch: int
    start: float
    duration: float
    velocity: int = 80
    start_tick: int = 0
    end_tick: int | None = 0
    alternatives: tuple[MidiNoteEvent, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class MidiStreamCandidate:
    track: int
    channel: int
    track_name: str
    program: int
    note_count: int
    pitch_low: int
    pitch_high: int
    median_pitch: float
    monophony_ratio: float
    max_polyphony: int
    density: float
    score: float

    @property
    def instrument(self) -> str:
        return _INSTRUMENT_NAMES.get(self.program, f'GM {self.program + 1}')


@dataclass(frozen=True)
class MidiPreprocessOptions:
    mode: str = 'auto'
    track: int | None = None
    channel: int | None = None
    short_note_strategy: str = 'repair'

    def __post_init__(self):
        if self.mode not in ('auto', 'manual', 'legacy'):
            raise ValueError('MIDI 预处理模式必须是 auto、manual 或 legacy')
        if self.short_note_strategy not in ('repair', 'keep'):
            raise ValueError('短音策略必须是 repair 或 keep')
        if self.mode == 'manual':
            if type(self.track) is not int or self.track < 0:
                raise ValueError('手动选择声部时需要有效的轨道编号')
            if type(self.channel) is not int or not 0 <= self.channel <= 15 or self.channel == 9:
                raise ValueError('手动选择声部时需要有效的非打击乐通道')
        elif self.track is not None or self.channel is not None:
            raise ValueError('只有手动模式才能指定轨道和通道')

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            'mode': self.mode,
            'track': self.track,
            'channel': self.channel,
            'short_note_strategy': self.short_note_strategy,
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> MidiPreprocessOptions:
        return cls(
            mode=values.get('mode', 'auto'),
            track=values.get('track'),
            channel=values.get('channel'),
            short_note_strategy=values.get('short_note_strategy', 'repair'),
        )


@dataclass
class MidiAnalysisResult:
    name: str
    events: tuple[MidiNoteEvent, ...]
    length: float
    source_sha256: str
    algorithm_version: str
    options: MidiPreprocessOptions
    selected: MidiStreamCandidate | None
    candidates: tuple[MidiStreamCandidate, ...]
    confidence: float
    confidence_level: str
    score_margin: float
    short_note_count: int = 0
    grace_note_count: int = 0
    repaired_short_notes: int = 0
    irreparable_short_notes: int = 0
    fallback_used: bool = False
    cache_hit: bool = False


@dataclass
class _RawNote:
    pitch: int
    start_tick: int
    end_tick: int | None
    velocity: int


@dataclass
class _Stream:
    track: int
    channel: int
    track_name: str
    program_counts: Counter[int] = field(default_factory=Counter)
    raw_notes: list[_RawNote] = field(default_factory=list)
    notes: list[MidiNoteEvent] = field(default_factory=list)

    @property
    def program(self) -> int:
        return self.program_counts.most_common(1)[0][0] if self.program_counts else 0


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def preprocess_midi(
    path: str | Path,
    options: MidiPreprocessOptions | Mapping[str, Any] | None = None,
    *,
    cache_dir: str | Path | None = None,
    source_sha256: str | None = None,
) -> MidiAnalysisResult:
    path = Path(path)
    if options is None:
        options = MidiPreprocessOptions()
    elif isinstance(options, Mapping):
        options = MidiPreprocessOptions.from_dict(options)
    snapshot = path.read_bytes()
    source_hash = hashlib.sha256(snapshot).hexdigest()
    if source_sha256 is not None and source_sha256 != source_hash:
        raise ValueError('曲谱文件已变化，请重新选择后再解析')
    cache_path = _cache_path(cache_dir, source_hash, options) if cache_dir is not None else None
    if cache_path is not None:
        cached = _read_cache(cache_path, path.stem, source_hash, options)
        if cached is not None:
            return cached

    midi = mido.MidiFile(file=io.BytesIO(snapshot))
    if midi.type == 2:
        raise ValueError('不支持 type 2（异步多轨）MIDI 文件')
    if midi.ticks_per_beat <= 0:
        raise ValueError('不支持使用 SMPTE 时间基准的 MIDI 文件')

    streams, tempo_events, track_end = _parse_streams(midi)
    all_ticks = [track_end]
    for stream in streams:
        for note in stream.raw_notes:
            all_ticks.append(note.start_tick)
            if note.end_tick is not None:
                all_ticks.append(note.end_tick)
    times = _build_time_map(all_ticks, tempo_events, midi.ticks_per_beat)
    length = times[track_end]
    for stream in streams:
        notes = []
        for raw in stream.raw_notes:
            start = times[raw.start_tick]
            if raw.end_tick is None:
                duration = min(0.5, max(0.0, length - start))
                end_tick = None
            else:
                duration = max(0.0, times[raw.end_tick] - start)
                end_tick = raw.end_tick
            notes.append(MidiNoteEvent(
                raw.pitch, start, duration, raw.velocity, raw.start_tick, end_tick,
            ))
        stream.notes = sorted(notes, key=lambda note: (note.start_tick, note.pitch))
        length = max(length, max((note.start + note.duration for note in notes), default=0.0))

    candidates = tuple(sorted(
        (_score_candidate(stream, length) for stream in streams if stream.notes),
        key=lambda candidate: (-candidate.score, candidate.track, candidate.channel),
    ))
    if not candidates:
        raise ValueError('MIDI 中没有可用音符（已忽略打击乐通道）')

    fallback_used = False
    repaired = irreparable = 0
    if options.mode == 'legacy':
        selected = None
        events = _merge_streams(streams)
        fallback_used = True
        confidence = 0.0
        confidence_level = 'legacy'
        score_margin = 0.0
        short_count = sum(note.duration + 1e-9 < _SHORT_NOTE_THRESHOLD for note in events)
        grace_count = sum(note.duration + 1e-9 < _GRACE_NOTE_THRESHOLD for note in events)
    else:
        if options.mode == 'manual':
            selected = next((candidate for candidate in candidates
                             if candidate.track == options.track and candidate.channel == options.channel), None)
            if selected is None:
                raise ValueError(f'指定的 MIDI 声部不存在：轨道 {options.track}、通道 {options.channel}')
            confidence = 1.0
            confidence_level = 'manual'
            score_margin = 1.0
        else:
            selected = candidates[0]
            confidence, score_margin = _confidence(candidates)
            confidence_level = _confidence_level(confidence)
        stream = next(stream for stream in streams
                      if stream.track == selected.track and stream.channel == selected.channel)
        events = _monophonicize(stream.notes)
        short_count = sum(note.duration + 1e-9 < _SHORT_NOTE_THRESHOLD for note in events)
        grace_count = sum(note.duration + 1e-9 < _GRACE_NOTE_THRESHOLD for note in events)
        if options.short_note_strategy == 'repair':
            events, repaired, irreparable = _repair_short_notes(events)
    length = max(length, max((event.start + event.duration for event in events), default=0.0))
    result = MidiAnalysisResult(
        name=path.stem,
        events=tuple(events),
        length=length,
        source_sha256=source_hash,
        algorithm_version=MIDI_PREPROCESS_VERSION,
        options=options,
        selected=selected,
        candidates=candidates,
        confidence=confidence,
        confidence_level=confidence_level,
        score_margin=score_margin,
        short_note_count=short_count,
        grace_note_count=grace_count,
        repaired_short_notes=repaired,
        irreparable_short_notes=irreparable,
        fallback_used=fallback_used,
    )
    if cache_path is not None:
        _write_cache(cache_path, result)
    return result


def _parse_streams(midi: mido.MidiFile) -> tuple[list[_Stream], list[tuple[int, int, int, int]], int]:
    streams: dict[tuple[int, int], _Stream] = {}
    programs: dict[tuple[int, int], int] = {}
    tempo_events: list[tuple[int, int, int, int]] = []
    track_end = 0
    for track_index, track in enumerate(midi.tracks):
        tick = 0
        track_name = ''
        active: dict[tuple[int, int], _RawNote] = {}
        for message_index, message in enumerate(track):
            tick += message.time
            if message.type == 'set_tempo':
                tempo_events.append((tick, track_index, message_index, message.tempo))
            elif message.type in ('track_name', 'set_track_name'):
                if message.name.strip():
                    track_name = message.name.strip()
            elif message.type == 'program_change' and message.channel != 9:
                programs[(track_index, message.channel)] = message.program
            elif message.type in ('note_on', 'note_off') and message.channel != 9:
                key = (track_index, message.channel)
                stream = streams.setdefault(key, _Stream(track_index, message.channel, track_name))
                if not stream.track_name and track_name:
                    stream.track_name = track_name
                program = programs.get(key, 0)
                is_on = message.type == 'note_on' and message.velocity > 0
                note_key = (message.channel, message.note)
                previous = active.pop(note_key, None)
                if previous is not None:
                    previous.end_tick = tick
                    stream.raw_notes.append(previous)
                if is_on:
                    active[note_key] = _RawNote(message.note, tick, None, message.velocity)
                    stream.program_counts[program] += 1
        for (channel, _pitch), note in active.items():
            stream = streams[(track_index, channel)]
            stream.raw_notes.append(note)
        track_end = max(track_end, tick)
    return list(streams.values()), sorted(tempo_events), track_end


def _build_time_map(
    ticks: list[int],
    tempo_events: list[tuple[int, int, int, int]],
    ticks_per_beat: int,
) -> dict[int, float]:
    ordered_ticks = sorted(set(ticks) | {event[0] for event in tempo_events})
    times: dict[int, float] = {}
    current_tick = 0
    current_time = 0.0
    tempo = 500_000
    tempo_index = 0
    for tick in ordered_ticks:
        current_time += mido.tick2second(tick - current_tick, ticks_per_beat, tempo)
        current_tick = tick
        while tempo_index < len(tempo_events) and tempo_events[tempo_index][0] <= tick:
            tempo = tempo_events[tempo_index][3]
            tempo_index += 1
        times[tick] = current_time
    return times


def _score_candidate(stream: _Stream, length: float) -> MidiStreamCandidate:
    notes = stream.notes
    count = len(notes)
    pitches = [note.pitch for note in notes]
    starts = [note.start for note in notes]
    ends = [note.start + note.duration for note in notes]
    first_start, last_end = min(starts), max(ends)
    span = max(0.001, last_end - first_start)
    density = count / span
    median_pitch = statistics.median(pitches)
    pitch_low, pitch_high = min(pitches), max(pitches)
    pitch_range = pitch_high - pitch_low
    durations = [note.duration for note in notes]
    median_duration = statistics.median(durations)
    long_ratio = sum(duration + 1e-9 >= 0.09 for duration in durations) / count
    monophony_ratio, max_polyphony = _polyphony_stats(notes)
    ordered = sorted(notes, key=lambda note: note.start)
    intervals = [later.start - earlier.start for earlier, later in zip(ordered, ordered[1:])
                 if later.start > earlier.start + 1e-6]
    steps = [abs(later.pitch - earlier.pitch) for earlier, later in zip(ordered, ordered[1:])]
    step_ratio = sum(step <= 7 for step in steps) / len(steps) if steps else 1.0
    coverage = min(1.0, span / max(length, 0.001))
    variety = min(1.0, len(set(pitches)) / 12)
    if intervals:
        mean_interval = statistics.mean(intervals)
        deviation = statistics.pstdev(intervals) / max(mean_interval, 0.001)
        regularity = max(0.0, 1.0 - max(0.0, deviation - 0.35))
    else:
        regularity = 0.0
    density_score = max(0.0, 1.0 - abs(math.log(max(density, 0.01) / 2.5)) / 1.8)
    register_score = max(0.0, 1.0 - abs(median_pitch - 70) / 30)
    range_score = max(0.0, 1.0 - abs(pitch_range - 18) / 24)
    repetition = _repetition_ratio(ordered)

    score = (
        18 * min(1.0, count / 100)
        + 22 * monophony_ratio
        + 10 * density_score
        + 18 * register_score
        + 6 * range_score
        + 8 * long_ratio
        + 5 * min(1.0, median_duration / 0.18)
        + 8 * step_ratio
        + 14 * coverage
        + 5 * variety
        + 4 * regularity
        - min(15, 4 * max(0, max_polyphony - 1))
    )
    if count < 8:
        score -= 8
    if median_pitch < 48:
        score -= 10
    elif median_pitch > 88:
        score -= 6
    if 32 <= stream.program <= 39:
        score -= 6
    elif 72 <= stream.program <= 79:
        score += 3
    elif 56 <= stream.program <= 71:
        score += 2
    elif 24 <= stream.program <= 31:
        score += 1
    name = stream.track_name.casefold()
    if any(token in name for token in ('melody', 'lead', 'vocal', '主旋律', '旋律', '人声')):
        score += 4
    if any(token in name for token in ('bass', 'drum', 'percussion', 'pad', 'chord', '伴奏', '低音', '鼓')):
        score -= 8
    if repetition > 0 and (median_pitch < 55 or len(set(pitches)) < 8 or step_ratio < 0.65):
        score -= 10 * repetition
    return MidiStreamCandidate(
        track=stream.track,
        channel=stream.channel,
        track_name=stream.track_name,
        program=stream.program,
        note_count=count,
        pitch_low=pitch_low,
        pitch_high=pitch_high,
        median_pitch=round(median_pitch, 2),
        monophony_ratio=round(monophony_ratio, 4),
        max_polyphony=max_polyphony,
        density=round(density, 3),
        score=round(max(0.0, min(100.0, score)), 2),
    )


def _polyphony_stats(notes: list[MidiNoteEvent]) -> tuple[float, int]:
    points = []
    for note in notes:
        points.append((note.start, 1))
        points.append((note.start + note.duration, -1))
    current = maximum = 0
    for _time, change in sorted(points, key=lambda item: (item[0], item[1])):
        current += change
        maximum = max(maximum, current)
    ordered = sorted(notes, key=lambda note: note.start)
    overlapped = sum(
        index + 1 < len(ordered)
        and ordered[index + 1].start < note.start + note.duration - 1e-6
        for index, note in enumerate(ordered)
    )
    return 1.0 - overlapped / max(1, len(ordered)), max(1, maximum)


def _repetition_ratio(notes: list[MidiNoteEvent]) -> float:
    if len(notes) < 16:
        return 0.0
    signature = []
    for earlier, later in zip(notes, notes[1:]):
        signature.append((round(later.start - earlier.start, 2), later.pitch - earlier.pitch))
    windows = [tuple(signature[index:index + 8]) for index in range(0, len(signature) - 7, 4)]
    if len(windows) < 3:
        return 0.0
    return 1.0 - len(set(windows)) / len(windows)


def _confidence(candidates: tuple[MidiStreamCandidate, ...]) -> tuple[float, float]:
    top = candidates[0]
    serious = [candidate for candidate in candidates
               if candidate.note_count >= min(12, max(4, top.note_count // 4))]
    if len(serious) < 2:
        margin = 1.0
    else:
        second = max(candidate.score for candidate in serious[1:])
        margin = max(0.0, (top.score - second) / max(top.score, 1.0))
    confidence = 0.72 * top.score / 100 + 0.28 * min(1.0, margin / 0.18)
    return round(max(0.0, min(1.0, confidence)), 4), round(margin, 4)


def _confidence_level(confidence: float) -> str:
    if confidence >= 0.78:
        return 'high'
    if confidence >= 0.52:
        return 'medium'
    return 'low'


def _monophonicize(notes: list[MidiNoteEvent]) -> list[MidiNoteEvent]:
    ordered = sorted(notes, key=lambda note: (note.start_tick, -note.pitch, -note.duration))
    chosen: list[MidiNoteEvent] = []
    for note in ordered:
        if chosen and note.start_tick == chosen[-1].start_tick:
            continue
        chosen.append(note)
    events = []
    for index, note in enumerate(chosen):
        next_start = chosen[index + 1].start if index + 1 < len(chosen) else math.inf
        duration = min(note.duration, max(0.0, next_start - note.start))
        events.append(replace(note, duration=duration, alternatives=()))
    return events


def _repair_short_notes(events: list[MidiNoteEvent]) -> tuple[list[MidiNoteEvent], int, int]:
    repaired = irreparable = 0
    result = []
    for index, event in enumerate(events):
        if event.duration + 1e-9 >= _SHORT_NOTE_THRESHOLD:
            result.append(event)
            continue
        next_start = events[index + 1].start if index + 1 < len(events) else math.inf
        available = next_start - event.start - _SHORT_NOTE_GAP
        if available + 1e-9 >= _SHORT_NOTE_THRESHOLD:
            duration = min(_SHORT_NOTE_TARGET, available)
            result.append(replace(event, duration=duration, end_tick=None))
            repaired += 1
        else:
            result.append(event)
            irreparable += 1
    return result, repaired, irreparable


def _merge_streams(streams: list[_Stream]) -> list[MidiNoteEvent]:
    raw = [note for stream in streams for note in stream.notes]
    raw.sort(key=lambda note: (note.start, -note.pitch, note.start_tick))
    groups: list[list[MidiNoteEvent]] = []
    for note in raw:
        if not groups or note.start - groups[-1][0].start >= _CHORD_WINDOW:
            groups.append([note])
        else:
            groups[-1].append(note)
    events = []
    for group in groups:
        ordered = sorted(group, key=lambda note: (-note.pitch, -note.duration))
        events.append(replace(ordered[0], alternatives=tuple(ordered[1:])))
    return events


def _cache_path(
    cache_dir: str | Path | None,
    source_hash: str,
    options: MidiPreprocessOptions,
) -> Path | None:
    if cache_dir is None:
        return None
    return Path(cache_dir) / f'{source_hash}-{MIDI_PREPROCESS_VERSION}-{options.fingerprint[:16]}.json'


def _write_cache(path: Path, result: MidiAnalysisResult):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'cache_version': 1,
            'source_sha256': result.source_sha256,
            'algorithm_version': result.algorithm_version,
            'options': result.options.to_dict(),
            'options_fingerprint': result.options.fingerprint,
            'length': result.length,
            'selected': asdict(result.selected) if result.selected else None,
            'candidates': [asdict(candidate) for candidate in result.candidates],
            'confidence': result.confidence,
            'confidence_level': result.confidence_level,
            'score_margin': result.score_margin,
            'short_note_count': result.short_note_count,
            'grace_note_count': result.grace_note_count,
            'repaired_short_notes': result.repaired_short_notes,
            'irreparable_short_notes': result.irreparable_short_notes,
            'fallback_used': result.fallback_used,
            'events': [_event_payload(event) for event in result.events],
        }
        descriptor, temporary = tempfile.mkstemp(prefix='.midi-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                json.dump(payload, target, ensure_ascii=False, separators=(',', ':'))
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (OSError, TypeError, ValueError):
        return


def _read_cache(
    path: Path,
    name: str,
    source_hash: str,
    options: MidiPreprocessOptions,
) -> MidiAnalysisResult | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('cache_version') != 1:
            return None
        if payload.get('source_sha256') != source_hash:
            return None
        if payload.get('algorithm_version') != MIDI_PREPROCESS_VERSION:
            return None
        if payload.get('options_fingerprint') != options.fingerprint:
            return None
        if payload.get('options') != options.to_dict():
            return None
        event_payloads = payload['events']
        candidate_payloads = payload['candidates']
        if (not isinstance(event_payloads, list) or not event_payloads
                or not isinstance(candidate_payloads, list) or not candidate_payloads):
            return None
        events = tuple(_event_from_payload(item) for item in event_payloads)
        candidates = tuple(_candidate_from_payload(item) for item in candidate_payloads)
        if len({(item.track, item.channel) for item in candidates}) != len(candidates):
            return None
        if candidates != tuple(sorted(candidates, key=lambda item: (-item.score, item.track, item.channel))):
            return None
        if any(later.start < earlier.start - 1e-9 for earlier, later in zip(events, events[1:])):
            return None
        length = _finite_number(payload['length'])
        confidence = _finite_number(payload['confidence'])
        margin = _finite_number(payload['score_margin'])
        if length < 0 or not 0 <= confidence <= 1 or not 0 <= margin <= 1:
            return None
        if length + 1e-6 < max(event.start + event.duration for event in events):
            return None
        selected_payload = payload.get('selected')
        selected = _candidate_from_payload(selected_payload) if selected_payload is not None else None
        level = payload['confidence_level']
        if level not in ('high', 'medium', 'low', 'manual', 'legacy'):
            return None
        if options.mode == 'legacy':
            if (selected is not None or payload.get('fallback_used') is not True or level != 'legacy'
                    or confidence != 0 or margin != 0):
                return None
        else:
            if selected is None or payload.get('fallback_used') is not False:
                return None
            matching = next((item for item in candidates
                             if item.track == selected.track and item.channel == selected.channel), None)
            if matching != selected:
                return None
            if options.mode == 'manual':
                if (selected.track != options.track or selected.channel != options.channel
                        or level != 'manual' or confidence != 1 or margin != 1):
                    return None
            else:
                if selected != candidates[0] or level != _confidence_level(confidence):
                    return None
            if any(not selected.pitch_low <= event.pitch <= selected.pitch_high or event.alternatives
                   for event in events):
                return None
        return MidiAnalysisResult(
            name=name,
            events=events,
            length=length,
            source_sha256=source_hash,
            algorithm_version=MIDI_PREPROCESS_VERSION,
            options=options,
            selected=selected,
            candidates=candidates,
            confidence=confidence,
            confidence_level=level,
            score_margin=margin,
            short_note_count=_nonnegative_int(payload['short_note_count']),
            grace_note_count=_nonnegative_int(payload['grace_note_count']),
            repaired_short_notes=_nonnegative_int(payload['repaired_short_notes']),
            irreparable_short_notes=_nonnegative_int(payload['irreparable_short_notes']),
            fallback_used=bool(payload.get('fallback_used')),
            cache_hit=True,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _event_payload(event: MidiNoteEvent) -> dict[str, Any]:
    payload = asdict(event)
    payload['alternatives'] = [_event_payload(item) for item in event.alternatives]
    return payload


def _event_from_payload(payload: Mapping[str, Any]) -> MidiNoteEvent:
    pitch = _nonnegative_int(payload['pitch'])
    velocity = _nonnegative_int(payload['velocity'])
    start = _finite_number(payload['start'])
    duration = _finite_number(payload['duration'])
    start_tick = _nonnegative_int(payload.get('start_tick', 0))
    end_tick = payload.get('end_tick')
    if end_tick is not None:
        end_tick = _nonnegative_int(end_tick)
    if (pitch > 127 or velocity > 127 or start < 0 or duration < 0
            or end_tick is not None and end_tick < start_tick):
        raise ValueError('invalid cached MIDI event')
    alternatives = tuple(_event_from_payload(item) for item in payload.get('alternatives', ()))
    return MidiNoteEvent(pitch, start, duration, velocity, start_tick, end_tick, alternatives)


def _candidate_from_payload(payload: Mapping[str, Any]) -> MidiStreamCandidate:
    track = _nonnegative_int(payload['track'])
    channel = _nonnegative_int(payload['channel'])
    program = _nonnegative_int(payload['program'])
    note_count = _nonnegative_int(payload['note_count'])
    pitch_low = _nonnegative_int(payload['pitch_low'])
    pitch_high = _nonnegative_int(payload['pitch_high'])
    max_polyphony = _nonnegative_int(payload['max_polyphony'])
    median_pitch = _finite_number(payload['median_pitch'])
    monophony_ratio = _finite_number(payload['monophony_ratio'])
    density = _finite_number(payload['density'])
    score = _finite_number(payload['score'])
    if (channel > 15 or channel == 9 or program > 127 or pitch_low > 127
            or pitch_high > 127 or note_count == 0 or pitch_low > pitch_high
            or not pitch_low <= median_pitch <= pitch_high or density < 0):
        raise ValueError('invalid cached MIDI candidate')
    if not 0 <= monophony_ratio <= 1 or not 0 <= score <= 100:
        raise ValueError('invalid cached MIDI candidate')
    return MidiStreamCandidate(
        track, channel, str(payload.get('track_name', '')), program, note_count,
        pitch_low, pitch_high, median_pitch, monophony_ratio, max(1, max_polyphony),
        density, score,
    )


def _finite_number(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('invalid cached number')
    return float(value)


def _nonnegative_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError('invalid cached integer')
    return value
