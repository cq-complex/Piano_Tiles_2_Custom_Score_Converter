import json
import math
import unittest

import pretty_midi

from main import (TICK_BEATS, base_beats_from_exponent, convert_midi,
                  alignment_quality, alignment_samples, detect_beat_scale,
                  estimate_key, key_histogram, minimum_normal_note, parse_beat_scale, score,
                  selected_track_indices, merged_score, blocks_per_second)


def make_midi(events):
    midi = pretty_midi.PrettyMIDI(initial_tempo=60)
    track = pretty_midi.Instrument(0)
    track.notes = [pretty_midi.Note(100, pitch, start * TICK_BEATS, end * TICK_BEATS)
                   for pitch, start, end in events]
    midi.instruments = [track]
    return midi, track


class ConversionTests(unittest.TestCase):
    def test_blocks_per_second_formula_and_threshold(self):
        self.assertEqual(blocks_per_second(120, 0.5), 4)
        self.assertEqual(blocks_per_second(120, 0.125), 16)
        self.assertEqual(blocks_per_second(390, 0.5), 13)
        self.assertLess(blocks_per_second(389.99, 0.5), 13)
        for bpm, base in ((0, 0.5), (120, 0), (float("inf"), 0.5), (120, float("nan"))):
            with self.assertRaises(ValueError):
                blocks_per_second(bpm, base)

    def test_merge_tracks_does_not_repeat_held_notes_at_new_attacks(self):
        midi, first = make_midi([(60, 0, 64)])
        _, second = make_midi([(64, 0, 32), (67, 32, 64)])
        midi.instruments.append(second)
        self.assertEqual(merged_score(midi, midi.instruments, 0, 64),
                         "(c1.e1)[K],g1[K]")
        self.assertEqual(len(first.notes), 1)
        self.assertEqual(first.notes[0].pitch, 60)
        self.assertEqual(first.notes[0].end, 2)

    def test_merge_tracks_preserves_repeated_attacks_and_unison_releases(self):
        midi, first = make_midi([(60, 0, 16), (60, 16, 32)])
        _, second = make_midi([(64, 0, 32)])
        midi.instruments.append(second)
        self.assertEqual(merged_score(midi, midi.instruments, 0, 32),
                         "(c1.e1)[L],c1[L]")
        first.notes = first.notes[:1]
        second.notes[0].pitch = 60
        self.assertEqual(merged_score(midi, midi.instruments, 0, 32), "c1[K]")

    def test_user_held_bass_regression_in_merge_export_and_preview(self):
        from score_editor import parse_score
        from midi_preview import render_notes
        midi, _ = make_midi([(69, 0, 16), (62, 16, 32), (65, 32, 48), (69, 48, 64)])
        _, bass = make_midi([(50, 0, 64)])
        midi.instruments.append(bass)
        expected = "(d.a1)[L],d1[L],f1[L],a1[L]"
        self.assertEqual(merged_score(midi, midi.instruments, 0, 64), expected)
        exported = convert_midi(midi, 0, "all", merge_all_tracks=True)
        tokens = [token.text for section in exported["musics"]
                  for token in parse_score(section["scores"][0])]
        self.assertEqual(",".join(tokens), expected)
        notes, _ = render_notes(",".join(tokens), 60)
        self.assertEqual(sum(pitch == 50 for _, _, pitch in notes), 1)

    def test_note_release_does_not_create_an_extra_attack_or_remove_real_reattack(self):
        midi, _ = make_midi([(60, 0, 64)])
        _, other = make_midi([(64, 0, 8), (60, 32, 48)])
        midi.instruments.append(other)
        self.assertEqual(merged_score(midi, midi.instruments, 0, 64),
                         "(c1.e1)[K],c1[L],U")

    def test_merge_tracks_preserves_silence_and_outputs_only_one_score(self):
        midi, _ = make_midi([(64, 0, 48)])
        _, other = make_midi([(67, 64, 80)])
        midi.instruments.append(other)
        self.assertEqual(merged_score(midi, midi.instruments, 0, 80), "e1[KL],U,g1[L]")
        merged = convert_midi(midi, 0, "all", split_points=(0.5, 1), merge_all_tracks=True)
        self.assertEqual([m["scores"] for m in merged["musics"]], [["e1[L]"], ["e1[L]"], ["e1[L],U,g1[L]"]])
        self.assertTrue(all(m["instruments"] == ["piano"] and m["alternatives"] == [""]
                            for m in merged["musics"]))
        separate = convert_midi(midi, 0, "all")
        self.assertTrue(all(len(m["scores"]) == 2 for m in separate["musics"]))

    def test_major_key_display_names(self):
        midi, _ = make_midi([(pitch, i * 16, (i + 1) * 16)
                             for i, pitch in enumerate([60, 62, 64, 65, 67, 69, 71])])
        histogram = key_histogram(midi)
        names = ("C", "bD", "D", "bE", "E", "F", "#F", "G", "bA", "A", "bB", "B")
        for transpose, name in enumerate(names):
            self.assertIn(f"推测调式：{name}大调", estimate_key(histogram, transpose))

    def test_whole_midi_key_fit_and_transposition(self):
        for pitches, name in [
                ([60, 62, 64, 65, 67, 69, 71], "C大调"),
                ([61, 63, 64, 66, 68, 70, 71], "B大调"),
                ([60, 62, 64, 65, 67, 69, 70], "F大调")]:
            midi, _ = make_midi([(pitch, i * 16, (i + 1) * 16)
                                 for i, pitch in enumerate(pitches)])
            histogram = key_histogram(midi)
            self.assertIn(name, estimate_key(histogram))
            if name == "C大调":
                self.assertIn("D大调", estimate_key(histogram, 2))
                self.assertIn("B大调", estimate_key(histogram, -1))
                self.assertEqual(estimate_key(histogram), estimate_key(histogram, 12))

    def test_key_uses_all_melodic_tracks_and_excludes_drums(self):
        midi, _ = make_midi([(p, i * 16, (i + 1) * 16)
                             for i, p in enumerate([60, 62, 64])])
        other = pretty_midi.Instrument(0)
        other.notes = [pretty_midi.Note(100, pitch, 0, 0.5) for pitch in (65, 67, 69, 71)]
        drum = pretty_midi.Instrument(0, is_drum=True)
        drum.notes = [pretty_midi.Note(100, 61, 0, 100)]
        midi.instruments.extend([other, drum])
        self.assertIn("C大调", estimate_key(key_histogram(midi)))
        self.assertIn("暂无", estimate_key(key_histogram(pretty_midi.PrettyMIDI())))

    def test_all_accompaniment_tracks_keep_order_alignment_and_metadata(self):
        from score_editor import parse_score
        midi = pretty_midi.PrettyMIDI(initial_tempo=120)
        for index in range(4):
            track = pretty_midi.Instrument(0)
            track.notes = [pretty_midi.Note(100, 60 + index, index * 0.25, 2 + index * 0.25)]
            midi.instruments.append(track)
        self.assertEqual(selected_track_indices(midi, 2, "all"), [2, 0, 1, 3])
        data = convert_midi(midi, 2, "all", split_points=(1.5, 3))
        for section in data["musics"]:
            self.assertEqual(len(section["scores"]), 4)
            self.assertEqual(len(section["instruments"]), 4)
            self.assertEqual(len(section["alternatives"]), 4)
            lengths = [sum(t.ticks for t in parse_score(line)) for line in section["scores"]]
            self.assertEqual(len(set(lengths)), 1)
        # Absolute leading silences are retained when another track starts earlier.
        self.assertTrue(data["musics"][0]["scores"][0].startswith("T"))
        self.assertTrue(data["musics"][0]["scores"][1].startswith("c1"))
        self.assertTrue(data["musics"][0]["scores"][2].startswith("U"))
        with self.assertRaises(ValueError):
            selected_track_indices(midi, 1, [0, 1])

    def test_live_alignment_score_matches_auto_detection(self):
        midi = pretty_midi.PrettyMIDI(initial_tempo=120)
        track = pretty_midi.Instrument(0)
        track.notes = [pretty_midi.Note(100, 60, i * 0.2, (i + 1) * 0.2) for i in range(48)]
        midi.instruments = [track]
        samples = alignment_samples(midi, [track])
        analysis = detect_beat_scale(midi, [track], samples)
        self.assertLess(alignment_quality(samples), 0.5)
        self.assertAlmostEqual(alignment_quality(samples, 1.25), 1)
        self.assertAlmostEqual(alignment_quality(samples, analysis["scale"]), 1)

    def test_alignment_empty_single_attack_and_sample_bound(self):
        midi = pretty_midi.PrettyMIDI()
        track = pretty_midi.Instrument(0)
        self.assertIsNone(alignment_quality(alignment_samples(midi, [track])))
        track.notes = [pretty_midi.Note(100, 60, 0, 0.25)]
        self.assertTrue(math.isfinite(alignment_quality(alignment_samples(midi, [track]))))
        track.notes = [pretty_midi.Note(100, 60, i * 0.25, (i + 1) * 0.25)
                       for i in range(2000)]
        self.assertTrue(all(len(values) <= 512 for values in alignment_samples(midi, [track])))

    def test_minimum_normal_note(self):
        for x, expected in [(-6, "H*8"), (-5, "H*4"), (-4, "H*2"), (-3, "H*1"),
                            (-2, "I"), (-1, "J"), (0, "K"), (1, "L"),
                            (2, "M"), (3, "N"), (4, "O")]:
            self.assertEqual(minimum_normal_note(x), expected)
        for x in (5, 6, 20):
            self.assertEqual(minimum_normal_note(x), "所有音符均可正常显示")

    def test_manual_scale_validation(self):
        self.assertEqual(parse_beat_scale("1.25"), 1.25)
        for value in ("", "abc", "0", "-1", "nan", "inf"):
            with self.assertRaises(ValueError):
                parse_beat_scale(value)

    def test_composite_duration_and_repeated_h(self):
        midi, track = make_midi([(67, 0, 12), (65, 12, 780)])
        self.assertEqual(score(midi, track, 0, 0, 780)[0],
                         "g1[MN],f1[HHH]")

    def test_separate_same_pitch_attacks_are_preserved(self):
        midi, track = make_midi([(67, 0, 12), (67, 12, 24)])
        self.assertEqual(score(midi, track, 0, 0, 24)[0],
                         "g1[MN],g1[MN]")

    def test_rounding_aligns_neighboring_events(self):
        midi, track = make_midi([(64, 0, 12.4), (65, 12.4, 24.2)])
        self.assertEqual(score(midi, track, 0, 0, 24)[0],
                         "e1[MN],f1[MN]")

    def test_base_beats_only_changes_metadata(self):
        midi, _ = make_midi([(64, 0, 100), (64, 100, 300)])
        default = convert_midi(midi, 0)
        for value in (1, 0, -1, 3):
            beats = base_beats_from_exponent(str(value))
            actual = convert_midi(midi, 0, base_beats=beats)
            self.assertEqual(len(actual["musics"]), 3)
            for section in actual["musics"]:
                self.assertEqual(section["baseBeats"], beats)
                section["baseBeats"] = 0.5
            self.assertEqual(actual, default)
        raw = json.dumps(default, separators=(",", ":"))
        self.assertNotIn("\n", raw)
        self.assertNotIn("\r", raw)

    def test_exponent_values_and_invalid_input(self):
        for value, expected in [("1", 0.5), ("0", 1), ("-1", 2), ("3", 0.125)]:
            self.assertEqual(base_beats_from_exponent(value), expected)
        for value in ("", "1.5", "abc", "99999", "-99999"):
            with self.assertRaises(ValueError):
                base_beats_from_exponent(value)

    def test_wrong_tempo_detection_and_playback_duration(self):
        for actual_bpm in (100, 137, 150):
            midi = pretty_midi.PrettyMIDI(initial_tempo=120)
            track = pretty_midi.Instrument(0)
            track.notes = [pretty_midi.Note(100, 60 + i % 5,
                                           i * 30 / actual_bpm, (i + 1) * 30 / actual_bpm)
                           for i in range(48)]
            midi.instruments = [track]
            analysis = detect_beat_scale(midi, [track])
            self.assertAlmostEqual(analysis["bpm"], actual_bpm, places=4)
            result = convert_midi(midi, 0, beat_scale=analysis["scale"])
            self.assertAlmostEqual(result["baseBpm"], actual_bpm, places=4)
            self.assertEqual(len(result["musics"]), 3)
            # Eight quarter beats per section, matching the original seconds.
            self.assertTrue(all(section["scores"][0].split(",") == [
                f"{('c1', '#c1', 'd1', '#d1', 'e1')[(section_index * 16 + i) % 5]}[L]"
                for i in range(16)] for section_index, section in enumerate(result["musics"])))
            self.assertAlmostEqual(24 * 60 / result["baseBpm"], track.notes[-1].end)

    def test_regular_tempo_is_unchanged(self):
        midi, track = make_midi([(60 + i % 5, i * 16, (i + 1) * 16) for i in range(32)])
        self.assertEqual(detect_beat_scale(midi, [track])["scale"], 1)

    def test_insufficient_notes_are_not_corrected(self):
        midi, track = make_midi([(60, 0, 12)])
        self.assertEqual(detect_beat_scale(midi, [track])["scale"], 1)


if __name__ == "__main__":
    unittest.main()
