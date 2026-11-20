import json
import unittest

from main import convert_midi
from score_editor import parse_score
from score_splitting import choose_boundaries, slice_score
from test_conversion import make_midi


def length(line):
    return sum(token.ticks for token in parse_score(line))


class SplittingTests(unittest.TestCase):
    def test_short_run_is_not_cut_at_mathematical_thirds(self):
        # Total 96; old boundary 32 was inside the run spanning 24..48.
        scores = ["c[LM],d[M],e[M],f[M],g[KL]"]
        bounds, _ = choose_boundaries(scores, 0.5)
        self.assertTrue(all(not 24 < cut < 48 for cut in bounds[1:-1]))
        sections = [slice_score(scores[0], bounds[i], bounds[i + 1])[0] for i in range(3)]
        self.assertTrue(any("d[M],e[M],f[M]" in line for line in sections))
        self.assertEqual(sum(map(length, sections)), 96)

    def test_accompaniment_activated_rest_run_is_protected(self):
        scores = ["c[LM],V,d[M],V,e[KL]", "e[KL],e[KL]"]
        bounds, _ = choose_boundaries(scores, 0.5)
        self.assertTrue(all(not 24 < cut < 48 for cut in bounds[1:-1]))

    def test_balanced_normal_notes(self):
        bounds, _ = choose_boundaries([",".join(["c[L]"] * 12)], 0.5)
        self.assertEqual(bounds, [0, 64, 128, 192])

    def test_auto_avoids_rest_runs_and_rest_edges_even_with_accompaniment(self):
        main = "c[L],d[L],e[L],U,U,U,U,U,U,f[L],g[L],a[L]"
        bounds, _ = choose_boundaries([main, "c[JKL]"], 0.5)
        self.assertEqual(bounds, [0, 32, 160, 192])
        segments = [parse_score(slice_score(main, left, right)[0])
                    for left, right in zip(bounds, bounds[1:])]
        self.assertTrue(all(note.kind == "note" and note.ticks >= 16
                            for note in (segments[0][-1], segments[1][0],
                                         segments[1][-1], segments[2][0])))

    def test_manual_rejects_rest_edges_inside_rests_and_short_notes(self):
        main = "c[L],U,U,d[L],e[M],f[L],g[K],a[L]"
        for points in ((0.5, 4), (0.75, 4), (1, 4), (1.5, 4), (2, 4)):
            with self.subTest(points=points), self.assertRaisesRegex(ValueError, "两侧必须紧邻"):
                choose_boundaries([main], 0.5, points)
        bounds, message = choose_boundaries([main], 0.5, (3.25, 3.751))
        self.assertEqual(bounds, [0, 104, 120, 136])
        self.assertIn("自选分段", message)
        for points in ((0, 1), (2, 1), (1, 10), (1, 1.001), (float("nan"), 2), (1,)):
            with self.assertRaises(ValueError):
                choose_boundaries([main], 0.5, points)

    def test_no_unsafe_fallback_when_two_valid_cuts_are_impossible(self):
        for score in ("c[M],U,d[L]", "c[L],U,U,d[L]", "c[K]", "c[M],d[M],e[K]"):
            with self.subTest(score=score), self.assertRaisesRegex(ValueError, "无法安全分为三段"):
                choose_boundaries([score], 0.5)

    def test_two_interior_cuts_cannot_create_short_middle_note(self):
        with self.assertRaisesRegex(ValueError, "之间太短"):
            choose_boundaries(["c[H]"], 0.5, (1, 1.03125))

    def test_splits_keep_compound_notes_intact_on_accompaniment(self):
        lines = ["c[K],d[K],e[K],f[K],g[K],a[K]", "c[K],(d~e~f)[J],(g@a)[K],b[J]"]
        bounds, _ = choose_boundaries(lines, 0.5)
        self.assertEqual(bounds, [0, 32, 128, 192])
        with self.assertRaisesRegex(ValueError, "琶音或连音内部"):
            choose_boundaries(lines, 0.5, (2, 4))

    def test_one_sustained_note_still_can_split(self):
        bounds, message = choose_boundaries(["c[HHH]"], 0.5)
        self.assertEqual(bounds, [0, 256, 512, 768])
        self.assertIn("音符内部", message)
        self.assertEqual([slice_score("c[HHH]", bounds[i], bounds[i + 1])[0]
                          for i in range(3)], ["c[H]"] * 3)

    def test_selected_ranges_apply_to_both_tracks_and_preserve_attacks(self):
        midi, track = make_midi([(60, 0, 24), (62, 24, 32), (64, 32, 40),
                                 (65, 40, 48), (67, 48, 96)])
        midi.instruments.append(track)
        info = {}
        data = convert_midi(midi, 0, 1, base_beats=0.25, split_points=(0.75, 1.5), split_info=info)
        self.assertEqual(info["boundaries"], [0, 24, 48, 96])
        self.assertEqual([length(m["scores"][0]) for m in data["musics"]], [24, 24, 48])
        self.assertTrue(all(m["scores"][0] == m["scores"][1] for m in data["musics"]))
        self.assertEqual(data["musics"][1]["scores"][0], "d1[M],e1[M],f1[M]")
        raw = json.dumps(data, separators=(",", ":"))
        self.assertNotIn("\n", raw)
        self.assertEqual([m["id"] for m in data["musics"]], [1, 2, 3])

    def test_slicing_does_not_retrigger_earlier_overlapping_note(self):
        midi, _ = make_midi([(60, 0, 96), (62, 16, 32), (64, 32, 48)])
        result = convert_midi(midi, 0, split_points=(0.5, 1))
        self.assertEqual(result["musics"][0]["scores"][0], "c1[L]")
        self.assertEqual(result["musics"][1]["scores"][0], "d1[L]")
        self.assertEqual(result["musics"][2]["scores"][0], "e1[L],T,U")


if __name__ == "__main__":
    unittest.main()
