"""Measure boundaries provided by MusicXML or MIDI time signatures."""
from bisect import bisect_right
from fractions import Fraction


def measure_beats(midi):
    if hasattr(midi, "musicxml_measures"):
        return midi.musicxml_measures
    # MIDI has no standard bar-line event. Only infer a grid when the file
    # explicitly provides time signatures; a signature change starts a bar.
    signatures = {
        Fraction(int(midi.time_to_tick(sig.time)), int(midi.resolution)):
        Fraction(4 * sig.numerator, sig.denominator)
        for sig in midi.time_signature_changes
        if sig.numerator > 0 and sig.denominator > 0
    }
    if not signatures:
        return ()
    end = Fraction(int(midi.time_to_tick(midi.get_end_time())), int(midi.resolution))
    result = []
    starts = sorted(signatures)
    for start, stop in zip(starts, starts[1:] + [end]):
        length = signatures[start]
        while start < stop:
            result.append(start)
            start += length
    return tuple(result)


def crosses_measure(midi, measures):
    resolution = int(midi.resolution)
    times = [midi.tick_to_time(int(round(beat * resolution))) for beat in measures]
    for track in midi.instruments:
        for note in track.notes:
            i = bisect_right(times, note.start + 1e-8)
            if i < len(times) and times[i] < note.end - 1e-8:
                return True
        for _, start, stop, _ in getattr(track, "musicxml_groups", ()):
            i = bisect_right(measures, start)
            if i < len(measures) and measures[i] < stop:
                return True
    return False
