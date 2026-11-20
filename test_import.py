import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pretty_midi

from json_input import read_score_document
from main import convert_midi, estimate_key, score_key_histogram, transpose_score
from musicxml_input import read_musicxml
from score_editor import find_issues, repair_score_issue, replace_fragment


class ImportTests(unittest.TestCase):
    def test_musicxml_and_reference_midi_preserve_unmarked_conversion(self):
        folder = Path(__file__).with_name("Musicxml_Example")
        xml = read_musicxml(folder / "Example.musicxml")
        midi = pretty_midi.PrettyMIDI(str(folder / "Example.mid"))
        self.assertEqual([len(t.notes) for t in xml.instruments], [590, 608])
        marked = convert_midi(xml, 0, 1)
        for track in xml.instruments:
            track.musicxml_staccatos = ()
        reference = convert_midi(midi, 0, 1)
        self.assertEqual(convert_midi(xml, 0, 1), reference)
        self.assertNotEqual(marked, reference)
        self.assertEqual(marked["baseBpm"], reference["baseBpm"])

    def test_musicxml_arpeggio_and_tuplets_preserve_direction_and_duration(self):
        def note(step, duration, chord=False, marks="", ratio=""):
            return ("<note>" + ("<chord/>" if chord else "")
                    + f"<pitch><step>{step}</step><octave>4</octave></pitch>"
                    + f"<duration>{duration}</duration>{ratio}"
                    + (f"<notations>{marks}</notations>" if marks else "") + "</note>")

        arpeggio = "".join(note(pitch, 30, i > 0, '<arpeggiate direction="down"/>')
                           for i, pitch in enumerate("CEG"))
        explicit = "".join(note(pitch, 10, marks=mark) for pitch, mark in zip("DEF", (
            '<tuplet type="start"/>', "", '<tuplet type="stop"/>')))
        ratio = ("<time-modification><actual-notes>5</actual-notes>"
                 "<normal-notes>4</normal-notes></time-modification>")
        inferred = "".join(note(pitch, 6, ratio=ratio) for pitch in "GABCD")
        plain = note("E", 30) + note("F", 30)
        xml = ("<score-partwise><part-list/><part id='P1'><measure number='1'>"
               "<attributes><divisions>30</divisions></attributes>"
               + arpeggio + explicit + inferred + plain + "</measure></part></score-partwise>")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "marked.musicxml"
            path.write_text(xml, encoding="utf-8")
            midi = read_musicxml(path)
        result = convert_midi(midi, 0, base_beats=0.25, split_points=(1, 3))
        self.assertEqual([s["scores"][0] for s in result["musics"]], [
            "(g1@e1@c1)[K]", "(d1~e1~f1)[K],(g1~a1~b1~c1~d1)[K]", "e1[K],f1[K]"])
        auto = convert_midi(midi, 0, base_beats=0.25, transpose=1, merge_all_tracks=True)
        self.assertIn("(#g1@f1@#c1)[K]", ",".join(s["scores"][0] for s in auto["musics"]))
        with self.assertRaisesRegex(ValueError, "琶音或连音内部"):
            convert_midi(midi, 0, base_beats=0.25, split_points=(0.5, 3))

    def test_native_notation_round_trips_and_transposes_without_losing_effects(self):
        from score_editor import parse_score
        line = "C-1[M]{3},mute[L],empty[L],chuanshao[L],6<(c~mute~d)[K]{22},e[L]>"
        section = {"bpm": 120, "baseBeats": 0.25, "scores": [line]}
        raw = json.dumps({"musics": [section] * 3})
        data = read_score_document(raw)
        self.assertEqual(data["musics"][0]["scores"], [line])
        self.assertFalse(any(i["serious"] for i in find_issues(data)))
        self.assertEqual([t.kind for t in parse_score(line)], ["note", "rest", "rest", "note", "6"])
        transpose_score(data, 2)
        self.assertEqual(data["musics"][0]["scores"],
                         ["D-1[M]{3},mute[L],empty[L],chuanshao[L],6<(d~mute~e)[K]{22},#f[L]>"])
        self.assertEqual(data["audition"], read_score_document(raw)["audition"])
        scale = {"bpm": 120, "baseBeats": 0.5,
                 "scores": ["c[L]{3},d[L],e[L],f[L],g[L],a[L],b[L],mute[L],chuanshao[L]"]}
        data = read_score_document(json.dumps({"musics": [scale] * 3}))
        self.assertIn("C大调", estimate_key(score_key_histogram(data)))
        transpose_score(data, 2)
        self.assertIn("D大调", estimate_key(score_key_histogram(data)))

    def test_imported_tuplet_can_use_a_combined_rest_slot(self):
        from score_suggestions import suggest_changes
        part = {"bpm": 120, "baseBeats": 0.5, "scores": ["a[MOP],V,X,a[MOP]"]}
        data = read_score_document(json.dumps({"musics": [part] * 3}))
        issue = next(i for i in find_issues(data) if i.get("optional"))
        options = suggest_changes(data, issue["section"], issue["first"], issue["last"])
        self.assertTrue(any(s.replacement == "(a~mute~a)[K]" for s in options))

    def test_arena_import_edit_and_round_trip_preserve_non_three_counts(self):
        from audition import audition_windows
        from score_parameters import is_arena_score
        for count in (1, 2, 3, 5):
            with self.subTest(count=count):
                data = read_score_document(json.dumps({
                    "musics": [{"bpm": 120 + i, "baseBeats": 0.5, "scores": ["c[M],d[M]"]}
                               for i in range(count)],
                    "audition": {"start": [0, 0], "end": [count - 1, 1]},
                }))
                self.assertEqual(is_arena_score(data), count != 3)
                replace_fragment(data, count - 1, 0, 1, "5<c[M],d[M]>")
                output = read_score_document(json.dumps(data, separators=(",", ":")))
                self.assertEqual(len(output["musics"]), count)
                self.assertEqual(output["musics"][-1]["scores"], ["5<c[M],d[M]>"])
                self.assertEqual([section["bpm"] for section in output["musics"]],
                                 list(range(120, 120 + count)))
                self.assertEqual(len(audition_windows(output)), count)
        with self.assertRaises(ValueError):
            read_score_document('{"musics":[]}')

    def test_arena_missing_tempos_are_optional_and_not_added_on_export(self):
        from midi_preview import build_audition_midi
        from score_parameters import effective_score_bpms, export_parameters
        missing = {"baseBeats": 0.5, "scores": ["c[K]"]}
        provided = {**missing, "bpm": 80}
        data = read_score_document(json.dumps({
            "baseBpm": 120, "musics": [missing, provided],
            "audition": {"start": [0, 0], "end": [1, 0]},
        }))
        self.assertEqual(effective_score_bpms(data), [120, 80])
        self.assertAlmostEqual(build_audition_midi(data)[1], 1.25)
        self.assertIn("BPM 未填写（试听参考 120）", export_parameters(data))
        self.assertNotIn("bpm", json.loads(json.dumps(data))["musics"][0])
        no_base = read_score_document(json.dumps({"musics": [missing, provided]}))
        self.assertEqual(effective_score_bpms(no_base), [80, 80])
        self.assertEqual(effective_score_bpms(read_score_document(
            json.dumps({"musics": [missing]}))), [120])
        for parts in ([missing, provided, provided], [{**missing, "bpm": None}]):
            with self.assertRaises(ValueError):
                read_score_document(json.dumps({"musics": parts}))

    def test_musicxml_staccato_triplet_folds_its_articulation_rests(self):
        def note(step, duration, markings=""):
            return (f"<note><pitch><step>{step}</step><octave>4</octave></pitch>"
                    f"<duration>{duration}</duration>{markings}</note>")

        def marked(mark):
            return ("<notations>" + mark +
                    "<articulations><staccato/></articulations></notations>")

        xml = ("<score-partwise><part-list/><part id='p'><measure number='1'>"
               "<attributes><divisions>6</divisions></attributes>"
               + note("C", 6) + note("D", 6)
               + note("E", 2, marked('<tuplet type="start"/>'))
               + note("F", 2, marked(""))
               + note("G", 2, marked('<tuplet type="stop"/>'))
               + "</measure></part></score-partwise>")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "staccato.musicxml"
            path.write_text(xml, encoding="utf-8")
            midi = read_musicxml(path)
        score = convert_midi(midi, 0, split_points=(1, 2))
        self.assertEqual(score["musics"][2]["scores"][0], "(e1~f1~g1)[K]")

    def test_marked_musicxml_staccato_uses_base_length_and_leaves_unsafe_slots(self):
        def note(step, duration, marked=False):
            accent = "<notations><articulations><staccato/></articulations></notations>" if marked else ""
            return (f"<note><pitch><step>{step}</step><octave>4</octave></pitch>"
                    f"<duration>{duration}</duration>{accent}</note>")

        xml = ("<score-partwise><part-list/><part id='p'><measure number='1'>"
               "<attributes><divisions>32</divisions></attributes>"
               + note("C", 32) + note("D", 32)
               + note("E", 24, True) + note("F", 8, True)
               + note("G", 4, True) + note("A", 16) + note("B", 32)
               + "</measure></part></score-partwise>")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "staccato.musicxml"
            path.write_text(xml, encoding="utf-8")
            midi = read_musicxml(path)
        quarter = convert_midi(midi, 0, base_beats=.25, split_points=(1, 2))
        eighth = convert_midi(midi, 0, base_beats=.125, split_points=(1, 2))
        self.assertEqual(quarter["musics"][2]["scores"][0],
                         "e1[M],U,f1[M],g1[O],X,a1[L],b1[K]")
        self.assertEqual(eighth["musics"][2]["scores"][0],
                         "e1[N],U,W,f1[N],W,g1[N],a1[L],b1[K]")

    def test_musicxml_staccato_does_not_extend_across_a_later_attack(self):
        from main import apply_musicxml_staccatos
        from test_conversion import make_midi

        midi, track = make_midi([(60, 0, 4), (62, 6, 16)])
        track.musicxml_staccatos = [(0, 0.5, (60,))]
        source = "c1[N],X,d1[MO]"
        self.assertEqual(apply_musicxml_staccatos(midi, source, [track], 0, 1, 0, .25), source)

    def test_import_and_repair_score_errors_independently(self):
        data = read_score_document(json.dumps({"musics": [
            {"bpm": 120, "baseBeats": 0.5,
             "scores": ["c[M],5<c[M],d[M],e[M]>,d[L],g[Z],8<f[J]>,10<a[L],b[M]>",
                        "c[J],f[broken,g[L]"]},
            {"bpm": 125, "baseBeats": 0.5, "scores": ["c[L]"]},
            {"bpm": 130, "baseBeats": 0.5, "scores": ["d[L]"]},
        ]}))
        issues = find_issues(data)
        self.assertEqual(sum(i["serious"] for i in issues), 5)
        self.assertEqual(sum(not i["serious"] for i in issues), 1)
        for repair in ("g[L]", "f[L]"):
            issue = next(i for i in find_issues(data) if i["syntax"])
            repair_score_issue(data, issue, repair)
        self.assertEqual(data["musics"][0]["scores"][1], "c[J],f[L],g[L]")
        replace_fragment(data, 0, 1, 1, "6<c[M],d[M],e[M]>")
        replace_fragment(data, 0, 4, 4, "f[J]")
        replace_fragment(data, 0, 5, 5, "6<a[L],b[M]>")
        self.assertFalse(any(i["serious"] for i in find_issues(data)))
        self.assertEqual(data["musics"][2]["bpm"], 130)

    def test_examples_load_without_changing_document(self):
        for path in Path(__file__).with_name("Song_JSON_Example").glob("*.json"):
            with self.subTest(path=path):
                raw = path.read_text(encoding="utf-8-sig")
                self.assertEqual(read_score_document(raw), json.loads(raw))
