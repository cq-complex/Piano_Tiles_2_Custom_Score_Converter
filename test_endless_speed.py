import unittest
from fractions import Fraction

from endless_speed import endless_speed


class EndlessSpeedTests(unittest.TestCase):
    @staticmethod
    def parts(bpm=125, lengths=(0.5, 0.5, 0.5)):
        return [{"bpm": bpm, "baseBeats": base} for base in lengths]

    def test_user_example_uses_each_integer_bpm_in_the_next_step(self):
        parts = self.parts()
        self.assertEqual([endless_speed(parts, stage).bpm for stage in range(1, 9)],
                         [143, 159, 172, 181, 188, 195, 202, 210])
        first = endless_speed(parts, 1)
        self.assertEqual(first.multiplier, Fraction(115, 100))
        self.assertEqual(first.blocks_per_second, Fraction(143, 30))

    def test_rotates_base_lengths_and_uses_the_actual_previous_speed(self):
        parts = self.parts(120, (0.25, 1, 0.5))
        steps = [endless_speed(parts, stage) for stage in range(1, 4)]
        self.assertEqual([s.score_section for s in steps], [1, 2, 3])
        self.assertEqual([s.bpm for s in steps], [69, 310, 168])
        self.assertEqual(steps[1].blocks_per_second, Fraction(310, 60))

    def test_stage_seven_uses_new_intercept_and_floor_still_applies(self):
        parts = self.parts(20)
        sixth, seventh = endless_speed(parts, 6), endless_speed(parts, 7)
        self.assertGreater(Fraction(143, 100) - Fraction(sixth.bpm, 500), Fraction(104, 100))
        self.assertEqual(seventh.multiplier, Fraction(143, 100) - Fraction(sixth.bpm, 500))
        fast = endless_speed(self.parts(401), 1)
        self.assertEqual(fast.multiplier, Fraction(104, 100))
        self.assertEqual(fast.bpm, 417)
        self.assertLess(Fraction(fast.bpm, 401), fast.multiplier)

    def test_rejects_arena_and_invalid_stage_numbers(self):
        with self.assertRaises(ValueError):
            endless_speed(self.parts()[:2], 1)
        for stage in (0, 1.5, True):
            with self.assertRaises(ValueError):
                endless_speed(self.parts(), stage)
