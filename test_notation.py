"""Tests for the visual editor's score mapping and source key handling."""
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

import pretty_midi

from main import convert_midi
from musicxml_input import read_musicxml
from notation_keys import KeySignature, key_fit_label, numbered_pitch, staff_spelling
from notation_model import edit_cell, visual_attacks, visual_cells
from test_conversion import make_midi
from test_score_editor import document


class NotationTests(unittest.TestCase):
    def test_key_fit_exposes_two_scores_when_tied_or_weak(self):
        self.assertIn("不确定", key_fit_label([1] + [0] * 11))
        weak = key_fit_label([1] * 12)
        self.assertIn("不明确", weak)
        self.assertEqual(weak.count("58.3%"), 2)
        self.assertEqual(numbered_pitch(77, KeySignature(-1)), ("1", 1))
        self.assertEqual(staff_spelling(70, KeySignature(-1)), (34, ""))

    def test_midi_modulation_maps_into_segments_after_scaling_and_transpose(self):
        midi, _ = make_midi([(60, i * 32, (i + 1) * 32) for i in range(6)])
        midi.key_signature_changes = [pretty_midi.KeySignature(0, 0),
                                      pretty_midi.KeySignature(21, 3)]
        info = {}
        convert_midi(midi, 0, beat_scale=2, split_points=(4, 8), transpose=2, split_info=info)
        self.assertEqual(info["notation_keys"], [[[(0, KeySignature(2))]],
                         [[(0, KeySignature(2)), (64, KeySignature(2, "minor"))]],
                         [[(0, KeySignature(2, "minor"))]]])

    def test_musicxml_retains_key_mode_and_staff_specific_override(self):
        note = ("<note><pitch><step>C</step><octave>4</octave></pitch>"
                "<duration>1</duration><staff>1</staff></note>")
        xml = ("<score-partwise><part-list/><part id='P'><measure number='1'>"
               "<attributes><divisions>1</divisions><key><fifths>-3</fifths>"
               "<mode>minor</mode></key></attributes>" + note + "</measure>"
               "<measure number='2'><attributes><key number='1'><fifths>1</fifths>"
               "<mode>major</mode></key></attributes>" + note * 3 + "</measure>"
               "</part></score-partwise>")
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "modulation.musicxml"
            source.write_text(xml, encoding="utf-8")
            midi = read_musicxml(source)
        self.assertEqual(midi.instruments[0].musicxml_keys,
                         ((0, KeySignature(-3, "minor")), (1, KeySignature(1))))

    def test_grid_alignment_and_playing_a_compound_attack_across_sections(self):
        from midi_preview import build_notation_preview
        from system_midi import midi_schedule

        data = {"musics": [
            {"bpm": 120, "scores": ["(c~d)[K]", "e[L],f[L]"]},
            {"bpm": 60, "scores": ["g[L]", "a[L]"]},
        ]}
        track_a, track_b = [visual_attacks(visual_cells(line), 120)
                            for line in data["musics"][0]["scores"]]
        self.assertEqual([a.tick for a in track_a], [a.tick for a in track_b])
        self.assertEqual([a.atom for a in track_a], [0, 1])
        midi, timeline = build_notation_preview(data, 0, track_a[1].tick, selected_layer=0)
        self.assertEqual(timeline, [(0, 0, .25, 16, .015625), (1, .25, .75, 0, .03125)])
        events, duration = midi_schedule(midi)
        attacks = [(at, event[1]) for at, event in events if event[0] & 0xF0 == 0x90]
        self.assertEqual(attacks, [(0, 50), (0, 53), (.25, 55), (.25, 57)])
        self.assertAlmostEqual(duration, .75)

    def test_arpeggio_starts_from_selected_attack_without_replaying_prior_ones(self):
        from midi_preview import build_notation_preview
        from system_midi import midi_schedule

        score = "(c@e@g)[K],a[L]"
        data = {"musics": [{"bpm": 120, "scores": [score]}]}
        attacks = visual_attacks(visual_cells(score), 120)
        midi, timeline = build_notation_preview(data, 0, attacks[1].tick,
                                                selected_layer=0, all_tracks=False)
        events, _ = midi_schedule(midi)
        notes = [(at, event[1]) for at, event in events if event[0] & 0xF0 == 0x90]
        self.assertEqual([pitch for _, pitch in notes], [52, 55, 57])
        self.assertEqual(notes[0][0], 0)
        self.assertAlmostEqual(timeline[0][2], .69)

    def test_move_cut_preserves_edits_keys_bpm_and_inclusive_audition(self):
        from score_timeline import audition_positions, move_section_cut, section_boundaries

        data = document("c[L],d[L]", "c[K]")
        for i, part in enumerate(data["musics"]):
            part["bpm"] = 120 + i * 10
        data["musics"][1]["scores"][0] = "g[L]{3};a[L]"
        data["audition"] = {"start": [1, 0], "end": [1, 1]}
        keys = [[[(0, KeySignature(0))], []], [[(0, KeySignature(1))], []], [[], []]]
        old_positions = audition_positions(data, section_boundaries(data))
        moved, moved_keys = move_section_cut(data, 1, 48, keys)
        self.assertEqual(section_boundaries(moved), [0, 48, 64, 96])
        self.assertEqual(moved["musics"][1]["scores"][0], "a[L]")
        self.assertEqual(moved["musics"][0]["scores"][0], "c[L],d[L],g[L]{3}")
        self.assertEqual(moved_keys[0][0][-1], (32, KeySignature(1)))
        self.assertEqual(moved_keys[1][0], [(0, KeySignature(1))])
        self.assertEqual([p["bpm"] for p in moved["musics"]], [120, 130, 140])
        self.assertEqual(audition_positions(moved, section_boundaries(moved)), old_positions)
        self.assertEqual(section_boundaries(data), [0, 32, 64, 96])

    def test_move_cut_rejects_rests_short_notes_and_compound_interiors(self):
        from score_timeline import move_section_cut

        data = document("c[L],U,d[M],e[M],f[L]")
        for tick in (16, 32, 40, 48):
            with self.subTest(tick=tick), self.assertRaisesRegex(ValueError, "正常长度"):
                move_section_cut(data, 1, tick)
        data = document("c[L],d[K],e[L]", "(c~d~e~f)[H]")
        with self.assertRaisesRegex(ValueError, "不能切开"):
            move_section_cut(data, 1, 16)

    def test_note_edits_keep_chords_wrappers_and_fixed_total_time(self):
        data = document("5<(c.e)[M]{5},g[M]>;(a~mute~b)[L],d[L]")
        score = data["musics"][0]["scores"]
        edit_cell(data, 0, 0, visual_cells(score[0]), 0, 1, "f", "M")
        self.assertEqual(score[0], "5<(c.f)[M]{5},g[M]>;(a~mute~b)[L],d[L]")
        edit_cell(data, 0, 0, visual_cells(score[0]), 2, 2, "#a", "LM")
        self.assertEqual(score[0], "5<(c.f)[M]{5},g[M]>;(a~mute~#a)[LM],d[M]")
        with self.assertRaisesRegex(ValueError, "小节线"):
            edit_cell(data, 0, 0, visual_cells(score[0]), 2, 2, "#a", "L", borrow=-1)
