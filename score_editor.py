"""Score grammar, block validation and an interactive correction window."""
from dataclasses import dataclass
from bisect import bisect_right
from fractions import Fraction
import copy
import json
import math
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from midi_preview import SystemMidiPlayer, build_preview_midi
from audition import remap_audition
from window_layout import size_window
from score_parameters import effective_score_bpms, export_parameters, is_arena_score

SIZES = dict(zip("HIJKLMNOP", (256, 128, 64, 32, 16, 8, 4, 2, 1)))
REST_SIZES = dict(zip("QRSTUVWXY", SIZES.values()))
BLOCK_NAMES = {"3": "狂戳块", "5": "双黑块", "6": "长条", "7": "长滑块",
               "8": "折叠滑块", "9": "伴奏块", "10": "爆裂块"}
PITCH = r"#?(?:[A-G]-[123]|[a-g][1-5]?)"
SILENT_SOUNDS = {"mute", "empty"}
SPECIAL_SOUNDS = SILENT_SOUNDS | {"chuanshao"}
SOUND_ATOM = rf"(?:mute|empty|chuanshao|{PITCH})"
SOUND = rf"(?:{SOUND_ATOM}|\({SOUND_ATOM}(?:[.~@]{SOUND_ATOM})*\))"
NOTE_FORM = re.compile(rf"({SOUND})\[([H-P]+)\](\{{[0-9]+\}})?")


@dataclass
class Token:
    text: str
    start: int
    end: int
    ticks: int
    kind: str = "note"
    children: tuple = ()


def parse_score(text):
    """Split commas/semicolons only outside brackets, preserving source spans."""
    if not text.strip():
        return []
    spans, stack, start = [], [], 0
    pairs = {">": "<", ")": "(", "]": "["}
    for i, char in enumerate(text):
        if char in "<([":
            stack.append(char)
        elif char in ">)]":
            if not stack or stack.pop() != pairs[char]:
                raise ValueError(f"第 {i + 1} 个字符的括号不匹配")
        elif char in ",;" and not stack:
            spans.append((start, i))
            start = i + 1
    if stack:
        raise ValueError("谱面存在未闭合的括号")
    spans.append((start, len(text)))
    tokens = []
    for left, right in spans:
        raw = text[left:right]
        value = raw.strip()
        if not value:
            if right == len(text):  # Existing examples allow a trailing separator.
                continue
            raise ValueError("分隔符之间不能有空音符")
        left += len(raw) - len(raw.lstrip())
        right = left + len(value)
        if re.fullmatch(r"[Q-Y]+", value):
            tokens.append(Token(value, left, right, sum(REST_SIZES[c] for c in value), "rest"))
            continue
        match = NOTE_FORM.fullmatch(value)
        if match:
            atoms = re.split(r"[.~@]", match[1].strip("()"))
            kind = "rest" if all(atom in SILENT_SOUNDS for atom in atoms) else "note"
            tokens.append(Token(value, left, right, sum(SIZES[c] for c in match[2]), kind))
            continue
        match = re.fullmatch(r"([1235-9]|10)<(.+)>", value)
        if not match:
            raise ValueError(f"无法识别的谱面字符：{value[:100]}")
        children = parse_score(match[2])
        if not children or any(child.kind not in ("note", "rest") for child in children):
            raise ValueError("特殊方块内仅支持普通音符、和弦或休止符，不支持嵌套方块")
        tokens.append(Token(value, left, right, sum(c.ticks for c in children),
                            match[1], tuple(children)))
    return tokens


@dataclass(frozen=True)
class WrappedGroup:
    section: int
    layer: int
    start: int
    end: int
    source: str
    replacement: str


def group_body(token):
    body = token.text[len(token.kind) + 1:-1]
    return body[token.children[0].start:token.children[-1].end]


def find_wrapped_groups(data):
    """List parseable special blocks on every track, even if a rule is violated."""
    groups = []
    for section, part in enumerate(data["musics"]):
        for layer, line in enumerate(part["scores"]):
            for token in inspect_score(line)[0]:
                if token.children:
                    groups.append(WrappedGroup(section, layer, token.start, token.end,
                                               token.text, group_body(token)))
    return groups


def dissolve_groups(data, groups):
    """Remove approved wrappers without changing flattened audition indices."""
    tracks = {}
    for group in groups:
        tracks.setdefault((group.section, group.layer), []).append(group)
    changed = {}
    for (section, layer), items in tracks.items():
        line = data["musics"][section]["scores"][layer]
        parts, cursor = [], 0
        for group in sorted(items, key=lambda item: item.start):
            if group.start < cursor or line[group.start:group.end] != group.source:
                raise ValueError("音符组已发生更改，请重新打开解散列表。")
            parts.extend((line[cursor:group.start], group.replacement))
            cursor = group.end
        parts.append(line[cursor:])
        changed[section, layer] = "".join(parts)
    for (section, layer), line in changed.items():
        data["musics"][section]["scores"][layer] = line


def merge_sounds(text, connector):
    """Manually combine attacks, retaining pitch order, duplicates and duration."""
    if connector not in (".", "~", "@"):
        raise ValueError("合并发音只支持 .、~ 或 @")
    tokens = parse_score(text)
    # A previously wrapped long strip can be converted without retyping it.
    if len(tokens) == 1 and tokens[0].kind == "6":
        tokens = list(tokens[0].children)
    if len(tokens) < 2:
        raise ValueError("请选择至少两个音符（可包含和弦），或一个包含多个音符的 6<...> 长条")
    if any(token.kind not in ("note", "rest") for token in tokens):
        raise ValueError("合并发音不能包含特殊方块")
    if any(token.text.endswith("}") for token in tokens):
        raise ValueError("不能合并带背景动效的音符，以免丢失动效")
    if connector == "~":
        if tokens[0].kind == "rest" or tokens[-1].kind == "rest":
            raise ValueError("连音可包含内部休止，起止位置须为发声音符")
        tokens = tuplet_slots(tokens)
    elif any(token.kind == "rest" for token in tokens):
        raise ValueError("含休止符的片段请使用 ~ 合并发音")
    sounds = [token.text.rsplit("[", 1)[0].strip("()") if token.kind == "note"
              else "mute" for token in tokens]
    if len(sounds) < 2:
        raise ValueError("合并发音需要至少两个发音或休止位置")
    total = sum(token.ticks for token in tokens)
    letters = []
    for letter, size in SIZES.items():
        count, total = divmod(total, size)
        letters.extend([letter] * count)
    merged = "(" + connector.join(sounds) + ")[" + "".join(letters) + "]"
    parse_score(merged)
    return merged


def normal_ticks(base_beats):
    if not math.isfinite(base_beats) or base_beats <= 0:
        raise ValueError("基础拍长必须是有限正数")
    return base_beats * 32


def block_error(token, minimum):
    """Return a hard rule violation, or an empty string."""
    if token.kind in ("note", "rest"):
        return ""
    lengths = [child.ticks for child in token.children]
    kind = token.kind
    if kind == "3" and any(length != minimum / 2 for length in lengths):
        return "狂戳块内每个音符必须等于最小正常音符的 1/2，且等长"
    if kind == "5" and (len(lengths) != 2 or sum(lengths) != minimum):
        return "双黑块必须恰好包含两个音符，长度之和等于最小正常音符"
    if kind == "6" and sum(lengths) < minimum:
        return "长条总长度不足最小正常长度，请考虑并入前一音或后一音"
    if kind in ("7", "8") and any(length > minimum * 2 for length in lengths):
        return "滑块内每个音符严禁大于最小正常音符的 2 倍"
    if kind == "10":
        if len(lengths) < 2 or any(length != minimum for length in lengths[1:]):
            return "爆裂块的后续音符必须全部等于最小正常音符"
        if sum(lengths[1:]) != 2 * lengths[0]:
            return "爆裂方块总长度必须严格等于主块长度的 2 倍"
    return ""


