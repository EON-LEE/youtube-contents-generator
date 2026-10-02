from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from story_pipeline.media import (
    ass_text, balanced_caption, captions_from_boundaries, load_episode, normalized_text,
    reconcile_caption_timing, timestamp, write_ass, write_srt,
)
from story_pipeline.models import PipelineError
from story_pipeline.evaluation import edit_distance


class SubtitleTests(unittest.TestCase):
    def test_small_service_overlap_is_trimmed_and_recorded(self):
        source = [{"start": 0.1, "end": 3.3125, "text": "첫 문장"},
                  {"start": 3.2625, "end": 6, "text": "다음 문장"}]
        corrected = reconcile_caption_timing(source)
        self.assertEqual(corrected[0]["end"], corrected[1]["start"])
        self.assertEqual(corrected[0]["service_boundary_end_trim_seconds"], 0.05)
        self.assertEqual(source[0]["end"], 3.3125)

    def test_material_service_overlap_is_rejected(self):
        with self.assertRaises(PipelineError):
            reconcile_caption_timing([
                {"start": 0, "end": 4, "text": "첫 문장"},
                {"start": 3, "end": 6, "text": "다음 문장"},
            ])

    def test_caption_balancing_preserves_words_without_a_tiny_last_line(self):
        text = "문손잡이 아래에는 반짝이는 잠금쇠가 달려 있었다."
        result = balanced_caption(text)
        self.assertEqual(normalized_text(text), normalized_text(result))
        self.assertEqual(len(result.splitlines()), 2)
        self.assertGreater(len(result.splitlines()[1]), 6)

    def test_character_comparison_counts_real_changes(self):
        self.assertEqual(edit_distance("미자", "미자"), 0)
        self.assertEqual(edit_distance("미자", "미사"), 1)
        self.assertEqual(edit_distance("열쇠", "새열쇠"), 1)
        self.assertEqual(edit_distance("", "한글"), 2)

    def test_sentence_subdivision_covers_text_and_stays_within_boundary(self):
        text = "어머니와 아들은 같은 일을 서로 다른 마음으로 기억하고 있었습니다. 오늘은 끝까지 들어 보기로 했습니다."
        captions = captions_from_boundaries(
            [{"type": "SentenceBoundary", "offset": 10_000_000, "duration": 150_000_000, "text": text}],
            duration=18,
        )
        self.assertGreater(len(captions), 1)
        self.assertEqual(captions[0]["start"], 1)
        self.assertEqual(captions[-1]["end"], 16)
        self.assertEqual(normalized_text("".join(c["text"] for c in captions)), normalized_text(text))
        self.assertTrue(all(len(line) <= 24 for c in captions for line in c["text"].splitlines()))
        self.assertTrue(all(len(c["text"].splitlines()) <= 2 for c in captions))
        for first, second in zip(captions, captions[1:]):
            self.assertAlmostEqual(first["end"], second["start"], places=3)

    def test_absent_or_invalid_real_boundaries_are_not_faked(self):
        with self.assertRaises(PipelineError):
            captions_from_boundaries([], duration=10)
        with self.assertRaises(PipelineError):
            captions_from_boundaries(
                [{"type": "SentenceBoundary", "offset": 10_000_000, "duration": 0, "text": "안녕"}],
                duration=10,
            )

    def test_output_formats_and_escape(self):
        self.assertEqual(timestamp(3661.25), "01:01:01,250")
        self.assertEqual(timestamp(3661.25, ass=True), "1:01:01.25")
        self.assertNotIn("{", ass_text(r"{\pos(1,2)}"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cues = [{"start": 0.1, "end": 2, "text": "첫째 줄\n둘째 줄"}]
            write_ass(root / "test.ass", cues)
            write_srt(root / "test.srt", cues)
            self.assertIn(r"첫째 줄\N둘째 줄", (root / "test.ass").read_text(encoding="utf-8-sig"))
            self.assertIn("00:00:00,100 --> 00:00:02,000", (root / "test.srt").read_text(encoding="utf-8"))


class EpisodeTests(unittest.TestCase):
    def test_original_episode_is_complete_and_has_adult_perspectives(self):
        episode = load_episode(Path(__file__).resolve().parents[1] / "episodes" / "changed-lock.json")
        self.assertEqual(len(episode["scenes"]), 14)
        self.assertEqual({scene["pov"] for scene in episode["scenes"]}, {"mother", "son"})
        count = sum(len(scene["narration"]) for scene in episode["scenes"])
        self.assertGreater(count, 7000)
        self.assertTrue(all(character["age"] >= 18 for character in episode["characters"]))
        self.assertIn("창작", episode["fiction_notice"])

    def test_unsafe_scene_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.json"
            path.write_text(json.dumps({
                "id": "test", "title": "Test", "fiction_notice": "Fiction",
                "scenes": [{"id": "../escape", "title": "x", "narration": "x", "pov": "mother", "visual": "door"}],
            }), encoding="utf-8")
            with self.assertRaises(PipelineError):
                load_episode(path)


if __name__ == "__main__":
    unittest.main()
