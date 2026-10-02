from __future__ import annotations

import math
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from story_pipeline.media import (
    compile_shot_timeline, improve_caption_readability, normalized_text,
    load_episode, render_clip, validate_storyboard, write_ass,
)
from story_pipeline.models import PipelineError


def example_scene():
    return {
        "id": "01", "narration": "엄마는 문을 보았다. 아들은 종이를 꺼냈다.",
        "shots": [
            {"id": "01", "anchor": "엄마는 문을", "subject": "mother", "emotion": "concerned", "reason": "첫 행동"},
            {"id": "02", "anchor": "아들은 종이를", "subject": "payslip", "emotion": "neutral", "reason": "새 증거"},
        ],
    }


class CaptionReadabilityTests(unittest.TestCase):
    def test_short_cue_merges_without_early_or_lost_text(self):
        source = [{"start": 0, "end": 2, "text": "그 편지를 가져와."},
                  {"start": 2.1, "end": 2.5, "text": "응."},
                  {"start": 2.5, "end": 6, "text": "나는 가방을 챙겼다."}]
        result = improve_caption_readability(source, duration=6)
        self.assertEqual(normalized_text("".join(x["text"] for x in source)),
                         normalized_text("".join(x["text"] for x in result)))
        self.assertEqual(len(result), 2)
        self.assertTrue(all(x["end"] - x["start"] >= 1 for x in result))
        self.assertEqual(source[1]["text"], "응.")
        self.assertEqual(improve_caption_readability(result, duration=6), result)

    def test_extend_short_cue_only_into_available_silence(self):
        source = [{"start": 0.2, "end": 0.9, "text": "응."}, {"start": 2, "end": 5, "text": "다음 문장."}]
        result = improve_caption_readability(source, duration=5)
        self.assertEqual(result[0]["start"], 0.2)
        self.assertAlmostEqual(result[0]["end"], 1.3)
        self.assertEqual(result[1]["start"], 2)

    def test_impossible_adjustment_remains_visible_warning(self):
        text = "가" * 48
        result = improve_caption_readability([
            {"start": 0, "end": 0.5, "text": "응"},
            {"start": 0.5, "end": 5, "text": text},
        ], duration=5)
        self.assertIn("readability_warning", result[0])
        self.assertEqual(result[0]["end"], 0.5)

    def test_merged_caption_never_exceeds_two_lines(self):
        source = [
            {"start": 0, "end": 2, "text": "엄마가 그때 고개를 들어 아들의 얼굴을 바라보았다."},
            {"start": 2, "end": 2.4, "text": "그래."},
        ]
        result = improve_caption_readability(source, duration=2.4)
        self.assertEqual(len(result), 1)
        self.assertTrue(all(len(line) <= 24 for line in result[0]["text"].splitlines()))


