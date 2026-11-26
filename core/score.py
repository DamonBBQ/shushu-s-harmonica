from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

import mido

_NOTE_OFFSETS = (0, 2, 4, 5, 7, 9, 11)
_TXT_TOKEN = re.compile(r'([b#^]*)([0-8i])(-*)', re.IGNORECASE)
_CHORD_WINDOW = 0.03


@dataclass
class ScoreEvent:
    pitch: int
    start: float
    duration: float
    velocity: int = 80
    alternatives: tuple[ScoreEvent, ...] = field(default=(), repr=False)


@dataclass
class Score:
    name: str
    events: list[ScoreEvent]
    length: float


def load_score(path: str | Path) -> Score:
    path = Path(path)
    if path.suffix.lower() in ('.mid', '.midi'):
        return load_midi(path)
    return load_txt(path)


def load_midi(path: str | Path) -> Score:
    path = Path(path)
    mid = mido.MidiFile(str(path))
    if mid.type == 2:
        raise ValueError('不支持 type 2（异步多轨）MIDI 文件')
    if mid.ticks_per_beat <= 0:
        raise ValueError('不支持使用 SMPTE 时间基准的 MIDI 文件')

    # 共享 tempo 时间轴，同时保留轨道编号，避免跨轨配对同频道的同音高。
    timeline = []
    end_tick = 0
    for track_index, track in enumerate(mid.tracks):
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type == 'set_tempo' or (
                msg.type in ('note_on', 'note_off') and msg.channel != 9
            ):
                timeline.append((tick, track_index, msg))
        end_tick = max(end_tick, tick)
    timeline.sort(key=lambda item: item[0])

    active: dict[tuple[int, int, int], tuple[float, int]] = {}
    raw: list[ScoreEvent] = []
    tempo = 500_000
    previous_tick = 0
    now = 0.0

    for tick, track_index, msg in timeline:
        now += mido.tick2second(tick - previous_tick, mid.ticks_per_beat, tempo)
        previous_tick = tick
        if msg.type == 'set_tempo':
            tempo = msg.tempo
            continue
        key = (track_index, msg.channel, msg.note)
        previous_note = active.pop(key, None)
        if previous_note is not None:
            start, velocity = previous_note
            raw.append(ScoreEvent(msg.note, start, now - start, velocity))
        if msg.type == 'note_on' and msg.velocity > 0:
            active[key] = (now, msg.velocity)

    for (_, _, note), (start, velocity) in active.items():
        raw.append(ScoreEvent(note, start, 0.5, velocity))

    if not raw:
        raise ValueError('MIDI 中没有可用音符（已忽略打击乐通道）')

    track_end = now + mido.tick2second(end_tick - previous_tick, mid.ticks_per_beat, tempo)
    length = max(track_end, max(event.start + event.duration for event in raw))
    return Score(name=path.stem, events=_group_chords(raw), length=length)


def _group_chords(events: list[ScoreEvent]) -> list[ScoreEvent]:
    events.sort(key=lambda event: event.start)
    groups: list[ScoreEvent] = []
    first = 0
    while first < len(events):
        end = first + 1
        while end < len(events) and events[end].start - events[first].start < _CHORD_WINDOW:
            end += 1
        primary, *alternatives = sorted(events[first:end], key=lambda event: -event.pitch)
        primary.alternatives = tuple(alternatives)
        groups.append(primary)
        first = end
    return groups


def load_txt(path: str | Path) -> Score:
    path = Path(path)
    text = path.read_text(encoding='utf-8-sig')
    title = path.stem
    bpm = 100
    body: list[tuple[int, str]] = []

    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.split('//', 1)[0].strip()
        if not line:
            continue
        header, equals, value = line.partition('=')
        header = header.strip().upper()
        if equals and header == 'TITLE':
            title = value.strip() or title
        elif equals and header == 'BPM':
            try:
                tempo_value = float(value.strip())
            except ValueError as exc:
                raise ValueError(f'第 {line_number} 行的 BPM 必须是数字') from exc
            if not isfinite(tempo_value):
                raise ValueError(f'第 {line_number} 行的 BPM 必须是有限数字')
            bpm = round(max(20, min(300, tempo_value)))
        elif re.match(r'@format(?:\s|=|$)', line, re.IGNORECASE):
            raise ValueError(f'第 {line_number} 行声明了暂不支持的 @format 曲谱，请导入 MIDI 或基本简谱 TXT')
        elif '键位标记' in line or re.match(r'小节\s+\d+\s*\(', line):
            raise ValueError(f'第 {line_number} 行识别到键位文本谱，请转换为 MIDI 或基本简谱 TXT 后导入')
        else:
            body.append((line_number, line))

    beat = 60.0 / bpm
    cursor = 0.0
    events: list[ScoreEvent] = []
    last_event: ScoreEvent | None = None

    for line_number, line in body:
        for token in line.replace('|', ' ').split():
            if set(token) == {'-'}:
                if cursor == 0:
                    raise ValueError(f'第 {line_number} 行的 {token!r} 前需要有音符或休止符')
                extension = len(token) * beat
                if last_event is not None:
                    last_event.duration += extension
                cursor += extension
                continue
            match = _TXT_TOKEN.fullmatch(token)
            if not match:
                raise ValueError(f'第 {line_number} 行含有无法识别的记号 {token!r}')
            prefix, symbol, trailing = match.groups()
            prefix, symbol = prefix.lower(), symbol.lower()
            beats = 1 + len(trailing)
            if symbol == '0':
                cursor += beats * beat
                last_event = None
                continue
            is_high_do = symbol in ('8', 'i')
            degree = 1 if is_high_do else int(symbol)
            octave = prefix.count('^') - prefix.count('b') + (1 if is_high_do else 0)
            pitch = 60 + octave * 12 + _NOTE_OFFSETS[degree - 1] + (1 if '#' in prefix else 0)
            event = ScoreEvent(pitch=pitch, start=cursor, duration=beats * beat)
            events.append(event)
            cursor += beats * beat
            last_event = event

    if not events:
        raise ValueError('TXT 中没有可解析的音符（支持 1-7、8/i、b 降调、^ 升调、# 半音、0 休止、- 延长）')

    return Score(name=title, events=events, length=cursor)
