"""Pitch names shared by JSON conversion and editing."""


def pitch_name(pitch, piano_range=True):
    low, high = (21, 108) if piano_range else (12, 119)
    if not low <= pitch <= high:
        raise ValueError(f"音高 {pitch} 超出" + ("钢琴音库 A0–C8" if piano_range else "谱面可表示范围 C0–B8"))
    octave, semitone = divmod(pitch, 12)
    octave -= 1
    name = ("c", "#c", "d", "#d", "e", "f", "#f", "g", "#g", "a", "#a", "b")[semitone]
    return (name.upper() + str(octave - 3)) if octave <= 2 else (
        name + (str(octave - 3) if octave >= 4 else ""))

