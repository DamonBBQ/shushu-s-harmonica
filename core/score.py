from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import mido

_NOTE_OFFSETS = (0, 2, 4, 5, 7, 9, 11)
_TXT_TOKEN = re.compile(r'([b#^]*)([0-9]|i)(-*)', re.IGNORECASE)


@dataclass
class ScoreEvent:
    pitch: int
    start: float
    duration: float
    velocity: int = 80


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

    active: dict[tuple[int, int], tuple[float, int]] = {}
    raw: list[ScoreEvent] = []
    now = 0.0

    for msg in mid:
        now += msg.time
        if msg.type not in ('note_on', 'note_off') or msg.channel == 9:
            continue
        key = (msg.channel, msg.note)
        if msg.type == 'note_on' and msg.velocity > 0:
            if key in active:
                start, velocity = active.pop(key)
                raw.append(ScoreEvent(msg.note, start, max(now - start, 0.02), velocity))
            active[key] = (now, msg.velocity)
        elif key in active:
            start, velocity = active.pop(key)
            raw.append(ScoreEvent(msg.note, start, max(now - start, 0.02), velocity))

    for (_, note), (start, velocity) in active.items():
        raw.append(ScoreEvent(note, start, 0.5, velocity))

    if not raw:
        raise ValueError('MIDI 中没有可用音符（已忽略打击乐通道）')

    mono = _to_monophonic(raw)
    length = max(e.start + e.duration for e in mono)
    return Score(name=path.stem, events=mono, length=length)


def _to_monophonic(events: list[ScoreEvent]) -> list[ScoreEvent]:
    events.sort(key=lambda e: (e.start, -e.pitch))
    result: list[ScoreEvent] = []
    prev: ScoreEvent | None = None
    i = 0
    while i < len(events):
        j = i
        cluster_start = events[i].start
        while j + 1 < len(events) and events[j + 1].start - cluster_start < 0.03:
            j += 1
        note = max(events[i:j + 1], key=lambda e: e.pitch)
        if prev is not None and prev.start + prev.duration > note.start:
            prev.duration = max(note.start - prev.start, 0.02)
        result.append(note)
        prev = note
        i = j + 1
    return result


def load_txt(path: str | Path) -> Score:
    path = Path(path)
    text = path.read_text(encoding='utf-8-sig')
    title = path.stem
    bpm = 100
    grid = 1
    body: list[str] = []

    for raw in text.splitlines():
        line = raw.split('//', 1)[0].strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith('TITLE'):
            title = line.split('=', 1)[-1].strip() or title
        elif upper.startswith('BPM'):
            try:
                bpm = int(round(float(line.split('=', 1)[-1].strip())))
            except ValueError:
                pass
        elif upper.startswith('GRID'):
            try:
                grid = int(line.split('=', 1)[-1].strip())
            except ValueError:
                pass
        else:
            body.append(line)

    bpm = max(20, min(300, bpm))
    grid = max(1, min(16, grid))
    beat = 60.0 / bpm
    unit = beat / grid
    cursor = 0.0
    events: list[ScoreEvent] = []
    last_kind = ''
    last_event: ScoreEvent | None = None

    for line in body:
        for token in line.replace('|', ' ').split():
            if not token:
                continue
            if set(token) == {'-'}:
                count = len(token)
                if last_kind == 'note' and last_event is not None:
                    last_event.duration += count * unit
                if last_kind in ('note', 'rest'):
                    cursor += count * unit
                continue
            match = _TXT_TOKEN.fullmatch(token)
            if not match:
                continue
            prefix, symbol, trailing = match.group(1).lower(), match.group(2).lower(), match.group(3)
            units = 1 + len(trailing)
            if symbol == '0':
                cursor += units * unit
                last_kind = 'rest'
                last_event = None
                continue
            is_high_do = symbol in ('8', 'i')
            degree = 1 if is_high_do else int(symbol)
            octave = prefix.count('^') - prefix.count('b') + (1 if is_high_do else 0)
            pitch = 60 + octave * 12 + _NOTE_OFFSETS[degree - 1] + (1 if '#' in prefix else 0)
            event = ScoreEvent(pitch=pitch, start=cursor, duration=units * unit)
            events.append(event)
            cursor += units * unit
            last_kind = 'note'
            last_event = event

    if not events:
        raise ValueError('TXT 中没有可解析的音符（支持 1-7、8/i、b 降调、^ 升调、# 半音、0 休止、- 延长、GRID 网格）')

    events.sort(key=lambda e: e.start)
    return Score(name=title, events=events, length=cursor)