"""Key fitting and notation shared by the converter and visual editor."""
from dataclasses import dataclass

MAJOR_NAMES = ("C", "bD", "D", "bE", "E", "F", "#F", "G", "bA", "A", "bB", "B")
NATURAL = (0, 2, 4, 5, 7, 9, 11)
LETTERS = "CDEFGAB"
MAJOR_FIFTHS = (0, -5, 2, -3, 4, -1, 6, 1, -4, 3, -2, 5)
MODES = {"": 0, "major": 0, "ionian": 0, "dorian": 1, "phrygian": 2,
         "lydian": 3, "mixolydian": 4, "minor": 5, "aeolian": 5, "locrian": 6}
MODE_NAMES = {"": "（调式未标注）", "major": "大调", "ionian": "伊奥尼亚调式",
              "minor": "小调", "aeolian": "艾奥利亚调式", "dorian": "多利亚调式",
              "phrygian": "弗里吉亚调式", "lydian": "利底亚调式",
              "mixolydian": "混合利底亚调式", "locrian": "洛克里亚调式"}


@dataclass(frozen=True)
class KeySignature:
    fifths: int
    mode: str = "major"

    @property
    def degree(self):
        return MODES.get(self.mode, 0)

    @property
    def tonic(self):
        return (7 * self.fifths + NATURAL[self.degree]) % 12

    @property
    def tonic_letter(self):
        return (4 * self.fifths + self.degree) % 7

    @property
    def alterations(self):
        order = "FCGDAEB" if self.fifths >= 0 else "BEADGCF"
        count, extra = divmod(abs(self.fifths), 7)
        sign = 1 if self.fifths >= 0 else -1
        return {letter: sign * (count + (i < extra)) for i, letter in enumerate(order)}

    @property
    def tonic_name(self):
        letter = LETTERS[self.tonic_letter]
        alter = self.alterations[letter]
        return letter + ("♯" * alter if alter > 0 else "♭" * -alter)

    @property
    def label(self):
        return self.tonic_name + MODE_NAMES.get(self.mode, "（调式未标注）")

    def transposed(self, semitones):
        if semitones % 12 == 0:
            return self
        relative_major = (7 * self.fifths + semitones) % 12
        return KeySignature(MAJOR_FIFTHS[relative_major], self.mode)

    def scale(self):
        zero = NATURAL[self.degree]
        return tuple((NATURAL[(self.degree + step) % 7] - zero) % 12 for step in range(7))


def major_fits(histogram, transpose=0):
    """Return weighted scale coverage (not a calibrated probability)."""
    total = sum(histogram)
    if total <= 0:
        return []
    return sorted(((root, sum(histogram[(root + step - transpose) % 12]
                              for step in NATURAL) / total)
                   for root in range(12)), key=lambda item: (-item[1], item[0]))


def key_fit_label(histogram, transpose=0):
    fits = major_fits(histogram, transpose)
    if not fits:
        return "推测调式：暂无有效旋律音符"
    (first, best), (second, runner_up) = fits[:2]
    candidates = (f"{MAJOR_NAMES[first]}大调 {best:.1%} / "
                  f"{MAJOR_NAMES[second]}大调 {runner_up:.1%}")
    if best < 0.8:
        return f"推测调式：不明确（音阶拟合：{candidates}）"
    suffix = "；不确定" if best - runner_up < 0.03 else ""
    return f"推测调式：{candidates}（音阶拟合{suffix}）"


def source_key_events(midi, source_tracks, origin, scale, transpose, beat_at):
    if hasattr(midi, "musicxml_measures"):
        raw = [getattr(track, "musicxml_keys", ()) for track in source_tracks]
        fallback = next((item for item in raw if item), ())
        if not fallback:
            fallback = next((track.musicxml_keys for track in midi.instruments
                             if getattr(track, "musicxml_keys", ())), ())
        timelines = [events or fallback for events in raw]
    else:
        times = [(beat_at(midi, event.time),
                  KeySignature(MAJOR_FIFTHS[(event.key_number % 12 + (3 if event.key_number >= 12 else 0)) % 12],
                               "minor" if event.key_number >= 12 else "major"))
                 for event in midi.key_signature_changes]
        timelines = [times for _ in source_tracks]
    result = []
    for timeline in timelines:
        quantized = {round((float(beat) - origin) * scale * 32): key.transposed(transpose)
                     for beat, key in timeline}
        result.append(tuple(sorted(quantized.items())))
    return tuple(result)


def local_key_events(events, start, end):
    current = next((key for at, key in reversed(events) if at <= start), None)
    initial = [(0, current)] if current is not None else []
    for at, key in events:
        if start < at < end and key != current:
            initial.append((at - start, key))
            current = key
    return initial


def staff_spelling(pitch, key):
    """Return diatonic staff position and written accidental relative to the key."""
    choices = []
    octave = pitch // 12 - 1
    for octv in range(octave - 1, octave + 2):
        for index, (letter, natural) in enumerate(zip(LETTERS, NATURAL)):
            alter = pitch - (12 * (octv + 1) + natural)
            if abs(alter) <= 2:
                native = key.alterations[letter]
                bias = int(alter > 0 if key.fifths < 0 else alter < 0)
                choices.append((alter != native, abs(alter), bias, abs(alter - native),
                                octv * 7 + index, alter, native))
    _, _, _, _, position, alter, native = min(choices)
    accidental = "" if alter == native else {0: "♮", 1: "♯", 2: "♯♯", -1: "♭", -2: "♭♭"}[alter]
    return position, accidental


def numbered_pitch(pitch, key):
    tonic = 60 + (key.tonic + 6) % 12 - 6
    octave, offset = divmod(pitch - tonic, 12)
    scale = key.scale()
    if offset in scale:
        return str(scale.index(offset) + 1), octave
    low = max(i for i, step in enumerate(scale) if step < offset)
    if key.fifths < 0:
        high = next((i for i, step in enumerate(scale) if step > offset), None)
        if high is not None:
            return "♭" * (scale[high] - offset) + str(high + 1), octave
        return "♭" * (12 - offset) + "1", octave + 1
    return "♯" * (offset - scale[low]) + str(low + 1), octave
