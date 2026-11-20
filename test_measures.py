import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pretty_midi

from main import convert_midi
from musicxml_input import read_musicxml
from score_editor import replace_fragment
from score_measures import crosses_measure, measure_beats
from score_parameters import export_parameters
from score_splitting import slice_score
from test_conversion import make_midi


class MeasureAndTempoTests(unittest.TestCase):
    def test_midi_bar_lines_prioritize_safe_three_part_split_and_use_semicolons(self):
        midi, _ = make_midi([(60 + i % 7, 32 * i, 32 * (i + 1)) for i in range(14)])
        midi.time_signature_changes.append(pretty_midi.TimeSignature(3, 4, 0))
        self.assertEqual(measure_beats(midi), (0, 3, 6, 9, 12))
        self.assertFalse(crosses_measure(midi, measure_beats(midi)))
        info = {}
        data = convert_midi(midi, 0, split_info=info, bpm_step=5)
        self.assertEqual(info["boundaries"], [0, 6 * 32, 9 * 32, 14 * 32])
        self.assertIn(";", data["musics"][0]["scores"][0])
        self.assertTrue(all(not s["scores"][0].startswith(";") and not s["scores"][0].endswith(";")
                            for s in data["musics"]))
        self.assertEqual([s["bpm"] for s in data["musics"]],
                         [data["baseBpm"] + i * 5 for i in range(3)])
        self.assertIn(f'第 3 段：BPM {data["baseBpm"] + 10}，基础拍长 0.5', export_parameters(data))
        plain_info = {}
        plain = convert_midi(midi, 0, split_info=plain_info, mark_measures=False)
        self.assertEqual(plain_info["boundaries"], [0, 5 * 32, 9 * 32, 14 * 32])
        self.assertNotIn(";", "".join(s["scores"][0] for s in plain["musics"]))
        with self.assertRaises(ValueError):
            convert_midi(midi, 0, bpm_step=-1)

    def test_signature_changes_and_crossing_notes(self):
        midi, _ = make_midi([(60, 0, 128), *[(62, i * 32, (i + 1) * 32) for i in range(4, 12)]])
        midi.time_signature_changes.extend([pretty_midi.TimeSignature(3, 4, 0),
                                            pretty_midi.TimeSignature(2, 4, 7)])
        self.assertEqual(measure_beats(midi), (0, 3, 6, 7, 9, 11))
        self.assertTrue(crosses_measure(midi, measure_beats(midi)))
        default = convert_midi(midi, 0, split_points=(6, 9))
        enabled = convert_midi(midi, 0, split_points=(6, 9), mark_measures=True)
        self.assertNotIn(";", default["musics"][0]["scores"][0])
        self.assertEqual(enabled["musics"][0]["scores"][0], "c1[I],d1[K],d1[K]")
        self.assertIn(";", enabled["musics"][1]["scores"][0])

    def test_musicxml_uses_pickup_measure_and_real_measure_positions(self):
        note = ("<note><pitch><step>C</step><octave>4</octave></pitch>"
                "<duration>1</duration></note>")
        xml = ("<score-partwise><part-list/><part id='p'>"
               "<measure number='0' implicit='yes'><attributes><divisions>1</divisions></attributes>"
               + note + "</measure>"
               + "".join(f"<measure number='{i}'>" + note * 3 + "</measure>" for i in range(1, 5))
               + "</part></score-partwise>")
        with TemporaryDirectory() as temp:
            path = Path(temp) / "pickup.musicxml"
            path.write_text(xml, encoding="utf-8")
            midi = read_musicxml(path)
        self.assertEqual(measure_beats(midi), (0, 1, 4, 7, 10))
        data = convert_midi(midi, 0, split_points=(4, 10))
        self.assertEqual(data["musics"][0]["scores"][0], "c1[K];c1[K],c1[K],c1[K]")

    def test_silence_split_at_measure_and_editor_preserves_bar_boundary(self):
        self.assertEqual(slice_score("c[L],T,d[L]", 0, 64, [32]), ("c[L],U;U,d[L]", 4))
        data = {"musics": [{"scores": ["c[L];d[L]"], "baseBeats": .5}]}
        replace_fragment(data, 0, 0, 1, "e[L],f[L]")
        self.assertEqual(data["musics"][0]["scores"][0], "e[L];f[L]")
        with self.assertRaisesRegex(ValueError, "跨越小节"):
            replace_fragment(data, 0, 0, 1, "(e~f)[K]")
