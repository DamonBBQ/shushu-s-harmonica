from __future__ import annotations

from dataclasses import dataclass
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
    pitches = list(pitches)
    best_t, best_hits = 0, -1
    for t in range(-30, 31):
        hits = sum(1 for p in pitches if RANGE_LOW <= p + t <= RANGE_HIGH)
        if hits > best_hits or (hits == best_hits and abs(t) < abs(best_t)):
            best_t, best_hits = t, hits
    return best_t


def _fold(pitch: int) -> int:
    while pitch < RANGE_LOW:
        pitch += 12
    while pitch > RANGE_HIGH:
        pitch -= 12
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
                     auto_fit: bool = True) -> tuple[list[PlayNote], int, int]:
    events = list(events)
    if not events:
        return [], 0, 0
    applied = transpose + (auto_transpose(e.pitch for e in events) if auto_fit else 0)
    min_span = (PREP_MS + DEFAULT_GAP_MS + MIN_HOLD_MS) / 1000.0
    notes: list[PlayNote] = []
    dropped = 0
    for event in events:
        if event.duration < min_span:
            dropped += 1
            continue
        key, shift, sharp = to_action(_fold(event.pitch + applied))
        notes.append(PlayNote(event.start, event.duration, key, shift, sharp))
    notes.sort(key=lambda n: n.start)
    return notes, applied, dropped