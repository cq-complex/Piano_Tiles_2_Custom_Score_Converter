"""Conservative, reviewable correction suggestions for abnormal score fragments."""
from dataclasses import dataclass
import copy
import math
import re

from score_editor import (NOTE_FORM, REST_SIZES, SIZES, block_warning, find_issues, group_body, inspect_score,
                          merge_sounds, normal_ticks, parse_score, replace_fragment,
                          resembles_tuplet, resembles_arpeggio, staccato_patterns, token_error)


@dataclass(frozen=True)
class Suggestion:
    section: int
    first: int
    last: int
    title: str
    reason: str
    source: str
    replacement: str
    layer: int = 0


def issue_key(data, issue):
    section = issue["section"]
    layer = issue.get("layer", 0)
    source = data["musics"][section]["scores"][layer]
    if issue.get("syntax"):
        return section, layer, issue["start"], issue["end"], source[issue["start"]:issue["end"]]
    tokens, _ = inspect_score(source)
    first, last = issue["first"], issue["last"]
    start = sum(t.ticks for t in tokens[:first])
    stop = start + sum(t.ticks for t in tokens[first:last + 1])
    return section, layer, start, stop, tuple(t.text for t in tokens[first:last + 1])


def suggest_changes(data, section_index, first, last, layer=0):
    section = data["musics"][section_index]
    if any(inspect_score(line)[1] for line in (section["scores"][layer:layer + 1]
                                               if layer else section["scores"])):
        return []
    source = section["scores"][layer]
    tokens = parse_score(source)
    if not 0 <= first <= last < len(tokens):
        return []
    selected = tokens[first:last + 1]
    minimum = normal_ticks(section["baseBeats"])
    result = []

    def add(title, reason, replacement, end=last, begin=first):
        original = source[tokens[begin].start:tokens[end].end]
        if re.findall(r"\{[0-9]+\}", original) != re.findall(r"\{[0-9]+\}", replacement):
            return
        # Every proposed result must pass the same hard rules as a manual edit.
        trial = copy.deepcopy(data)
        try:
            replace_fragment(trial, section_index, begin, end, replacement, layer=layer)
        except ValueError:
            return
        if not any(s.first == begin and s.last == end and s.replacement == replacement for s in result):
            result.append(Suggestion(section_index, begin, end, title, reason, original, replacement, layer))

    def spell(ticks, alphabet):
        letters = []
        for symbol, size in alphabet.items():
            count, ticks = divmod(ticks, size)
            letters.append(symbol * count)
        return "".join(letters)

    for start, end, sounds in staccato_patterns(tokens, source, minimum):
        if start != first or last not in (first, end):
            continue
        ticks = sum(token.ticks for token in tokens[start:end + 1])
        if len(sounds) == 3:
            attacks = "~".join(NOTE_FORM.fullmatch(tokens[i].text)[1].strip("()") for i in sounds)
            add("~ 跳音三连音", "按三个等长位置合并，保留音高顺序和包括休止在内的总时长",
                f"({attacks})[{spell(ticks, SIZES)}]", end=end)
        elif tokens[start].ticks > math.ceil(minimum):
            target = math.ceil(minimum)
            form = NOTE_FORM.fullmatch(tokens[start].text)
            rests = ",".join(spell(ticks - target, REST_SIZES))
            add("缩短跳音（保留休止）", "把发声音符缩短到最小正常时长，剩余时长写为休止",
                f"{form[1]}[{spell(target, SIZES)}]{form[3] or ''},{rests}", end=end)
        break

    wrapped = selected[0] if first == last and selected[0].children else None
    invalid_group = wrapped and (
        block_warning(wrapped, minimum)
        or (not layer and token_error(wrapped, minimum, section,
                                      sum(t.ticks for t in tokens[:first]))))

    def add_dissolve():
        if invalid_group:
            add("解散该音符组", "仅移除方块外壳，保留组内音符、分隔符和总时长；"
                "解散后如仍有短音符，可以继续校正。",
                re.sub(r"\s+", "", group_body(wrapped)))

    # Older edits may already contain an undersized long strip. Unwrap that
    # strip when proposing a repair, rather than nesting another block.
    if len(selected) == 1 and selected[0].kind == "6" and selected[0].ticks < minimum:
        selected = list(selected[0].children)
    if any(t.kind not in ("note", "rest") for t in selected):
        add_dissolve()
        return result
    lengths = [t.ticks for t in selected]
    raw = ",".join(t.text for t in selected)
    total = sum(lengths)

    if layer:
        if resembles_tuplet(selected, minimum):
            add("~ 连音片段", "各音时长接近，合并后保持总时长", merge_sounds(raw, "~"))
        if resembles_arpeggio(selected[:-1], selected[-1], minimum):
            add("@ 琶音", "短起音和后一音音高同向，合并后保持总时长", merge_sounds(raw, "@"))
        return result

    # A short note may borrow following silence without introducing an attack.
    # Include trailing rests already grouped into this issue by accompaniment.
    if (selected[0].kind == "note" and selected[0].ticks < minimum
            and all(t.kind == "rest" for t in selected[1:])):
        note = selected[0]
        target = max(1, math.ceil(minimum))
        available = total
        end = last
        while available < target and end + 1 < len(tokens) and tokens[end + 1].kind == "rest":
            end += 1
            available += tokens[end].ticks
        if end > last or len(selected) > 1:
            if available >= target:
                suffix = note.text.partition("]")[2]
                extended = note.text.rsplit("[", 1)[0] + "[" + spell(target, SIZES) + "]" + suffix
                if available > target:
                    extended += "," + ",".join(spell(available - target, REST_SIZES))
                add("延长音符（吸收后续休止）",
                    "用纳入范围内的后续休止补足正常长度，仅延长原音符，总时长不变",
                    extended, end=end)
                strip = raw + ("," + ",".join(t.text for t in tokens[last + 1:end + 1])
                               if end > last else "")
                add("6 长条（含后续休止）",
                    "保留音符与休止写法，合计长度已达到最小正常长度",
                    f"6<{strip}>", end=end)

    if last + 1 < len(tokens) and resembles_arpeggio(selected, tokens[last + 1], minimum):
        combined = merge_sounds(raw + "," + tokens[last + 1].text, "@")
        add("@ 琶音（含后一音）", f"短音群加后一音合计 [{combined.rsplit('[', 1)[1]}；"
            "范围将多包含一个音符", combined, last + 1)

    if len(selected) == 2 and total == minimum:
        add("5 双黑块", "两个短音符的总时长恰好等于最小正常长度", f"5<{raw}>")

    half_notes = len(selected) >= 2 and all(length == minimum / 2 for length in lengths)
    if half_notes and len(selected) % 2 == 0:
        pairs = [f"5<{selected[i].text},{selected[i + 1].text}>"
                 for i in range(0, len(selected), 2)]
        add("5 双黑块阵", "偶数个等长半格音符，可逐对组成双黑块", ",".join(pairs))
    if half_notes:
        add("3 狂戳块", "每个音符均为最小正常长度的一半，满足等长规则", f"3<{raw}>")

    if resembles_tuplet(selected, minimum):
        add("~ 连音片段", "各音时长接近（与均值相差不超过 1P），"
            "总时长为正常长度的整数倍，可能是连音取整", merge_sounds(raw, "~"))

    if len(selected) >= 2 and max(lengths) < minimum / 2:
        add("7 长滑块", "连续细分短音；单独处理此片段，不纳入后一音", f"7<{raw}>")
        add("8 折叠滑块", "连续细分短音；单独处理此片段，不纳入后一音", f"8<{raw}>")

    if total >= minimum:
        add("6 长条（通用）", "总长度已达到最小正常长度，保持内部音符和总时长", f"6<{raw}>")
    else:
        if last + 1 < len(tokens) and tokens[last + 1].kind in ("note", "rest"):
            following = tokens[last + 1]
            if total + following.ticks >= minimum:
                label = "后一休止符" if following.kind == "rest" else "后一音"
                add(f"6 长条（并入{label}）", f"当前片段不足一格，纳入{label}后达到最小正常长度",
                    f"6<{raw},{following.text}>", end=last + 1)
        if first > 0 and tokens[first - 1].kind in ("note", "rest"):
            previous = tokens[first - 1]
            if total + previous.ticks >= minimum:
                label = "前一休止符" if previous.kind == "rest" else "前一音"
                add(f"6 长条（并入{label}）", f"当前片段不足一格，纳入{label}后达到最小正常长度",
                    f"6<{previous.text},{raw}>", begin=first - 1)
    add_dissolve()
    return result


