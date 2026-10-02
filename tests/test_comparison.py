from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from story_pipeline.comparison import compare_editions
from story_pipeline.models import PipelineError


class ComparisonTests(unittest.TestCase):
    def write_metadata_fixture(self, root: Path, *, revised: bool) -> None:
        root.mkdir()
        video = b"test-only-file-for-report-hash"
        (root / "episode.mp4").write_bytes(video)
        report = {
            "simulated": False, "duration_seconds": 1280,
            "short_subtitles_under_one_second": 0 if revised else 8,
            "measured_loudness_lufs": -18, "measured_true_peak_dbtp": -3,
            "decode_all_frames_pass": True, "target_duration_pass": True,
            "stream_format_pass": True, "loudness_pass": True, "true_peak_pass": True,
        }
        if revised:
            report.update({
                "shot_count": 56, "average_shot_seconds": 22.86,
                "story_anchored_shots": 56,
                "video_sha256": hashlib.sha256(video).hexdigest(),
            })
        (root / "evaluation.json").write_text(json.dumps(report), encoding="utf-8")
        (root / "episode-script.json").write_text(json.dumps({
            "scenes": [{"narration": "text"} for _ in range(14)],
            "revision_notes": ["Revised action and scene direction."],
        }), encoding="utf-8")

    def test_legacy_baseline_and_measured_revision_are_compared_without_quality_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "old", Path(directory) / "new"
            self.write_metadata_fixture(old, revised=False)
            self.write_metadata_fixture(new, revised=True)
            report = compare_editions(old, new)
            self.assertEqual(report["measurements"]["shot_count"], {"before": 14, "after": 56})
            self.assertFalse(report["youtube_connection"])
            self.assertTrue(report["not_demonstrated"])
            self.assertTrue((new / "before-after.json").is_file())
            self.assertTrue((new / "comparison.html").is_file())
            self.assertFalse((old / "before-after.json").exists())

    def test_changed_video_invalidates_its_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "old", Path(directory) / "new"
            self.write_metadata_fixture(old, revised=False)
            self.write_metadata_fixture(new, revised=True)
            (new / "episode.mp4").write_bytes(b"changed")
            with self.assertRaisesRegex(PipelineError, "changed after"):
                compare_editions(old, new)

    def test_same_directory_is_not_an_independent_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "same"
            self.write_metadata_fixture(root, revised=True)
            with self.assertRaisesRegex(PipelineError, "separate"):
                compare_editions(root, root)


if __name__ == "__main__":
    unittest.main()