def block_warning(token, minimum):
    if token.kind == "2" and token.ticks != minimum:
        return (f"警告：2<...> 可能将内容压成一个黑块；组内总时长为 {token.ticks:g}P，"
                f"不等于最小正常音符 {minimum:g}P，可能打乱节奏。")
    return ""


def accompaniment_matches(section, start, end, minimum):
    for line in section["scores"][1:]:
        cursor, covered, valid = 0, 0, True
        for token in parse_score(line):
            stop = cursor + token.ticks
            if cursor < end and stop > start:
                if (cursor < start or stop > end or token.kind != "note"
                        or token.ticks != minimum):
                    valid = False
                    break
                covered += token.ticks
            cursor = stop
        if valid and covered == end - start:
            return True
    return False


def accompaniment_activity(section):
    """Union of sounding accompaniment spans in score ticks, excluding rests."""
    spans = []

    def collect(token, start):
        if token.children:
            for child in token.children:
                collect(child, start)
                start += child.ticks
        elif token.kind == "note":
            sound = NOTE_FORM.fullmatch(token.text)[1].strip("()")
            groups = sound.split("~") if "@" not in sound else [sound]
            if len(groups) == 1:
                spans.append((start, start + token.ticks))
                return
            duration = Fraction(token.ticks, len(groups))
            for i, group in enumerate(groups):
                if any(name not in SILENT_SOUNDS for name in group.split(".")):
                    spans.append((start + i * duration, start + (i + 1) * duration))

    for line in section["scores"][1:]:
        cursor = 0
        for token in parse_score(line):
            collect(token, cursor)
            cursor += token.ticks
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def overlaps_accompaniment(activity, start, end):
    # Positive overlap only: a note ending exactly at the rest's start is silent.
    index = bisect_right(activity, start, key=lambda span: span[1])
    return index < len(activity) and activity[index][0] < end


def token_error(token, minimum, section, start, activity=None):
    error = block_error(token, minimum)
    if error:
        return error
    if token.kind == "9" and not accompaniment_matches(
            section, start, start + token.ticks, minimum):
        return "伴奏块对应时段必须完整覆盖伴奏，且伴奏中的每个音符均为最小正常长度"
    if token.kind == "note" and token.ticks < minimum:
        return f"普通音符长度 {token.ticks:g}P 小于最小正常长度 {minimum:g}P"
    if token.kind == "rest" and token.ticks < minimum:
        if activity is None:
            activity = accompaniment_activity(section)
        if overlaps_accompaniment(activity, start, start + token.ticks):
            return (f"短休止符被伴奏音符触发为黑块，长度 {token.ticks:g}P "
                    f"小于最小正常长度 {minimum:g}P")
    return ""


def inspect_score(text):
    """Recover at separators outside matched brackets, retaining invalid spans."""
    try:
        tokens = parse_score(text)
        if tokens:
            return tokens, {}
        return [], {0: (0, len(text), "谱面不能为空")}
    except ValueError:
        pass
    stack, pairs = [], {}
    for pos, char in enumerate(text):
        if char in "<([":
            stack.append((char, pos))
        elif char in ">)]" and stack and stack[-1][0] == {">": "<", ")": "(", "]": "["}[char]:
            _, start = stack.pop()
            pairs[start] = pos
    tokens, errors = [], {}
    start = pos = 0
    while pos <= len(text):
        if pos in pairs:
            pos = pairs[pos] + 1
            continue
        if pos == len(text) or text[pos] in ",;":
            raw = text[start:pos]
            if raw.strip() or pos < len(text):
                try:
                    parsed = parse_score(raw)
                    if len(parsed) != 1:
                        raise ValueError("分隔符之间不能有空音符")
                    token = parsed[0]
                    token.start += start
                    token.end += start
                except ValueError as exc:
                    errors[len(tokens)] = (start, pos, str(exc))
                    token = Token(raw, start, pos, 0, "invalid")
                tokens.append(token)
            start = pos + 1
        pos += 1
    return tokens, errors


def tuplet_slots(tokens):
    """Treat adjacent rest spellings as a single silent attack slot."""
    slots = []
    for token in tokens:
        if token.kind == "rest" and slots and slots[-1].kind == "rest":
            previous = slots[-1]
            slots[-1] = Token("", previous.start, token.end,
                              previous.ticks + token.ticks, "rest")
        else:
            slots.append(token)
    return slots


def silent_triplets(tokens):
    """Locate note, one rest run, note (the run may use multiple symbols)."""
    i = 1
    while i < len(tokens) - 1:
        if tokens[i].kind != "rest":
            i += 1
            continue
        end = i + 1
        while end < len(tokens) and tokens[end].kind == "rest":
            end += 1
        if end < len(tokens):
            yield i - 1, end
        i = end


def staccato_patterns(tokens, source, minimum, include_unadjustable=False):
    """Find near-equal note/rest pairs and rounded, regular groups of three."""
    pairs = []
    i = 0
    while i < len(tokens):
        note = tokens[i]
        end = i + 1
        rest = 0
        previous = note
        if (note.kind == "note" and "~" not in note.text and "@" not in note.text
                and "chuanshao" not in note.text):
            while (end < len(tokens) and tokens[end].kind == "rest"
                   and not tokens[end].text.endswith("}")
                   and ";" not in source[previous.end:tokens[end].start]):
                previous = tokens[end]
                rest += previous.ticks
                end += 1
        if rest and 4 * abs(rest - note.ticks) <= max(4, rest, note.ticks):
            pairs.append((i, end - 1, note.ticks + rest))
        i = end
    i = 0
    while i < len(pairs):
        first, end, total = pairs[i]
        group = pairs[i:i + 3]
        if len(group) == 3:
            lengths = [slot[2] for slot in group]
            duration = sum(lengths)
            adjacent = all(a[1] + 1 == b[0] and ";" not in source[tokens[a[1]].end:tokens[b[0]].start]
                           for a, b in zip(group, group[1:]))
            if (adjacent and duration >= minimum and duration % minimum == 0
                    and any(ticks % minimum for ticks in lengths)
                    and all(abs(3 * ticks - duration) <= 3 for ticks in lengths)
                    and all(not tokens[slot[0]].text.endswith("}") for slot in group)):
                yield first, group[-1][1], tuple(slot[0] for slot in group)
                i += 3
                continue
        if include_unadjustable or tokens[first].ticks > math.ceil(minimum):
            yield first, end, (first,)
        i += 1


def resembles_tuplet(tokens, minimum):
    if (any(t.kind not in ("note", "rest") or t.text.endswith("}") for t in tokens)
            or not tokens or tokens[0].kind != "note" or tokens[-1].kind != "note"):
        return False
    slots = tuplet_slots(tokens)
    if len(slots) < 3:
        return False
    lengths = [t.ticks for t in slots]
    total = sum(lengths)
    return (min(lengths) != max(lengths) and total >= minimum
            and total % minimum == 0
            and all(abs(length * len(slots) - total) <= len(slots) for length in lengths))


