"""Absolute score positions and lossless movement of an existing section cut."""
from bisect import bisect_left, bisect_right
from copy import deepcopy
from itertools import accumulate

from notation_keys import local_key_events
from score_editor import inspect_score, parse_score, normal_ticks
from score_splitting import slice_score


def section_boundaries(data):
    lengths = [max(sum(t.ticks for t in inspect_score(line)[0]) for line in part["scores"])
               for part in data["musics"]]
    return [0, *accumulate(lengths)]


def main_edges(part):
    tokens = [child for top in inspect_score(part["scores"][0])[0] for child in top.children or (top,)]
    return [0, *accumulate(t.ticks for t in tokens)]


def audition_positions(data, bounds):
    """Return the absolute inclusive range as a pair of time edges."""
    result = {}
    selection = data.get("audition")
    if not isinstance(selection, dict):
        return result
    for name in ("start", "end"):
        coordinates = selection.get(name)
        if (not isinstance(coordinates, (tuple, list)) or len(coordinates) != 2
                or any(type(value) is not int for value in coordinates)):
            continue
        section, note = coordinates
        if not 0 <= section < len(data["musics"]):
            continue
        edges = main_edges(data["musics"][section])
        if 0 <= note < len(edges) - 1:
            result[name] = bounds[section] + edges[note + (name == "end")]
    return result


def main_coordinate(data, bounds, tick, end=False):
    """Map a time to the containing main-track item; end uses its left limit."""
    if end and tick > 0:
        section = bisect_left(bounds, tick) - 1
    else:
        section = bisect_right(bounds, tick) - 1
    section = max(0, min(section, len(data["musics"]) - 1))
    local = tick - bounds[section]
    edges = main_edges(data["musics"][section])
    if not 0 <= local <= edges[-1] or len(edges) < 2:
        raise ValueError("此位置没有可用的主旋律音符。")
    index = (bisect_left(edges, local) if end else bisect_right(edges, local)) - 1
    return [section, max(0, min(index, len(edges) - 2))]


def move_section_cut(data, boundary, tick, notation_keys=None, measure_ticks=()):
    """Move one cut without regenerating the MIDI or changing section settings."""
    bounds = section_boundaries(data)
    if not 1 <= boundary < len(bounds) - 1:
        raise ValueError("不存在该分段点。")
    if not bounds[boundary - 1] < tick < bounds[boundary + 1]:
        raise ValueError("分段点必须位于前后两个分段点之间，且不能位于曲首或曲尾。")
    if tick == bounds[boundary]:
        return data, notation_keys
    pair = data["musics"][boundary - 1:boundary + 1]
    if len(pair[0]["scores"]) != len(pair[1]["scores"]) or any(
            pair[0].get(key) != pair[1].get(key) for key in ("instruments", "alternatives")):
        raise ValueError("相邻两段的音轨或配器设置不同，移动分段会改变音轨含义。")
    origin = bounds[boundary - 1]
    midpoint = bounds[boundary] - origin
    length = bounds[boundary + 1] - origin
    cut = tick - origin
    left_min, right_min = (normal_ticks(part["baseBeats"]) for part in pair)
    new_lines = [[], []]

    for layer, (first, second) in enumerate(zip(*(part["scores"] for part in pair))):
        barlines = [at - origin for at in measure_ticks if origin < at < origin + length]
        source = []
        cursor = 0
        for index, line in enumerate((first, second)):
            tokens = parse_score(line)
            previous = 0
            for token in tokens:
                if ";" in line[previous:token.start]:
                    barlines.append(cursor)
                previous = token.end
                source.append(token.text)
                cursor += token.ticks
            stop = midpoint if index == 0 else length
            if cursor < stop:
                from notation_model import length_letters
                from score_editor import REST_SIZES
                source.append(length_letters(stop - cursor, REST_SIZES))
                cursor = stop
        line = ",".join(source)
        cursor = 0
        left_neighbor = right_neighbor = None
        for token in parse_score(line):
            end = cursor + token.ticks
            if cursor < cut < end and (token.children or "~" in token.text or "@" in token.text):
                raise ValueError("分段点不能切开音符组、琶音或连音。")
            if cursor < cut <= end:
                left_neighbor = token.kind, cut - cursor
            if cursor <= cut < end:
                right_neighbor = token.kind, end - cut
            cursor = end
        if layer == 0 and (not left_neighbor or not right_neighbor
                           or left_neighbor[0] != "note" or right_neighbor[0] != "note"
                           or left_neighbor[1] < left_min or right_neighbor[1] < right_min):
            raise ValueError("分段点两侧必须紧邻正常长度的主旋律音符，请避开休止符和短音符。")
        barlines = sorted(set(barlines))
        new_lines[0].append(slice_score(line, 0, cut, barlines)[0])
        new_lines[1].append(slice_score(line, cut, length, barlines)[0])

    result = deepcopy(data)
    for i, lines in enumerate(new_lines, boundary - 1):
        result["musics"][i]["scores"] = lines
    updated_bounds = bounds.copy()
    updated_bounds[boundary] = tick
    for name, position in audition_positions(data, bounds).items():
        result["audition"][name] = main_coordinate(result, updated_bounds, position, end=name == "end")
    keys = deepcopy(notation_keys)
    if keys is not None:
        for layer in range(len(new_lines[0])):
            events = dict(keys[boundary - 1][layer])
            events.update({midpoint + at: key for at, key in keys[boundary][layer]})
            timeline = sorted(events.items())
            keys[boundary - 1][layer] = local_key_events(timeline, 0, cut)
            keys[boundary][layer] = local_key_events(timeline, cut, length)
    return result, keys
