import copy
from dataclasses import replace
import unittest

from score_editor import find_issues, parse_score
from score_suggestions import (apply_all_suggestions, apply_suggestion, issue_key,
                               suggest_changes, prepare_all_suggestions, suggestion_rows, batch_result)
from test_score_editor import document


class SuggestionTests(unittest.TestCase):
    def test_staccato_shorten_and_triplet_preserve_total_time(self):
        source = "g2[MN],V,W"
        data = document(source, base=0.25)
        issue = find_issues(data)[0]
        self.assertIn("跳音", issue["reason"])
        plan = suggest_changes(data, 0, 0, 2)[0]
        self.assertEqual(plan.replacement, "g2[M],U")
        apply_suggestion(data, plan)
        self.assertEqual(data["musics"][0]["scores"][0], "g2[M],U")
        for source, expected in (("d3[N],W", "d3[M]"), ("d3[N],W,Y", "d3[M],Y"),
                                 ("d3[NP],W", "d3[M],Y")):
            short = document(source, base=0.25)
            plan = suggest_changes(short, 0, 0, 0)[0]
            self.assertIn("吸收", plan.title)
            apply_suggestion(short, plan)
            self.assertEqual(short["musics"][0]["scores"][0], expected)

        source = "(c2.d2.g2)[NP],W,X,g2[NP],W,Y,g2[NO],W,Y"
        for base in (0.5, 0.25):
            data = document(source + ",a2[L]", base=base)
            plans, result = prepare_all_suggestions(data)
            self.assertTrue(any(plan.title == "~ 跳音三连音" for plan in plans))
            self.assertEqual(result["musics"][0]["scores"][0], "(c2.d2.g2~g2~g2)[K],a2[L]")
        separated = document("g2[MN];V,W", base=0.25)
        self.assertFalse(any("跳音" in plan.title for plan in suggest_changes(separated, 0, 0, 0)))

    def test_short_note_absorbs_following_rest_or_uses_long_strip(self):
        data = document("b1[NOP],Y", base=0.25)
        options = suggest_changes(data, 0, 0, 0)
        self.assertEqual(options[0].replacement, "b1[M]")
        self.assertTrue(any(s.replacement == "6<b1[NOP],Y>" for s in options))
        self.assertTrue(all((s.first, s.last) == (0, 1) for s in options))
        apply_suggestion(data, options[0])
        self.assertEqual(data["musics"][0]["scores"][0], "b1[M]")

    def test_absorption_also_works_when_rest_is_in_the_issue(self):
        data = document("b1[NOP],Y", "c[M]", base=0.25)
        issue = find_issues(data)[0]
        self.assertEqual((issue["first"], issue["last"]), (0, 1))
        options = suggest_changes(data, 0, 0, 1)
        self.assertEqual(options[0].replacement, "b1[M]")
        apply_suggestion(data, options[0])
        self.assertEqual(data["musics"][0]["scores"][1], "c[M]")

    def test_rest_absorption_must_reach_minimum_and_keeps_following_notes(self):
        self.assertEqual(suggest_changes(document("b1[NOP],Y", base=0.5), 0, 0, 0), [])
        data = document("(b1.d2)[NOP],Y,c2[L]", base=0.25)
        option = suggest_changes(data, 0, 0, 0)[0]
        self.assertEqual(option.replacement, "(b1.d2)[M]")
        apply_suggestion(data, option)
        self.assertEqual(data["musics"][0]["scores"][0], "(b1.d2)[M],c2[L]")

    def test_bulk_rest_absorption_and_ignored_rest_are_respected(self):
        data = document("b1[NOP],Y", base=0.25)
        self.assertEqual(apply_all_suggestions(data), 3)
        self.assertEqual(find_issues(data), [])
        data = document("b1[NOP],Y", "c[M]", base=0.25)
        ignored = {issue_key(data, find_issues(data)[0])}
        self.assertEqual(apply_all_suggestions(data, ignored), 2)
        self.assertEqual(data["musics"][0]["scores"][0], "b1[NOP],Y")

    def test_ignored_issue_is_excluded_from_bulk_but_can_be_manually_applied(self):
        data = document("c[M],d[M]")
        issue = find_issues(data)[0]
        ignored = {issue_key(data, issue)}
        self.assertEqual(apply_all_suggestions(data, ignored), 2)
        self.assertEqual(data["musics"][0]["scores"][0], "c[M],d[M]")
        suggestion = suggest_changes(data, 0, 0, 1)[0]
        apply_suggestion(data, suggestion)
        self.assertEqual(data["musics"][0]["scores"][0], "5<c[M],d[M]>")

    def test_neighbor_expansion_cannot_modify_an_ignored_fragment(self):
        data = document("6<c[M]>,d[M]")
        issues = find_issues(data)
        ignored = {issue_key(data, issue) for issue in issues if issue["first"] == 1}
        plans, result = prepare_all_suggestions(data, ignored)
        self.assertEqual(len(plans), 3)
        self.assertTrue(all(p.first == p.last == 0 and p.title == "解散该音符组" for p in plans))
        self.assertEqual(result["musics"][0]["scores"], ["c[M],d[M]"])

    def test_confirmation_plan_exactly_matches_commit_and_does_not_mutate(self):
        data = document("c[O],e[L],f[O]")
        before = copy.deepcopy(data)
        plans, result = prepare_all_suggestions(data)
        self.assertEqual(data, before)
        # Overlapping proposals must not appear in the confirmation list.
        self.assertEqual(len(plans), 3)
        self.assertTrue(all(p.first == 1 and p.last == 2 for p in plans))
        self.assertEqual(apply_all_suggestions(data), len(plans))
        self.assertEqual(data, result)

    def test_arpeggio_accepts_three_components_when_smallest_is_normal(self):
        data = document("d3[O],f3[KLNO]", base=0.25)
        options = suggest_changes(data, 0, 0, 0)
        self.assertEqual(options[0].replacement, "(d3@f3)[KLM]")
        apply_suggestion(data, options[0])
        self.assertEqual(data["musics"][0]["scores"][0], "(d3@f3)[KLM]")
        self.assertEqual(len(find_issues(data)), 2)
        # At base 0.5, M is smaller than L and this arpeggio is not suggested.
        options = suggest_changes(document("d3[O],f3[KLNO]", base=0.5), 0, 0, 0)
        self.assertFalse(any(s.replacement.startswith("(") for s in options))

    def test_single_half_grid_note_can_join_regular_arpeggio(self):
        options = suggest_changes(document("d3[O],f3[KLNO]", base=0.125), 0, 0, 0)
        self.assertEqual(options[0].replacement, "(d3@f3)[KLM]")

    def test_arpeggio_includes_arrival_and_has_alternative_sliders(self):
        for ending, expected in [("JKLMO", "I"), ("JLMO", "JK")]:
            data = document(f"#d2[O],#g2[O],c3[O],#d3[{ending}]")
            options = suggest_changes(data, 0, 0, 2)
            self.assertEqual(options[0].replacement, f"(#d2@#g2@c3@#d3)[{expected}]")
            self.assertEqual(options[0].last, 3)
            self.assertTrue(any(s.replacement.startswith("7<") for s in options))
            before = copy.deepcopy(data)
            apply_suggestion(data, options[0])
            self.assertEqual(data["musics"][1:], before["musics"][1:])

    def test_unequal_pickups_form_arpeggio_with_arrival(self):
        for source, expected, end in (
            ("a1[P],c2[O],#f2[LMNP]", "(a1@c2@#f2)[K]", 2),
            ("d1[P],g1[O],b1[P],d2[KMN]", "(d1@g1@b1@d2)[KL]", 3),
        ):
            with self.subTest(source=source):
                data = document(source + ",e2[L]")
                plans, result = prepare_all_suggestions(data)
                self.assertEqual((plans[0].first, plans[0].last), (0, end))
                self.assertEqual(plans[0].replacement, expected)
                self.assertEqual(result["musics"][0]["scores"][0], expected + ",e2[L]")

    def test_unequal_pickups_still_require_regular_total_and_pitch_direction(self):
        for source in ("a1[P],c2[O],#f2[LMN]", "c2[P],a1[O],#f2[LMNP]"):
            with self.subTest(source=source):
                options = suggest_changes(document(source), 0, 0, 1)
                self.assertFalse(any("@" in option.replacement for option in options))

    def test_two_half_notes_and_unequal_pair_suggest_double_black(self):
        for source in ("c[N],d[N]", "c[NO],d[O]"):
            options = suggest_changes(document(source, base=0.25), 0, 0, 1)
            self.assertEqual(options[0].replacement, f"5<{source}>")

    def test_even_half_notes_offer_array_and_spam(self):
        options = suggest_changes(document("c[M],d[M],e[M],f[M]"), 0, 0, 3)
        self.assertEqual(options[0].replacement, "5<c[M],d[M]>,5<e[M],f[M]>")
        self.assertTrue(any(s.replacement == "3<c[M],d[M],e[M],f[M]>" for s in options))

    def test_odd_half_notes_offer_spam_without_array(self):
        options = suggest_changes(document("c[M],d[M],e[M]"), 0, 0, 2)
        self.assertEqual(options[0].replacement, "3<c[M],d[M],e[M]>")
        self.assertFalse(any(s.replacement.startswith("5<") for s in options))

    def test_rounded_tuplet_suggests_uniform_sound(self):
        source = "#c3[NP],(#c3.#d3)[NO],(#c3.#d3)[NP]"
        options = suggest_changes(document(source), 0, 0, 2)
        self.assertEqual(options[0].replacement, "(#c3~#c3.#d3~#c3.#d3)[L]")

    def test_detect_triplet_even_if_no_note_is_short(self):
        source = "(#f1.#f2.b1)[MOP],(e1.e2.g1)[MO],(#f1.d1.d2)[MOP]"
        data = document(source, base=0.25)
        issue = find_issues(data)[0]
        self.assertTrue(issue["optional"])
        options = suggestion_rows(data)[0][1]
        self.assertEqual(options[0].replacement, "(#f1.#f2.b1~e1.e2.g1~#f1.d1.d2)[K]")
        self.assertEqual((options[0].first, options[0].last), (0, 2))
        result = batch_result(data, [options[0]])
        self.assertFalse(any(issue["section"] == 0 for issue in find_issues(result)))
        self.assertFalse(find_issues(document("c[M],d[M],e[M]", base=0.25)))

    def test_batch_commits_only_chosen_alternative_or_custom_change(self):
        data = document("c[M],d[M],e[M],f[M]")
        before = copy.deepcopy(data)
        rows = suggestion_rows(data)
        row, options = rows[0]
        alternative = next(plan for plan in options if plan.replacement.startswith("3<"))
        result = batch_result(data, [alternative, replace(rows[1][1][0], replacement="(c~d~e~f)[K]")],
                              [rows[2][0]])
        self.assertEqual([part["scores"][0] for part in result["musics"]],
                         ["3<c[M],d[M],e[M],f[M]>", "(c~d~e~f)[K]", "c[M],d[M],e[M],f[M]"])
        for plans, ignored in (([alternative, options[0]], []), ([alternative], [row]),
                               ([replace(alternative, replacement="c[L]")], [])):
            with self.assertRaises(ValueError):
                batch_result(data, plans, ignored)
        self.assertEqual(data, before)

    def test_accompaniment_suggestions_allow_only_sound_merges_on_their_own_track(self):
        main = "a1[P],c2[O],#f2[LMNP],b1[L]"
        data = document(main, main, base=0.25)
        for section in data["musics"]:
            section["scores"].append("c[NP],d[NO],e[NP],f[K],5<c[L],d[L]>")
        issues = [i for i in find_issues(data) if i["section"] == 0 and i["layer"]]
        self.assertEqual([(i["layer"], i["first"], i["last"]) for i in issues],
                         [(1, 0, 2), (2, 0, 2)])
        self.assertTrue(all(i["optional"] and not i["serious"] for i in issues))
        options = [opts for issue, opts in suggestion_rows(data) if issue["section"] == 0]
        self.assertEqual([[o.replacement for o in group] for group in options[1:]],
                         [["(a1@c2@#f2)[K]"], ["(c~d~e)[L]"]])
        batch, result = prepare_all_suggestions(data)
        self.assertEqual(len(batch), 9)
        self.assertEqual(result["musics"][0]["scores"],
                         ["(a1@c2@#f2)[K],b1[L]", "(a1@c2@#f2)[K],b1[L]",
                          "(c~d~e)[L],f[K],5<c[L],d[L]>"])
        _, ignored_result = prepare_all_suggestions(data, {issue_key(data, issues[0])})
        self.assertEqual(ignored_result["musics"][0]["scores"][1], main)
        self.assertNotEqual(ignored_result["musics"][0]["scores"][0], main)
        self.assertEqual(batch_result(data, options[1])["audition"], data["audition"])

    def test_invalid_groups_offer_dissolve_without_changing_contents(self):
        for group, body in (
            ("5<(c~d)[M], (e@g)[M];f[M]{3}>", "(c~d)[M],(e@g)[M];f[M]{3}"),
            ("7<c[J],e[M]>", "c[J],e[M]"),
            ("9<c[L],d[L]>", "c[L],d[L]"),
            ("2<c[K]>", "c[K]"),
        ):
            with self.subTest(group=group):
                data = document(group + ",g[L]")
                option = suggest_changes(data, 0, 0, 0)[0]
                self.assertEqual((option.title, option.replacement), ("解散该音符组", body))
                audition = copy.deepcopy(data["audition"])
                apply_suggestion(data, option)
                self.assertEqual(data["musics"][0]["scores"], [body + ",g[L]"])
                self.assertEqual(data["audition"], audition)
        for group in ("1<c[M]>", "2<c[L]>", "5<c[M],d[M]>"):
            self.assertEqual(suggest_changes(document(group), 0, 0, 0), [])

    def test_accompaniment_edit_does_not_invalidate_existing_type_nine(self):
        data = document("9<c[L],d[L]>", "e[L],f[L]")
        from score_editor import replace_fragment
        with self.assertRaisesRegex(ValueError, "9<"):
            replace_fragment(data, 0, 0, 1, "(e~f)[K]", layer=1)

    def test_repeated_fine_notes_are_sliders_not_arpeggio(self):
        options = suggest_changes(document("c[N],c[N],c[N],c[N],c[L]"), 0, 0, 3)
        self.assertEqual(options[0].replacement, "7<c[N],c[N],c[N],c[N]>")
        self.assertTrue(any(s.replacement.startswith("8<") for s in options))
        self.assertFalse(any(s.replacement.startswith("(") for s in options))

    def test_short_note_without_neighbors_cannot_form_a_long_strip(self):
        options = suggest_changes(document("c[MN]"), 0, 0, 0)
        self.assertEqual(options, [])

    def test_single_short_note_can_form_arpeggio_with_next_note(self):
        options = suggest_changes(document("c2[O],e2[IJKLMNO]"), 0, 0, 0)
        self.assertEqual(options[0].replacement, "(c2@e2)[H]")
        self.assertEqual((options[0].first, options[0].last), (0, 1))

    def test_undersized_fragment_has_both_neighbor_expansion_options(self):
        data = document("g[L],c[O],f[L]")
        options = suggest_changes(data, 0, 1, 1)
        strips = [s for s in options if s.replacement.startswith("6<")]
        self.assertEqual({(s.first, s.last) for s in strips}, {(0, 1), (1, 2)})
        for suggestion in strips:
            self.assertGreaterEqual(parse_score(suggestion.replacement)[0].ticks, 16)
        self.assertFalse(any(s.replacement == "6<c[O]>" for s in options))

    def test_old_undersized_long_strip_can_be_repaired_without_nesting(self):
        data = document("6<c2[O]>,e2[IJKLMNO]")
        self.assertEqual(len(find_issues(data)), 3)
        option = suggest_changes(data, 0, 0, 0)[0]
        self.assertEqual(option.replacement, "(c2@e2)[H]")
        apply_suggestion(data, option)
        self.assertEqual(len(find_issues(data)), 2)

    def test_overlapping_neighbor_suggestions_do_not_duplicate_or_corrupt_notes(self):
        data = document("c[O],e[L],f[O]")
        self.assertEqual(apply_all_suggestions(data), 3)
        self.assertEqual(len(find_issues(data)), 3)
        for section in data["musics"]:
            self.assertEqual(section["scores"][0], "c[O],6<e[L],f[O]>")
            self.assertEqual(sum(t.ticks for t in parse_score(section["scores"][0])), 20)

    def test_rest_activated_by_accompaniment_can_get_double_black(self):
        data = document("V,c[M]", "d[M],V")
        options = suggest_changes(data, 0, 0, 1)
        self.assertEqual(options[0].replacement, "5<V,c[M]>")
        apply_suggestion(data, options[0])
        self.assertEqual(len(find_issues(data)), 2)
        self.assertEqual(data["musics"][0]["scores"][1], "d[M],V")

    def test_bulk_application_skips_requested_issue_and_keeps_duration(self):
        data = document("c[M],d[M],e[L],f[N],g[N]")
        before = copy.deepcopy(data)
        skipped = {issue_key(data, find_issues(data)[0])}
        count = apply_all_suggestions(data, skipped)
        self.assertEqual(count, 5)
        self.assertEqual(len(find_issues(data)), 1)
        self.assertTrue(data["musics"][0]["scores"][0].startswith("c[M],d[M],"))
        for original, changed in zip(before["musics"], data["musics"]):
            self.assertEqual(sum(t.ticks for t in parse_score(original["scores"][0])),
                             sum(t.ticks for t in parse_score(changed["scores"][0])))

    def test_stale_suggestion_is_rejected(self):
        data = document("c[M],d[M]")
        option = suggest_changes(data, 0, 0, 1)[0]
        apply_suggestion(data, option)
        before = copy.deepcopy(data)
        with self.assertRaises(ValueError):
            apply_suggestion(data, option)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