def arpeggio_pickup_limit(count, minimum):
    return minimum if count == 1 else minimum / 2


def resembles_arpeggio(pickup, ending, minimum):
    from midi_preview import pitch_number

    if not pickup or ending.ticks < minimum or any(
            t.ticks >= arpeggio_pickup_limit(len(pickup), minimum) for t in pickup):
        return False
    notes = [*pickup, ending]
    if any(t.kind != "note" or t.text.startswith("(") or t.text.endswith("}") for t in notes):
        return False
    if sum(t.ticks for t in notes) % max(1, minimum):
        return False
    try:
        pitches = [pitch_number(t.text.rsplit("[", 1)[0]) for t in notes]
    except ValueError:
        return False
    steps = [b - a for a, b in zip(pitches, pitches[1:])]
    return all(0 < step <= 12 for step in steps) or all(-12 <= step < 0 for step in steps)


def accompaniment_phrases(tokens, minimum):
    # Inspect longer groups of short notes as well as triplets anywhere.
    cursor = 0
    while cursor < len(tokens):
        end = cursor
        while (end < len(tokens) and tokens[end].kind == "note"
               and tokens[end].ticks < minimum):
            end += 1
        if end - cursor > 3 and resembles_tuplet(tokens[cursor:end], minimum):
            yield cursor, end - 1, "疑似连音"
        for first in range(cursor, max(cursor + 1, end)):
            if first + 2 < len(tokens) and resembles_tuplet(tokens[first:first + 3], minimum):
                yield first, first + 2, "疑似三连音"
        if end > cursor and end < len(tokens):
            # At most 88 distinct piano pitches can form a monotone arpeggio.
            for first in range(max(cursor, end - 87), end):
                if resembles_arpeggio(tokens[first:end], tokens[end], minimum):
                    yield first, end, "疑似琶音"
                    break
        cursor = max(cursor + 1, end)


def find_issues(data):
    """Find short notes and serious syntax or block errors in all parts."""
    issues = []
    for section_index, section in enumerate(data["musics"]):
        minimum = normal_ticks(section["baseBeats"])
        scans = [inspect_score(line) for line in section["scores"]]
        accompaniment_valid = not any(errors for _, errors in scans[1:])
        activity = accompaniment_activity(section) if accompaniment_valid else []
        for layer, (tokens, syntax) in enumerate(scans):
            def report(first, last, start, end, reason, serious, invalid=False, warning=False):
                issues.append(dict(section=section_index, layer=layer, first=first, last=last,
                                   start=start, end=end, reason=reason, serious=serious,
                                   syntax=invalid, warning=warning))

            for i, (start, end, reason) in syntax.items():
                report(i, i, start, end, "语法错误：" + reason, True, True)
            patterns = list(staccato_patterns(tokens, section["scores"][layer], minimum,
                                               include_unadjustable=True)) if not syntax else []
            short_staccato = {first for first, _, sounds in patterns
                              if len(sounds) == 1 and tokens[first].ticks < minimum}
            cursor, errors = 0, []
            for index, token in enumerate(tokens):
                warning = block_warning(token, minimum)
                if warning:
                    report(index, index, token.start, token.end, warning, False, warning=True)
                reason = ""
                if token.kind != "invalid":
                    if not layer and token.kind == "9" and (syntax or not accompaniment_valid):
                        pass  # Its timing cannot be verified until syntax is repaired.
                    elif not layer:
                        reason = token_error(token, minimum, section, cursor, activity)
                errors.append(reason)
                cursor += token.ticks
            i = 0
            while i < len(tokens):
                token = tokens[i]
                reason = errors[i]
                last = i
                if reason:
                    serious = bool(token.children)
                    if not serious:
                        while (last + 1 < len(tokens) and errors[last + 1]
                               and not tokens[last + 1].children):
                            last += 1
                        if last > i:
                            reason = f"{last - i + 1} 个连续异常短音符/休止符，小于最小正常长度 {minimum:g}P"
                            if any(t.kind == "rest" for t in tokens[i:last + 1]):
                                reason += "（含被伴奏触发的休止符）"
                    if i in short_staccato:
                        reason += "（疑似跳音，已短于最小正常时值）"
                    report(i, last, token.start, tokens[last].end, reason, serious)
                i = last + 1
            if not syntax:
                for first, last, sounds in patterns:
                    if len(sounds) == 1 and tokens[first].ticks <= math.ceil(minimum):
                        continue
                    issues.append(dict(section=section_index, layer=layer, first=first, last=last,
                                       start=tokens[first].start, end=tokens[last].end,
                                       reason=("疑似跳音三连音" if len(sounds) == 3 else "疑似跳音：发声和后续休止接近等长"),
                                       serious=False, syntax=False, optional=True))
                silent = ((first, last, "疑似三连音（中间为休止）")
                          for first, last in silent_triplets(tokens)
                          if last - first > 2 or not layer
                          if resembles_tuplet(tokens[first:last + 1], minimum))
                phrases = list(silent)
                if layer:
                    phrases.extend(accompaniment_phrases(tokens, minimum))
                for first, last, reason in phrases:
                    issues.append(dict(section=section_index, layer=layer, first=first, last=last,
                                       start=tokens[first].start, end=tokens[last].end,
                                       reason=reason + ("（伴奏，可合并发音）" if layer else ""),
                                       serious=False, syntax=False, optional=True))
        # Rounded triplets can still be irregular when each note exceeds the minimum.
        tokens, syntax = scans[0]
        i = 0
        while not syntax and i + 2 < len(tokens):
            triplet = tokens[i:i + 3]
            if (all(t.kind == "note" and t.ticks >= minimum for t in triplet)
                    and resembles_tuplet(triplet, minimum)):
                issues.append(dict(section=section_index, layer=0, first=i, last=i + 2,
                                   start=triplet[0].start, end=triplet[-1].end,
                                   reason="疑似三连音：各音时长接近，合计为正常长度的整数倍",
                                   serious=False, syntax=False, optional=True))
                i += 3
            else:
                i += 1
    return sorted(issues, key=lambda i: (i["section"], i["layer"], i["start"]))


def repair_score_issue(data, issue, replacement):
    section = data["musics"][issue["section"]]
    layer = issue["layer"]
    source = section["scores"][layer]
    replacement = replacement.strip()
    tokens = parse_score(replacement)
    if not tokens:
        raise ValueError("请输入有效的替换音符或休止符")
    if layer and any(t.children for t in tokens):
        raise ValueError("伴奏不能包含特殊方块")
    if not issue["syntax"]:
        old = parse_score(source[issue["start"]:issue["end"]])
        if sum(t.ticks for t in old) != sum(t.ticks for t in tokens):
            raise ValueError("替换前后总长度必须一致，以保持伴奏同步")
    minimum = normal_ticks(section["baseBeats"])
    for token in tokens:
        error = block_error(token, minimum)
        if error:
            raise ValueError(error)
    left, right = issue["start"], issue["end"]
    updated = source[:left] + replacement + source[right:]
    if not layer:
        old, _ = inspect_score(source)
        if old:
            remap_audition(data, issue["section"], old, issue["first"], issue["last"], tokens)
    section["scores"][layer] = updated


