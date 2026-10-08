"""Offline regression checks for short-clip ASR; no models or cloud calls."""
import unittest

import numpy as np

from review import pad_recheck_audio, recheck_issues


class RecheckRegression(unittest.TestCase):
    def test_original_nine_second_hallucination_is_rejected(self):
        bad = [{'start': 0.0, 'end': 29.98,
                'text': '请不吝点赞 订阅 转发 打赏支持明镜与点点栏目',
                'no_speech_prob': .8358022570610046}]
        issues = recheck_issues(bad, 9)
        self.assertTrue(any('TIMING' in s for s in issues))
        self.assertTrue(any('UNCERTAIN' in s for s in issues))

    def test_float32_hallucination_with_valid_timing_still_rejected(self):
        self.assertTrue(recheck_issues([{'start': 0, 'end': 9,
                                        'text': 'unrelated output', 'no_speech_prob': .846}], 9))

    def test_short_waveform_preserved_with_silent_tail(self):
        audio = np.linspace(-.1, .1, 9 * 16000, dtype=np.float32)
        padded = pad_recheck_audio(audio)
        self.assertEqual(len(padded), 30 * 16000)
        np.testing.assert_array_equal(padded[:len(audio)], audio)
        self.assertFalse(padded[len(audio):].any())
        self.assertEqual(padded.dtype, audio.dtype)

    def test_second_window_and_exact_window(self):
        for seconds, expected in [(30, 30), (31, 60), (60, 60)]:
            x = np.ones(seconds * 16000, dtype=np.float32) * .1
            self.assertEqual(len(pad_recheck_audio(x)), expected * 16000)

    def test_invalid_audio(self):
        for x in [np.array([]), np.zeros(16000), np.array([float('nan')]),
                  np.array([float('inf')]), np.ones((2, 16000))]:
            with self.assertRaises(ValueError):
                pad_recheck_audio(x)

    def test_empty_nonfinite_or_overlapping_output_rejected(self):
        for segments in [[], [{'start': 0, 'end': float('nan'), 'text': 'x'}],
                         [{'start': 0, 'end': 2, 'text': 'x'},
                          {'start': 1, 'end': 3, 'text': 'y'}]]:
            self.assertTrue(recheck_issues(segments, 9))

    def test_valid_clip_remains_candidate(self):
        self.assertEqual(recheck_issues([
            {'start': 0, 'end': 4, 'text': '现在用AI做视频', 'no_speech_prob': .387},
            {'start': 4, 'end': 9, 'text': '发挥到80% 90%甚至拉满', 'no_speech_prob': .387}
        ], 9), [])


if __name__ == '__main__':
    unittest.main()
