"""Render edited score fragments for Windows system MIDI output."""
import math
from fractions import Fraction
import re

import mido
from audition import audition_windows
from score_parameters import effective_score_bpms
from system_midi import SystemMidiPlayer


def pitch_number(name, piano_range=True):
    match = re.fullmatch(r"(#?)([a-gA-G])(-[123]|[1-5])?", name)
    if not match:
        raise ValueError(f"无法试听音高：{name}")
    sharp, letter, suffix = match.groups()
    octave = 3 + int(suffix or 0)
    if letter.isupper() and not (suffix and suffix.startswith("-")):
        raise ValueError(f"大写音名需要负音组：{name}")
    if letter.islower() and suffix and suffix.startswith("-"):
        raise ValueError(f"小写音名不能使用负音组：{name}")
    number = (octave + 1) * 12 + {"c": 0, "d": 2, "e": 4, "f": 5,
                                "g": 7, "a": 9, "b": 11}[letter.lower()] + bool(sharp)
    if piano_range and not 21 <= number <= 108:
        raise ValueError(f"音高 {name} 超出钢琴范围 A0–C8")
    return number


def phrase_attacks(sound, duration):
    """Return sound groups, connectors and shared playback offsets in seconds."""
    parts = re.split(r"([~@])", sound.strip("()"))
    groups, connectors = parts[::2], parts[1::2]
    count = len(groups)
    quick_step = min(duration * 0.25, 0.12) / max(1, count - 1)
    onsets = [0.0]
    for connector in connectors:
        onsets.append(onsets[-1] + (duration / count if connector == "~" else quick_step))
    if onsets[-1] > duration * 0.9:
        onsets = [onset * duration * 0.9 / onsets[-1] for onset in onsets]
    return groups, connectors, onsets


def render_notes(text, bpm):
    """Return (start_seconds, end_seconds, pitch) events and total seconds.

    Game-specific sound timing is approximated: '~' advances evenly, '@'
    advances within an initial burst of at most 120 ms, '.' sounds together.
    """
    from score_editor import SPECIAL_SOUNDS, parse_score

    if not math.isfinite(bpm) or bpm <= 0:
        raise ValueError("试听 BPM 必须为有限正数")
    seconds_per_unit = 60 / bpm / 32
    events = []

    def render_token(token, start):
        duration = token.ticks * seconds_per_unit
        if token.kind == "rest":
            return
        if token.children:
            cursor = start
            for child in token.children:
                render_token(child, cursor)
                cursor += child.ticks * seconds_per_unit
            return
        sound = token.text.rsplit("[", 1)[0]
        groups, connectors, onsets = phrase_attacks(sound, duration)
        for i, (group, onset) in enumerate(zip(groups, onsets)):
            stop = onsets[i + 1] if i < len(connectors) and connectors[i] == "~" else duration
            for name in group.split("."):
                if name not in SPECIAL_SOUNDS:
                    events.append((start + onset, start + stop, pitch_number(name, piano_range=False)))

    cursor = 0.0
    for token in parse_score(text):
        render_token(token, cursor)
        cursor += token.ticks * seconds_per_unit
    return events, cursor


def build_preview_midi(section, first, last, draft=None, accompaniment=True, layer=0):
    """Build exactly the selected time window, optionally auditioning a draft."""
    from score_editor import parse_score

    tokens = parse_score(section["scores"][layer])
    if not 0 <= first <= last < len(tokens):
        raise ValueError("试听音符范围无效")
    start = sum(t.ticks for t in tokens[:first])
    duration = sum(t.ticks for t in tokens[first:last + 1])
    main_text = ",".join(t.text for t in tokens[first:last + 1])
    if draft is not None:
        if not draft.strip():
            raise ValueError("编辑片段不能为空")
        draft_tokens = parse_score(draft)
        if sum(t.ticks for t in draft_tokens) != duration:
            raise ValueError("试听编辑片段前，请保持其总时长与所选范围一致")
        main_text = draft
    bpm = float(section["bpm"])
    main_notes, seconds = render_notes(main_text, bpm)
    layers = [main_notes]
    if accompaniment:
        offset = start * 60 / bpm / 32
        for index, line in enumerate(section["scores"]):
            if index == layer:
                continue
            layers.append(clip_notes(render_notes(line, bpm)[0], offset, offset + seconds))
    return encode_preview(layers, bpm, seconds)


def clip_notes(notes, start, end):
    # Rendering accumulates floating point seconds. Ignore zero-length
    # overlaps caused by rounding at the window edges.
    return [(max(on, start) - start, min(off, end) - start, pitch)
            for on, off, pitch in notes if min(off, end) - max(on, start) > 1e-9]


def build_split_preview_midi(data, split_index, before):
    """Audition a window beside a split, even when it crosses another split."""
    from score_editor import parse_score

    sections = data["musics"]
    bpm_by_section = [float(bpm) for bpm in effective_score_bpms(data)]
    if any(not math.isfinite(bpm) or bpm <= 0 for bpm in bpm_by_section):
        raise ValueError("试听 BPM 必须为有限正数")
    seconds_per_tick = [60 / (32 * Fraction(str(bpm))) for bpm in bpm_by_section]
    boundaries = [Fraction(0)]
    for section, tick in zip(sections, seconds_per_tick):
        boundaries.append(boundaries[-1] + tick * sum(
            token.ticks for token in parse_score(section["scores"][0])))
    cut = boundaries[split_index]
    tick = seconds_per_tick[split_index - int(before)]
    duration = math.ceil(5 / tick) * tick
    start = max(0, cut - duration) if before else cut
    end = cut if before else min(boundaries[-1], cut + duration)
    layers = [[] for _ in range(max(len(section["scores"]) for section in sections))]
    for section, bpm, left, right in zip(sections, bpm_by_section, boundaries, boundaries[1:]):
        if left >= end or right <= start:
            continue
        local_start = float(max(start, left) - left)
        local_end = float(min(end, right) - left)
        offset = float(max(start, left) - start)
        for layer, line in zip(layers, section["scores"]):
            notes, _ = render_notes(line, bpm)
            layer.extend((on + offset, off + offset, pitch)
                         for on, off, pitch in clip_notes(notes, local_start, local_end))
    return encode_preview(layers, bpm_by_section[split_index - int(before)], float(end - start))