def replace_fragment(data, section_index, first, last, replacement, layer=0):
    """Validate a replacement transaction before changing the document."""
    section = data["musics"][section_index]
    source = section["scores"][layer]
    tokens = parse_score(source)
    if not 0 <= first <= last < len(tokens):
        raise ValueError("音符范围无效")
    replacement = replacement.strip()
    if not replacement or any(c.isspace() for c in replacement):
        raise ValueError("替换片段不能为空或包含空白字符")
    new_tokens = parse_score(replacement)
    if layer and any(t.children for t in new_tokens):
        raise ValueError("伴奏的发音编辑请使用普通音符、和弦或休止符")
    old_ticks = sum(t.ticks for t in tokens[first:last + 1])
    if sum(t.ticks for t in new_tokens) != old_ticks:
        raise ValueError(f"替换前后总长度必须一致（当前片段为 {old_ticks}P），以保持伴奏同步")
    minimum = normal_ticks(section["baseBeats"])
    cursor = sum(t.ticks for t in tokens[:first])
    for token in new_tokens:
        # Ordinary short notes may be left for subsequent correction.
        if token.kind not in ("note", "rest"):
            error = token_error(token, minimum, section, cursor)
            if error:
                raise ValueError(error)
        cursor += token.ticks
    if ";" in source[tokens[first].start:tokens[last].end]:
        barlines = set()
        tick = 0
        for i in range(first, last):
            tick += tokens[i].ticks
            if ";" in source[tokens[i].end:tokens[i + 1].start]:
                barlines.add(tick)
        parts, tick = [], 0
        for left, right in zip(new_tokens, new_tokens[1:]):
            tick += left.ticks
            separator = replacement[left.end:right.start]
            if tick in barlines:
                separator = ";"
                barlines.remove(tick)
            parts.append(left.text + separator)
        if barlines:
            raise ValueError("此修改会跨越小节线，请分小节编辑")
        replacement = "".join(parts) + new_tokens[-1].text
    updated = source[:tokens[first].start] + replacement + source[tokens[last].end:]
    parse_score(updated)
    if layer and "9<" in section["scores"][0] and not any(
            inspect_score(line)[1] for line in section["scores"]):
        changed = {**section, "scores": section["scores"].copy()}
        changed["scores"][layer] = updated
        start = 0
        for token in parse_score(section["scores"][0]):
            end = start + token.ticks
            if (token.kind == "9" and accompaniment_matches(section, start, end, minimum)
                    and not accompaniment_matches(changed, start, end, minimum)):
                raise ValueError("此修改会破坏主旋律 9<...> 伴奏块所需的等长伴奏")
            start = end
    section["scores"][layer] = updated
    if not layer:
        remap_audition(data, section_index, tokens, first, last, new_tokens)


def preview_text(data, all_layers=False):
    """Return valid formatted JSON and main-score character offsets."""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    offsets, cursor = [], 0
    for section in data["musics"]:
        cursor = text.index('"scores": [', cursor)
        row = []
        cursor += len('"scores": [')
        for line in section["scores"]:
            encoded = json.dumps(line, ensure_ascii=False)
            start = text.index(encoded, cursor)
            row.append(start + 1)
            cursor = start + len(encoded)
        offsets.append(row if all_layers else row[0])
    return text, offsets