class StoryboardTests(unittest.TestCase):
    def test_revised_episode_directs_the_actual_story_actions(self):
        path = Path(__file__).resolve().parents[1] / "episodes" / "changed-lock-v2.json"
        episode = load_episode(path)
        self.assertEqual(len(episode["scenes"]), 14)
        self.assertEqual(sum(len(scene["shots"]) for scene in episode["scenes"]), 56)
        scenes = {scene["id"]: scene for scene in episode["scenes"]}
        self.assertEqual(scenes["06"]["pov"], "son")
        self.assertNotIn("mother", [shot["subject"] for shot in scenes["06"]["shots"]])
        self.assertEqual(scenes["06"]["shots"][1]["detail"], "face_down")
        self.assertEqual(scenes["09"]["shots"][1]["subject"], "payslip")
        self.assertEqual(scenes["12"]["shots"][1]["detail"], "key_handover")
        self.assertEqual(scenes["10"]["shots"][0]["detail"], "damp")
        self.assertEqual(scenes["13"]["shots"][2]["detail"], "dry")

    def test_each_scene_starts_with_its_own_narration_anchor(self):
        path = Path(__file__).resolve().parents[1] / "episodes" / "changed-lock-v2.json"
        episode = json.loads(path.read_text(encoding="utf-8"))
        for scene in episode["scenes"]:
            validate_storyboard(scene)
            self.assertTrue(normalized_text(scene["narration"]).startswith(
                normalized_text(scene["shots"][0]["anchor"])
            ))

    def test_cuts_follow_narration_anchor_not_equal_time(self):
        scene = example_scene()
        speech = {"duration_seconds": 12, "captions": [
            {"start": 0.1, "end": 4, "text": "엄마는 문을 보았다."},
            {"start": 4.7, "end": 11.8, "text": "아들은 종이를 꺼냈다."},
        ]}
        result = compile_shot_timeline(scene, speech)
        self.assertEqual(result[0]["start"], 0)
        self.assertAlmostEqual(result[1]["start"], 4.7, delta=1 / 24)
        self.assertEqual(result[0]["end"], result[1]["start"])
        self.assertEqual(sum(shot["frames"] for shot in result), math.ceil(12.35 * 24))
        self.assertEqual(result[1]["subject"], "payslip")

    def test_missing_repeated_and_out_of_order_anchors_fail(self):
        for anchor in ("없는문장입니다", "엄마는 문을"):
            scene = example_scene()
            scene["shots"][1]["anchor"] = anchor
            with self.subTest(anchor=anchor), self.assertRaises(PipelineError):
                validate_storyboard(scene)
        scene = example_scene()
        scene["narration"] += " 아들은 종이를 꺼냈다."
        with self.assertRaisesRegex(PipelineError, "ambiguous"):
            validate_storyboard(scene)

    def test_different_speech_text_cannot_align(self):
        with self.assertRaisesRegex(PipelineError, "different speech"):
            compile_shot_timeline(example_scene(), {
                "duration_seconds": 12, "captions": [{"start": 0, "end": 12, "text": "다른 내용입니다."}],
            })

    def test_invalid_subject_rejected(self):
        scene = example_scene()
        scene["shots"][1]["subject"] = "unrelated_picture"
        with self.assertRaisesRegex(PipelineError, "subject"):
            validate_storyboard(scene)

    def test_hands_require_explicit_story_prop_not_a_generic_envelope(self):
        scene = example_scene()
        scene["shots"][1]["subject"] = "hands"
        with self.assertRaisesRegex(PipelineError, "prop/action"):
            validate_storyboard(scene)
        scene["shots"][1]["detail"] = "key_handover"
        validate_storyboard(scene)


try:
    from PIL import Image
    import imageio_ffmpeg
    MEDIA_AVAILABLE = True
except ImportError:
    MEDIA_AVAILABLE = False


@unittest.skipUnless(MEDIA_AVAILABLE, "Media extra is needed for real rendering integration test.")
class MultiShotRenderTests(unittest.TestCase):
    def test_encoded_frames_change_at_the_directed_cut_and_cache_is_reused(self):
        from story_pipeline.media import execute, ffmpeg_executable
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fonts").mkdir()
            for identifier, color in (("01", "red"), ("02", "blue")):
                Image.new("RGB", (1280, 720), color).save(root / f"01-shot-{identifier}.png")
            with wave.open(str(root / "01.wav"), "wb") as audio:
                audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                audio.writeframes(b"".join(
                    struct.pack("<h", round(1000 * math.sin(2 * math.pi * 220 * n / 24000)))
                    for n in range(round(3.65 * 24000))
                ))
            write_ass(root / "01.ass", [{"start": 0, "end": 3.5, "text": "Test"}])
            timeline = [
                {"id": "01", "frames": 48, "start": 0, "end": 2, "duration_seconds": 2},
                {"id": "02", "frames": 48, "start": 2, "end": 4, "duration_seconds": 2},
            ]
            ffmpeg = ffmpeg_executable()
            duration = render_clip(
                ffmpeg, {"id": "01"}, {"duration_seconds": 3.65, "fingerprint": "test"}, root, timeline
            )
            self.assertAlmostEqual(duration, 4, delta=0.1)
            for stamp, name, channel in ((0.5, "red", 0), (2.5, "blue", 2)):
                execute([ffmpeg, "-y", "-v", "error", "-ss", str(stamp), "-i", "01.mp4",
                         "-frames:v", "1", name + ".png"], cwd=root, log=root / (name + ".log"))
                with Image.open(root / (name + ".png")) as frame:
                    pixel = frame.convert("RGB").getpixel((640, 300))
                self.assertGreater(pixel[channel], 200)
                self.assertLess(pixel[2 - channel], 40)
            timestamp = (root / "01.mp4").stat().st_mtime_ns
            render_clip(ffmpeg, {"id": "01"}, {"duration_seconds": 3.65, "fingerprint": "test"}, root, timeline)
            self.assertEqual(timestamp, (root / "01.mp4").stat().st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
