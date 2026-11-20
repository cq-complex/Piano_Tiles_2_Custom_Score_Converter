"""Choose three balanced sections with sounding, normal-length ends at each cut."""
from bisect import bisect_left, bisect_right
import math

from score_editor import NOTE_FORM, SIZES, REST_SIZES, normal_ticks, parse_score


def slice_score(text, start, end, measures=()):
    """Clip a track; mark bar lines without cutting sounding notes for notation."""
    result, cursor = [], 0
    measure_set = set(measures)

    def append(value, at):
        if result:
            result.append(";" if at in measure_set else ",")
        result.append(value)

    for token in parse_score(text):
        stop = cursor + token.ticks
        left, right = max(cursor, start), min(stop, end)
        if left < right:
            note = NOTE_FORM.fullmatch(token.text)
            internal = ()
            if token.kind == "rest" and not note:
                internal = measures[bisect_right(measures, left):bisect_left(measures, right)]
            if left == cursor and right == stop and not internal:
                append(token.text, left)
            elif note:
                if "~" in note[1] or "@" in note[1]:
                    raise ValueError("分割点不能切开琶音或连音")
                letters = []
                remaining = right - left
                for letter, size in SIZES.items():
                    count, remaining = divmod(remaining, size)
                    letters.extend([letter] * count)
                append(note[1] + "[" + "".join(letters) + "]" + (note[3] or ""), left)
            elif token.kind == "rest":
                for edge in (*internal, right):
                    remaining = edge - left
                    for letter, size in REST_SIZES.items():
                        count, remaining = divmod(remaining, size)
                        for _ in range(count):
                            append(letter, left)
                            left += size
            else:
                raise ValueError("不能从特殊方块内部切分")
        cursor = stop
        if cursor >= end:
            break
    return "".join(result), (len(result) + 1) // 2


def choose_boundaries(scores, base_beats, split_points=None, measures=()):
    """Return integer P-unit boundaries and a plain-language explanation."""
    main = parse_score(scores[0])
    positions = [0]
    for token in main:
        positions.append(positions[-1] + token.ticks)
    total = positions[-1]
    if total < 3:
        raise ValueError("谱面至少需要三个最小时值才能分为三段")
    minimum = math.ceil(normal_ticks(base_beats))
    normal = [token.kind == "note" and token.ticks >= minimum for token in main]
    complete = {point for i, point in enumerate(positions[1:-1], 1)
                if normal[i - 1] and normal[i]}
    interiors = [(start + minimum, end - minimum)
                 for i, (start, end) in enumerate(zip(positions, positions[1:]))
                 if normal[i] and end - start >= 2 * minimum]

    # Keep each compound attack intact, including those on accompaniment.
    spans = []
    for line in scores:
        if "~" not in line and "@" not in line:
            continue
        cursor = 0
        for token in parse_score(line):
            stop = cursor + token.ticks
            if ("~" in token.text or "@" in token.text) and stop - cursor > 1:
                spans.append((cursor + 1, stop - 1))
            cursor = stop
    blocked = []
    for left, right in sorted(spans):
        if blocked and left <= blocked[-1][1] + 1:
            blocked[-1] = (blocked[-1][0], max(right, blocked[-1][1]))
        else:
            blocked.append((left, right))
    blocked_ends = [end for _, end in blocked]

    def inside_phrase(point):
        j = bisect_left(blocked_ends, point)
        return j < len(blocked) and blocked[j][0] <= point

    complete = {point for point in complete if not inside_phrase(point)}
    allowed_interiors = []
    for left, right in interiors:
        j = bisect_left(blocked_ends, left)
        while j < len(blocked) and blocked[j][0] <= right:
            start, stop = blocked[j]
            if left < start:
                allowed_interiors.append((left, start - 1))
            left = max(left, stop + 1)
            j += 1
        if left <= right:
            allowed_interiors.append((left, right))
    interiors = allowed_interiors

    if split_points is not None:
        try:
            points = tuple(float(value) for value in split_points)
            if len(points) != 2 or not all(math.isfinite(value) for value in points):
                raise ValueError
            cuts = [round(value * 32) for value in points]
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("请输入两个有效分割点，单位为四分音符拍") from exc
        if not 0 < cuts[0] < cuts[1] < total:
            raise ValueError(f"取整后必须满足 0 < 分割点1 < 分割点2 < 总拍长 {total / 32:g}")
        for i, cut in enumerate(cuts, 1):
            if inside_phrase(cut):
                raise ValueError(f"分割点 {i}（{cut / 32:g} 拍）位于琶音或连音内部，请移到片段外")
            if cut not in complete and not any(left <= cut <= right for left, right in interiors):
                raise ValueError(f"分割点 {i}（{cut / 32:g} 拍）两侧必须紧邻正常长度的主旋律音符，"
                                 "请避开休止符和短音符")
        if cuts[1] - cuts[0] < minimum:
            raise ValueError("两个分割点之间太短，会截出非正常长度的音符")
        count = sum(cut not in complete for cut in cuts)
        return [0, *cuts, total], ("自选分段" if not count else
                                  f"自选分段；{count} 个边界位于主旋律音符内部，将截分音符")

    targets = (total / 3, total * 2 / 3)
    # Prefer two intact boundaries. A long sounding note can supply an
    # interior cut if both pieces, including any middle piece, are normal.
    ranges = sorted([(p, p) for p in complete] + interiors)
    starts = [left for left, _ in ranges]
    ends = [right for _, right in ranges]
    intact = sorted(complete)
    barlines = set(measures) & complete
    preferred = sorted(barlines)
    probes = set(complete)
    # The optimal second cut lies at its target, the joint optimum when
    # spacing binds, or an endpoint (including a shifted first interval).
    joint = (total + minimum) / 2
    candidates = {round(targets[1]), math.floor(joint), math.ceil(joint)}
    for left, right in ranges:
        candidates.update((left, right, left + minimum, right + minimum))
    for value in candidates:
        i = bisect_right(starts, value) - 1
        if i >= 0:
            probes.add(min(value, ends[i]))
        if i + 1 < len(ranges):
            probes.add(starts[i + 1])

    best = None
    for second in probes:
        limit = second - minimum
        first_options = []
        for choices in (preferred, intact):
            last = bisect_right(choices, limit)
            if last:
                i = bisect_left(choices, targets[0], 0, last)
                first_options.extend(choices[j] for j in (i - 1, i) if 0 <= j < last)
        count = bisect_right(starts, limit)
        if count:
            wanted = min(round(targets[0]), limit)
            i = bisect_right(starts, wanted, 0, count) - 1
            if i >= 0:
                first_options.append(min(wanted, ends[i]))
            if i + 1 < count:
                first_options.append(starts[i + 1])
        for first in first_options:
            internal = int(first not in complete) + int(second not in complete)
            cost = (first - targets[0]) ** 2 + (second - targets[1]) ** 2
            candidate = (-sum(p in barlines for p in (first, second)), internal, cost, first, second)
            if best is None or candidate < best:
                best = candidate

    if best is None:
        raise ValueError("无法安全分为三段：找不到两个两侧均为正常主旋律音符的分割点。"
                         "分割点也不能切开琶音或连音。请尝试减小基础拍长、选择更连续的主音轨，或调整源谱。")
    bars, internal, _, first, second = best
    message = "自动分段：两个分割点两侧均为正常长度的主旋律音符"
    if bars:
        message += f"；其中 {-bars} 个位于小节边界"
    if internal:
        message += f"；完整音符边界不足，{internal} 个分割点位于长音符内部"
    return [0, first, second, total], message