class ScoreEditor(tk.Toplevel):
    def __init__(self, master, data, initial_name, ignored_issues=None, notation_keys=None, on_split_change=None,
                 measure_ticks=()):
        super().__init__(master)
        self.data = data
        self.initial_name = initial_name
        self.history = []
        self.notation_keys = notation_keys
        self.on_split_change = on_split_change
        self.measure_ticks = measure_ticks
        self.notation_window = None
        self.skipped_issues = ignored_issues if ignored_issues is not None else set()
        self.current_suggestions = []
        self._loaded_suggestion = None
        self.preview_player = SystemMidiPlayer()
        self._preview_poll = None
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.title(("竞技场谱面 · " if is_arena_score(data) else "") + "JSON 预览与非正常音符校正")
        size_window(self, 1480, 960, 930, 680)
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status).grid(row=0, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(self, text="选择问题片段定位；可扩大音符范围后套用方块。P 为最小长度单位，"
                  "有伴奏发声的短休止符也会列为异常。修改前后总长度必须一致。").grid(
                      row=1, column=0, sticky="w", padx=10)
        panes = ttk.Panedwindow(self, orient="vertical")
        panes.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)
        issue_frame = ttk.Frame(panes)
        issue_frame.columnconfigure(0, weight=1)
        issue_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(issue_frame, columns=("section", "range", "reason", "fragment"),
                                 show="headings", selectmode="browse", height=5)
        for key, label, width in (("section", "段 / 轨", 66), ("range", "音符序号", 90),
                                  ("reason", "问题", 310), ("fragment", "片段", 450)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.tag_configure("ignored", foreground="#777777")
        scrollbar = ttk.Scrollbar(issue_frame, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self.select_issue)
        panes.add(issue_frame, weight=1)
        suggestion_frame = ttk.LabelFrame(panes, text="建议更改（按推荐顺序；点击预览，不会自动提交）")
        suggestion_frame.columnconfigure(0, weight=1)
        suggestion_frame.rowconfigure(0, weight=1)
        self.suggestion_tree = ttk.Treeview(
            suggestion_frame, columns=("name", "range", "reason", "result"),
            show="headings", height=3, selectmode="browse")
        for key, label, width in (("name", "建议方案", 160), ("range", "音符范围", 90),
                                   ("reason", "理由", 340), ("result", "修改后片段", 380)):
            self.suggestion_tree.heading(key, text=label)
            self.suggestion_tree.column(key, width=width)
        self.suggestion_tree.grid(row=0, column=0, sticky="nsew")
        suggestion_scroll = ttk.Scrollbar(suggestion_frame, command=self.suggestion_tree.yview)
        suggestion_scroll.grid(row=0, column=1, sticky="ns")
        self.suggestion_tree.configure(yscrollcommand=suggestion_scroll.set)
        self.suggestion_tree.bind("<<TreeviewSelect>>", self.load_suggestion)
        suggestion_actions = ttk.Frame(suggestion_frame)
        suggestion_actions.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        for label, action in (("应用此建议", self.apply_selected_suggestion),
                              ("一键应用所有首选建议", self.apply_all_recommendations),
                              ("忽略当前片段", self.skip_issue),
                              ("取消当前忽略", self.unignore_issue),
                              ("取消全部忽略", self.restore_skipped)):
            ttk.Button(suggestion_actions, text=label, command=action).pack(side="left", padx=5)
        panes.add(suggestion_frame, weight=1)
        preview = ttk.Frame(panes)
        preview.rowconfigure(0, weight=1)
        preview.columnconfigure(0, weight=1)
        self.json_view = tk.Text(preview, wrap="none", height=12)
        self.json_view.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(preview, command=self.json_view.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(preview, orient="horizontal", command=self.json_view.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.json_view.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.json_view.tag_configure("problem", background="#ffe2a8", foreground="#592800")
        self.json_view.tag_configure("warning", background="#fff1b8", foreground="#734300")
        self.json_view.tag_configure("optional", background="#e8ddff", foreground="#462776")
        self.json_view.tag_configure("serious", background="#ffc2c2", foreground="#850000")
        self.json_view.tag_configure("selected", background="#2363a0", foreground="white")
        self.json_view.tag_raise("selected")
        panes.add(preview, weight=3)
        controls = ttk.Frame(self, padding=10)
        controls.grid(row=3, column=0, sticky="ew")
        self.section = tk.StringVar(value="1")
        self.layer = tk.StringVar(value="1")
        self.first = tk.StringVar(value="1")
        self.last = tk.StringVar(value="1")
        for i, (label, variable, high) in enumerate((
                ("段", self.section, len(data["musics"])), ("轨", self.layer, max(len(s["scores"]) for s in data["musics"])),
                ("起始音符", self.first, 1000000),
                ("结束音符", self.last, 1000000))):
            ttk.Label(controls, text=label).grid(row=0, column=2 * i, padx=(0, 4))
            ttk.Spinbox(controls, from_=1, to=high, textvariable=variable, width=7).grid(
                row=0, column=2 * i + 1, padx=(0, 12))
        ttk.Button(controls, text="载入范围", command=self.load_range).grid(row=0, column=8)
        self.block = ttk.Combobox(controls, state="readonly", width=13,
                                  values=[f"{key} {name}" for key, name in BLOCK_NAMES.items()])
        self.block.current(1)
        self.block.grid(row=0, column=9, padx=10)
        ttk.Button(controls, text="套用方块", command=self.wrap_fragment).grid(row=0, column=10)
        self.fragment = tk.Text(controls, height=3, wrap="word")
        self.fragment.grid(row=1, column=0, columnspan=11, sticky="ew", pady=8)
        controls.columnconfigure(10, weight=1)
        self.help = tk.StringVar()
        ttk.Label(controls, textvariable=self.help, wraplength=1000).grid(
            row=2, column=0, columnspan=11, sticky="w")
        self.block.bind("<<ComboboxSelected>>", self.show_help)
        self.show_help()
        sound_tools = ttk.Frame(controls)
        sound_tools.grid(row=3, column=0, columnspan=11, sticky="w", pady=(8, 0))
        ttk.Label(sound_tools, text="合并发音").grid(row=0, column=0, padx=(0, 8))
        self.sound_mode = ttk.Combobox(sound_tools, state="readonly", width=16,
                                       values=("@ 短时琶音", "~ 均匀滑音", ". 同时和弦"))
        self.sound_mode.current(0)
        self.sound_mode.grid(row=0, column=1)
        ttk.Button(sound_tools, text="合并片段音符", command=self.merge_fragment).grid(
            row=0, column=2, padx=8)
        ttk.Label(sound_tools, text="保留总时长、音高顺序和重复音；改变内部发音方式。"
                  "合并后点击“应用修改”。").grid(row=1, column=0, columnspan=3,
                                                   sticky="w", pady=(4, 0))
        playback = ttk.Frame(controls)
        playback.grid(row=4, column=0, columnspan=11, sticky="w", pady=(8, 0))
        ttk.Button(playback, text="试听原片段", command=lambda: self.play_fragment(False)).grid(
            row=0, column=0)
        ttk.Button(playback, text="试听编辑片段", command=lambda: self.play_fragment(True)).grid(
            row=0, column=1, padx=8)
        ttk.Button(playback, text="停止", command=self.stop_preview).grid(row=0, column=2)
        self.preview_accompaniment = tk.BooleanVar(value=True)
        ttk.Checkbutton(playback, text="包含其他音轨", variable=self.preview_accompaniment).grid(
            row=0, column=3, padx=8)
        self.playback_status = tk.StringVar(value="系统钢琴音；~、@ 发音时序为近似试听")
        ttk.Label(playback, textvariable=self.playback_status).grid(row=1, column=0, columnspan=4,
                                                                  sticky="w", pady=(4, 0))
        actions = ttk.Frame(self, padding=(10, 0, 10, 10))
        actions.grid(row=4, column=0, sticky="e")
        ttk.Button(actions, text="应用修改并重新检查", command=self.apply_edit).pack(side="left", padx=5)
        ttk.Button(actions, text="可视化谱面…", command=self.open_notation).pack(side="left", padx=5)
        ttk.Button(actions, text="解散所有音符组…", command=self.dissolve_note_groups).pack(side="left", padx=5)
        ttk.Button(sound_tools, text="谱面模拟器…", command=self.open_simulator).grid(
            row=0, column=4, padx=6)
        ttk.Button(sound_tools, text="解散当前音符组", command=self.dissolve_current_group).grid(
            row=0, column=3, padx=6)
        ttk.Button(actions, text="撤销", command=self.undo).pack(side="left", padx=5)
        ttk.Button(actions, text="导出修改后的 JSON", command=self.export).pack(side="left", padx=5)
        ttk.Button(actions, text="关闭（保留已应用修改）", command=self.destroy).pack(side="left", padx=5)
        self.refresh()
        # Settings cannot change underneath this document while editing.
        self.transient(master)
        self.grab_set()

    def open_simulator(self):
        from score_simulator import ScoreSimulator

        self.stop_preview(quiet=True)
        try:
            dialog = ScoreSimulator(self, self.data)
        except ValueError as exc:
            messagebox.showwarning("无法打开谱面模拟器", str(exc), parent=self)
            return
        dialog.grab_set()
        self.wait_window(dialog)
        if self.winfo_exists():
            self.grab_set()

    def open_notation(self):
        from notation_view import NotationWindow

        if self.notation_window is not None and self.notation_window.winfo_exists():
            self.notation_window.lift()
            return
        self.stop_preview(quiet=True)
        self.notation_window = NotationWindow(self)

    def show_help(self, *_):
        kind = self.block.get().split()[0]
        self.help.set({
            "3": "狂戳：每个内部音符等长，且等于最小正常音符的 1/2。",
            "5": "双黑：恰好两个音符，时值之和等于最小正常音符。",
            "6": "长条：总长度必须达到最小正常长度；不足时应纳入前一音或后一音。",
            "7": "长滑块：每个音符建议 ≤ 最小正常长度/2，严禁 > 最小正常长度×2。",
            "8": "折叠滑块：每个音符建议 ≤ 最小正常长度/2，严禁 > 最小正常长度×2。",
            "9": "伴奏块：同一时段须有完整伴奏，伴奏中的每个音符均为最小正常长度。",
            "10": "爆裂：第一个音符是主块；后续音符各为最小正常长度，后续总长严格等于主块×2。",
        }[kind])

    def refresh(self):
        from score_suggestions import issue_key
        self.issues = find_issues(self.data)
        self.current_suggestions = []
        self._loaded_suggestion = None
        self.loaded_range = None
        self.loaded_repair = None
        self.suggestion_tree.delete(*self.suggestion_tree.get_children())
        self.tree.delete(*self.tree.get_children())
        for i, issue in enumerate(self.issues):
            source = self.data["musics"][issue["section"]]["scores"][issue["layer"]]
            ignored = issue_key(self.data, issue) in self.skipped_issues
            self.tree.insert("", "end", iid=str(i), values=(
                f'{issue["section"] + 1}' + (f' / 轨{issue["layer"] + 1}' if issue["layer"] else ""),
                f'{issue["first"] + 1}–{issue["last"] + 1}',
                ("已忽略（仅批量）；" if ignored else "")
                + issue["reason"], source[issue["start"]:issue["end"]]),
                tags=("ignored",) if ignored else ())
        text, self.layer_offsets = preview_text(self.data, all_layers=True)
        self.json_view.configure(state="normal")
        self.json_view.delete("1.0", "end")
        self.json_view.insert("1.0", text)
        for issue in self.issues:
            left, right = self.issue_json_span(issue)
            self.json_view.tag_add("serious" if issue["serious"] else
                                   "warning" if issue.get("warning") else
                                   "optional" if issue.get("optional") else "problem",
                                   f"1.0+{left}c", f"1.0+{right}c")
        self.json_view.tag_raise("serious")
        self.json_view.configure(state="disabled")
        count = sum(i["last"] - i["first"] + 1 for i in self.issues)
        ignored_count = sum(issue_key(self.data, issue) in self.skipped_issues for issue in self.issues)
        self.status.set(f"共 {len(self.issues)} 个待校正片段，涉及 {count} 个音符/方块；"
                        f"已忽略 {ignored_count} 个（仍可手动应用）。"
                        "橙色：短音符；黄色：警告；红色：严重错误；紫色：疑似连音或琶音；蓝色：当前范围。已应用修改会保留，可随时导出。")

    def issue_json_span(self, issue):
        section, layer = issue["section"], issue["layer"]
        offset = self.layer_offsets[section][layer]
        line = self.data["musics"][section]["scores"][layer]
        left, right = (offset + len(json.dumps(line[:issue[key]], ensure_ascii=False)) - 2
                       for key in ("start", "end"))
        return left, max(left + 1, right)

    def select_issue(self, *_):
        selected = self.tree.selection()
        if not selected:
            return
        issue = self.issues[int(selected[0])]
        section = self.data["musics"][issue["section"]]
        layer = issue["layer"]
        if issue["syntax"] or (not layer and any(inspect_score(line)[1] for line in section["scores"])):
            if self.loaded_repair == issue:
                return
            self.stop_preview(quiet=True)
            self.loaded_repair, self.loaded_range = issue, None
            self.current_suggestions = []
            self._loaded_suggestion = None
            self.suggestion_tree.delete(*self.suggestion_tree.get_children())
            source = section["scores"][issue["layer"]]
            self.fragment.delete("1.0", "end")
            self.fragment.insert("1.0", source[issue["start"]:issue["end"]])
            self.json_view.tag_remove("selected", "1.0", "end")
            left, _ = self.issue_json_span(issue)
            self.json_view.see(f"1.0+{left}c")
            self.help.set("在下方文本框修改所选片段，然后点击“应用修改并重新检查”。"
                          "语法损坏时无法核对原时长，请确认替换后的时长。")
            return
        target = (issue["section"], issue["first"], issue["last"], layer)
        # Tk may deliver a queued duplicate selection event after playback
        # starts. Reloading the same range would stop audio and discard a draft.
        if getattr(self, "loaded_range", None) == target:
            return
        self.section.set(str(issue["section"] + 1))
        self.layer.set(str(layer + 1))
        self.first.set(str(issue["first"] + 1))
        self.last.set(str(issue["last"] + 1))
        self.load_range()

    def selected_range(self):
        try:
            section, first, last = (int(value.get()) - 1 for value in
                                     (self.section, self.first, self.last))
            if not 0 <= section < len(self.data["musics"]):
                raise ValueError
            scores = self.data["musics"][section]["scores"]
            layer = int(self.layer.get()) - 1
            if not 0 <= layer < len(scores):
                raise ValueError
            tokens = parse_score(scores[layer])
            if not 0 <= first <= last < len(tokens):
                raise ValueError
        except (ValueError, IndexError) as exc:
            raise ValueError("请输入有效的段号、轨号和音符范围（序号从 1 开始）") from exc
        return section, first, last, tokens, layer

    def load_range(self, refresh_suggestions=True):
        try:
            section, first, last, tokens, layer = self.selected_range()
        except ValueError as exc:
            messagebox.showerror("范围无效", str(exc), parent=self)
            return
        self.stop_preview(quiet=True)
        source = self.data["musics"][section]["scores"][layer]
        self.loaded_repair = None
        self.loaded_range = (section, first, last, layer)
        self.fragment.delete("1.0", "end")
        self.fragment.insert("1.0", source[tokens[first].start:tokens[last].end])
        left = self.layer_offsets[section][layer] + len(json.dumps(source[:tokens[first].start], ensure_ascii=False)) - 2
        right = self.layer_offsets[section][layer] + len(json.dumps(source[:tokens[last].end], ensure_ascii=False)) - 2
        self.json_view.tag_remove("selected", "1.0", "end")
        self.json_view.tag_add("selected", f"1.0+{left}c", f"1.0+{right}c")
        self.json_view.see(f"1.0+{left}c")
        self.json_view.xview_moveto(0)
        self.json_view.see(f"1.0+{left}c")
        if refresh_suggestions:
            self._loaded_suggestion = None
            self.show_suggestions(section, first, last, layer)

    def show_suggestions(self, section, first, last, layer):
        from score_suggestions import suggest_changes
        try:
            self.current_suggestions = suggest_changes(self.data, section, first, last, layer=layer)
        except ValueError:
            self.current_suggestions = []
        self.suggestion_tree.delete(*self.suggestion_tree.get_children())
        for index, suggestion in enumerate(self.current_suggestions):
            self.suggestion_tree.insert("", "end", iid=str(index), values=(
                suggestion.title, f"{suggestion.first + 1}–{suggestion.last + 1}",
                suggestion.reason, suggestion.replacement))
        if layer:
            self.help.set("伴奏轨可用 ~ 或 @ 合并发音，修改前后须保持总时长。")
        elif not self.current_suggestions:
            self.help.set("暂无可安全应用的建议。长度不足时不能直接包装为长条；"
                          "请扩大音符范围、调整分段或手动编辑。")
        else:
            self.show_help()

    def selected_suggestion(self):
        selected = self.suggestion_tree.selection()
        if not selected or int(selected[0]) >= len(self.current_suggestions):
            raise ValueError("请先选择一个建议方案")
        return self.current_suggestions[int(selected[0])]

    def load_suggestion(self, *_):
        if not self.suggestion_tree.selection():
            return
        suggestion = self.selected_suggestion()
        self.section.set(str(suggestion.section + 1))
        self.layer.set(str(suggestion.layer + 1))
        self.first.set(str(suggestion.first + 1))
        self.last.set(str(suggestion.last + 1))
        self.load_range(refresh_suggestions=False)
        self._loaded_suggestion = suggestion
        self.fragment.delete("1.0", "end")
        self.fragment.insert("1.0", suggestion.replacement)

    def apply_selected_suggestion(self):
        from score_suggestions import apply_suggestion
        try:
            suggestion = (self.selected_suggestion() if self.suggestion_tree.selection()
                          else getattr(self, "_loaded_suggestion", None))
            if suggestion is None:
                raise ValueError("请先选择一个建议方案")
            section, first, last, _, layer = self.selected_range()
            if (section, first, last, layer) != (suggestion.section, suggestion.first, suggestion.last, suggestion.layer):
                raise ValueError("范围已更改，请重新点击建议方案预览后再应用")
            before = copy.deepcopy(self.data)
            apply_suggestion(self.data, suggestion)
        except ValueError as exc:
            messagebox.showerror("无法应用建议", str(exc), parent=self)
            return
        self.history.append(before)
        self.stop_preview(quiet=True)
        self.loaded_range = None
        self.fragment.delete("1.0", "end")
        self.refresh()
        self.select_next_issue(suggestion.section, suggestion.first)

    def select_next_issue(self, section=0, first=0):
        from score_suggestions import issue_key
        available = [(index, issue) for index, issue in enumerate(self.issues)
                     if issue_key(self.data, issue) not in self.skipped_issues]
        if available:
            index, _ = next(((index, issue) for index, issue in available
                             if (issue["section"], issue["first"]) >= (section, first)),
                            available[0])
            self.tree.selection_set(str(index))

    def apply_all_recommendations(self):
        from score_suggestions import issue_key, suggestion_rows
        try:
            rows = suggestion_rows(self.data, self.skipped_issues)
        except ValueError as exc:
            messagebox.showerror("无法批量应用建议", str(exc), parent=self)
            return
        if not rows:
            messagebox.showinfo("没有可应用的建议", "当前没有未忽略且可安全应用的建议。", parent=self)
            return
        accepted = self.confirm_bulk_changes(rows)
        if accepted is None:
            return
        plans, result, ignored = accepted
        self.skipped_issues.update(issue_key(self.data, issue) for issue in ignored)
        if plans:
            self.history.append(copy.deepcopy(self.data))
            self.data.clear()
            self.data.update(result)
        self.stop_preview(quiet=True)
        self.fragment.delete("1.0", "end")
        self.refresh()
        self.status.set(f"已应用 {len(plans)} 条建议，新增忽略 {len(ignored)} 个片段。"
                        "已忽略片段仍可手动修改。")

    def confirm_bulk_changes(self, rows):
        from dataclasses import replace
        from score_suggestions import batch_result, default_plans
        dialog = tk.Toplevel(self)
        dialog.title("确认批量应用建议")
        size_window(dialog, 1250, 810, 860, 620)
        dialog.transient(self)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(dialog, text="选择一条建议查看完整原文、切换方案或手动修改。"
                  "每条可应用、本次跳过或忽略；只应用选为“应用”的更改。").grid(
                      row=0, column=0, columnspan=2, sticky="w", padx=12, pady=10)
        columns = ("status", "range", "title", "replacement")
        table = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        for key, label, width in zip(columns, ("处理方式", "段 / 轨 / 音符", "方案", "改为"),
                                     (90, 120, 210, 500)):
            table.heading(key, text=label)
            table.column(key, width=width, stretch=key == "replacement")
        table.grid(row=1, column=0, sticky="nsew", padx=(12, 0))
        scroll = ttk.Scrollbar(dialog, command=table.yview)
        scroll.grid(row=1, column=1, sticky="ns", padx=(0, 12))
        table.configure(yscrollcommand=scroll.set)
        detail = ttk.Frame(dialog, padding=12)
        detail.grid(row=2, column=0, columnspan=2, sticky="ew")
        detail.columnconfigure(3, weight=1)
        mode = ttk.Combobox(detail, values=("应用", "本次跳过", "忽略"), state="readonly", width=10)
        mode.grid(row=0, column=0, padx=(0, 8))
        ttk.Label(detail, text="方案").grid(row=0, column=1)
        choice = ttk.Combobox(detail, state="readonly", width=36)
        choice.grid(row=0, column=2, sticky="w", padx=8)
        reason = tk.StringVar(dialog)
        ttk.Label(detail, textvariable=reason, wraplength=910).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=6)
        original = tk.Text(detail, height=2, wrap="char", state="disabled")
        ttk.Label(detail, text="原文").grid(row=2, column=0, sticky="nw")
        original.grid(row=2, column=1, columnspan=3, sticky="ew")
        draft = tk.Text(detail, height=3, wrap="char")
        ttk.Label(detail, text="改为（可编辑）").grid(row=3, column=0, sticky="nw", pady=6)
        draft.grid(row=3, column=1, columnspan=3, sticky="ew", pady=6)
        defaults = set(default_plans(rows))
        states = [dict(mode="应用" if options[0] in defaults else "本次跳过",
                       choice=0, text=options[0].replacement) for _, options in rows]
        current = None
        decision = None

        def show_row(i):
            state = states[i]
            plan = rows[i][1][state["choice"]]
            title = plan.title + ("（已编辑）" if state["text"] != plan.replacement else "")
            table.item(str(i), values=(state["mode"], f"{plan.section + 1} / {plan.layer + 1} / {plan.first + 1}–{plan.last + 1}",
                                       title, state["text"]))

        def save_draft():
            if current is not None:
                states[current]["text"] = draft.get("1.0", "end-1c").strip()
                show_row(current)

        def show_plan():
            state = states[current]
            plan = rows[current][1][state["choice"]]
            reason.set(plan.reason)
            original.configure(state="normal")
            original.delete("1.0", "end")
            original.insert("1.0", plan.source)
            original.configure(state="disabled")
            draft.delete("1.0", "end")
            draft.insert("1.0", state["text"])

        def select_row(*_):
            nonlocal current
            selected = table.selection()
            if not selected or int(selected[0]) == current:
                return
            save_draft()
            current = int(selected[0])
            options = rows[current][1]
            choice.configure(values=[f"{i + 1}. {plan.title}" for i, plan in enumerate(options)])
            mode.set(states[current]["mode"])
            choice.current(states[current]["choice"])
            show_plan()

        def change_mode(*_):
            if current is not None:
                states[current]["mode"] = mode.get()
                save_draft()

        def change_choice(*_):
            if current is not None:
                index = choice.current()
                states[current].update(choice=index, text=rows[current][1][index].replacement)
                show_plan()
                show_row(current)

        def apply():
            nonlocal decision
            save_draft()
            plans, ignored = [], []
            for (issue, options), state in zip(rows, states):
                if state["mode"] == "应用":
                    plans.append(replace(options[state["choice"]], replacement=state["text"]))
                elif state["mode"] == "忽略":
                    ignored.append(issue)
            try:
                result = batch_result(self.data, plans, ignored)
            except ValueError as exc:
                messagebox.showerror("无法应用所选建议", str(exc), parent=dialog)
                return
            decision = plans, result, ignored
            dialog.destroy()

        for i in range(len(rows)):
            table.insert("", "end", iid=str(i))
            show_row(i)
        table.bind("<<TreeviewSelect>>", select_row)
        mode.bind("<<ComboboxSelected>>", change_mode)
        choice.bind("<<ComboboxSelected>>", change_choice)
        actions = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        actions.grid(row=3, column=0, columnspan=2, sticky="e")
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="left", padx=8)
        ttk.Button(actions, text="确认所选更改", command=apply).pack(side="left")
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        table.selection_set("0")
        select_row()
        dialog.grab_set()
        table.focus_set()
        self.wait_window(dialog)
        self.grab_set()
        return decision

    def skip_issue(self):
        from score_suggestions import issue_key
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("未选择片段", "请先在问题列表中选择要忽略的片段", parent=self)
            return
        self.skipped_issues.add(issue_key(self.data, self.issues[int(selected[0])]))
        self.refresh()
        self.select_next_issue()

    def unignore_issue(self):
        from score_suggestions import issue_key
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("未选择片段", "请先选择要取消忽略的片段", parent=self)
            return
        index = int(selected[0])
        self.skipped_issues.discard(issue_key(self.data, self.issues[index]))
        self.refresh()
        self.tree.selection_set(str(index))

    def restore_skipped(self):
        self.skipped_issues.clear()
        self.refresh()

    def wrap_fragment(self):
        if self.layer.get() != "1" or (self.loaded_repair and self.loaded_repair["layer"]):
            messagebox.showerror("无法套用", "方块类型只在主旋律中生效。", parent=self)
            return
        value = self.fragment.get("1.0", "end-1c").strip()
        try:
            tokens = parse_score(value)
            if not tokens or any(t.kind not in ("note", "rest") for t in tokens):
                raise ValueError("请先载入普通音符片段，或先手动去掉原有特殊方块外壳")
        except ValueError as exc:
            messagebox.showerror("无法套用", str(exc), parent=self)
            return
        kind = self.block.get().split()[0]
        self.fragment.delete("1.0", "end")
        self.fragment.insert("1.0", f"{kind}<{value}>")

    def merge_fragment(self):
        value = self.fragment.get("1.0", "end-1c").strip()
        connector = self.sound_mode.get().split()[0]
        try:
            result = merge_sounds(value, connector)
        except ValueError as exc:
            messagebox.showerror("无法合并发音", str(exc), parent=self)
            return
        self.fragment.delete("1.0", "end")
        self.fragment.insert("1.0", result)

    def play_fragment(self, edited=False):
        try:
            if self.loaded_repair is not None:
                raise ValueError("请先修复谱面错误，再试听")
            section, first, last, _, layer = self.selected_range()
            if edited and getattr(self, "loaded_range", None) != (section, first, last, layer):
                raise ValueError("请先载入当前范围，再试听编辑片段")
            draft = self.fragment.get("1.0", "end-1c") if edited else None
            part = self.data["musics"][section]
            if "bpm" not in part:
                part = {**part, "bpm": effective_score_bpms(self.data)[section]}
            midi, duration = build_preview_midi(
                part, first, last, draft=draft,
                accompaniment=self.preview_accompaniment.get(), layer=layer)
            self.stop_preview(quiet=True)
            self.preview_player.play(midi)
            source = "编辑片段（尚未应用）" if edited else "当前谱面原片段"
            self._playback_label = source
            self.playback_status.set(f"正在试听{source}，约 {duration:.2f} 秒")
            self._preview_poll = self.after(150, self._poll_playback)
        except (ValueError, RuntimeError, OSError) as exc:
            self.stop_preview(quiet=True)
            self.playback_status.set("试听失败")
            messagebox.showerror("无法试听", str(exc), parent=self)

    def _poll_playback(self):
        self._preview_poll = None
        try:
            if self.preview_player.is_playing():
                self.playback_status.set(
                    f"正在试听{self._playback_label}，"
                    f"{self.preview_player.position_seconds:.2f} / "
                    f"{self.preview_player.duration_seconds:.2f} 秒")
                self._preview_poll = self.after(150, self._poll_playback)
            else:
                self.stop_preview(quiet=True)
                self.playback_status.set("试听结束")
        except (RuntimeError, OSError) as exc:
            self.stop_preview(quiet=True)
            self.playback_status.set("试听失败")
            messagebox.showerror("无法试听", str(exc), parent=self)

    def stop_preview(self, quiet=False):
        if self._preview_poll is not None:
            self.after_cancel(self._preview_poll)
            self._preview_poll = None
        try:
            self.preview_player.stop()
        except (RuntimeError, OSError) as exc:
            if not quiet:
                messagebox.showerror("停止试听失败", str(exc), parent=self)
        if hasattr(self, "playback_status") and not quiet:
            self.playback_status.set("已停止")

    def destroy(self):
        self.stop_preview(quiet=True)
        super().destroy()

    def apply_edit(self):
        try:
            replacement = self.fragment.get("1.0", "end-1c").strip()
            before = copy.deepcopy(self.data)
            if self.loaded_repair is not None:
                issue = self.loaded_repair
                section, first = issue["section"], issue["first"]
                repair_score_issue(self.data, issue, replacement)
            else:
                section, first, last, _, layer = self.selected_range()
                if self.loaded_range != (section, first, last, layer):
                    raise ValueError("范围已更改，请先点击“载入范围”再编辑")
                replace_fragment(self.data, section, first, last, replacement, layer=layer)
        except ValueError as exc:
            messagebox.showerror("无法应用修改", str(exc), parent=self)
            return
        self.history.append(before)
        self.stop_preview(quiet=True)
        self.loaded_range = None
        self.fragment.delete("1.0", "end")
        self.refresh()
        if self.issues:
            self.tree.selection_set(str(next(
                (i for i, issue in enumerate(self.issues)
                 if (issue["section"], issue["first"]) >= (section, first)), 0)))

    def dissolve_current_group(self):
        try:
            if self.loaded_repair is not None:
                issue = self.loaded_repair
                section, layer = issue["section"], issue["layer"]
                start, end = issue["start"], issue["end"]
            else:
                section, first, last, tokens, layer = self.selected_range()
                if first != last or not tokens[first].children:
                    raise ValueError("请选择一个音符组，并将起止音符设为该组的序号。")
                start, end = tokens[first].start, tokens[first].end
            group = next((group for group in find_wrapped_groups(self.data)
                          if (group.section, group.layer, group.start, group.end) ==
                          (section, layer, start, end)), None)
            if group is None:
                raise ValueError("当前选择不是可解散的单个音符组。")
            before = copy.deepcopy(self.data)
            dissolve_groups(self.data, [group])
        except ValueError as exc:
            messagebox.showwarning("无法解散当前音符组", str(exc), parent=self)
            return
        self.history.append(before)
        self.stop_preview(quiet=True)
        self.fragment.delete("1.0", "end")
        self.refresh()
        self.status.set("已解散当前音符组，其他组保持原样。可撤销本次操作。")

    def dissolve_note_groups(self):
        from dissolve_dialog import DissolveGroupsDialog

        groups = find_wrapped_groups(self.data)
        if not groups:
            messagebox.showinfo("解散音符组", "没有可解散的音符组。", parent=self)
            return
        self.stop_preview(quiet=True)
        dialog = DissolveGroupsDialog(self, groups)
        dialog.grab_set()
        self.wait_window(dialog)
        self.grab_set()
        if not dialog.result:
            return
        before = copy.deepcopy(self.data)
        try:
            dissolve_groups(self.data, dialog.result)
        except ValueError as exc:
            messagebox.showerror("无法解散音符组", str(exc), parent=self)
            return
        self.history.append(before)
        self.fragment.delete("1.0", "end")
        self.refresh()
        self.status.set(f"已解散 {len(dialog.result)} 组，保留 {len(groups) - len(dialog.result)} 组。"
                        "可撤销本次操作。")

    def replace_sections(self, data, keys):
        self.history.append((copy.deepcopy(self.data), self.notation_keys))
        self.data.clear()
        self.data.update(data)
        self.notation_keys = keys
        self.refresh()
        if self.on_split_change is not None:
            self.on_split_change(self.notation_keys)

    def undo(self):
        if self.history:
            self.stop_preview(quiet=True)
            snapshot = self.history.pop()
            moved = isinstance(snapshot, tuple)
            if moved:
                snapshot, self.notation_keys = snapshot
            self.data.clear()
            self.data.update(snapshot)
            if moved and self.on_split_change is not None:
                self.on_split_change(self.notation_keys)
            self.loaded_range = None
            self.fragment.delete("1.0", "end")
            self.refresh()

    def export(self):
        if any(issue["serious"] for issue in self.issues):
            messagebox.showerror("无法导出", "请先修复红色错误。", parent=self)
            return
        from json_input import ensure_audition
        ensure_audition(self.data)
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                           initialfile=self.initial_name,
                                           filetypes=[("JSON 文件", "*.json")])
        if not path:
            return
        try:
            from pathlib import Path
            Path(path).write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")),
                                  encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        messagebox.showinfo("导出成功", f"已保存：{path}\n剩余待校正片段：{len(self.issues)}"
                            f"\n\n{export_parameters(self.data)}", parent=self)
