from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import inf, isfinite
from typing import Iterable

from .score import ScoreEvent

KEY_LABELS = ('Z', 'X', 'C', 'V', 'B', 'N', 'M', ',')
KEY_SCANCODES = (0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33)

BASE_OCTAVE = 4
RANGE_LOW = 48
RANGE_HIGH = 85
PREP_MS = 12
MIN_HOLD_MS = 40
DEFAULT_GAP_MS = 20

_NATURAL_KEY = {0: 0, 2: 1, 4: 2, 5: 3, 7: 4, 9: 5, 11: 6}
_SHARP_BASE = {1: 0, 3: 2, 6: 5, 8: 7, 10: 9}


@dataclass(frozen=True)
class PlayNote:
    start: float
    duration: float
    key_index: int
    shift: int = 0
    sharp: bool = False


def describe_action(note: PlayNote) -> str:
    mods = []
    if note.shift < 0:
        mods.append('降八度')
    elif note.shift > 0:
        mods.append('升八度')
    if note.sharp:
        mods.append('升半音')
    label = KEY_LABELS[note.key_index]
    return label + ('（' + '+'.join(mods) + '）' if mods else '')


def auto_transpose(pitches: Iterable[int]) -> int:
    counts = Counter(pitches)
    best_t, best_hits = 0, -1
    for t in range(-30, 31):
        hits = sum(counts[pitch] for pitch in range(RANGE_LOW - t, RANGE_HIGH - t + 1))
        if hits > best_hits or (hits == best_hits and abs(t) < abs(best_t)):
            best_t, best_hits = t, hits
    return best_t


def _fold(pitch: int) -> int:
    if pitch < RANGE_LOW:
        return RANGE_LOW + (pitch - RANGE_LOW) % 12
    if pitch > RANGE_HIGH:
        return RANGE_HIGH - (RANGE_HIGH - pitch) % 12
    return pitch


def to_action(pitch: int) -> tuple[int, int, bool]:
    if not RANGE_LOW <= pitch <= RANGE_HIGH:
        raise ValueError(f'MIDI 音高 {pitch} 超出游戏音域 {RANGE_LOW}～{RANGE_HIGH}')
    pc = pitch % 12
    octave = pitch // 12 - 1
    shift = octave - BASE_OCTAVE
    # 最高音区只支持 do / #do：C6 = 逗号+右键，C#6 再按中键
    if pc in (0, 1) and octave > BASE_OCTAVE:
        return 7, 1 if octave > BASE_OCTAVE + 1 else 0, pc == 1
    if pc in _SHARP_BASE:
        return _NATURAL_KEY[_SHARP_BASE[pc]], shift, True
    return _NATURAL_KEY[pc], shift, False


def build_play_notes(events: Iterable[ScoreEvent], transpose: int = 0,
                     auto_fit: bool = True, *, speed: float = 1.0) -> tuple[list[PlayNote], int, int]:
    if not isfinite(speed) or speed <= 0:
        raise ValueError('播放速度必须为有限的正数')

    groups = []
    for event in events:
        candidates = (event, *event.alternatives)
        groups.append((min(candidate.start for candidate in candidates), candidates))
    groups.sort(key=lambda group: group[0])
    if not groups:
        return [], 0, 0

    min_span = (PREP_MS + DEFAULT_GAP_MS + MIN_HOLD_MS) / 1000.0
    playable: list[tuple[ScoreEvent, float]] = []
    for index, (_, candidates) in enumerate(groups):
        next_start = groups[index + 1][0] if index + 1 < len(groups) else inf
        for candidate in candidates:
            duration = min(candidate.duration, next_start - candidate.start)
            # MIDI 秒数相减可能让刚好 72ms 的音略低于边界。
            if duration / speed + 1e-9 >= min_span:
                playable.append((candidate, duration))
                break

    applied = transpose
    if auto_fit:
        applied += auto_transpose(event.pitch + transpose for event, _ in playable)

    notes = []
    for event, duration in playable:
        key, shift, sharp = to_action(_fold(event.pitch + applied))
        notes.append(PlayNote(event.start, duration, key, shift, sharp))
    return notes, applied, len(groups) - len(notes)
