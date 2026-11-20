import unittest
from random import Random

from simulator_model import folded_path, model_score


class SimulatorLayoutTests(unittest.TestCase):
    def test_mixed_score_respects_lane_and_block_rules(self):
        data = {"musics": [{"bpm": 120, "baseBeats": 0.5, "scores": [
            "c[L],d[K],5<e[M],f[M]>,5<g[M],a[M]>,5<b[M],c[M]>,"
            "3<a[M],a[M],a[M],a[M],a[M],a[M]>,7<c[M],d[M],e[L]>,"
            "8<f[M],g[M]>,8<a[L],b[L]>,9<c1[K]>,10<d1[L],e1[L],f1[L]>",
            "R,S,T,e[L],f[L]",
        ]}]}
        sections, blocks, warnings = model_score(data, Random(7))
        self.assertEqual([b.kind for b in blocks],
                         ["black", "hold", "5", "5", "5", "3", "7", "8", "8", "9", "head", "burst", "burst"])
        self.assertEqual([b.lanes for b in blocks[2:5]], [(0, 2), (1, 3), (0, 2)])
        self.assertTrue(all(a.exit != b.entry for a, b in zip(blocks, blocks[1:])))
        self.assertFalse(warnings)
        self.assertEqual((blocks[5].lanes, blocks[5].count), ((1, 2), 6))
        self.assertEqual(len(blocks[6].path), 2)
        self.assertEqual({p[0] for p in blocks[6].path}, {0, 3})
        self.assertEqual(blocks[6].length, 2)
        self.assertEqual(abs(blocks[7].path[0][0] - blocks[7].path[-1][0]), 2)
        self.assertEqual(len(blocks[8].path), 3)
        self.assertEqual(blocks[9].sides, [(0, 1), (1, 1)])
        self.assertEqual(blocks[10].lanes, (1, 2))
        self.assertEqual(sections[0].end, 19)

    def test_fold_direction_reflects_only_on_outer_columns(self):
        self.assertEqual(folded_path(0, 1), ((0, 0), (2, 1)))
        self.assertEqual(folded_path(3, 2), ((3, 0), (0, 0.75), (1, 1)))
        self.assertEqual(folded_path(0, 3), ((0, 0), (3, 0.5), (0, 1)))

    def test_silent_rests_are_gaps_and_accompanied_rests_make_tiles(self):
        part = {"bpm": 120, "baseBeats": 0.5, "scores": ["U,U,c[L]", "U,d[L],U"]}
        sections, blocks, _ = model_score({"musics": [part, part]})
        self.assertEqual([(b.section, b.start) for b in blocks], [(0, 1), (0, 2), (1, 4), (1, 5)])
        self.assertEqual(sections[-1].end, 6)



    def test_single_tiles_use_four_lanes_and_avoid_both_double_lanes(self):
        def layout(main):
            return model_score({"musics": [{"bpm": 120, "baseBeats": 0.5, "scores": [main]}]}, Random(7))[1]

        for note in ("a[L]", "a[K]"):
            blocks = layout(",".join([note] * 12))
            self.assertEqual({b.entry for b in blocks}, set(range(4)))
            self.assertTrue(all(a.exit != b.entry for a, b in zip(blocks, blocks[1:])))
        mixed = layout("5<c[M],d[M]>,a[L],5<c[M],d[M]>,a[K],5<c[M],d[M]>,7<e[M],f[M]>")
        for double, next_block in zip(mixed[::2], mixed[1::2]):
            self.assertNotIn(next_block.entry, double.lanes)

    def test_only_silent_rests_release_collision_rule(self):
        def lanes(main, accompaniment=None, seed=7):
            scores = [main] + ([accompaniment] if accompaniment else [])
            _, blocks, _ = model_score({"musics": [{"bpm": 120, "baseBeats": 0.25, "scores": scores}]}, Random(seed))
            return [b.entry for b in blocks]

        examples = [lanes("a[M],V,a[M]", seed=seed) for seed in range(16)]
        self.assertTrue(any(a == b for a, b in examples))
        self.assertTrue(any(a != b for a, b in examples))
        main = ",V,".join(["a[M]"] * 24)
        first, second = lanes(main), lanes(main, seed=13)
        self.assertEqual(set(first), set(range(4)))
        self.assertEqual(set(second), set(range(4)))
        self.assertNotEqual(first, second)
        self.assertNotEqual(*lanes("a[M],a[M]"))
        activated = lanes("a[M],V,a[M]", "V,e[M],V")
        self.assertEqual(len(activated), 3)
        self.assertTrue(all(a != b for a, b in zip(activated, activated[1:])))

    def test_warning_tiles_keep_their_shape_and_ignore_valid_short_children(self):
        main = "a[M],5<c[L],d[L]>,7<a[J]>,3<e[M],f[M]>,10<c[L],d[M]>,c[L]"
        _, blocks, _ = model_score({"musics": [{"bpm": 120, "baseBeats": 0.5, "scores": [main]}]})
        self.assertEqual([b.kind for b in blocks], ["black", "5", "7", "3", "head", "burst", "black"])
        self.assertEqual([bool(b.warning) for b in blocks], [True, True, True, False, True, True, False])
        self.assertTrue(blocks[2].path)
