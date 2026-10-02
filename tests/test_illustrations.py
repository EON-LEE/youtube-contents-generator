from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
if PIL_AVAILABLE:
    from PIL import Image, ImageDraw
    from story_pipeline import illustrations as art


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is not installed")
class IllustrationTests(unittest.TestCase):
    def setUp(self):
        # Keep test artifacts inside the repository, not in system temp folders.
        self.root = Path(__file__).parent / f".illustrations-{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def require_font(self):
        try:
            art._font(44)
        except RuntimeError as exc:
            self.skipTest(str(exc))

    def test_every_scene_tag_and_reserved_footer(self):
        self.require_font()
        hashes = set()
        for tag in art.TAGS:
            with self.subTest(tag=tag):
                output = self.root / "nested" / f"{tag}.png"
                art.render_scene(
                    {"id": "01", "title": "다시 찾은 나", "visual_tag": tag,
                     "viewpoint": "son" if tag == "station" else "mother"},
                    {}, output, (960, 540),
                )
                self.assertTrue(output.is_file())
                with Image.open(output) as image:
                    self.assertEqual(image.size, (960, 540))
                    self.assertEqual(image.mode, "RGB")
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.getpixel((480, 500)), (23, 43, 58))
                    self.assertEqual(image.getpixel((10, 10)), (243, 231, 207))
                hashes.add(hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(len(hashes), len(art.TAGS))

    def test_scene_and_cover_are_deterministic(self):
        self.require_font()
        for renderer, args in (
            (art.render_scene, ({"title": "문 앞에서", "visual": "door"}, {})),
            (art.render_cover, ({"title": "바뀐 문 앞에서\n다시 찾은 나"},)),
        ):
            first, second = self.root / "first.png", self.root / "second.png"
            renderer(*args, first, (960, 540))
            renderer(*args, second, (960, 540))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with Image.open(first) as image:
                self.assertEqual(image.size, (960, 540))
                self.assertEqual(image.mode, "RGB")

    def test_wrapping_preserves_complete_title_and_bounds(self):
        self.require_font()
        draw = ImageDraw.Draw(Image.new("RGB", (1920, 1080)))
        font = art._font(48, True)
        title = "어머니가 되찾은 오래된 열쇠와 새로운 하루"
        lines = art._wrap(draw, title, font, 360)
        self.assertEqual("".join(lines), title)
        for line in lines:
            bounds = draw.textbbox((0, 0), line, font=font)
            self.assertLessEqual(bounds[2] - bounds[0], 360)
        with self.assertRaisesRegex(ValueError, "without truncation"):
            art._text(draw, title * 50, (0, 0), 300, 3)

    def test_missing_korean_font_is_explicit(self):
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Korean illustration font missing"):
                art.render_cover({}, self.root / "cover.png", (960, 540))
        self.assertFalse((self.root / "cover.png").exists())

    def test_pov_alias_and_narration_remain_outside_artwork(self):
        self.require_font()
        first, second = self.root / "first.png", self.root / "second.png"
        art.render_scene({"pov": "son", "narration": "이 문장은 그리지 않습니다"},
                         {}, first, (960, 540))
        art.render_scene({"viewpoint": "son"}, {}, second, (960, 540))
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_invalid_size_is_rejected(self):
        for size in ((0, 10), (-1, 10), (10.5, 10), (10,)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                art._save(Image.new("RGB", (10, 10)), self.root / "bad.png", size)

    def test_expressions_follow_scene_tone(self):
        for tag in art.TAGS:
            expected = ("concerned" if tag in ("door", "rain", "letter", "window")
                        else "smile" if tag in ("workshop", "garden") else "neutral")
            with self.subTest(tag=tag), patch.object(art, "_person") as person:
                art._art(ImageDraw.Draw(Image.new("RGB", (1920, 1080))), tag)
                self.assertTrue(person.called)
                for call in person.call_args_list:
                    self.assertEqual(call.kwargs.get("expression", "neutral"), expected)

    def test_hair_does_not_wrap_around_chin(self):
        for mother in (True, False):
            with self.subTest(mother=mother):
                image = Image.new("RGB", (500, 650), art.CREAM)
                art._person(ImageDraw.Draw(image), 220, 70, mother=mother)
                self.assertEqual(image.getpixel((220, 180)), (220, 170, 137))
                for x in (178, 262):
                    self.assertEqual(image.getpixel((x, 180)), (243, 231, 207))

    def test_expressions_produce_distinct_faces(self):
        faces = set()
        for expression in ("neutral", "concerned", "smile"):
            image = Image.new("RGB", (500, 650), art.CREAM)
            art._person(ImageDraw.Draw(image), 220, 70, expression=expression)
            faces.add(image.crop((174, 100, 267, 180)).tobytes())
        self.assertEqual(len(faces), 3)


if __name__ == "__main__":
    unittest.main()
