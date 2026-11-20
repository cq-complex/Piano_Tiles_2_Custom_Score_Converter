"""Four-lane Piano Tiles score layout, independent of the drawing surface."""
from bisect import bisect_left
from dataclasses import dataclass, field
from math import floor, isfinite
from random import Random

from score_editor import accompaniment_activity, find_issues, inspect_score, overlaps_accompaniment
from score_parameters import effective_score_bpms

COLORS = {"black": "#20232b", "hold": "#2165d2", "slide": "#e75099",
          "side": "#8bd3fa", "burst": "#21a365", "warning": "#f4cb42"}
MIDDLE = (1, 2)
EDGES = (0, 3)


@dataclass
class Block:
    kind: str
    start: float
    length: float
    token: str
    section: int
    tick: int
    count: int = 0
    sides: list = field(default_factory=list)
    entry: int = 0
    exit: int = 0
    lanes: tuple = ()
    path: tuple = ()
    warning: str = ""


@dataclass(frozen=True)
class Section:
    start: float
    end: float
    ticks: int
    bpm: float
    minimum: float


def folded_path(start, length):
    """Travel two lane intervals per normal note, bouncing at outer lanes."""
    travel = 2 * length
    if travel <= 0:
        return ((start, 0.0),)
    direction = 1 if start == 0 else -1
    path = [(start, 0.0)]
    for distance in range(3, int(travel) + 1, 3):
        if distance < travel:
            lane = 3 if (distance // 3) % 2 else 0
            path.append((lane if start == 0 else 3 - lane, distance / travel))
    phase = travel % 6
    displacement = min(phase, 6 - phase)
    path.append((start + direction * displacement, 1.0))
    return tuple(path)


def choices(block, double_pair):
    kind = block.kind
    if kind in ("3", "head"):
        for entry, exit in (MIDDLE, MIDDLE[::-1]):
            yield entry, exit, MIDDLE, ()
    elif kind == "5":
        for start in (double_pair, 1 - double_pair):
            pair = (start, start + 2)
            for entry, exit in (pair, pair[::-1]):
                yield entry, exit, pair, ()
    elif kind in ("7", "8"):
        for start in EDGES:
            path = ((start, 0.0), (3 - start, 1.0)) if kind == "7" else folded_path(start, block.length)
            yield start, floor(path[-1][0] + 0.5), (start,), path
    else:
        for lane in range(4):
            yield lane, lane, (lane,), ()


def place_blocks(blocks, rng=None):
    """Avoid collisions first, then favor shuffled lanes for free tiles."""
    if rng is None:
        rng = Random()
    shuffled_lanes = []
    previous_preference = None
    layers = []
    previous = {(): (0, 0, None, None)}
    double_pair = 1
    prior = None
    for block in blocks:
        adjacent = prior is not None and abs(prior.start + prior.length - block.start) <= 1e-8
        if not adjacent:
            double_pair = 1
        double_pair = 1 - double_pair if block.kind == "5" else 1
        preferred = None
        if block.kind not in ("3", "head", "5", "7", "8"):
            # Keep the shuffled bag across silence. Rare repeats after a gap
            # are allowed without letting isolated notes favor any one lane.
            if not adjacent and previous_preference is not None and rng.randrange(4) == 0:
                preferred = previous_preference
            else:
                if not shuffled_lanes:
                    shuffled_lanes = list(range(4))
                    rng.shuffle(shuffled_lanes)
                preferred = shuffled_lanes.pop()
            previous_preference = preferred
        current = {}
        for option in choices(block, double_pair):
            entry, exit, lanes, _ = option
            incoming = lanes if block.kind == "5" else (entry,)
            outgoing = lanes if block.kind == "5" else (exit,)
            old_mask, (collisions, penalty, _, _) = min(
                previous.items(), key=lambda item: (
                    item[1][0] + int(adjacent and bool(set(item[0]).intersection(incoming))),
                    item[1][1]))
            collision = int(adjacent and bool(set(old_mask).intersection(incoming)))
            if block.kind == "5":
                bias = int(lanes[0] != double_pair)
            else:
                bias = int(entry != preferred) if len(lanes) == 1 and not option[3] else 0
            new = collisions + collision, penalty + bias, old_mask, option
            if outgoing not in current or new[:2] < current[outgoing][:2]:
                current[outgoing] = new
        layers.append(current)
        previous, prior = current, block
    if not layers:
        return 0
    last = min(previous, key=lambda mask: previous[mask][:2])
    conflicts = previous[last][0]
    for block, layer in zip(reversed(blocks), reversed(layers)):
        _, _, last, option = layer[last]
        block.entry, block.exit, block.lanes, block.path = option
    return conflicts


def model_score(data, rng=None):
    sections, blocks, warnings = [], [], []
    issues = {}
    for issue in find_issues(data):
        if issue["layer"] == 0 and not issue.get("optional") and not issue.get("syntax"):
            for index in range(issue["first"], issue["last"] + 1):
                issues.setdefault((issue["section"], index), []).append(issue["reason"])
    base = 0.0
    for i, (part, bpm) in enumerate(zip(data["musics"], effective_score_bpms(data))):
        minimum = part["baseBeats"] * 32
        if not isfinite(minimum) or minimum <= 0:
            raise ValueError("基础拍长必须大于零。")
        tracks = [inspect_score(line) for line in part["scores"]]
        if any(errors for _, errors in tracks):
            warnings.append(f"第 {i + 1} 段有语法错误，无法识别的项目已跳过。")
        notes = tracks[0][0]
        activity = accompaniment_activity(part) if not any(errors for _, errors in tracks[1:]) else []
        accompaniment = []
        for tokens, errors in tracks[1:]:
            events = []
            cursor = 0
            for token in tokens:
                for note in token.children or (token,):
                    if note.kind == "note":
                        events.append((cursor, note.ticks))
                    cursor += note.ticks
            if not errors:
                accompaniment.append(([at for at, _ in events], events))
        cursor = 0
        for index, token in enumerate(notes):
            tick = cursor
            cursor += token.ticks
            if not token.ticks:
                continue
            size = token.ticks / minimum
            start = base + tick / minimum
            kind = token.kind
            if kind == "rest" and not overlaps_accompaniment(activity, tick, cursor):
                continue
            if kind in ("note", "rest", "invalid", "1", "2"):
                kind = "hold" if size > 1 and kind != "2" else "black"
            warning = "；".join(issues.get((i, index), ()))
            if not warning and token.ticks < minimum:
                warning = f"方块时长 {token.ticks:g}P 小于最小正常长度 {minimum:g}P"
            if kind == "10" and token.children:
                for j, child in enumerate(token.children):
                    blocks.append(Block("head" if j == 0 else "burst", base + tick / minimum,
                                        child.ticks / minimum, child.text, i, tick,
                                        warning=warning or (f"方块时长 {child.ticks:g}P 小于最小正常长度 {minimum:g}P"
                                                            if child.ticks < minimum else "")))
                    tick += child.ticks
                continue
            block = Block(kind, start, size, token.text, i, tick, len(token.children), warning=warning)
            if kind == "9":
                best = []
                for times, events in accompaniment:
                    lo, hi = bisect_left(times, tick), bisect_left(times, cursor)
                    candidates = [(at, length) for at, length in events[lo:hi] if at + length <= cursor]
                    if any(length != minimum for _, length in candidates):
                        continue
                    if len(candidates) > len(best):
                        best = candidates
                block.sides = [((at - tick) / minimum, length / minimum) for at, length in best]
            blocks.append(block)
        ticks = max(sum(token.ticks for token in tokens) for tokens, _ in tracks)
        sections.append(Section(base, base + ticks / minimum, ticks, bpm, minimum))
        base = sections[-1].end
    conflicts = place_blocks(blocks, rng)
    if conflicts:
        warnings.append(f"有 {conflicts} 处固定列相接，无法同时满足相邻不同列。")
    return sections, blocks, warnings