def apply_suggestion(data, suggestion):
    section = data["musics"][suggestion.section]
    tokens = parse_score(section["scores"][suggestion.layer])
    if not 0 <= suggestion.first <= suggestion.last < len(tokens):
        raise ValueError("建议已过期，请重新选择片段")
    actual = section["scores"][suggestion.layer][tokens[suggestion.first].start:tokens[suggestion.last].end]
    if actual != suggestion.source:
        raise ValueError("片段已发生更改，请重新生成建议")
    replace_fragment(data, suggestion.section, suggestion.first, suggestion.last,
                     suggestion.replacement, layer=suggestion.layer)


def suggestion_rows(data, skipped=()):
    issues = [issue for issue in find_issues(data) if not issue.get("syntax")]
    ignored = [issue for issue in issues if issue_key(data, issue) in skipped]
    rows = []
    for issue in issues:
        if issue_key(data, issue) in skipped:
            continue
        options = suggest_changes(data, issue["section"], issue["first"], issue["last"], layer=issue["layer"])
        options = [option for option in options if not overlaps_issues(option, ignored)]
        if options:
            rows.append((issue, options))
    return rows


def overlaps_issues(plan, issues):
    return any(plan.section == issue["section"] and plan.layer == issue["layer"]
               and plan.first <= issue["last"] and plan.last >= issue["first"]
               for issue in issues)


