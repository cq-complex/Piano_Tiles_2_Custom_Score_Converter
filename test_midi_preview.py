import copy
import io
import unittest

import mido

from midi_preview import (SystemMidiPlayer, build_preview_midi, build_split_preview_midi,
                          pitch_number, render_notes)


class MidiPreviewTests(unittest.TestCase):
    def test_split_preview_four_windows_round_up_and_clip_to_song(self):
        data = {"musics": [
            {"bpm": 137, "scores": ["c1[H]", "c[H]"]},
            {"bpm": 137, "scores": ["e1[L]", "U"]},
            {"bpm": 137, "scores": ["g1[HH]", "g[HH]"]},
        ]}
        tick_seconds = 60 / 137 / 32
        for point, before, ticks, melody, accompaniment in (
            (1, True, 256, [60], [48]),
            (1, False, 366, [64, 67], [55]),
            (2, True, 272, [60, 64], [48]),
            (2, False, 366, [67], [55]),
        ):
            with self.subTest(point=point, before=before):
                midi, seconds = build_split_preview_midi(data, point, before)
                self.assertAlmostEqual(seconds, ticks * tick_seconds)
                self.assertAlmostEqual(midi.length, seconds, places=5)
                for track, pitches in zip(midi.tracks[1:], (melody, accompaniment)):
                    self.assertEqual([msg.note for msg in track if msg.type == "note_on"], pitches)
                if point == 1 and not before:
                    # Accompaniment starts after the short middle section.
                    first_note = next(msg for msg in midi.tracks[2] if msg.type == "note_on")
                    self.assertEqual(first_note.time, 480)

    def test_pitch_mapping(self):
        self.assertEqual([pitch_number(name) for name in ("A-3", "C-2", "c", "a1", "#d2", "c5")],
                         [21, 24, 48, 69, 75, 108])
        for name in ("g5", "A", "a-1", "bad"):
            with self.assertRaises(ValueError):
                pitch_number(name)

    def test_chords_rests_and_wrapped_notes(self):
        notes, duration = render_notes("V,6<(c1.e1)[M],g1[M]>,U", 120)
        self.assertAlmostEqual(duration, 0.625)
        self.assertEqual(len(notes), 3)
        self.assertEqual(notes[0][:2], notes[1][:2])
        self.assertAlmostEqual(notes[0][0], 0.125)
        self.assertAlmostEqual(notes[2][0], 0.25)

    def test_glissando_and_quick_arpeggio_have_different_onsets(self):
        uniform, total = render_notes("(c1~e1~g1)[K]", 120)
        fast, _ = render_notes("(c1@e1@g1)[K]", 120)
        self.assertAlmostEqual(total, 0.5)
        self.assertEqual([note[2] for note in uniform], [60, 64, 67])
        self.assertAlmostEqual(uniform[1][0], 1 / 6)
        self.assertAlmostEqual(fast[-1][0], 0.12)
        self.assertTrue(all(note[1] <= total for note in fast + uniform))

    def test_preview_clips_sustained_accompaniment_and_keeps_trailing_rest(self):
        section = {"bpm": 120, "scores": ["c1[L],d1[L],U", "c[K],U"]}
        midi, seconds = build_preview_midi(section, 1, 2)
        self.assertAlmostEqual(seconds, 0.5)
        stream = io.BytesIO()
        midi.save(file=stream)
        stream.seek(0)
        loaded = mido.MidiFile(file=stream)
        self.assertAlmostEqual(loaded.length, 0.5)
        self.assertEqual([m.note for m in loaded.tracks[1] if m.type == "note_on"], [62])
        self.assertEqual([m.note for m in loaded.tracks[2] if m.type == "note_on"], [48])
        self.assertEqual(next(m for m in loaded.tracks[2] if m.type == "note_on").time, 0)

    def test_draft_is_auditioned_without_mutating_document(self):
        section = {"bpm": 150, "scores": ["c1[M],e1[M]", "c[L]"]}
        before = copy.deepcopy(section)
        midi, seconds = build_preview_midi(section, 0, 1, draft="(c1@e1)[L]",
                                           accompaniment=False)
        self.assertEqual(len(midi.tracks), 2)
        self.assertAlmostEqual(seconds, 0.2)
        self.assertEqual(section, before)
        for draft in ("", "c1[K]", "c1[Z]"):
            with self.assertRaises(ValueError):
                build_preview_midi(section, 0, 1, draft=draft)

    def test_accompaniment_preview_uses_its_own_note_range(self):
        section = {"bpm": 120, "scores": ["c1[K],e1[K]", "a[L],g[M],f[M],d[K]"]}
        solo, seconds = build_preview_midi(section, 1, 2, draft="(g~f)[L]",
                                           accompaniment=False, layer=1)
        self.assertEqual(seconds, 0.25)
        self.assertEqual([msg.note for msg in solo.tracks[1] if msg.type == "note_on"], [55, 53])
        mixed, _ = build_preview_midi(section, 1, 2, layer=1)
        self.assertEqual([msg.note for msg in mixed.tracks[2] if msg.type == "note_on"], [60])

    def test_native_rest_names_and_effect_suffix_preserve_preview_timing(self):
        events, duration = render_notes("C-1[M]{3},empty[L],chuanshao[L],(a~mute~a)[K]", 120)
        self.assertEqual([pitch for _, _, pitch in events], [36, 57, 57])
        self.assertAlmostEqual(duration, 1.125)
        self.assertAlmostEqual(events[2][0] - events[1][0], 1 / 3)

    def test_silent_main_can_preview_accompaniment(self):
        section = {"bpm": 120, "scores": ["V", "c[M]"]}
        self.assertEqual(len(build_preview_midi(section, 0, 0)[0].tracks), 3)
        with self.assertRaises(ValueError):
            build_preview_midi(section, 0, 0, accompaniment=False)

    def test_trailing_rests_have_a_real_end_event(self):
        for bpm in (60, 120, 137):
            midi, seconds = build_preview_midi({"bpm": bpm, "scores": ["c1[M],S"]}, 0, 1)
            track = midi.tracks[1]
            final = track[-2]
            self.assertEqual(final.type, "control_change")
            self.assertEqual(final.control, 123)
            self.assertGreater(final.time, 0)
            self.assertEqual(track[-1].time, 0)
            total_ticks = sum(message.time for message in track)
            tempo = next(m.tempo for m in midi.tracks[0] if m.type == "set_tempo")
            self.assertAlmostEqual(mido.tick2second(total_ticks, midi.ticks_per_beat, tempo),
                                   seconds, places=5)



class VirtualClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class VirtualCancel:
    def __init__(self, clock, cancel_at=None):
        self.clock = clock
        self.cancel_at = cancel_at
        self.cancelled = False

    def set(self):
        self.cancelled = True

    def clear(self):
        self.cancelled = False

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        target = self.clock.value + seconds
        if self.cancel_at is not None and target >= self.cancel_at:
            self.clock.value = self.cancel_at
            self.cancelled = True
        else:
            self.clock.value = target
        return self.cancelled


class FakeOutput:
    def __init__(self, clock):
        self.clock = clock
        self.messages = []
        self.opened = False
        self.closed_at = None

    def open(self):
        self.opened = True

    def send(self, data):
        self.messages.append((self.clock(), data))
        # Simulate driver overhead to detect accidental cumulative sleep drift.
        self.clock.value += 0.0001

    def close(self):
        self.closed_at = self.clock()


class SystemMidiSchedulerTests(unittest.TestCase):
    def make_player(self, cancel_at=None):
        clock = VirtualClock()
        output = FakeOutput(clock)
        player = SystemMidiPlayer(output_factory=lambda: output, clock=clock)
        player._cancel = VirtualCancel(clock, cancel_at)
        return player, output, clock

    def test_every_event_and_full_duration_without_external_player_status(self):
        from system_midi import midi_schedule
        midi, seconds = build_preview_midi({
            'bpm': 137, 'scores': [','.join(['c1[P]', 'e1[P]', 'g1[P]'] * 100) + ',Q',
                                   'c[HHH]']}, 0, 300)
        expected, duration = midi_schedule(midi)
        player, output, clock = self.make_player()
        player.play(midi)
        player._thread.join(timeout=2)
        self.assertFalse(player.is_playing())
        self.assertEqual(player.events_sent, player.total_events)
        self.assertEqual(len(output.messages), len(expected))
        for (actual_time, actual_data), (expected_time, expected_data) in zip(output.messages, expected):
            self.assertEqual(actual_data, expected_data)
            self.assertGreaterEqual(actual_time + 1e-9, expected_time)
            self.assertLess(actual_time - expected_time, 0.002)
        self.assertGreaterEqual(output.closed_at, duration + player.RELEASE_SECONDS)
        self.assertAlmostEqual(player.position_seconds, duration)
        self.assertAlmostEqual(duration, seconds, places=4)
        player.stop()

    def test_stop_during_rest_does_not_send_future_notes(self):
        midi, _ = build_preview_midi({'bpm': 60, 'scores': ['c1[L],T,e1[L]']}, 0, 2)
        player, output, _ = self.make_player(cancel_at=0.75)
        player.play(midi)
        player._thread.join(timeout=2)
        self.assertFalse(player.is_playing())
        pitches = [data[1] for _, data in output.messages if data[0] & 0xF0 == 0x90]
        self.assertEqual(pitches, [60])
        self.assertLess(player.events_sent, player.total_events)
        self.assertIsNotNone(output.closed_at)
        player.stop()

    def test_tempo_changes_in_all_tracks_are_respected(self):
        from system_midi import midi_schedule
        midi = mido.MidiFile(ticks_per_beat=480)
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage('set_tempo', tempo=500000),
            mido.MetaMessage('set_tempo', tempo=1000000, time=480),
            mido.MetaMessage('end_of_track', time=480)]))
        midi.tracks.append(mido.MidiTrack([
            mido.Message('note_on', note=60),
            mido.Message('note_off', note=60, time=960)]))
        events, seconds = midi_schedule(midi)
        self.assertAlmostEqual(seconds, 1.5)
        self.assertAlmostEqual(events[-1][0], 1.5)

    def test_output_error_is_reported_and_device_closed(self):
        midi, _ = build_preview_midi({'bpm': 120, 'scores': ['c1[L]']}, 0, 0)
        player, output, _ = self.make_player()
        def fail(data):
            raise RuntimeError('simulated MIDI output failure')
        output.send = fail
        player.play(midi)
        player._thread.join(timeout=2)
        with self.assertRaisesRegex(RuntimeError, 'simulated MIDI output failure'):
            player.is_playing()
        self.assertIsNotNone(output.closed_at)
        player.stop()

    def test_open_failure_is_reported_and_resources_released(self):
        midi, _ = build_preview_midi({'bpm': 120, 'scores': ['c1[L]']}, 0, 0)
        player, output, _ = self.make_player()
        def fail():
            raise RuntimeError('no system MIDI device')
        output.open = fail
        with self.assertRaisesRegex(RuntimeError, 'no system MIDI device'):
            player.play(midi)
        self.assertIsNotNone(output.closed_at)


if __name__ == '__main__':
    unittest.main()
