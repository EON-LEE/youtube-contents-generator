from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from uuid import uuid4

from story_pipeline.models import PipelineError
from story_pipeline.studio import render_v3
from studio_fixtures import make_episode, make_frames, make_libraries, make_speech

MEDIA = all(importlib.util.find_spec(name) for name in ("PIL", "imageio_ffmpeg"))


def font_available() -> bool:
    from story_pipeline.fonts import korean_font
    try:
        korean_font()
    except PipelineError:
        return False
    return True


class TimelineTests(unittest.TestCase):
    def speech(self, scene):
        return {"duration_seconds": 4.0, "captions": [
            {"start": 0.1, "end": 1.9, "text": scene["narration"].split(". ")[0] + "."},
            {"start": 2.1, "end": 3.9, "text": scene["narration"].split(". ", 1)[1]},
        ]}

    def test_v3_timeline_uses_anchor_cue_starts(self):
        scene = make_episode()["scenes"][0]
        timeline = render_v3.compile_shot_timeline_v3(scene, self.speech(scene), {"minji", "junho"})
        self.assertEqual(timeline[0]["start"], 0.0)
        self.assertAlmostEqual(timeline[1]["start"], 50 / 24)
        self.assertAlmostEqual(timeline[-1]["end"], 4.375, places=3)
        self.assertEqual(timeline[0]["scene"], "01")

    def test_unknown_character_rejected_by_v3_validation(self):
        scene = make_episode()["scenes"][0]
        with self.assertRaises(PipelineError):
            render_v3.compile_shot_timeline_v3(scene, self.speech(scene), {"junho"})

    def test_anchor_end_time(self):
        scene = make_episode()["scenes"][0]
        captions = self.speech(scene)["captions"]
        self.assertAlmostEqual(render_v3.anchor_seconds(scene["narration"], captions, "들어왔다", end=True), 3.9)

    def test_global_timeline_keeps_scene_order_and_frame_grid(self):
        episode = make_episode()
        speech = [{**self.speech(scene), "scene": scene["id"]} for scene in episode["scenes"]]
        plan = render_v3.episode_timeline(episode, speech)
        for actual, expected in zip(plan["scene_starts"], [0.0, 4.35, 8.7, 13.05]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(sum(shot["global_frames"] for shot in plan["shots"]), round(17.4 * 24))
        self.assertEqual(plan["captions"][2]["scene"], "02")


@unittest.skipUnless(MEDIA, "Media extra is needed for the render test.")
class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not font_available():
            raise unittest.SkipTest("Korean font is required for burned-in subtitles.")
        from story_pipeline.studio.audio_mix import mix_episode
        cls.root = Path(__file__).parent / f".render-{uuid4().hex}"
        cls.episode = make_episode()
        cls.speech = make_speech(cls.root, cls.episode)
        music, sfx = make_libraries(cls.root)
        cls.mix = mix_episode(cls.episode, cls.speech, cls.root / "scenes", cls.root / "audio",
                              music_dir=music, sfx_dir=sfx)
        cls.frames = make_frames(cls.root / "frames", cls.episode)
        cls.result = render_v3.render_episode_v3(
            cls.episode, cls.speech, Path(cls.mix["file"]), cls.frames, cls.root / "out",
            resolution=(1280, 720), preset="ultrafast", duration_range=(10, 60))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_technical_checks_pass(self):
        evaluation = self.result["evaluation"]
        self.assertTrue(evaluation["technical_pass"], evaluation)
        self.assertEqual((evaluation["video_width"], evaluation["video_height"]), (1280, 720))
        saved = json.loads((self.root / "out" / "evaluation.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["technical_pass"])

    def test_chapters_faststart_and_sidecars(self):
        from story_pipeline.media import ffmpeg_executable
        video = self.root / "out" / "episode.mp4"
        probe = subprocess.run([ffmpeg_executable(), "-hide_banner", "-i", str(video)], capture_output=True,
                               text=True, encoding="utf-8", errors="replace").stderr
        self.assertEqual(probe.count("Chapter #"), 4)
        data = video.read_bytes()
        self.assertLess(data.find(b"moov"), data.find(b"mdat"))
        timeline = json.loads((self.root / "out" / "shot-timeline.json").read_text(encoding="utf-8"))
        self.assertEqual(len(timeline), 8)
        self.assertTrue(Path(timeline[0]["image"]).is_file())
        self.assertTrue((self.root / "out" / "episode.ko.srt").is_file())

    def test_duration_outside_target_fails_gate_without_raising(self):
        from story_pipeline.media import ffmpeg_executable
        captions = json.loads((self.root / "out" / "captions.json").read_text(encoding="utf-8"))
        check = self.root / "check"
        check.mkdir(exist_ok=True)
        result = render_v3.technical_checks(ffmpeg_executable(), self.root / "out" / "episode.mp4", captions,
                                            check, resolution=(1280, 720))
        self.assertFalse(result["target_duration_pass"])
        self.assertFalse(result["technical_pass"])

    def test_rejects_unsupported_resolution_and_mismatched_audio(self):
        with self.assertRaises(PipelineError):
            render_v3.render_episode_v3(self.episode, self.speech, Path(self.mix["file"]), self.frames,
                                        self.root / "bad", resolution=(640, 360))
        short = self.speech[:-1] + [{**self.speech[-1], "duration_seconds": self.speech[-1]["duration_seconds"] + 2}]
        with self.assertRaisesRegex(PipelineError, "remix"):
            render_v3.render_episode_v3(self.episode, short, Path(self.mix["file"]), self.frames,
                                        self.root / "bad", resolution=(1280, 720))


if __name__ == "__main__":
    unittest.main()
