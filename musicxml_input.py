"""Read MusicXML score time into the same PrettyMIDI model as MIDI files."""
from dataclasses import dataclass, field
from fractions import Fraction
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import mido
import pretty_midi

from notation_keys import KeySignature

STEPS = dict(zip("CDEFGAB", (0, 2, 4, 5, 7, 9, 11)))
BEATS = dict(zip(("whole", "half", "quarter", "eighth", "16th", "32nd"),
                 (4, 2, 1, Fraction(1, 2), Fraction(1, 4), Fraction(1, 8))))


@dataclass
class Attack:
    start: Fraction
    duration: Fraction
    pitches: list = field(default_factory=list)
    arpeggios: list = field(default_factory=list)
    tuplets: list = field(default_factory=list)
    ratio: tuple = ()
    simple: bool = True
    staccato: bool = False


def notation_groups(voices):
    """Yield (connector, start, end, ordered (beat, pitches) attacks)."""
    def tuplet(group):
        if len(group) < 2:
            return None
        first = group[0]
        if not all(a.simple and a.pitches and not a.arpeggios
                   and a.duration == first.duration
                   and a.start == first.start + i * first.duration
                   for i, a in enumerate(group)):
            return None
        connector = "staccato~" if any(a.staccato for a in group) else "~"
        return (connector, first.start, group[-1].start + first.duration,
                tuple((a.start, tuple(a.pitches)) for a in group))

    for attacks in voices.values():
        attacks = sorted(attacks.values(), key=lambda a: a.start)
        explicit, occupied = {}, set()
        for i, attack in enumerate(attacks):
            if (attack.arpeggios and len(set(attack.pitches)) > 1 and attack.simple
                    and not attack.staccato and len({number for number, _ in attack.arpeggios}) == 1):
                directions = {direction for _, direction in attack.arpeggios if direction}
                if len(directions) <= 1:
                    pitches = sorted(set(attack.pitches), reverse="down" in directions)
                    yield "@", attack.start, attack.start + attack.duration, ((attack.start, tuple(pitches)),)
            for mark, number in dict.fromkeys(attack.tuplets):
                if mark == "start":
                    explicit[number] = i
                elif mark == "stop" and number in explicit:
                    start = explicit.pop(number)
                    occupied.update(range(start, i + 1))
                    group = tuplet(attacks[start:i + 1])
                    if group:
                        yield group
        for start in explicit.values():
            occupied.update(range(start, len(attacks)))

        i = 0
        while i < len(attacks):
            ratio = attacks[i].ratio
            if not ratio or i in occupied:
                i += 1
                continue
            end = i + 1
            while end < len(attacks) and end not in occupied and attacks[end].ratio == ratio:
                end += 1
            actual, _ = ratio
            # Without brackets, only consume complete consecutive runs of this ratio.
            if (end - i) % actual == 0:
                for start in range(i, end, actual):
                    group = tuplet(attacks[start:start + actual])
                    if group:
                        yield group
            i = end


