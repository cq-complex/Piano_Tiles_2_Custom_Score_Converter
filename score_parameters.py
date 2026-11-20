"""Playback parameters shared by conversion and export."""
import math


def is_arena_score(data):
    return len(data["musics"]) != 3


def positive_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def effective_score_bpms(data):
    """Read playback tempos without filling missing fields in the document."""
    sections = data["musics"]
    if not any("bpm" not in section for section in sections):
        return [section["bpm"] for section in sections]
    fallback = data.get("baseBpm")
    if not positive_number(fallback):
        fallback = next((s["bpm"] for s in sections if positive_number(s.get("bpm"))), 120)
    return [section.get("bpm", fallback) for section in sections]


def section_bpms(base, step=0):
    try:
        base, step = float(base), float(step)
        values = [round(base + i * step, 6) for i in range(3)]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("请输入有效的 BPM 公差") from exc
    if not math.isfinite(step) or step < 0 or any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("BPM 公差必须为非负数，且三段 BPM 均须为有限正数")
    return values


def export_parameters(data):
    lines = []
    for i, (section, bpm) in enumerate(zip(data["musics"], effective_score_bpms(data)), 1):
        value = str(bpm) if "bpm" in section else f"未填写（试听参考 {bpm:g}）"
        lines.append(f'第 {i} 段：BPM {value}，基础拍长 {section["baseBeats"]}')
    return "\n".join(["各段参数（请在 APK 对应 CSV 中填写）：", *lines])


def rolling_speed_hint(speed):
    """Return the text and foreground color for a speed in tiles per second."""
    for upper, text, color in (
        (2, "警告：速度过慢，体验可能较差", "#9a5200"),
        (5, "比较慢，适合初学者", "#25613a"),
        (8, "中等速度，适合有一定经验的玩家", "#215d88"),
        (10, "较快，适合经验丰富的玩家", "#63428d"),
        (13, "特别快，适合音游大手子", "#a54308"),
    ):
        if speed < upper:
            return text, color
    return "警告：谱面滚动速度可能过快，难以游玩", "#b3261e"