def build_audition_midi(data, selection=None):
    """Play an inclusive excerpt of the flattened melody with all its layers."""
    windows = audition_windows(data, selection)
    layers = [[] for _ in range(max(len(data["musics"][i]["scores"])
                                    for i, _, _, _ in windows))]
    seconds = 0.0
    for index, first, last, bpm in windows:
        seconds_per_tick = 60 / bpm / 32
        start, end = first * seconds_per_tick, last * seconds_per_tick
        for layer, line in zip(layers, data["musics"][index]["scores"]):
            notes, _ = render_notes(line, bpm)
            layer.extend((on + seconds, off + seconds, pitch)
                         for on, off, pitch in clip_notes(notes, start, end))
        seconds += (last - first) * seconds_per_tick
    return encode_preview(layers, windows[0][3], seconds)


def build_notation_preview(data, section_index, start_tick, selected_layer=None, all_tracks=True):
    """Play from the chosen attack through the remaining sections.

    Return the MIDI and (section, elapsed start/end, local starting tick,
    seconds per tick) spans used by the visual playhead.
    """
    if not 0 <= section_index < len(data["musics"]) or start_tick < 0:
        raise ValueError("请选择有效的试听起点")
    parts = data["musics"][section_index:]
    bpms = effective_score_bpms(data)[section_index:]
    layers = [[] for _ in range(max(len(part["scores"]) for part in parts) if all_tracks else 1)]
    elapsed, timeline = 0.0, []
    for index, (part, bpm) in enumerate(zip(parts, bpms), section_index):
        if not math.isfinite(bpm) or bpm <= 0:
            raise ValueError("试听 BPM 必须为有限正数")
        tick_seconds = 60 / bpm / 32
        local_tick = start_tick if index == section_index else 0
        start = local_tick * tick_seconds
        if all_tracks:
            selected = range(len(part["scores"]))
        else:
            track = selected_layer if selected_layer is not None else 0
            selected = [track] if track < len(part["scores"]) else []
        rendered = [(layer, *render_notes(part["scores"][layer], bpm)) for layer in selected]
        from score_editor import inspect_score
        end = max(sum(token.ticks for token in inspect_score(line)[0])
                  for line in part["scores"]) * tick_seconds
        if start >= end:
            continue
        for layer, notes, _ in rendered:
            if index == section_index and layer == selected_layer:
                notes = [(on, off, pitch) for on, off, pitch in notes if on >= start - 1e-8]
            destination = layer if all_tracks else 0
            layers[destination].extend((on + elapsed, off + elapsed, pitch)
                                       for on, off, pitch in clip_notes(notes, start, end))
        timeline.append((index, elapsed, elapsed + end - start, local_tick, tick_seconds))
        elapsed += end - start
    return encode_preview(layers, bpms[0], elapsed)[0], timeline


def encode_preview(layers, bpm, seconds):
    if not any(layers):
        raise ValueError("所选片段及启用的伴奏均为休止符，没有可试听音符")

    resolution = 960
    tempo = round(60_000_000 / bpm)
    if not 1 <= tempo <= 0xFFFFFF:
        raise ValueError("当前 BPM 超出标准 MIDI 可表示范围")
    midi = mido.MidiFile(type=1, ticks_per_beat=resolution)
    tempo_track = mido.MidiTrack()
    tempo_track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    end_tick = max(1, round(seconds * bpm / 60 * resolution))
    tempo_track.append(mido.MetaMessage("end_of_track", time=end_tick))
    midi.tracks.append(tempo_track)
    for index, layer in enumerate(layers):
        # The editor exports piano tracks. Avoid MIDI's percussion channel.
        channel = index % 15
        if channel >= 9:
            channel += 1
        track = mido.MidiTrack()
        track.append(mido.Message("program_change", program=0, channel=channel, time=0))
        scheduled = []
        for on, off, pitch in layer:
            on_tick = min(end_tick - 1, max(0, round(on * bpm / 60 * resolution)))
            off_tick = min(end_tick, max(on_tick + 1, round(off * bpm / 60 * resolution)))
            scheduled.append((on_tick, 1, pitch))
            scheduled.append((off_tick, 0, pitch))
        cursor = 0
        for tick, is_on, pitch in sorted(scheduled):
            track.append(mido.Message("note_on" if is_on else "note_off", note=pitch,
                                      velocity=90 if is_on else 0, channel=channel,
                                      time=tick - cursor))
            cursor = tick
        # Release all notes on this channel at the full selected endpoint.
        track.append(mido.Message("control_change", control=123, value=0,
                                  channel=channel, time=end_tick - cursor))
        track.append(mido.MetaMessage("end_of_track", time=0))
        midi.tracks.append(track)
    return midi, seconds
