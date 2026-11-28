from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

from .midi_preprocess import (
    MidiAnalysisResult,
    MidiNoteEvent,
    MidiPreprocessOptions,
    preprocess_midi,
)

_NOTE_OFFSETS = (0, 2, 4, 5, 7, 9, 11)
_TXT_TOKEN = re.compile(r'([b#^]*)([0-8i])(-*)', re.IGNORECASE)


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
    source_sha256: str | None = None
    midi_analysis: MidiAnalysisResult | None = field(default=None, repr=False, compare=False)


def load_score(
    path: str | Path,
    *,
    midi_options: MidiPreprocessOptions | None = None,
    cache_dir: str | Path | None = None,
    source_sha256: str | None = None,
) -> Score:
    path = Path(path)
    if path.suffix.lower() in ('.mid', '.midi'):
        return load_midi(
            path, options=midi_options, cache_dir=cache_dir, source_sha256=source_sha256,
        )
    score = load_txt(path)
    score.source_sha256 = source_sha256
    return score


def load_midi(
    path: str | Path,
    *,
    options: MidiPreprocessOptions | None = None,
    cache_dir: str | Path | None = None,
    source_sha256: str | None = None,
) -> Score:
    path = Path(path)
    analysis = preprocess_midi(
        path, options, cache_dir=cache_dir, source_sha256=source_sha256,
    )
    return Score(
        name=path.stem,
        events=[_to_score_event(event) for event in analysis.events],
        length=analysis.length,
        source_sha256=analysis.source_sha256,
        midi_analysis=analysis,
    )


def _to_score_event(event: MidiNoteEvent) -> ScoreEvent:
    return ScoreEvent(
        pitch=event.pitch,
        start=event.start,
        duration=event.duration,
        velocity=event.velocity,
        alternatives=tuple(_to_score_event(item) for item in event.alternatives),
    )


def load_txt(path: str | Path) -> Score:
    path = Path(path)
    text = path.read_text(encoding='utf-8-sig')
    title = path.stem
    bpm = 100
    grid = 1
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
        elif equals and header == 'GRID':
            try:
                grid = max(1, min(16, int(value.strip())))
            except ValueError as exc:
                raise ValueError(f'第 {line_number} 行的 GRID 必须是整数') from exc
        elif re.match(r'@format(?:\s|=|$)', line, re.IGNORECASE):
            raise ValueError(f'第 {line_number} 行声明了暂不支持的 @format 曲谱，请导入 MIDI 或基本简谱 TXT')
        elif '键位标记' in line or re.match(r'小节\s+\d+\s*\(', line):
            raise ValueError(f'第 {line_number} 行识别到键位文本谱，请转换为 MIDI 或基本简谱 TXT 后导入')
        else:
            body.append((line_number, line))

    unit = 60.0 / bpm / grid
    cursor = 0.0
    events: list[ScoreEvent] = []
    last_event: ScoreEvent | None = None

    for line_number, line in body:
        for token in line.replace('|', ' ').split():
            if set(token) == {'-'}:
                if cursor == 0:
                    raise ValueError(f'第 {line_number} 行的 {token!r} 前需要有音符或休止符')
                extension = len(token) * unit
                if last_event is not None:
                    last_event.duration += extension
                cursor += extension
                continue
            match = _TXT_TOKEN.fullmatch(token)
            if not match:
                raise ValueError(f'第 {line_number} 行含有无法识别的记号 {token!r}')
            prefix, symbol, trailing = match.groups()
            prefix, symbol = prefix.lower(), symbol.lower()
            duration = (1 + len(trailing)) * unit
            if symbol == '0':
                cursor += duration
                last_event = None
                continue
            is_high_do = symbol in ('8', 'i')
            degree = 1 if is_high_do else int(symbol)
            octave = prefix.count('^') - prefix.count('b') + (1 if is_high_do else 0)
            pitch = 60 + octave * 12 + _NOTE_OFFSETS[degree - 1] + (1 if '#' in prefix else 0)
            event = ScoreEvent(pitch=pitch, start=cursor, duration=duration)
            events.append(event)
            cursor += duration
            last_event = event

    if not events:
        raise ValueError('TXT 中没有可解析的音符（支持 1-7、8/i、b 降调、^ 升调、# 半音、0 休止、- 延长）')

    return Score(name=title, events=events, length=cursor)
