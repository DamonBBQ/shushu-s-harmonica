#!/usr/bin/env python3
"""Build GRID=4 playable scores from data/tabs.json.

The source images only encode pitch and octave, not rhythm. This script
reconstructs rhythm from the grouping (spaces / commas / pipes / line ends):
each group becomes a beat-aligned cell of 16th notes, long groups use faster
subdivisions, and phrase marks become breath rests.

Run from the project root:
    .venv/bin/python tools/build_from_image_tabs.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.mapping import build_play_notes
from core.score import load_txt

TABS_FILE = ROOT / 'data' / 'tabs.json'
BPM_FILE = ROOT / 'data' / 'bpm.json'
OUT_DIR = ROOT / 'scores' / '图片曲谱'
CHART_FILE = ROOT / 'docs' / '图片曲谱_键位版.md'

GRID = 4
DEGREE = {'Z': 1, 'X': 2, 'C': 3, 'V': 4, 'B': 5, 'N': 6, 'M': 7}
FALLBACK_BPM = {
    '兰亭序': 76, '失眠': 72, '等你的季节': 72, '发如雪': 72, '断了的弦': 84,
    '暗号': 90, 'call of silence': 76, '搁浅': 66, '说了再见': 72,
    '最长的电影': 64, '诀别书': 72, '于是': 80, '雨爱': 72, '烟花易冷': 76,
    '反方向的钟': 96, '青花瓷': 108, '花海': 76, '太聪明': 96, '最后一页': 72,
    '鸟之诗': 82, '坏女孩': 100, '雨下一整晚': 88, '明明就': 72,
    '这么可爱的鼠鼠为啥不能养': 100, '你是我的风景': 72, '情歌-梁静茹': 64,
    '夏天的风': 72, '如果爱忘了': 70, '海阔天空': 72, '我怀念的': 68,
    '一直很安静': 70, 'wonderful u': 60,
}


def load_bpm() -> dict[str, int]:
    bpm = dict(FALLBACK_BPM)
    if BPM_FILE.is_file():
        raw = json.loads(BPM_FILE.read_text(encoding='utf-8'))
        for title, item in raw.items():
            if isinstance(item, dict) and 'bpm' in item:
                bpm[title] = int(item['bpm'])
    return bpm


def jianpu(note: dict) -> str:
    token = str(DEGREE[note['letter']])
    if note['oct'] == 1:
        return '^' + token
    if note['oct'] == -1:
        return 'b' + token
    return token


def key_label(note: dict) -> str:
    suffix = '↑' if note['oct'] == 1 else ('↓' if note['oct'] == -1 else '')
    return note['letter'] + suffix


def plan_group(notes: list[dict], bpm: int, phrase_end: bool = False) -> tuple[list[int], int, int]:
    """Return (durations, trailing rest, extra units added to the group)."""
    n = len(notes)
    if n == 1:
        unit = 4
    elif n == 2:
        unit = 2 if bpm >= 90 else 4
    elif n <= 8:
        unit = 2
    else:
        unit = 1
    raw = n * unit
    total = max(4, ((raw + 3) // 4) * 4)
    durations = [unit] * n
    durations[-1] += total - raw
    extension = 0
    if phrase_end and n >= 4:
        # Shorten the two approach notes and hold the final note: a small
        # cadence instead of a flat row of equal eighths.
        add = min(2, 8 - durations[-1])
        take = add
        index = n - 2
        while take > 0 and index >= 0:
            moved = min(durations[index] - 1, take)
            durations[index] -= moved
            take -= moved
            index -= 1
        durations[-1] += add - take
        if take > 0:
            # Dense runs have no unit to borrow; lengthen the final note and
            # shorten the following breath instead.
            extra = max(0, min(4, 8) - durations[-1])
            durations[-1] += extra
            extension += extra
    trailing_rest = 0
    if durations[-1] > 8:
        trailing_rest = durations[-1] - 8
        durations[-1] = 8
    return durations, trailing_rest, extension


def rest_token(units: int) -> str:
    return '0' + '-' * (units - 1)


def note_token(note: dict, units: int) -> str:
    return jianpu(note) + '-' * (units - 1)


def build_lines(song: dict, bpm: int) -> tuple[list[list[str]], list[list[dict]]]:
    """Return rendered token lines and the original note groups."""
    rendered: list[list[str]] = []
    source_lines: list[list[dict]] = []
    line_count = len(song['lines'])
    for line_index, line in enumerate(song['lines']):
        groups = line['groups']
        if not groups:
            continue
        parts: list[str] = []
        source_lines.append(groups)
        extension = 0
        for group_index, group in enumerate(groups):
            notes = group['notes']
            durations, trailing_rest, added = plan_group(
                notes, bpm, phrase_end=group_index == len(groups) - 1)
            extension += added
            for note, units in zip(notes, durations):
                parts.append(note_token(note, units))
            if trailing_rest:
                parts.append(rest_token(trailing_rest))
            if group_index >= len(groups) - 1:
                continue
            sep = group.get('sep_after')
            if sep == ',':
                parts.append(',')
                parts.append(rest_token(2))
            elif sep == '|':
                parts.append('|')
                parts.append(rest_token(4))
        end_rest = 8 if (line_index % 4 == 3 or line_index == line_count - 1) else 4
        end_rest = max(1, end_rest - extension)
        parts.append(rest_token(end_rest))
        rendered.append(parts)
    return rendered, source_lines


def render_score(title: str, bpm: int, rendered: list[list[str]]) -> str:
    lines = [
        f'TITLE={title}',
        f'BPM={bpm}',
        f'GRID={GRID}',
        '// 转录自社区图片曲谱；原图只有音高，节奏按分句重建，可在界面调速',
        '',
    ]
    for parts in rendered:
        lines.append(' '.join(parts))
    return '\n'.join(lines) + '\n'


def render_chart(songs: list[dict], bpm_map: dict[str, int]) -> str:
    out = [
        '# 图片曲谱键位版',
        '',
        '从四张社区单音曲谱图转录的 32 首曲目。原图只标音高和换气位置，没有时值；',
        '这里保留游戏键位，红色高音记作 `↑`，绿色低音记作 `↓`。',
        '可直接照着在游戏里弹；带节奏的完整版本见 `scores/图片曲谱/` 下的同名 TXT。',
        '',
        '键位与音高：`Z`=do、`X`=re、`C`=mi、`V`=fa、`B`=sol、`N`=la、`M`=si。',
        '空格/逗号/竖线表示原谱的换气或乐句分隔。',
        '',
        '---',
        '',
    ]
    for song in songs:
        title = song['title']
        bpm = bpm_map.get(title, 80)
        notes = sum(len(g['notes']) for line in song['lines'] for g in line['groups'])
        out.append(f'## {title}')
        out.append('')
        out.append(f'`{bpm} BPM` · `{notes} 个音`')
        out.append('')
        for line in song['lines']:
            chunks = []
            groups = line['groups']
            for group_index, group in enumerate(groups):
                chunks.append(' '.join(key_label(n) for n in group['notes']))
                if group_index >= len(groups) - 1:
                    continue
                sep = group.get('sep_after')
                if sep == ',':
                    chunks.append('，')
                elif sep == '|':
                    chunks.append('｜')
            out.append('  '.join(chunks))
        out.append('')
    return '\n'.join(out)


def sanitize(title: str) -> str:
    bad = '\\/:*?"<>|'
    cleaned = ''.join('_' if ch in bad else ch for ch in title).strip()
    return cleaned or 'untitled'


def main() -> int:
    songs = json.loads(TABS_FILE.read_text(encoding='utf-8'))
    bpm_map = load_bpm()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CHART_FILE.parent.mkdir(parents=True, exist_ok=True)

    total_notes = 0
    total_dropped = 0
    summary = []
    for song in songs:
        title = song['title']
        bpm = bpm_map.get(title, 80)
        rendered, _ = build_lines(song, bpm)
        text = render_score(title, bpm, rendered)
        path = OUT_DIR / f'{sanitize(title)}.txt'
        path.write_text(text, encoding='utf-8')

        score = load_txt(path)
        play, applied, dropped = build_play_notes(score.events)
        expected = sum(len(g['notes']) for line in song['lines'] for g in line['groups'])
        total_notes += expected
        total_dropped += dropped
        summary.append((title, bpm, expected, len(score.events), score.length, applied, dropped))

    CHART_FILE.write_text(render_chart(songs, bpm_map), encoding='utf-8')

    print(f'生成 {len(songs)} 首曲谱 -> {OUT_DIR}')
    print(f'生成键位谱 -> {CHART_FILE}')
    print(f'转录音符总数 {total_notes}，播放时被丢弃 {total_dropped}')
    print()
    for title, bpm, expected, parsed, length, applied, dropped in summary:
        minutes, seconds = divmod(int(round(length)), 60)
        flag = '' if (parsed == expected and dropped == 0) else '  <-- CHECK'
        print(f'{title:24s} {bpm:3d} BPM  {expected:3d} 音  '
              f'{minutes}:{seconds:02d}  transpose={applied:+d}{flag}')
    return 0 if total_dropped == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())