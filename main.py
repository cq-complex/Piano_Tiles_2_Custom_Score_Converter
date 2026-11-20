"""MIDI to Piano Tiles 2 custom JSON converter."""
import json
import math
import re
import sys
from pathlib import Path
from app_version import __version__
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import pretty_midi
import numpy as np
from score_editor import (NOTE_FORM, SOUND_ATOM, SPECIAL_SOUNDS, ScoreEditor, find_issues, inspect_score, parse_score)
from musicxml_input import read_musicxml
from notation_keys import key_fit_label, local_key_events, source_key_events
from json_input import read_score_document, JsonSyntaxDialog, ensure_audition
from audition_dialog import AuditionDialog
from endless_speed import EndlessSpeedDialog
from score_splitting import choose_boundaries, slice_score
from score_pitch import pitch_name
from window_layout import size_window
from score_measures import measure_beats, crosses_measure
from score_parameters import (effective_score_bpms, export_parameters, is_arena_score,
                              positive_number, rolling_speed_hint, section_bpms)
from score_timeline import section_boundaries
from midi_preview import SystemMidiPlayer, build_split_preview_midi, pitch_number

LENGTHS = [(256, "H"), (128, "I"), (64, "J"), (32, "K"),
           (16, "L"), (8, "M"), (4, "N"), (2, "O"), (1, "P")]
RESTS = dict(zip("HIJKLMNOP", "QRSTUVWXY"))
# Score notation is fixed: [L] = half a quarter-note beat, [P] = 1/32.
# baseBeats controls gameplay and must never change this conversion.
TICK_BEATS = 1 / 32


def selected_track_indices(midi, main, accompaniment=None):
    if not isinstance(main, int) or not 0 <= main < len(midi.instruments):
        raise ValueError("请选择有效的主旋律音轨")
    if accompaniment == "all":
        others = [i for i in range(len(midi.instruments)) if i != main]
    elif accompaniment is None:
        others = []
    else:
        others = list(accompaniment) if isinstance(accompaniment, (list, tuple)) else [accompaniment]
    if any(not isinstance(i, int) or not 0 <= i < len(midi.instruments) for i in others):
        raise ValueError("伴奏音轨序号无效")
    if main in others or len(set(others)) != len(others):
        raise ValueError("主旋律与伴奏不能重复选择同一音轨")
    return [main, *others]


def key_histogram(midi):
    histogram = np.zeros(12)
    for track in midi.instruments:
        if not track.is_drum:
            for note in track.notes:
                if note.end > note.start and note.velocity > 0:
                    histogram[note.pitch % 12] += note.end - note.start
    return histogram


def score_key_histogram(data):
    histogram = np.zeros(12)

    def add(token, beat_seconds):
        if token.children:
            for child in token.children:
                add(child, beat_seconds)
        elif token.kind == "note":
            sound = NOTE_FORM.fullmatch(token.text)[1]
            groups = re.split(r"[~@]", sound.strip("()"))
            weight = token.ticks * beat_seconds / len(groups)
            for group in groups:
                for name in group.split("."):
                    if name not in SPECIAL_SOUNDS:
                        histogram[pitch_number(name, piano_range=False) % 12] += weight

    for section, bpm in zip(data["musics"], effective_score_bpms(data)):
        for line in section["scores"]:
            for token in inspect_score(line)[0]:
                add(token, 60 / bpm)
    return histogram


def transpose_score(data, semitones):
    """Transpose score strings atomically, preserving notation and metadata."""
    def transform(line):
        result, cursor = [], 0
        for token in parse_score(line):
            result.append(line[cursor:token.start])
            if token.children:
                prefix, _, inside = token.text.partition("<")
                result.append(prefix + "<" + transform(inside[:-1]) + ">")
            else:
                match = NOTE_FORM.fullmatch(token.text)
                if match:
                    def transpose_atom(match):
                        atom = match[0]
                        return atom if atom in SPECIAL_SOUNDS else pitch_name(
                            pitch_number(atom, piano_range=False) + semitones, piano_range=False)

                    sound = re.sub(SOUND_ATOM, transpose_atom, match[1])
                    result.append(sound + token.text[match.end(1):])
                else:
                    result.append(token.text)
            cursor = token.end
        result.append(line[cursor:])
        return "".join(result)

    lines = [[transform(line) for line in section["scores"]] for section in data["musics"]]
    for section, scores in zip(data["musics"], lines):
        section["scores"] = scores


def estimate_key(histogram, transpose=0):
    return key_fit_label(histogram, transpose)


def minimum_normal_note(exponent):
    if exponent >= 5:
        return "所有音符均可正常显示"
    if exponent <= -3:
        return f"H*{2 ** (-3 - exponent)}"
    return "IJKLMNO"[exponent + 2]


def blocks_per_second(bpm, base_beats):
    if not math.isfinite(bpm) or bpm <= 0 or not math.isfinite(base_beats) or base_beats <= 0:
        raise ValueError("BPM 和基础拍长必须是有限正数")
    speed = bpm / base_beats / 60
    if not math.isfinite(speed):
        raise ValueError("速度超出可表示范围")
    return speed


def parse_beat_scale(value):
    try:
        scale = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("手动缩放比例必须是有限正数，例如 0.8 或 1.25") from exc
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("手动缩放比例必须是有限正数，例如 0.8 或 1.25")
    return scale


def base_beats_from_exponent(value):
    try:
        exponent = int(value)
        result = 0.5 ** exponent
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("基础拍长指数 x 必须是有效整数") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("基础拍长指数超出可表示范围")
    return result