def read_musicxml(path):
    if str(path).lower().endswith(".mxl"):
        with ZipFile(path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(x for x in container.iter() if x.tag.split("}")[-1] == "rootfile")
            root = ET.fromstring(archive.read(rootfile.attrib["full-path"]))
    else:
        root = ET.parse(path).getroot()
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    if root.tag == "score-timewise":
        parts = {}
        for measure in list(root):
            if measure.tag != "measure":
                continue
            root.remove(measure)
            for item in measure.findall("part"):
                part = parts.setdefault(item.attrib["id"], ET.Element("part", item.attrib))
                dest = ET.SubElement(part, "measure", measure.attrib)
                dest.extend(item)
        root.extend(parts.values())
    elif root.tag != "score-partwise":
        raise ValueError("文件不是 MusicXML 乐谱")

    names = {p.get("id"): p for p in root.findall("part-list/score-part")}
    tempos, tracks, measures = {}, [], []
    for part in root.findall("part"):
        info = names.get(part.get("id"), ET.Element("score-part"))
        program = int(info.findtext("midi-instrument/midi-program", "1")) - 1
        name = info.findtext("part-name", "")
        staves, pending, notation = {}, {}, {}
        key_marks = []
        divisions, transpose, base = 1, 0, Fraction(0)
        part_measures = []
        for measure in part.findall("measure"):
            part_measures.append(base)
            cursor = far = last = base
            chord_staccato = False
            for item in measure:
                tag = item.tag
                if tag == "attributes":
                    divisions = int(item.findtext("divisions", str(divisions)))
                    trans = item.find("transpose")
                    if trans is not None:
                        transpose = int(trans.findtext("chromatic", "0")) + 12 * int(
                            trans.findtext("octave-change", "0"))
                    for element in item.findall("key"):
                        fifths = element.findtext("fifths")
                        if fifths is not None:
                            signature = KeySignature(int(fifths), element.findtext("mode", "").lower())
                            staff = element.get("number")
                            key_marks.append((cursor, int(staff) if staff else None,
                                              signature.transposed(transpose)))
                elif tag in ("backup", "forward"):
                    delta = Fraction(item.findtext("duration", "0")) / divisions
                    cursor += delta if tag == "forward" else -delta
                    far = max(far, cursor)
                elif tag in ("sound", "direction"):
                    sound = item if tag == "sound" else item.find("sound")
                    offset = Fraction(item.findtext("offset", "0")) / divisions
                    if sound is not None and sound.get("tempo"):
                        tempos[max(0, cursor + offset)] = (float(sound.get("tempo")), 1)
                    else:
                        metro = item.find("direction-type/metronome")
                        if metro is not None and metro.findtext("per-minute"):
                            unit = BEATS.get(metro.findtext("beat-unit"), 1)
                            dots = len(metro.findall("beat-unit-dot"))
                            bpm = float(metro.findtext("per-minute")) * unit * (2 - 0.5 ** dots)
                            at = max(0, cursor + offset)
                            if tempos.get(at, (0, 0))[1] == 0:
                                tempos[at] = (bpm, 0)
                elif tag == "note":
                    duration = Fraction(item.findtext("duration", "0")) / divisions
                    if not duration or item.find("grace") is not None:
                        continue
                    chord = item.find("chord") is not None
                    start = last if chord else cursor
                    end = start + duration
                    staccato = item.find("notations/articulations/staccato") is not None
                    if not chord:
                        last, cursor = start, end
                        chord_staccato = staccato
                    far = max(far, end)
                    pitch = item.find("pitch")
                    staff = int(item.findtext("staff", "1"))
                    voice = item.findtext("voice", "1")
                    attacks = notation.setdefault(staff, {}).setdefault(voice, {})
                    attack = attacks.setdefault(start, Attack(start, duration))
                    attack.tuplets.extend((mark.get("type"), mark.get("number", "1"))
                                          for mark in item.findall("notations/tuplet"))
                    attack.arpeggios.extend((mark.get("number", "1"), mark.get("direction"))
                                           for mark in item.findall("notations/arpeggiate"))
                    if not chord:
                        actual = int(item.findtext("time-modification/actual-notes", "0"))
                        normal = int(item.findtext("time-modification/normal-notes", "0"))
                        if actual >= 2 and normal > 0 and actual != normal:
                            attack.ratio = (actual, normal)
                    ties = {t.get("type") for t in item.findall("tie") + item.findall("notations/tied")}
                    attack.staccato |= staccato or chord_staccato
                    attack.simple &= (pitch is not None and not ties
                                      and attack.duration == duration
                                      and item.find("notations/ornaments/tremolo") is None
                                      and item.find("notations/non-arpeggiate") is None)
                    if pitch is None:
                        continue
                    key = (staff, voice,
                           12 * (int(pitch.findtext("octave")) + 1)
                           + STEPS[pitch.findtext("step")]
                           + round(Fraction(pitch.findtext("alter", "0"))) + transpose)
                    attack.pitches.append(key[2])
                    if staccato or chord_staccato:
                        end = start + duration / 2
                    notes = staves.setdefault(staff, [])
                    if "stop" in ties and key in pending:
                        event = pending.pop(key)
                        event[1] = end
                    else:
                        event = [start, end, key[2]]
                        notes.append(event)
                    if "start" in ties:
                        pending[key] = event
            base = far
        if not measures and staves:
            measures = part_measures
        for staff, notes in sorted(staves.items()):
            keys = {beat: signature for beat, target, signature in key_marks
                    if target is None or target == staff}
            voices = notation[staff]
            groups = tuple(notation_groups(voices))
            tuplets = [(start, end) for kind, start, end, _ in groups if kind == "staccato~"]
            staccatos = tuple((a.start, a.start + a.duration, tuple(a.pitches))
                             for voice in voices.values() for a in voice.values()
                             if a.staccato and a.simple and a.pitches and not a.arpeggios
                             and not (a.ratio or a.tuplets)
                             and not any(start <= a.start < end for start, end in tuplets))
            tracks.append((f"{name} · {staff}" if len(staves) > 1 else name, program, notes,
                           groups, tuple(sorted(keys.items())), staccatos))
    if not tracks:
        raise ValueError("MusicXML 没有可转换的有音高音符")

    resolution = 960
    midi = mido.MidiFile(type=1, ticks_per_beat=resolution, charset="utf8")
    tempo_track = midi.add_track()
    tempos.setdefault(0, (120, 0))
    previous = 0
    for beat, (bpm, _) in sorted(tempos.items()):
        tick = round(beat * resolution)
        tempo_track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm),
                                            time=tick - previous))
        previous = tick
    # Use MIDI just for the tempo map. Construct instruments directly so that
    # overlapping same-pitch voices remain separate events.
    result = pretty_midi.PrettyMIDI(mido_object=midi)
    for name, program, notes, groups, keys, staccatos in tracks:
        track = pretty_midi.Instrument(program, name=name)
        for start, end, pitch in notes:
            start_tick = max(0, round(start * resolution))
            end_tick = max(start_tick + 1, round(end * resolution))
            track.notes.append(pretty_midi.Note(80, pitch, result.tick_to_time(start_tick),
                                                result.tick_to_time(end_tick)))
        track.musicxml_groups = groups
        track.musicxml_keys = keys
        track.musicxml_staccatos = staccatos
        result.instruments.append(track)
    result.musicxml_measures = tuple(sorted(set(measures)))
    return result
