import json
from pathlib import Path
import unittest

from audition import audition_windows
from main import convert_midi
from midi_preview import build_audition_midi
from score_editor import replace_fragment
from system_midi import midi_schedule
from test_conversion import make_midi


class AuditionTests(unittest.TestCase):
    def test_examples_play_and_start_end_are_inclusive(self):
        for path in Path(__file__).with_name('Song_JSON_Example').glob('*.json'):
            with self.subTest(example=path.name):
                data = json.loads(path.read_text(encoding='utf-8-sig'))
                midi, seconds = build_audition_midi(data)
                self.assertGreater(seconds, 0)
                self.assertAlmostEqual(midi.length, seconds, places=4)
        data = {'musics': [{'bpm': 120, 'scores': [','.join(['c[L]'] * 20)]}],
                'audition': {'start': [0, 0], 'end': [0, 16]}}
        midi, seconds = build_audition_midi(data)
        self.assertEqual(sum(msg.type == 'note_on' for msg in midi.tracks[1]), 17)
        self.assertEqual(seconds, 4.25)

    def test_cross_segment_selection_cuts_inside_blocks_and_aligns_accompaniment(self):
        data = {'musics': [
            {'bpm': 120, 'scores': ['c1[L],6<d1[M],e1[M]>', 'c[K]']},
            {'bpm': 60, 'scores': ['6<(f1.a1)[M],g1[M]>,e1[L]', 'V,d[LM]']},
            {'bpm': 60, 'scores': ['a1[L]', 'e[L]']},
        ], 'audition': {'start': [0, 2], 'end': [1, 0]}}
        midi, seconds = build_audition_midi(data)
        events, midi_seconds = midi_schedule(midi)
        attacks = [(time, msg[0] & 0x0f, msg[1]) for time, msg in events if msg[0] & 0xf0 == 0x90]
        self.assertEqual(attacks, [(0., 0, 64), (0., 1, 48), (0.125, 0, 65), (0.125, 0, 69)])
        self.assertAlmostEqual(midi_seconds, seconds)
        self.assertEqual(seconds, 0.375)
        self.assertEqual(audition_windows(data, {'start': [1, 1], 'end': [1, 1]}), [(1, 8, 16, 60)])
        with self.assertRaises(ValueError):
            audition_windows(data, {'start': [1, 1], 'end': [0, 0]})
        with self.assertRaises(ValueError):
            audition_windows(data, {'start': [0, 0], 'end': [0, 3]})

    def test_wrapping_keeps_inner_indices_and_merging_moves_endpoints(self):
        data = {'musics': [{'bpm': 120, 'baseBeats': 0.5, 'scores': ['c1[M],d1[M],e1[L]']}],
                'audition': {'start': [0, 1], 'end': [0, 2]}}
        replace_fragment(data, 0, 0, 1, '5<c1[M],d1[M]>')
        self.assertEqual(data['audition'], {'start': [0, 1], 'end': [0, 2]})
        replace_fragment(data, 0, 0, 0, '(c1~d1)[L]')
        self.assertEqual(data['audition'], {'start': [0, 0], 'end': [0, 1]})
        self.assertEqual(audition_windows(data), [(0, 0, 32, 120)])
        data['audition']['end'] = [0, 0]
        replace_fragment(data, 0, 0, 0, '5<c1[M],d1[M]>')
        self.assertEqual(data['audition'], {'start': [0, 0], 'end': [0, 1]})
        self.assertEqual(audition_windows(data), [(0, 0, 16, 120)])

    def test_conversion_default_never_points_past_last_item(self):
        midi, _ = make_midi([(60, 0, 16), (62, 16, 32), (64, 32, 48)])
        data = convert_midi(midi, 0, split_points=(0.5, 1))
        self.assertEqual(data['audition'], {'start': [0, 0], 'end': [0, 0]})
        self.assertEqual(audition_windows(data), [(0, 0, 16, 60)])