def enable_dpi_awareness():
    """Ask Windows to render Tk at the monitor's native resolution."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        # Per-monitor DPI awareness v2, available on recent Windows versions.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def beat_at(midi, seconds):
    times, bpms = midi.get_tempo_changes()
    beats = 0.0
    for i, start in enumerate(times):
        if seconds <= start:
            break
        stop = min(seconds, times[i + 1] if i + 1 < len(times) else seconds)
        beats += (stop - start) * bpms[i] / 60
        if stop == seconds:
            break
    return beats


def pieces(ticks, rest=False):
    result = []
    for size, letter in LENGTHS:
        while ticks >= size:
            result.append(RESTS[letter] if rest else letter)
            ticks -= size
    return result


def alignment_samples(midi, tracks):
    """Prepare bounded samples in quarter-note beats for live tempo scoring."""
    notes = [n for track in tracks for n in track.notes if n.end > n.start and n.velocity > 0]
    if not notes:
        return (np.array([]),) * 3
    starts = np.array([n.start for n in notes])
    ends = np.array([n.end for n in notes])
    times, bpms = midi.get_tempo_changes()
    cumulative = np.concatenate(([0.0], np.cumsum(np.diff(times) * bpms[:-1] / 60)))

    def beats(seconds):
        indexes = np.maximum(0, np.searchsorted(times, seconds, side="right") - 1)
        return cumulative[indexes] + (seconds - times[indexes]) * bpms[indexes] / 60

    unique_starts = np.unique(starts)
    positions = beats(unique_starts)
    intervals = np.diff(positions)[np.diff(unique_starts) > 0.025]
    positions -= positions[0]
    durations = beats(ends) - beats(starts)

    def sample(values, limit=512):
        return values[np.linspace(0, len(values) - 1, min(limit, len(values))).astype(int)]

    return tuple(sample(values) for values in (positions, intervals, durations))


def alignment_quality(samples, scale=1.0):
    """Same 1/4-beat grid and weighting for automatic and manual correction."""
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("节拍缩放比例必须是有限正数")
    if not len(samples[0]):
        return None
    result = weight_sum = 0.0
    for values, weight in zip(samples, (0.5, 0.35, 0.15)):
        if not len(values):
            continue
        with np.errstate(over="ignore", invalid="ignore"):
            units = values * scale / 0.25
        if not np.all(np.isfinite(units)):
            raise ValueError("缩放比例过大，无法计算对齐评分")
        errors = np.abs(units - np.rint(units))
        result += weight * float(np.mean(np.maximum(0, 1 - errors / 0.16)))
        weight_sum += weight
    return result / weight_sum


def detect_beat_scale(midi, tracks, samples=None):
    """Infer a regular sixteenth-note grid; retain original tempo if ambiguous.

    Tempo notation has half/double-time ambiguity. Prefer the candidate nearest
    the original tempo and require a substantial improvement before applying it.
    """
    original = float(midi.get_tempo_changes()[1][0])
    unchanged = {"scale": 1.0, "bpm": original, "message": "未发现需要校正的节拍"}
    if len(midi.get_tempo_changes()[0]) > 1:
        return {**unchanged, "message": "含中途变速，自动校正未应用"}
    if samples is None:
        samples = alignment_samples(midi, tracks)
    positions, intervals, durations = samples
    if len(positions) < 8:
        return {**unchanged, "message": "有效起音少于 8 个，保留原 BPM"}
    if not len(intervals):
        return unchanged
    low, high = max(40, original / 2), min(240, original * 2)
    candidates = set(np.arange(low, high + 0.01, 0.5))
    candidates.add(original)
    for interval in intervals:
        for beats in (0.125, 0.25, 0.5, 1, 1.5, 2, 3, 4):
            bpm = original * beats / interval
            if low <= bpm <= high:
                candidates.add(round(bpm, 6))
    def quality(bpm):
        return alignment_quality(samples, bpm / original)
    baseline = quality(original)
    best = max(candidates, key=lambda bpm: quality(bpm) - 0.04 * abs(math.log2(bpm / original)))
    improved = quality(best)
    if baseline >= 0.9 or improved < 0.75 or improved - baseline < 0.15:
        if baseline >= 0.9:
            message = f"原节拍对齐评分 {baseline:.0%}，保留 BPM {original:g}"
        else:
            message = (f"未找到可靠校正：原评分 {baseline:.0%}；最佳候选 BPM {best:.6g}，"
                       f"评分 {improved:.0%}。当前保留原值，可关闭自动后手动缩放。")
        return {**unchanged, "message": message}
    return {"scale": best / original, "bpm": best,
            "message": (f"BPM {original:g} → {best:.6g}，节拍 ×{best / original:.6g}；"
                        f"对齐评分 {baseline:.0%} → {improved:.0%}")}


def score(midi, instrument, origin, start_tick, end_tick, transpose=0, beat_scale=1.0):
    groups = {}
    if instrument is not None:
        for note in instrument.notes:
            if note.velocity <= 0 or note.end <= note.start:
                continue
            note_start = round((beat_at(midi, note.start) - origin) * beat_scale / TICK_BEATS)
            note_end = max(note_start + 1, round((beat_at(midi, note.end) - origin) * beat_scale / TICK_BEATS))
            if note_start < end_tick and note_end > start_tick:
                start = max(note_start, start_tick)
                groups.setdefault(start, []).append((pitch_name(note.pitch + transpose), note_end))
    starts = sorted(groups)
    tokens = []
    cursor = start_tick
    for i, start in enumerate(starts):
        if start > cursor:
            tokens.extend(pieces(start - cursor, True))
            cursor = start
        if start < cursor:
            continue
        next_start = starts[i + 1] if i + 1 < len(starts) else end_tick
        end = min(max(end for _, end in groups[start]), next_start, end_tick)
        if end <= start:
            continue
        names = sorted({name for name, _ in groups[start]})
        sound = names[0] if len(names) == 1 else "(" + ".".join(names) + ")"
        # Combine only the duration of this event. Never combine separate
        # MIDI note attacks, even when they have the same pitch.
        duration = "".join(pieces(end - start))
        tokens.append(f"{sound}[{duration}]")
        cursor = end
    if cursor < end_tick:
        tokens.extend(pieces(end_tick - cursor, True))
    return ",".join(tokens), len(tokens)


def merged_score(midi, tracks, origin, end_tick, transpose=0, beat_scale=1.0):
    """Merge original attacks only; never repeat a held note at later attacks."""
    groups = {}
    for track in tracks:
        for note in track.notes:
            if note.velocity <= 0 or note.end <= note.start:
                continue
            pitch = note.pitch + transpose
            pitch_name(pitch)
            start = max(0, round((beat_at(midi, note.start) - origin) * beat_scale / TICK_BEATS))
            end = min(end_tick, max(start + 1, round(
                (beat_at(midi, note.end) - origin) * beat_scale / TICK_BEATS)))
            if start < end:
                groups.setdefault(start, []).append((pitch, end))
    starts = sorted(groups)
    result, cursor = [], 0
    for i, start in enumerate(starts):
        if start > cursor:
            result.extend(pieces(start - cursor, True))
        next_attack = starts[i + 1] if i + 1 < len(starts) else end_tick
        end = min(max(stop for _, stop in groups[start]), next_attack)
        # Simultaneous unisons are one attack. A later same-pitch MIDI note
        # remains a separate attack, including when an earlier note is held.
        names = [pitch_name(pitch) for pitch in sorted({pitch for pitch, _ in groups[start]})]
        sound = names[0] if len(names) == 1 else "(" + ".".join(names) + ")"
        result.append(sound + "[" + "".join(pieces(end - start)) + "]")
        cursor = end
    if cursor < end_tick:
        result.extend(pieces(end_tick - cursor, True))
    return ",".join(result)


def apply_musicxml_notation(midi, line, tracks, origin, beat_scale, transpose):
    """Fold marked groups only when their attacks match the rendered track."""
    groups = [group for track in tracks for group in getattr(track, "musicxml_groups", ())]
    if not groups:
        return line
    tokens = parse_score(line)
    positions = [0]
    for token in tokens:
        positions.append(positions[-1] + token.ticks)
    indices = {position: index for index, position in enumerate(positions)}

    def tick(beat):
        at = midi.tick_to_time(round(beat * midi.resolution))
        return round((beat_at(midi, at) - origin) * beat_scale / TICK_BEATS)

    replacements = {}
    occupied = set()
    for connector, start, end, attacks in sorted(groups, key=lambda group: group[1]):
        left, right = indices.get(tick(start)), indices.get(tick(end))
        if left is None or right is None:
            continue
        marked_staccato = connector == "staccato~"
        attack_indices = [indices.get(tick(beat)) for beat, _ in attacks]
        if marked_staccato:
            if (any(index is None or not left <= index < right for index in attack_indices)
                    or len(set(attack_indices)) != len(attacks)):
                continue
            attack_set = set(attack_indices)
            if any(index in occupied or (index not in attack_set and tokens[index].kind != "rest")
                   for index in range(left, right)):
                continue
            connector = "~"
        elif right - left != len(attacks):
            continue
        sounds = []
        for index, (beat, pitches) in zip(attack_indices if marked_staccato else range(left, right), attacks):
            token = tokens[index]
            if index in occupied or token.kind != "note" or positions[index] != tick(beat):
                break
            expected = [pitch_name(pitch + transpose) for pitch in pitches]
            existing = token.text.rsplit("[", 1)[0].strip("()").split(".")
            if set(existing) != set(expected):
                break
            sounds.append("@".join(expected) if connector == "@" else ".".join(existing))
        else:
            duration = "".join(pieces(positions[right] - positions[left]))
            replacements[left] = (right, f"({connector.join(sounds)})[{duration}]")
            occupied.update(range(left, right))

    result = []
    index = 0
    while index < len(tokens):
        if index in replacements:
            index, replacement = replacements[index]
            result.append(replacement)
        else:
            result.append(tokens[index].text)
            index += 1
    return ",".join(result)


def apply_musicxml_staccatos(midi, line, tracks, origin, scale, transpose, base_beats):
    """Resize marked attacks only within their own notated note/rest slot."""
    from bisect import bisect_left, bisect_right
    from itertools import accumulate

    marked = [mark for track in tracks for mark in getattr(track, "musicxml_staccatos", ())]
    if not marked:
        return line
    target = math.ceil(base_beats * 32)

    def tick(beat):
        return round((float(beat) - origin) * scale / TICK_BEATS)

    notes = sorted((tick(beat_at(midi, note.start)), tick(beat_at(midi, note.end)),
                    note.pitch)
                   for track in tracks for note in track.notes if note.velocity > 0)
    starts = [start for start, _, _ in notes]
    earlier_end = list(accumulate((end for _, end, _ in notes), max))
    measures = sorted({tick(beat) for beat in getattr(midi, "musicxml_measures", ())})
    tokens = parse_score(line)
    positions = [0, *accumulate(token.ticks for token in tokens)]
    indices = {position: index for index, position in enumerate(positions)}
    replacements = {}
    for start, stop, pitches in marked:
        left, right = tick(start), tick(stop)
        index = indices.get(left)
        if index is None or index >= len(tokens) or index in replacements:
            continue
        token = tokens[index]
        middle = positions[index + 1]
        if (token.kind != "note" or target == token.ticks or target > right - left
                or middle > right):
            continue
        expected = sorted(pitches)
        first, last = bisect_left(starts, left), bisect_right(starts, left)
        if (first and earlier_end[first - 1] > left
                or sorted(pitch for _, _, pitch in notes[first:last]) != expected
                or any(end != middle for _, end, _ in notes[first:last])
                or last < len(notes) and starts[last] < right):
            continue
        written = token.text.rsplit("[", 1)[0].strip("()")
        if set(written.split(".")) != {pitch_name(pitch + transpose) for pitch in pitches}:
            continue
        j = bisect_right(measures, left)
        if j < len(measures) and measures[j] < max(middle, left + target):
            continue
        end_index = index + 1
        while end_index < len(tokens) and positions[end_index] < right:
            if tokens[end_index].kind != "rest":
                break
            end_index += 1
        if positions[end_index] < left + target:
            continue
        # Re-emit any unused time in the consumed rest token(s); the next attack stays fixed.
        sound = token.text.rsplit("[", 1)[0]
        new = [sound + "[" + "".join(pieces(target)) + "]"]
        rest_end = min(right, positions[end_index])
        new.extend(pieces(rest_end - left - target, True))
        new.extend(pieces(positions[end_index] - rest_end, True))
        replacements[index] = end_index, new

    if not replacements:
        return line
    output, index = [], 0
    while index < len(tokens):
        if index in replacements:
            index, new = replacements[index]
            output.extend(new)
        else:
            output.append(tokens[index].text)
            index += 1
    return ",".join(output)


def convert_midi(midi, main_index, accompaniment_index=None, base_beats=0.5, transpose=0,
                 beat_scale=1.0, split_points=None, split_info=None, merge_all_tracks=False,
                 mark_measures=None, bpm_step=0):
    if not math.isfinite(base_beats) or base_beats <= 0:
        raise ValueError("基础拍长必须大于零")
    if isinstance(transpose, bool) or not isinstance(transpose, int):
        raise ValueError("移调值必须是整数半音")
    if not math.isfinite(beat_scale) or beat_scale <= 0:
        raise ValueError("节拍缩放比例必须是有限正数")
    tracks = (list(midi.instruments) if merge_all_tracks else
              [midi.instruments[i] for i in selected_track_indices(midi, main_index, accompaniment_index)])
    notes = [note for track in tracks for note in track.notes if note.end > note.start]
    if not notes:
        raise ValueError("所选音轨没有音符")
    origin = min(beat_at(midi, note.start) for note in notes)
    end_beat = max(beat_at(midi, note.end) for note in notes)
    marked_ends = [float(stop) for track in tracks for connector, _, stop, _ in
                   getattr(track, "musicxml_groups", ()) if connector == "staccato~"]
    marked_ends.extend(float(stop) for track in tracks for _, stop, _ in
                       getattr(track, "musicxml_staccatos", ()))
    end_beat = max(end_beat, max(marked_ends, default=end_beat))
    end_tick = max(3, round((end_beat - origin) * beat_scale / TICK_BEATS))
    full_scores = ([merged_score(midi, tracks, origin, end_tick, transpose, beat_scale)]
                   if merge_all_tracks else
                   [score(midi, track, origin, 0, end_tick, transpose, beat_scale)[0]
                    for track in tracks])
    source_tracks = [tracks] if merge_all_tracks else [[track] for track in tracks]
    full_scores = [apply_musicxml_notation(midi, line, source, origin, beat_scale, transpose)
                   for line, source in zip(full_scores, source_tracks)]
    source_measures = measure_beats(midi) if mark_measures is not False else ()
    if mark_measures is None and crosses_measure(midi, source_measures):
        source_measures = ()
    measures = sorted({
        tick for beat in source_measures
        if 0 < (tick := round((float(beat) - origin) * beat_scale / TICK_BEATS)) < end_tick
    })
    adjusted = [apply_musicxml_staccatos(midi, line, source, origin, beat_scale, transpose, base_beats)
                for line, source in zip(full_scores, source_tracks)]
    try:
        boundaries, split_message = choose_boundaries(adjusted, base_beats, split_points, measures)
    except ValueError:
        if adjusted == full_scores:
            raise
        boundaries, split_message = choose_boundaries(full_scores, base_beats, split_points, measures)
    else:
        full_scores = adjusted
    if split_info is not None:
        notation_tracks = [midi.instruments[main_index]] if merge_all_tracks and (
            0 <= main_index < len(midi.instruments)) else [source[0] for source in source_tracks]
        events = source_key_events(midi, notation_tracks, origin, beat_scale, transpose, beat_at)
        notation_keys = [[local_key_events(track, left, right) for track in events]
                         for left, right in zip(boundaries, boundaries[1:])]
        split_info.update(boundaries=boundaries, message=split_message, notation_keys=notation_keys,
                          measure_ticks=tuple(measures))
    bpm = round(float(midi.get_tempo_changes()[1][0]) * beat_scale, 6)
    tempos = section_bpms(bpm, bpm_step)
    musics = []
    first_count = 0
    for section in range(3):
        rendered = [slice_score(line, boundaries[section], boundaries[section + 1], measures)
                    for line in full_scores]
        if section == 0:
            first_count = rendered[0][1]
        musics.append({"bpm": tempos[section], "baseBeats": base_beats,
                       "scores": [text for text, _ in rendered], "id": section + 1,
                       "instruments": ["piano"] * len(full_scores),
                       "alternatives": [""] * len(full_scores)})
    return {"baseBpm": bpm, "musics": musics,
            "audition": {"start": [0, 0], "end": [0, min(16, first_count - 1)]}}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self._last_dpi = None
        self._update_scaling()
        self.bind("<Configure>", self._on_configure)
        self.title(f"钢琴块2自制谱转换器 v{__version__}")
        size_window(self, 1320, 1020, 820, 620)
        self.midi = None
        self.imported_score = None
        self._bpm_override = None
        self._json_transpose = 0
        self.path = None
        self._score_documents = {}
        self._ignored_fragments = {}
        self._auto_splits = {}
        self._split_messages = {}
        self.split_player = SystemMidiPlayer()
        self._split_playback_poll = None
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        canvas = tk.Canvas(self, highlightthickness=0, background=self.cget("background"))
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=canvas.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        frame = ttk.Frame(canvas, padding=16)
        content = canvas.create_window(0, 0, window=frame, anchor="nw")
        frame.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox(content)))
        ttk.Button(frame, text="导入 MIDI / MusicXML / JSON", command=self.open_midi).grid(row=0, column=0)
        self.filename = ttk.Label(frame, text="尚未选择文件", width=48)
        self.filename.grid(row=0, column=1, padx=10)
        ttk.Label(frame, text="主旋律音轨").grid(row=1, column=0, pady=8)
        self.main_track = ttk.Combobox(frame, state="readonly", width=45)
        self.main_track.grid(row=1, column=1)
        self.merge_all_tracks = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="合并全部音轨", variable=self.merge_all_tracks,
                        command=self.update_merge_mode).grid(row=1, column=2, padx=8, sticky="w")
        ttk.Label(frame, text="伴奏音轨").grid(row=2, column=0, pady=8)
        self.accompaniment = ttk.Combobox(frame, state="readonly", width=45)
        self.accompaniment.grid(row=2, column=1)
        ttk.Label(frame, text="基础拍长 0.5^x").grid(row=3, column=0, pady=8)
        self.base_exponent = tk.StringVar(value="1")
        beat_input = ttk.Frame(frame)
        beat_input.grid(row=3, column=1, sticky="w")
        ttk.Spinbox(beat_input, from_=-20, to=20, textvariable=self.base_exponent,
                    width=10).grid(row=0, column=0)
        self.base_value = tk.StringVar()
        ttk.Label(beat_input, textvariable=self.base_value).grid(row=0, column=1, padx=10)
        self.minimum_note = tk.StringVar()
        ttk.Label(beat_input, textvariable=self.minimum_note).grid(row=0, column=2, padx=10)
        self.base_exponent.trace_add("write", self._update_base_value)
        self._update_base_value()
        ttk.Label(frame, text="移调（半音）").grid(row=4, column=0, pady=8)
        self.transpose = tk.StringVar(value="0")
        transpose_frame = ttk.Frame(frame)
        transpose_frame.grid(row=4, column=1, sticky="w")
        ttk.Spinbox(transpose_frame, from_=-87, to=87, textvariable=self.transpose, width=10).grid(
            row=0, column=0)
        self.key_label = tk.StringVar(value="推测调式：尚未选择 MIDI")
        self._key_histogram = None
        ttk.Label(transpose_frame, textvariable=self.key_label, wraplength=560).grid(row=0, column=1, padx=10)
        self.transpose.trace_add("write", self.update_key)
        tempo_options = ttk.Frame(frame)
        tempo_options.grid(row=5, column=0, columnspan=2, sticky="w", pady=8)
        self.auto_tempo = tk.BooleanVar(value=False)
        ttk.Checkbutton(tempo_options, text="自动校正节拍（保持播放速度）",
                        variable=self.auto_tempo, command=self.update_tempo_report).grid(row=0, column=0)
        ttk.Button(tempo_options, text="检测节拍", command=self.detect_tempo).grid(row=0, column=1, padx=8)
        ttk.Label(tempo_options, text="手动缩放比例").grid(row=1, column=0, sticky="w", pady=6)
        self.manual_scale = tk.StringVar(value="1")
        self.scale_input = ttk.Entry(tempo_options, textvariable=self.manual_scale, width=12)
        self.scale_input.grid(row=1, column=1, sticky="w")
        self.scaled_bpm = tk.StringVar(value="关闭自动时生效；1 表示不缩放")
        ttk.Label(tempo_options, textvariable=self.scaled_bpm).grid(row=1, column=2, padx=8)
        self.tempo_report = tk.StringVar(value="选择 MIDI 后可检测节拍；检测结果为推测，可取消校正。")
        self.speed_text = tk.StringVar(value="当前速度：尚未选择 MIDI")
        ttk.Label(tempo_options, textvariable=self.speed_text).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=4)
        self.arena_speed_section = ttk.Combobox(tempo_options, state="readonly", width=10)
        self.arena_speed_section.grid(row=2, column=2, sticky="e", padx=8)
        self.arena_speed_section.grid_remove()
        self.arena_speed_section.bind("<<ComboboxSelected>>", lambda _: self.update_speed())
        self.speed_hints = []
        for row in range(3, 6):
            hint = ttk.Label(tempo_options)
            hint.grid(row=row, column=0, columnspan=3, sticky="w")
            hint.grid_remove()
            self.speed_hints.append(hint)
        ttk.Label(frame, textvariable=self.tempo_report, wraplength=580).grid(
            row=6, column=0, columnspan=2, sticky="w")
        self._tempo_cache = {}
        self._alignment_cache = {}
        self.manual_scale.trace_add("write", self.update_tempo_report)
        for widget in (self.main_track, self.accompaniment):
            widget.bind("<<ComboboxSelected>>", self.update_tempo_report)
        split_frame = ttk.LabelFrame(frame, text="三段分割", padding=8)
        self.split_frame = split_frame
        split_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.split_mode = tk.StringVar(value="自动避开异常片段")
        split_selector = ttk.Combobox(split_frame, state="readonly", textvariable=self.split_mode,
                                     values=("自动避开异常片段", "自选分割点"), width=22)
        split_selector.grid(row=0, column=0, padx=(0, 8))
        split_selector.bind("<<ComboboxSelected>>", self.update_split_preview)
        self.split_first = tk.StringVar(value="")
        self.split_second = tk.StringVar(value="")
        self.split_entries = []
        for column, (label, variable) in enumerate(
                (("分割点1", self.split_first), ("分割点2", self.split_second)), 1):
            ttk.Label(split_frame, text=label).grid(row=0, column=column * 2 - 1)
            entry = ttk.Entry(split_frame, textvariable=variable, width=10, state="disabled")
            entry.grid(row=0, column=column * 2, padx=5)
            self.split_entries.append(entry)
        ttk.Button(split_frame, text="查看分段", command=self.update_split_preview).grid(row=0, column=5, padx=5)
        ttk.Button(split_frame, text="重新自动规划", command=self.replan_splits).grid(row=0, column=6)
        ttk.Label(split_frame, text="分割点单位：四分音符拍；以所选音轨首个音符为 0，"
                  "使用节拍缩放后的坐标。分割点两侧须为正常主旋律音符。",
                  wraplength=700).grid(row=1, column=0, columnspan=7, sticky="w", pady=4)
        self.split_report = tk.StringVar(value="读取 MIDI 后点击“查看分段”，可查看三段区间。")
        ttk.Label(split_frame, textvariable=self.split_report, wraplength=700).grid(
            row=2, column=0, columnspan=7, sticky="w")
        playback = ttk.Frame(split_frame)
        playback.grid(row=3, column=0, columnspan=7, sticky="w", pady=(6, 0))
        for point in (1, 2):
            for before, side in ((True, "前"), (False, "后")):
                ttk.Button(playback, text=f"分割点 {point} · 试听{side} 5 秒",
                           command=lambda i=point, b=before: self.play_split_preview(i, b)).pack(
                               side="left", padx=(0, 5))
        self.split_stop_button = ttk.Button(playback, text="停止试听",
                                            command=self.stop_split_preview, state="disabled")
        self.split_stop_button.pack(side="left")
        self.split_playback_status = tk.StringVar()
        ttk.Label(split_frame, textvariable=self.split_playback_status).grid(
            row=4, column=0, columnspan=7, sticky="w")
        settings = ttk.Frame(frame)
        settings.grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.mark_measures = tk.BooleanVar(value=False)
        self.measure_warning = False
        self.measure_toggle = ttk.Checkbutton(
            settings, text="用 ; 分隔小节（自动分段优先小节边界）",
            variable=self.mark_measures, command=self.change_measure_setting, state="disabled")
        self.measure_toggle.grid(row=0, column=0, sticky="w")
        self.measure_status = tk.StringVar(value="导入有小节信息的乐谱后可用")
        ttk.Label(settings, textvariable=self.measure_status, wraplength=405).grid(
            row=0, column=1, columnspan=2, sticky="w", padx=8)
        self.tempo_gradient = tk.BooleanVar(value=False)
        self.gradient_toggle = ttk.Checkbutton(settings, text="3段梯度增速",
                                               variable=self.tempo_gradient, command=self.change_gradient)
        self.gradient_toggle.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.bpm_step = tk.StringVar(value="5")
        self.gradient_input = ttk.Entry(settings, textvariable=self.bpm_step, width=8, state="disabled")
        ttk.Label(settings, text="BPM 公差").grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))
        self.gradient_input.grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Button(settings, text="重设速度", command=self.reset_speed).grid(
            row=1, column=3, sticky="w", padx=(10, 0), pady=(6, 0))
        ttk.Button(settings, text="乐谱无尽段速度计算", command=self.open_endless_speed).grid(
            row=1, column=4, sticky="w", padx=(10, 0), pady=(6, 0))
        self.bpm_step.trace_add("write", lambda *_: self.update_speed())
        actions = ttk.Frame(self, padding=(16, 8, 16, 12))
        actions.grid(row=2, column=0, columnspan=2, sticky="e")
        ttk.Button(actions, text="谱面模拟器…", command=self.open_simulator).grid(
            row=0, column=3, padx=(8, 0))
        ttk.Button(actions, text="编辑 / 试听歌单片段", command=self.edit_audition).grid(
            row=0, column=0, padx=(0, 8))
        ttk.Button(actions, text="预览 / 校正非正常音符", command=self.preview_json).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(actions, text="生成 JSON", command=self.save_json).grid(row=0, column=2)

    def _update_base_value(self, *_):
        try:
            value = base_beats_from_exponent(self.base_exponent.get())
            self.base_value.set(f"= {value:.12g}")
            self.minimum_note.set("当前最小正常音符：" + minimum_normal_note(int(self.base_exponent.get())))
        except ValueError:
            self.base_value.set("请输入有效整数 x")
            self.minimum_note.set("")
        self.update_speed()

    def configure_measures(self):
        measures = measure_beats(self.midi) if self.midi is not None else ()
        available = any(beat > 0 for beat in measures)
        self.measure_warning = available and crosses_measure(self.midi, measures)
        self.measure_toggle.configure(state="normal" if available else "disabled")
        self.mark_measures.set(available and not self.measure_warning)
        self.update_measure_status(available)

    def update_measure_status(self, available=True):
        if not available:
            self.measure_status.set("没有可用的小节边界")
        elif self.measure_warning and not self.mark_measures.get():
            self.measure_status.set("检测到跨小节音符，默认关闭")
        elif self.measure_warning:
            self.measure_status.set("跨小节的音符保留原样，该轨跳过被跨越的分号")
        else:
            self.measure_status.set("")

    def change_measure_setting(self):
        if self.mark_measures.get() and self.measure_warning:
            if not messagebox.askyesno("启用小节分隔？", "乐谱含跨小节音符。仍要启用吗？\n"
                                       "跨越小节线的音符会保留原样，该音轨不会在音符中插入分号。",
                                       parent=self):
                self.mark_measures.set(False)
        self.update_measure_status()
        self.update_split_preview()

    def arena_loaded(self):
        return self.imported_score is not None and is_arena_score(self.imported_score)

    def configure_score_mode(self):
        arena = self.arena_loaded()
        self.gradient_toggle.configure(state="disabled" if arena else "normal")
        if arena:
            self.split_frame.grid_remove()
            self.arena_speed_section.configure(
                values=[f"第 {i} 段" for i in range(1, len(self.imported_score["musics"]) + 1)])
            self.arena_speed_section.current(0)
        else:
            self.split_frame.grid()

    def change_gradient(self):
        self.gradient_input.configure(
            state="normal" if self.tempo_gradient.get() and not self.arena_loaded() else "disabled")
        self.update_speed()

    def midi_output_base(self):
        original = float(self.midi.get_tempo_changes()[1][0])
        scale = (self.tempo_analysis()["scale"] if self.auto_tempo.get()
                 else parse_beat_scale(self.manual_scale.get()))
        return round(original * scale, 6)

    def open_endless_speed(self):
        if self.arena_loaded():
            messagebox.showwarning("乐谱无尽段速度计算", "此公式仅适用于普通三段谱面。", parent=self)
            return
        try:
            data = self.current_json()
        except ValueError as exc:
            messagebox.showwarning("无法计算无尽速度", str(exc), parent=self)
            return
        self.stop_split_preview()
        EndlessSpeedDialog(self, data)

    def reset_speed(self):
        if self.midi is None and self.imported_score is None:
            messagebox.showwarning("重设速度", "请先导入谱面。", parent=self)
            return
        if self.imported_score is not None:
            base = self._imported_base_bpm
        else:
            try:
                base = self.midi_output_base()
            except ValueError:
                base = float(self.midi.get_tempo_changes()[1][0])
        value = simpledialog.askfloat(
            "重设速度", "输入新的基础 BPM。只改变速度，保留谱面和分段。",
            initialvalue=self._bpm_override if self._bpm_override is not None else base,
            minvalue=0.000001, parent=self)
        if value is None:
            return
        if not positive_number(value):
            messagebox.showwarning("无法重设速度", "请输入有效的正数 BPM。", parent=self)
            return
        old = self._bpm_override
        self._bpm_override = value
        try:
            self.output_tempos(self.imported_score if self.imported_score is not None else {"baseBpm": base})
            self.stop_split_preview()
        except (ValueError, RuntimeError) as exc:
            self._bpm_override = old
            messagebox.showwarning("无法重设速度", str(exc), parent=self)
            return
        self.update_speed()

    def output_tempos(self, data):
        imported = data is self.imported_score
        base = self._imported_base_bpm if imported else data["baseBpm"]
        if self.tempo_gradient.get() and not self.arena_loaded():
            return section_bpms(self._bpm_override if self._bpm_override is not None else base,
                                self.bpm_step.get())
        if imported:
            if self._bpm_override is None:
                return self._imported_bpms
            # Preserve the original BPM differences between imported sections.
            result = [round(self._bpm_override + (bpm - base), 6) for bpm in self._imported_bpms]
            if not all(positive_number(bpm) for bpm in result):
                raise ValueError("此速度会让部分谱段的 BPM 无效，请选更大的数值。")
            return result
        return section_bpms(self._bpm_override if self._bpm_override is not None else base)

    def update_speed(self):
        if not hasattr(self, "speed_text"):
            return
        for hint in self.speed_hints:
            hint.grid_remove()
        self.arena_speed_section.grid_remove()
        if self.midi is None and self.imported_score is None:
            self.speed_text.set("当前速度：尚未选择文件")
            return
        try:
            if self.imported_score is not None:
                bpms = self.output_tempos(self.imported_score)
                bases = [s["baseBeats"] for s in self.imported_score["musics"]]
                if self.arena_loaded():
                    index = max(0, self.arena_speed_section.current())
                    bpm = bpms[index]
                    speed = blocks_per_second(bpm, bases[index])
                    if "bpm" in self.imported_score["musics"][index]:
                        self.scaled_bpm.set(f"选中段 BPM：{bpm:g}")
                    else:
                        self.scaled_bpm.set(f"选中段 BPM：未填写（试听参考 {bpm:g}）")
                    self.speed_text.set(f"第 {index + 1} 段：{speed:.3f} 块/s")
                    message, color = rolling_speed_hint(speed)
                    self.speed_hints[0].configure(text=message, foreground=color)
                    self.speed_hints[0].grid()
                    if len(bpms) > 1:
                        self.arena_speed_section.grid()
                    return
                self.scaled_bpm.set("各段 BPM：" + " / ".join(f"{b:g}" for b in bpms))
            else:
                base = base_beats_from_exponent(self.base_exponent.get())
                bpms = self.output_tempos({"baseBpm": self.midi_output_base()})
                bases = [base] * 3
            speeds = [blocks_per_second(bpm, base) for bpm, base in zip(bpms, bases)]
            if len(set(bpms)) > 1 or self.tempo_gradient.get() or self.imported_score is not None:
                text = "各段块/s：" + " / ".join(f"{speed:.3f}" for speed in speeds)
                if self.imported_score is None:
                    text += "（BPM " + " / ".join(f"{b:g}" for b in bpms) + "）"
            else:
                text = f"当前速度：{speeds[0]:.3f} 块/s"
                if self._bpm_override is not None:
                    text += f"（BPM {bpms[0]:g}）"
            self.speed_text.set(text)
            separate = len(set(speeds)) > 1
            for section, (hint, speed) in enumerate(zip(
                    self.speed_hints, speeds if separate else speeds[:1]), 1):
                message, color = rolling_speed_hint(speed)
                prefix = f"第 {section} 段： " if separate else ""
                hint.configure(text=prefix + message, foreground=color)
                hint.grid()
        except (ValueError, OverflowError):
            self.speed_text.set("当前速度：输入无效，请检查 BPM、公差和基础拍长")

    def update_split_preview(self, *_):
        manual = self.split_mode.get() == "自选分割点"
        for entry in self.split_entries:
            entry.configure(state="normal" if manual else "disabled")
        if self.midi is None:
            return
        try:
            self.current_json()
        except ValueError as exc:
            self.split_report.set(str(exc))

    def replan_splits(self):
        self._auto_splits.clear()
        self.split_mode.set("自动避开异常片段")
        self.update_split_preview()

    def play_split_preview(self, point, before):
        try:
            self.stop_split_preview()
            midi, duration = build_split_preview_midi(self.current_json(), point, before)
            self.split_player.play(midi)
        except (ValueError, RuntimeError, OSError, OverflowError) as exc:
            self.split_playback_status.set(f"无法试听：{exc}")
            return
        side = "前" if before else "后"
        self.split_playback_status.set(f"正在试听分割点 {point} 的{side}侧，约 {duration:.3f} 秒")
        self.split_stop_button.configure(state="normal")
        self._split_playback_poll = self.after(150, self.poll_split_preview)

    def poll_split_preview(self):
        self._split_playback_poll = None
        try:
            if self.split_player.is_playing():
                self._split_playback_poll = self.after(150, self.poll_split_preview)
                return
            self.stop_split_preview()
            self.split_playback_status.set("试听结束")
        except (RuntimeError, OSError) as exc:
            self.split_stop_button.configure(state="disabled")
            self.split_playback_status.set(f"试听失败：{exc}")

    def stop_split_preview(self):
        if self._split_playback_poll is not None:
            self.after_cancel(self._split_playback_poll)
            self._split_playback_poll = None
        self.split_player.stop()
        self.split_stop_button.configure(state="disabled")
        self.split_playback_status.set("")

    def destroy(self):
        try:
            self.stop_split_preview()
        finally:
            super().destroy()

    def tempo_analysis(self):
        if self.midi is None:
            raise ValueError("请先选择 MIDI 文件")
        main = self.main_track.current()
        accompaniment = self.accompaniment_selection()
        key = (main, accompaniment, self.merge_all_tracks.get())
        if key not in self._tempo_cache:
            tracks = self.analysis_tracks()
            self._tempo_cache[key] = detect_beat_scale(self.midi, tracks, self.tempo_samples())
        return self._tempo_cache[key]

    def tempo_samples(self):
        main = self.main_track.current()
        accompaniment = self.accompaniment_selection()
        if not 0 <= main < len(self.midi.instruments):
            raise ValueError("请选择主旋律音轨")
        key = (id(self.midi), main, accompaniment, self.merge_all_tracks.get())
        if key not in self._alignment_cache:
            tracks = self.analysis_tracks()
            self._alignment_cache[key] = alignment_samples(self.midi, tracks)
        return self._alignment_cache[key]

    def analysis_tracks(self):
        if self.merge_all_tracks.get():
            return list(self.midi.instruments)
        return [self.midi.instruments[i] for i in selected_track_indices(
            self.midi, self.main_track.current(), self.accompaniment_selection())]

    def update_merge_mode(self):
        state = "disabled" if self.merge_all_tracks.get() else "readonly"
        self.main_track.configure(state=state)
        self.accompaniment.configure(state=state)
        self.update_tempo_report()
        self.update_split_preview()

    def accompaniment_selection(self):
        if self.accompaniment.get() == "其余全部音轨":
            return "all"
        value = self.accompaniment.current() - 1
        return value if value >= 0 else None

    def sync_json_transpose(self, transpose):
        if transpose != self._json_transpose:
            transpose_score(self.imported_score, transpose - self._json_transpose)
            self._json_transpose = transpose

    def update_key(self, *_):
        if self.midi is None and self.imported_score is None:
            return
        try:
            transpose = int(self.transpose.get())
        except ValueError:
            self.key_label.set("推测调式：请输入整数移调值")
            return
        if self.imported_score is not None:
            try:
                self.sync_json_transpose(transpose)
            except ValueError as exc:
                self.key_label.set(f"无法移调：{exc}")
            else:
                self.key_label.set(estimate_key(score_key_histogram(self.imported_score)))
            return
        if self._key_histogram is None:
            self._key_histogram = key_histogram(self.midi)
        self.key_label.set(estimate_key(self._key_histogram, transpose))

    def update_tempo_report(self, *_):
        self.scale_input.configure(state="disabled" if self.auto_tempo.get() else "normal")
        self.update_speed()
        if self.midi is None:
            return
        if not self.auto_tempo.get():
            try:
                scale = parse_beat_scale(self.manual_scale.get())
                original = float(self.midi.get_tempo_changes()[1][0])
                if not math.isfinite(original * scale):
                    raise ValueError("缩放后的 BPM 超出可表示范围")
                samples = self.tempo_samples()
                quality = alignment_quality(samples, scale)
                baseline = alignment_quality(samples)
                score_label = (f"当前对齐评分 {quality:.1%}（原 {baseline:.1%}）"
                               if quality is not None else "对齐评分：暂无有效音符")
                self.scaled_bpm.set(f"BPM {original:g} → {original * scale:.6g}；{score_label}")
                self.tempo_report.set(f"手动节拍 ×{scale:g}，音符时值与 BPM 同比例缩放。")
            except ValueError as exc:
                self.scaled_bpm.set("当前 BPM / 对齐评分不可用")
                self.tempo_report.set(str(exc))
            return
        try:
            analysis = self.tempo_analysis()
            self.scaled_bpm.set(f"自动 BPM {analysis['bpm']:.6g}")
            self.tempo_report.set(analysis["message"])
        except ValueError as exc:
            self.scaled_bpm.set("当前 BPM / 对齐评分不可用")
            self.tempo_report.set(str(exc))

    def detect_tempo(self):
        if self.midi is None:
            messagebox.showwarning("未选择文件", "请先选择 MIDI 文件")
            return
        self.auto_tempo.set(True)
        self.update_tempo_report()

    def _update_scaling(self):
        if sys.platform != "win32":
            return
        import ctypes

        try:
            dpi = ctypes.windll.user32.GetDpiForWindow(self.winfo_id())
            if dpi and dpi != self._last_dpi:
                self.tk.call("tk", "scaling", dpi / 72)
                self._last_dpi = dpi
        except (AttributeError, OSError, tk.TclError):
            pass

    def _on_configure(self, event):
        if event.widget is self:
            self._update_scaling()

    def open_midi(self):
        path = filedialog.askopenfilename(filetypes=[
            ("支持的乐谱文件", "*.mid *.midi *.musicxml *.xml *.mxl *.json"),
            ("MIDI 文件", "*.mid *.midi"), ("MusicXML 文件", "*.musicxml *.xml *.mxl"),
            ("JSON 谱面", "*.json")])
        if not path:
            return
        extension = Path(path).suffix.lower()
        if extension == ".json":
            self.open_score_json(Path(path))
            return
        try:
            midi = (read_musicxml(path) if extension in (".musicxml", ".xml", ".mxl")
                    else pretty_midi.PrettyMIDI(path))
            labels = [f"{i}: {track.name or '未命名'} ({len(track.notes)} 音符)"
                      for i, track in enumerate(midi.instruments)]
            if not labels:
                raise ValueError("MIDI 文件没有音轨")
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        self.imported_score = None
        self._bpm_override = None
        self.midi, self.path = midi, Path(path)
        self.configure_score_mode()
        self.stop_split_preview()
        self._key_histogram = None
        self.update_key()
        self._tempo_cache.clear()
        self._alignment_cache.clear()
        self._score_documents.clear()
        self._ignored_fragments.clear()
        self._auto_splits.clear()
        self._split_messages.clear()
        self.filename.configure(text=self.path.name)
        self.main_track.configure(values=labels)
        self.accompaniment.configure(values=["无"] + labels + ["其余全部音轨"])
        melodic = [i for i, track in enumerate(midi.instruments)
                   if track.notes and not track.is_drum]
        self.main_track.current(melodic[0] if melodic else 0)
        self.accompaniment.current(len(labels) + 1 if len(labels) > 2 else
                                   melodic[1] + 1 if len(melodic) > 1 else 0)
        self.configure_measures()
        self.manual_scale.set("1")
        self.update_tempo_report()
        self.update_split_preview()

    def open_score_json(self, path):
        try:
            source = path.read_text(encoding="utf-8-sig")
            try:
                data = read_score_document(source)
            except json.JSONDecodeError:
                data = JsonSyntaxDialog(self, source).result
                if data is None:
                    return
        except (OSError, ValueError) as exc:
            messagebox.showerror("读取 JSON 失败", str(exc), parent=self)
            return
        self.stop_split_preview()
        self.midi, self.path, self.imported_score = None, path, data
        self._json_transpose = 0
        self._bpm_override = None
        self._imported_bpms = effective_score_bpms(data)
        base = data.get("baseBpm")
        self._imported_base_bpm = base if positive_number(base) else self._imported_bpms[0]
        self.configure_score_mode()
        self.tempo_gradient.set(False)
        self.change_gradient()
        self.configure_measures()
        self.transpose.set("0")
        self._score_documents.clear()
        self._ignored_fragments.clear()
        self._auto_splits.clear()
        self._split_messages.clear()
        self.filename.configure(text=path.name)
        self.main_track.configure(values=())
        self.main_track.set("")
        self.accompaniment.configure(values=())
        self.accompaniment.set("")
        self.update_tempo_report()
        self.update_key()
        count = len(data["musics"])
        if self.arena_loaded():
            self.tempo_report.set(f"竞技场谱面（{count} 段）：编辑和导出保留原段数及各段参数。")
        else:
            self.tempo_report.set("导入的 JSON 保留原三段及基础拍长，可选用梯度增速。")
        self.split_report.set("已导入 JSON，保留文件中的三段谱面。可直接预览、校正和导出。")
        self.preview_json()

    def apply_gradient(self, data):
        if is_arena_score(data) and self._bpm_override is None:
            return data
        bpms = self.output_tempos(data)
        if self._bpm_override is not None:
            data["baseBpm"] = self._bpm_override
        for section, bpm in zip(data["musics"], bpms):
            if "bpm" in section:
                section["bpm"] = bpm
        return data

    def on_visual_split(self, data, layout_prefix, notation_keys, measure_ticks):
        bounds = section_boundaries(data)
        if data is self.imported_score:
            return
        points = tuple(at / 32 for at in bounds[1:-1])
        key = (*layout_prefix, points)
        for old in [k for k, doc in self._score_documents.items() if doc is data]:
            self._score_documents.pop(old)
            self._split_messages.pop(old, None)
        message = "已在可视化谱面设置分段点"
        self._score_documents[key] = data
        self._split_messages[key] = dict(boundaries=bounds, message=message, notation_keys=notation_keys,
                                         measure_ticks=measure_ticks)
        self._current_document_key = key
        self.split_mode.set("自选分割点")
        self.split_first.set(f"{points[0]:.12g}")
        self.split_second.set(f"{points[1]:.12g}")
        for entry in self.split_entries:
            entry.configure(state="normal")
        self.split_report.set(message + f"：{points[0]:g} 拍、{points[1]:g} 拍。")

    def current_json(self):
        if self.imported_score is not None:
            self.sync_json_transpose(int(self.transpose.get()))
            ensure_audition(self.imported_score)
            return self.apply_gradient(self.imported_score)
        if self.midi is None:
            raise ValueError("请先选择 MIDI 文件")
        try:
            transpose = int(self.transpose.get())
        except ValueError as exc:
            raise ValueError("移调值必须是整数半音，例如 1 或 -1") from exc
        accompaniment = self.accompaniment_selection()
        scale = (self.tempo_analysis()["scale"] if self.auto_tempo.get()
                 else parse_beat_scale(self.manual_scale.get()))
        self.update_tempo_report()
        base_beats = base_beats_from_exponent(self.base_exponent.get())
        layout_key = (id(self.midi), self.main_track.current(), accompaniment, transpose, scale,
                      self.merge_all_tracks.get(), self.mark_measures.get())
        if any(getattr(track, "musicxml_staccatos", ()) for track in self.analysis_tracks()):
            layout_key = (*layout_key, base_beats)
        automatic = self.split_mode.get() == "自动避开异常片段"
        points = None
        if automatic and layout_key in self._auto_splits:
            points = self._auto_splits[layout_key][0]
        elif not automatic:
            try:
                points = (float(self.split_first.get()), float(self.split_second.get()))
            except ValueError as exc:
                raise ValueError("请输入两个分割点（四分音符拍），再点击“查看分段”") from exc
        key = (*layout_key, base_beats, points)
        if key not in self._score_documents:
            info = {}
            self._score_documents[key] = convert_midi(
                self.midi, self.main_track.current(),
                accompaniment, base_beats, transpose, scale,
                points, info, merge_all_tracks=self.merge_all_tracks.get(),
                mark_measures=self.mark_measures.get())
            self._split_messages[key] = info
        info = self._split_messages[key]
        if automatic and layout_key not in self._auto_splits:
            points = tuple(value / 32 for value in info["boundaries"][1:3])
            self._auto_splits[layout_key] = (points, info["message"])
            # Keep edits attached to the actual chosen layout on later calls.
            actual_key = (*layout_key, base_beats, points)
            if actual_key in self._score_documents:
                self._score_documents[key] = self._score_documents[actual_key]
            else:
                self._score_documents[actual_key] = self._score_documents[key]
            self._split_messages[actual_key] = info
        if automatic:
            self.split_first.set(f"{points[0]:.12g}")
            self.split_second.set(f"{points[1]:.12g}")
        bounds = [value / 32 for value in info["boundaries"]]
        ranges = "；".join(f"第{i + 1}段 [{bounds[i]:g}, {bounds[i + 1]:g})"
                           for i in range(3))
        message = self._auto_splits[layout_key][1] if automatic else info["message"]
        self.split_report.set(ranges + " 拍\n" + message)
        self._current_notation_keys = info["notation_keys"]
        self._current_measure_ticks = info["measure_ticks"]
        self._current_document_key = key
        return self.apply_gradient(self._score_documents[key])

    def edit_audition(self):
        try:
            self.stop_split_preview()
            AuditionDialog(self, self.current_json())
        except (ValueError, KeyError) as exc:
            messagebox.showerror("无法编辑歌单片段", str(exc), parent=self)

    def open_simulator(self):
        from score_simulator import ScoreSimulator

        try:
            data = self.current_json()
            self.stop_split_preview()
            ScoreSimulator(self, data).grab_set()
        except (ValueError, RuntimeError, OSError) as exc:
            messagebox.showwarning("无法打开谱面模拟器", str(exc), parent=self)

    def preview_json(self):
        try:
            self.stop_split_preview()
            data = self.current_json()
        except Exception as exc:
            messagebox.showerror("预览失败", str(exc))
            return
        layout_prefix = None if data is self.imported_score else self._current_document_key[:-1]
        measures = () if layout_prefix is None else self._current_measure_ticks
        editor = ScoreEditor(self, data, self.path.stem + ".json",
                             ignored_issues=self._ignored_fragments.setdefault(id(data), set()),
                             notation_keys=None if data is self.imported_score else self._current_notation_keys,
                             measure_ticks=measures, on_split_change=None if layout_prefix is None else
                             lambda keys: self.on_visual_split(data, layout_prefix, keys, measures))
        if data is self.imported_score:
            editor.bind("<Destroy>", lambda event: self.update_key() if event.widget is editor else None,
                        add="+")

    def save_json(self):
        try:
            data = self.current_json()
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc))
            return
        if any(issue["serious"] for issue in find_issues(data)):
            messagebox.showerror("无法导出", "请先在校正界面修复红色错误。", parent=self)
            return
        destination = filedialog.asksaveasfilename(
            defaultextension=".json", initialfile=self.path.stem + ".json",
            filetypes=[("JSON 文件", "*.json")])
        if not destination:
            return
        try:
            Path(destination).write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc))
            return
        messagebox.showinfo("生成成功", f"已保存：{destination}\n\n{export_parameters(data)}", parent=self)


if __name__ == "__main__":
    if sys.argv[1:] == ["--version"]:
        print(__version__)
        sys.exit(0)
    enable_dpi_awareness()
    App().mainloop()
