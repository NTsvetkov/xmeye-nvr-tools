"""Regression checks for recorder filename/FC end boundaries.

Run: python -m unittest discover -s scripts -p "test_xmeye_timeline.py"
"""
import contextlib
import io
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from xmeye_convert import Keyframe, StreamInfo, build_timeline


class FilenameEndTests(unittest.TestCase):
    def timeline(self, final_gap, filename="01_2026-11-03_160001_161618.xmeye"):
        start = datetime(2026, 11, 3, 16, 0, 1)
        named_end = datetime(2026, 11, 3, 16, 16, 18)
        last = named_end - timedelta(seconds=final_gap)
        # One keyframe per second, twenty frames in each GOP.
        count = int((last - start).total_seconds()) + 1
        keyframes = [Keyframe(i, i * 20, start + timedelta(seconds=i), 20, 2)
                     for i in range(count)]
        keyframes[-1].timestamp = last
        info = StreamInfo("h264", "h264", keyframes, count, count * 19)
        messages = io.StringIO()
        with contextlib.redirect_stdout(messages):
            segments, end, source = build_timeline(
                Path(filename), info, 60.0, 10.0, None
            )
        return info, segments, end, source, messages.getvalue()

    def test_one_second_overrun_keeps_all_frames_and_final_gop(self):
        info, segments, end, source, warning = self.timeline(-1)
        self.assertEqual(end, datetime(2026, 11, 3, 16, 16, 20))
        self.assertEqual(sum(s.frame_count for s in segments), info.video_frames)
        self.assertEqual(segments[-1].end_keyframe, len(info.keyframes))
        self.assertTrue(all(s.duration > 0 for s in segments))
        self.assertTrue(all(abs(s.fps - 20) < 0.000001 for s in segments))
        self.assertIn("filename end tolerance", source)
        self.assertIn("Warning:", warning)

    def test_larger_overruns_are_rejected(self):
        for gap in (-1.001, -2, -30):
            with self.subTest(gap=gap):
                with self.assertRaisesRegex(RuntimeError, "Recording end disagrees"):
                    self.timeline(gap)

    def test_zero_gap_still_infers_final_gop(self):
        info, _, end, _, warning = self.timeline(0)
        self.assertEqual(end, info.keyframes[-1].timestamp + timedelta(seconds=1))
        self.assertEqual(warning, "")

    def test_positive_gap_keeps_filename_end(self):
        _, _, end, source, warning = self.timeline(1)
        self.assertEqual(end, datetime(2026, 11, 3, 16, 16, 18))
        self.assertEqual(source, "recording filename")
        self.assertEqual(warning, "")

    def test_large_positive_gap_still_infers_end(self):
        info, _, end, source, _ = self.timeline(31)
        self.assertEqual(end, info.keyframes[-1].timestamp + timedelta(seconds=1))
        self.assertIn("recording ended before filename boundary", source)

    def test_mismatched_start_is_still_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "first FC timestamps disagree"):
            self.timeline(-1, "01_2026-11-03_150001_161618.xmeye")


if __name__ == "__main__":
    unittest.main()
