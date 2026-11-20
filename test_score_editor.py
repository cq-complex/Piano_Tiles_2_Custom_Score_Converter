import copy
import json
import unittest

from score_editor import (accompaniment_matches, block_error, dissolve_groups, find_issues,
                          find_wrapped_groups,
                          merge_sounds, parse_score, preview_text, replace_fragment)


def document(main, accompaniment=None, base=0.5):
    return {"baseBpm": 120, "musics": [
        {"id": i + 1, "baseBeats": base, "bpm": 120,
         "scores": [main] + ([accompaniment] if accompaniment is not None else []),
         "instruments": ["piano"], "alternatives": [""]}
        for i in range(3)], "audition": {"start": [0, 0], "end": [0, 4]}}


class ScoreEditorTests(unittest.TestCase):
    def test_dissolve_selected_wrappers_preserves_sound_and_audition_indices(self):
        from audition import audition_windows
        data = document("3<c[M],d[M]>,7<(c~d)[L],(e@g)[L]>;5<f[M],a[M]>",
                        "6<C-1[L]{3},mute[L]>,c[J]")
        data["audition"]["end"] = [0, 4]
        before = audition_windows(data)
        groups = [g for g in find_wrapped_groups(data) if g.section == 0]
        self.assertEqual(len(groups), 4)
        dissolve_groups(data, [groups[0], groups[1], groups[3]])
        self.assertEqual(data["musics"][0]["scores"], [
            "c[M],d[M],(c~d)[L],(e@g)[L];5<f[M],a[M]>", "C-1[L]{3},mute[L],c[J]",
        ])
        self.assertEqual(audition_windows(data), before)
        self.assertTrue(data["musics"][1]["scores"][0].startswith("3<"))
        self.assertEqual(len(find_wrapped_groups(data)), 9)

    def test_native_type_one_and_two_parse_and_only_wrong_type_two_warns(self):
        from score_editor import BLOCK_NAMES
        data = document("1<c[M],d[L]>,2<(e~f)[M],g[M]>,2<a[K]>", "2<b[M]>")
        self.assertNotIn("1", BLOCK_NAMES)
        self.assertNotIn("2", BLOCK_NAMES)
        self.assertEqual([token.kind for token in parse_score(data["musics"][0]["scores"][0])],
                         ["1", "2", "2"])
        issues = [issue for issue in find_issues(data) if issue["section"] == 0]
        self.assertEqual([(i["layer"], i["first"]) for i in issues], [(0, 2), (1, 0)])
        self.assertTrue(all(i["warning"] and not i["serious"] and not i["syntax"] for i in issues))
        self.assertEqual(issues[0]["reason"].split("总时长为 ")[1].split("，")[0], "32P")
        groups = [g for g in find_wrapped_groups(data) if g.section == 0]
        dissolve_groups(data, groups)
        self.assertEqual(data["musics"][0]["scores"],
                         ["c[M],d[L],(e~f)[M],g[M],a[K]", "b[M]"])

    def test_merge_user_arpeggio_example(self):
        source = "#d2[O],#g2[O],c3[O],#d3[JKLMO]"
        self.assertEqual(merge_sounds(source, "@"), "(#d2@#g2@c3@#d3)[I]")

    def test_merge_user_tuplet_and_chord_example(self):
        source = "#c3[NP],(#c3.#d3)[NO],(#c3.#d3)[NP]"
        self.assertEqual(merge_sounds(source, "~"), "(#c3~#c3.#d3~#c3.#d3)[L]")

    def test_merge_preserves_mixed_relations_order_and_repeated_pitches(self):
        source = "(a2.c2~#c3@d2)[K];a2[M],a2[M]"
        self.assertEqual(merge_sounds(source, "@"), "(a2.c2~#c3@d2@a2@a2)[KL]")
        self.assertEqual(merge_sounds("c[M],e[M]", "."), "(c.e)[L]")
        self.assertEqual(merge_sounds("c[H],c[H],c[H]", "~"), "(c~c~c)[HHH]")

    def test_mute_inside_tuplet_does_not_activate_covered_main_rest(self):
        data = document("c[L],V,c[L]", "V,(c~mute~c)[LM],V")
        self.assertFalse(find_issues(data))
        data = document("c[L],V,c[L]", "c[L],c[M],c[L]")
        self.assertTrue(find_issues(data))

    def test_merge_existing_long_strip_and_reject_unsupported_fragments(self):
        self.assertEqual(merge_sounds("6<c[M],d[M]>", "@"), "(c@d)[L]")
        for source in ("", "c[L]", "V,c[M]", "6<c[M],V>", "5<c[M],d[M]>,e[L]"):
            with self.assertRaises(ValueError):
                merge_sounds(source, "~")
        with self.assertRaises(ValueError):
            merge_sounds("c[M],d[M]", "!")

    def test_merged_fragment_can_be_applied_and_resolves_issues(self):
        source = "#c3[NP],(#c3.#d3)[NO],(#c3.#d3)[NP]"
        data = document(source, "c[L]")
        self.assertEqual(len(find_issues(data)), 3)
        merged = merge_sounds(source, "~")
        replace_fragment(data, 0, 0, 2, merged)
        self.assertEqual(data["musics"][0]["scores"][0], merged)
        self.assertEqual(data["musics"][0]["scores"][1], "c[L]")
        self.assertEqual(len(find_issues(data)), 2)
        self.assertEqual(sum(t.ticks for t in parse_score(source)),
                         parse_score(merged)[0].ticks)

    def test_short_rest_silent_on_both_tracks_is_valid(self):
        self.assertEqual(find_issues(document("V,V,U", "V,V,U")), [])
        self.assertEqual(find_issues(document("V,V,U")), [])

    def test_short_rest_activated_by_accompaniment(self):
        data = document("V,U", "c[M],U")
        issues = find_issues(data)
        self.assertEqual(len(issues), 3)
        self.assertTrue(all(i["first"] == i["last"] == 0 for i in issues))
        self.assertTrue(all("伴奏" in i["reason"] for i in issues))
        text, offsets = preview_text(data)
        for issue in issues:
            self.assertEqual(text[offsets[issue["section"]] + issue["start"]:
                                  offsets[issue["section"]] + issue["end"]], "V")

    def test_partial_overlap_and_sustained_accompaniment(self):
        # A sustained note also activates a later short rest.
        issues = find_issues(document("c[L],V,U", "e[KL],V"))
        self.assertEqual(len(issues), 3)
        self.assertTrue(all(i["first"] == 1 for i in issues))
        # Accompaniment starts partway through the short rest.
        self.assertEqual(len(find_issues(document("V,U", "W,c[N],U"))), 3)
        # Touching a boundary is not an overlap.
        issues = find_issues(document("c[M],V,U", "e[M],V,U"))
        self.assertEqual(len(issues), 3)
        self.assertTrue(all(i["first"] == i["last"] == 0 for i in issues))
        self.assertEqual(find_issues(document("V,U", "V,c[L]")), [])

    def test_any_accompaniment_track_can_activate_a_rest(self):
        data = document("V,U", "V,U")
        for section in data["musics"]:
            section["scores"].append("(c.e)[M],U")
        self.assertEqual(len(find_issues(data)), 3)
        for section in data["musics"]:
            section["baseBeats"] = 0.25  # V is now a normal block.
        self.assertEqual(find_issues(data), [])

    def test_mixed_run_can_be_corrected_without_rewriting_accompaniment(self):
        data = document("c[M],V,U", "V,e[M],U")
        issues = find_issues(data)
        self.assertEqual(len(issues), 3)
        self.assertTrue(all((i["first"], i["last"]) == (0, 1) for i in issues))
        self.assertTrue(all("伴奏" in i["reason"] for i in issues))
        replace_fragment(data, 0, 0, 1, "5<c[M],V>")
        self.assertEqual(data["musics"][0]["scores"][1], "V,e[M],U")
        self.assertEqual(len(find_issues(data)), 2)

    def test_parse_nested_delimiters_and_spans(self):
        text = "(c1.e1)[MN],5<d1[MN],e1[N]>;U,c1[HHH];"
        tokens = parse_score(text)
        self.assertEqual([t.ticks for t in tokens], [12, 16, 16, 768])
        self.assertEqual([t.kind for t in tokens], ["note", "5", "rest", "note"])
        self.assertTrue(all(text[t.start:t.end] == t.text for t in tokens))
        for invalid in ("g[MN}", "5<c[M]>", "c[Z]", "c[L],,d[M]", "c[L]>"):
            if invalid == "5<c[M]>":  # Syntactically legal; rule check rejects it.
                self.assertTrue(block_error(parse_score(invalid)[0], 16))
            else:
                with self.assertRaises(ValueError):
                    parse_score(invalid)

    def test_list_all_short_runs_and_ignore_accompaniment(self):
        data = document("c[M],d[N],e[MNOP],f[L],g[P],U,a[L]",
                        "c[P],d[P]")
        issues = find_issues(data)
        self.assertEqual(len(issues), 6)
        self.assertEqual([(i["first"], i["last"]) for i in issues[:2]], [(0, 2), (4, 4)])
        for section in data["musics"]:
            section["baseBeats"] = 1 / 32
        self.assertEqual(find_issues(data), [])

    def test_valid_wrappers(self):
        for text, minimum in [
                ("3<c[M],d[M],e[M]>", 16),
                ("5<c[MN],d[N]>", 16),
                ("5<c[MNO],d[O]>", 16),
                ("6<c[M],d[M]>", 16),
                ("7<c[K],d[P]>", 16),
                ("8<c[N],d[N]>", 16),
                ("10<g[J]," + ",".join(["c[L]"] * 8) + ">", 16),
                ("10<g[K]," + ",".join(["c[M]"] * 8) + ">", 8)]:
            self.assertEqual(block_error(parse_score(text)[0], minimum), "", text)

    def test_invalid_wrappers(self):
        for text in ("3<c[M],d[N]>", "5<c[M]>", "6<c[M],d[N]>",
                     "5<c[M],d[N]>", "7<c[KL],d[M]>", "8<c[J]>",
                     "10<c[J],d[L]>", "10<c[L],d[M],e[M]>"):
            self.assertTrue(block_error(parse_score(text)[0], 16), text)

    def test_accompaniment_block(self):
        data = document("9<c[L],d[L]>", "e[L],f[L]")
        section = data["musics"][0]
        self.assertTrue(accompaniment_matches(section, 0, 32, 16))
        self.assertEqual(find_issues(data), [])
        section["scores"][1] = "e[M],f[LM]"
        self.assertEqual(sum(i["serious"] for i in find_issues(data)), 1)
        section["scores"][1] = "U,f[L]"
        self.assertFalse(accompaniment_matches(section, 0, 32, 16))

    def test_transaction_and_duration_alignment(self):
        data = document("c[M],d[M],e[L]", "c[K]")
        before = copy.deepcopy(data)
        with self.assertRaises(ValueError):
            replace_fragment(data, 0, 0, 1, "5<c[M],d[N]>")
        self.assertEqual(data, before)
        with self.assertRaises(ValueError):
            replace_fragment(data, 0, 0, 1, "c[K]")
        self.assertEqual(data, before)
        replace_fragment(data, 0, 0, 1, "5<c[M],d[M]>")
        self.assertEqual(data["musics"][0]["scores"][0], "5<c[M],d[M]>,e[L]")
        self.assertEqual(data["musics"][0]["scores"][1], "c[K]")
        self.assertEqual(len(find_issues(data)), 2)
        self.assertEqual(len(data["musics"]), 3)
        raw = json.dumps(data, separators=(",", ":"))
        self.assertNotIn("\n", raw)

    def test_accompaniment_edit_rejected_without_matching_track(self):
        data = document("c[M],d[M]", "e[M],f[M]")
        before = copy.deepcopy(data)
        with self.assertRaisesRegex(ValueError, "伴奏"):
            replace_fragment(data, 0, 0, 1, "9<c[M],d[M]>")
        self.assertEqual(data, before)
        data["musics"][0]["scores"][1] = "e[L]"
        replace_fragment(data, 0, 0, 1, "9<c[M],d[M]>")
        self.assertTrue(data["musics"][0]["scores"][0].startswith("9<"))

    def test_highlight_offsets_locate_exact_json(self):
        data = document("c[M],d[M],e[L]", "c[M],d[M],e[L]")
        replace_fragment(data, 0, 0, 1, "5<c[M],d[M]>")
        text, offsets = preview_text(data)
        self.assertEqual(json.loads(text), data)
        for issue in find_issues(data):
            main = data["musics"][issue["section"]]["scores"][0]
            start = offsets[issue["section"]] + issue["start"]
            stop = offsets[issue["section"]] + issue["end"]
            self.assertEqual(text[start:stop], main[issue["start"]:issue["end"]])


if __name__ == "__main__":
    unittest.main()
