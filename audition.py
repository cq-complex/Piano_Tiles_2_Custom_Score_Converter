"""Inclusive playlist excerpt coordinates on the flattened main score."""
from bisect import bisect_right
from itertools import accumulate
import math

from score_parameters import effective_score_bpms


def audition_tokens(tokens):
    return [note for token in tokens for note in (token.children or (token,))]


def audition_sections(data):
    from score_editor import parse_score

    return [audition_tokens(parse_score(section['scores'][0])) for section in data['musics']]


def audition_windows(data, selection=None, sections=None):
    """Return (section, local start tick, local end tick, BPM) for each part."""
    if selection is None:
        selection = data['audition']
    if sections is None:
        sections = audition_sections(data)
    anchors = []
    for name, label in (('start', '起点'), ('end', '终点')):
        pair = selection.get(name)
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(type(value) is not int for value in pair)):
            raise ValueError(f'歌单片段{label}需要两个整数')
        section, index = pair
        if not 0 <= section < len(sections) or not 0 <= index < len(sections[section]):
            raise ValueError(f'歌单片段{label}超出该段的音符范围')
        anchors.append((section, index))
    (first, start), (last, end) = anchors
    if anchors[0] > anchors[1]:
        raise ValueError('歌单片段的终点不能早于起点')
    windows = []
    bpms = effective_score_bpms(data)
    for index in range(first, last + 1):
        tokens = sections[index]
        bpm = float(bpms[index])
        if not math.isfinite(bpm) or bpm <= 0:
            raise ValueError('歌单片段的 BPM 必须为有限正数')
        left = sum(token.ticks for token in tokens[:start]) if index == first else 0
        selected_through = tokens[:end + 1] if index == last else tokens
        right = sum(token.ticks for token in selected_through)
        windows.append((index, left, right, bpm))
    return windows


def remap_audition(data, section_index, old_tokens, first, last, replacement):
    """Translate endpoints through a duration-preserving edit."""
    prefix = sum(len(token.children) or 1 for token in old_tokens[:first])
    old = audition_tokens(old_tokens[first:last + 1])
    new = audition_tokens(replacement)
    old_edges = [0, *accumulate(token.ticks for token in old)]
    new_ends = list(accumulate(token.ticks for token in new))
    for name in ('start', 'end'):
        anchor = data.get('audition', {}).get(name)
        if not anchor or anchor[0] != section_index:
            continue
        relative = anchor[1] - prefix
        if relative >= len(old):
            anchor[1] += len(new) - len(old)
        elif relative >= 0:
            tick = old_edges[relative] if name == 'start' else old_edges[relative + 1] - 1
            anchor[1] = prefix + bisect_right(new_ends, tick)
