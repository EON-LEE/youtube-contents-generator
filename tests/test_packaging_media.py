from __future__ import annotations

import importlib.util
import shutil
import subprocess
import unittest
from pathlib import Path
from uuid import uuid4

from story_pipeline.models import PipelineError
from story_pipeline.studio import packaging_media as pkg
from studio_fixtures import make_episode, write_wav

MEDIA = all(importlib.util.find_spec(name) for name in ("PIL", "imageio_ffmpeg"))


def font_available() -> bool:
    from story_pipeline.fonts import korean_font
    try:
        korean_font(bold=True)
    except PipelineError:
        return False
    return True


def long_captions(scene_offset: float = 100.0):
    # Scene 01: "민지는 문을 열었다." 0-10 s, "바람이 차갑게 들어왔다." 10-25 s (scene-local).
    return [
        {"scene": "01", "start": scene_offset + 0.0, "end": scene_offset + 10.0, "text": "민지는 문을 열었다."},
        {"scene": "01", "start": scene_offset + 10.0, "end": scene_offset + 25.0, "text": "바람이 차갑게 들어왔다."},
    ]


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.episode = make_episode()
        self.packaging = {"title": "엄마의 겨울 편지", "description": "따뜻한 가족 오디오드라마", "tags": ["오디오드라마", "가족 이야기"]}
        self.chapters = [("시작", 0.0), ("편지", 300.0), ("식탁", 700.0)]

    def test_description_contains_chapters_disclosure_and_attributions(self):
        meta = pkg.build_metadata(self.episode, self.packaging, self.chapters, 1300.0,
                                  attributions=["Calm by Tester (CC0)"])
        self.assertIn("0:00 시작\n5:00 편지\n11:40 식탁", meta["description"])
        self.assertIn(pkg.AI_DISCLOSURE, meta["description"])
        self.assertIn("Calm by Tester (CC0)", meta["description"])
        self.assertEqual(meta["tags_characters"], len("오디오드라마") + len("가족 이야기") + 2 + 1)
        self.assertTrue(meta["contains_synthetic_media"])

    def test_chapter_rules(self):
        for chapters in ([("a", 0.0), ("b", 100.0)], [("a", 5.0), ("b", 100.0), ("c", 200.0)],
                         [("a", 0.0), ("b", 5.0), ("c", 200.0)]):
            with self.subTest(chapters=chapters), self.assertRaises(PipelineError):
                pkg.build_metadata(self.episode, self.packaging, chapters, 1300.0)
        with self.assertRaisesRegex(PipelineError, "10 seconds"):
            pkg.build_metadata(self.episode, self.packaging, [("a", 0.0), ("b", 100.0), ("c", 1295.0)], 1300.0)

    def test_youtube_limits(self):
        with self.assertRaisesRegex(PipelineError, "title"):
            pkg.build_metadata(self.episode, {**self.packaging, "title": "가" * 101}, self.chapters, 1300.0)
        with self.assertRaisesRegex(PipelineError, "5000"):
            pkg.build_metadata(self.episode, {**self.packaging, "description": "가" * 1700}, self.chapters, 1300.0)
        with self.assertRaisesRegex(PipelineError, "500"):
            pkg.build_metadata(self.episode, {**self.packaging, "tags": ["태그" * 10] * 30}, self.chapters, 1300.0)
        with self.assertRaisesRegex(PipelineError, "angle"):
            pkg.build_metadata(self.episode, {**self.packaging, "description": "<b>"}, self.chapters, 1300.0)

    def test_hour_long_stamp(self):
        self.assertEqual(pkg.chapter_stamp(3725.9), "1:02:05")

    def test_short_window_bounds(self):
        starts = {"01": 100.0}
        short = {"scene_id": "01", "start_anchor": "민지는 문을", "end_anchor": "들어왔다", "hook": "문이 열렸다"}
        self.assertEqual(pkg.short_window(self.episode, short, long_captions(), starts), (100.0, 125.25))
        with self.assertRaisesRegex(PipelineError, "15-59"):
            pkg.short_window(self.episode, {**short, "end_anchor": "문을 열었다"}, long_captions(), starts)
        with self.assertRaises(PipelineError):
            pkg.short_window(self.episode, {**short, "scene_id": "09"}, long_captions(), starts)


class CaptionFileTests(unittest.TestCase):
    def test_srt_and_vtt(self):
        root = Path(__file__).parent / f".captions-{uuid4().hex}"
        root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        files = pkg.write_caption_files(root, [{"start": 1.5, "end": 3.25, "text": "첫 줄\n둘째 줄"}])
        vtt = Path(files["vtt"]).read_text(encoding="utf-8")
        self.assertTrue(vtt.startswith("WEBVTT\n\n1\n00:00:01.500 --> 00:00:03.250\n첫 줄\n둘째 줄"))
        self.assertIn("00:00:01,500 --> 00:00:03,250", Path(files["srt"]).read_text(encoding="utf-8"))


@unittest.skipUnless(MEDIA, "Media extra is needed for packaging media tests.")
class PackagingMediaTests(unittest.TestCase):
    def setUp(self):
        if not font_available():
            self.skipTest("Korean font is required.")
        from PIL import Image
        self.root = Path(__file__).parent / f".packaging-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.image = self.root / "key.png"
        Image.new("RGB", (1920, 1080), "#6a8caf").save(self.image)
        self.second = self.root / "second.png"
        Image.new("RGB", (1920, 1080), "#af6a6a").save(self.second)

    def test_thumbnail_candidates_fit_and_are_small(self):
        from PIL import Image
        results = pkg.render_thumbnail_candidates(
            self.image, ["엄마가 숨긴 편지", "그날 밤 우리는 결국 서로에게 아무 말도 하지 못했다"], self.root / "thumbs")
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertLessEqual(len(result["lines"]), 2)
            self.assertLess(result["bytes"], pkg.THUMBNAIL_MAX_BYTES)
            with Image.open(result["file"]) as image:
                self.assertEqual((image.format, image.size), ("JPEG", (1280, 720)))
        self.assertGreater(results[0]["font_size"], results[1]["font_size"])
        with self.assertRaisesRegex(PipelineError, "two lines"):
            pkg.render_thumbnail(self.image, "아주 긴 문장 " * 30, self.root / "x.jpg")

    def test_vertical_short_render(self):
        from story_pipeline.media import ffmpeg_executable, probe_duration
        source = write_wav(self.root / "episode.wav", 130.0, rate=48000)
        timeline = [
            {"scene": "01", "id": "01", "global_start": 100.0, "global_end": 110.0, "image": str(self.image)},
            {"scene": "01", "id": "02", "global_start": 110.0, "global_end": 126.0, "image": str(self.second)},
        ]
        short = {"scene_id": "01", "start_anchor": "민지는 문을", "end_anchor": "들어왔다", "hook": "그 문 뒤에 누가 있었을까"}
        results = pkg.render_shorts(make_episode(), {"shorts": [short]}, captions=long_captions(),
                                    shot_timeline=timeline, starts={"01": 100.0}, source_media=source,
                                    directory=self.root / "shorts")
        video = Path(results[0]["file"])
        ffmpeg = ffmpeg_executable()
        self.assertAlmostEqual(probe_duration(ffmpeg, video), 25.25, delta=0.15)
        probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace").stderr
        self.assertIn("1080x1920", probe)
        self.assertIn("Audio: aac", probe)
        self.assertFalse((self.root / "shorts" / "short-01.work").exists())


if __name__ == "__main__":
    unittest.main()