def batch_result(data, plans, ignored=()):
    """Validate exactly the user's chosen changes; commit none on conflict."""
    trial = copy.deepcopy(data)
    occupied = {}
    for plan in sorted(plans, key=lambda s: (s.section, s.layer, s.first), reverse=True):
        if overlaps_issues(plan, ignored):
            raise ValueError(f"第 {plan.section + 1} 段的建议覆盖了已忽略片段，请切换方案或跳过。")
        if plan.last >= occupied.get((plan.section, plan.layer), math.inf):
            raise ValueError(f"第 {plan.section + 1} 段有两条已勾选建议的范围重叠，请切换方案或跳过其中一条。")
        apply_suggestion(trial, plan)
        occupied[plan.section, plan.layer] = plan.first
    return trial


def default_plans(rows):
    # Preserve right-to-left priority when the default choices overlap.
    choices = [options[0] for _, options in rows]
    triplets = [plan for plan in choices if plan.title == "~ 跳音三连音"]
    priority = set(triplets)
    priority_spans = [dict(section=plan.section, layer=plan.layer, first=plan.first, last=plan.last)
                      for plan in triplets]
    choices = [plan for plan in choices if plan in priority or not overlaps_issues(plan, priority_spans)]
    occupied = {}
    accepted = []
    for plan in sorted(choices,
                       key=lambda s: (s.section, s.layer, s.first), reverse=True):
        if plan.last < occupied.get((plan.section, plan.layer), math.inf):
            occupied[plan.section, plan.layer] = plan.first
            accepted.append(plan)
    return accepted


def prepare_all_suggestions(data, skipped=()):
    """Build the default change list and its uncommitted result."""
    plans = default_plans(suggestion_rows(data, skipped))
    return plans, batch_result(data, plans)


def apply_all_suggestions(data, skipped=()):
    """Apply a validated batch atomically, right to left."""
    accepted, trial = prepare_all_suggestions(data, skipped)
    if accepted:
        data.clear()
        data.update(trial)
    return len(accepted)
