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
    from story_pipeline import storyboard_art as art


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is not installed")
class StoryboardArtTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f".storyboard-{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        try:
            art._font(20)
        except RuntimeError as error:
            self.skipTest(str(error))

    def test_all_subjects_distinct_deterministic_png_and_reserved_footer(self):
        hashes = set()
        for subject in art.SUBJECTS:
            shot = {"subject": subject, "emotion": "concerned", "id": "06-02"}
            scene = {"id": "06", "title": "아들의 하루", "pov": "son", "visual": "rain"}
            with self.subTest(subject=subject):
                a, b = self.root / f"{subject}.png", self.root / "repeat.png"
                art.render_shot(scene, shot, {}, a, (960, 540))
                art.render_shot(scene, shot, {}, b, (960, 540))
                self.assertEqual(a.read_bytes(), b.read_bytes())
                with Image.open(a) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.size, (960, 540))
                    self.assertEqual(image.mode, "RGB")
                    self.assertEqual(image.crop((0, 425, 960, 540)).getextrema(),
                                     ((23, 23), (43, 43), (58, 58)))
                    # Compare art only, excluding both the caption and chapter title.
                    hashes.add(hashlib.sha256(image.crop((0, 125, 960, 420)).tobytes()).hexdigest())
        self.assertEqual(len(hashes), len(art.SUBJECTS))

    def test_invalid_subject_emotion_and_size(self):
        for shot, message in (
            ({"subject": "unknown", "emotion": "neutral"}, "subject"),
            ({"subject": "son", "emotion": "happy"}, "emotion"),
            ({"subject": "son"}, "emotion"),
        ):
            with self.subTest(shot=shot), self.assertRaisesRegex(ValueError, message):
                art.render_shot({}, shot, {}, self.root / "bad.png")
        with self.assertRaises(ValueError):
            art.render_shot({}, {"subject": "keys", "emotion": "neutral"},
                            {}, self.root / "bad.png", (0, 1080))
        self.assertFalse((self.root / "bad.png").exists())

    def test_son_reactions_and_rain_wide_never_default_to_mother(self):
        for subject, tag in (("son", "rain"), ("wide", "rain"), ("wide", "window")):
            with self.subTest(subject=subject, tag=tag), patch.object(art, "_person") as person:
                art.render_shot({"pov": "아들", "visual": tag},
                                {"subject": subject, "emotion": "concerned"},
                                {}, self.root / "son.png", (480, 270))
                person.assert_called_once()
                self.assertIs(person.call_args.kwargs["mother"], False)
                self.assertEqual(person.call_args.kwargs["expression"], "concerned")

    def test_scene_sensitive_objects_and_face_emotions(self):
        with patch.object(art, "_key") as key:
            art.render_shot({"id": "scene12"}, {"subject": "keys", "emotion": "neutral"},
                            {}, self.root / "keys.png", (480, 270))
            self.assertEqual(key.call_count, 2)
        for subject, variants in (
            ("envelope", [("10", ""), ("11", ""), ("11", "sealed")]),
            ("apron", [("13", ""), ("14", ""), ("14", "torn")]),
        ):
            images = []
            for scene_id, anchor in variants:
                output = self.root / "state.png"
                art.render_shot({"id": scene_id}, {"subject": subject, "emotion": "neutral",
                                                "anchor": anchor}, {}, output, (960, 540))
                with Image.open(output) as image:
                    images.append(image.crop((0, 130, 960, 420)).tobytes())
            self.assertNotEqual(images[0], images[1])
            self.assertEqual(images[0], images[2])
        faces = set()
        for emotion in art.EMOTIONS:
            image = Image.new("RGB", (1920, 1080))
            art._insert(ImageDraw.Draw(image), {}, {"subject": "son", "emotion": emotion})
            faces.add(image.crop((970, 350, 1320, 690)).tobytes())
        self.assertEqual(len(faces), 3)

    def test_payslip_has_no_salary_claims_or_private_story_text(self):
        image = Image.new("RGB", (1920, 1080))
        draw = ImageDraw.Draw(image)
        with patch.object(draw, "text", wraps=draw.text) as text:
            art._documents(draw, "payslip")
        self.assertEqual([call.args[1] for call in text.call_args_list],
                         ["근무 내역", "근무일", "근무일", "근무일", "근무일"])

    def test_detail_variants_are_distinct_and_deterministic_in_memory(self):
        for subject, details in art.DETAILS.items():
            hashes = set()
            for detail in details:
                with self.subTest(subject=subject, detail=detail):
                    shot = {"subject": subject, "emotion": "neutral", "detail": detail}
                    renders = []
                    for _ in range(2):
                        with patch.object(art, "_save") as save:
                            art.render_shot({"id": "12"}, shot, {}, self.root / "unused.png")
                            image = save.call_args.args[0]
                            renders.append(image.crop((0, 250, 1920, 842)).tobytes())
                            self.assertEqual(image.crop((0, 842, 1920, 1080)).getextrema(),
                                             ((23, 23), (43, 43), (58, 58)))
                    self.assertEqual(renders[0], renders[1])
                    hashes.add(hashlib.sha256(renders[0]).hexdigest())
            self.assertEqual(len(hashes), len(details), subject)

    def test_invalid_details_fail_before_rendering(self):
        for subject, detail in (("hands", "call"), ("phone", "key_handover"),
                                ("room", "unknown"), ("apron", "damp"),
                                ("son", "fitting"), ("hands", None)):
            with self.subTest(subject=subject, detail=detail), patch.object(art, "_save") as save:
                with self.assertRaisesRegex(ValueError, "Unknown shot detail"):
                    art.render_shot({}, {"subject": subject, "emotion": "neutral",
                                         "detail": detail}, {}, self.root / "invalid.png")
                save.assert_not_called()

    def test_critical_detail_semantics(self):
        draw = ImageDraw.Draw(Image.new("RGB", (1920, 1080)))
        with patch.object(art, "_key") as key, patch.object(art, "_paper") as paper, \
                patch.object(art, "_hand") as hand:
            art._insert(draw, {"id": "12"}, {"subject": "hands", "emotion": "neutral",
                                           "detail": "key_handover"})
            self.assertEqual(key.call_count, 2)
            self.assertEqual(hand.call_count, 2)
            paper.assert_not_called()
        for detail in art.DETAILS["apron"]:
            with patch.object(art, "_person") as person, patch.object(art, "_stitched_panel") as panel:
                art._insert(draw, {}, {"subject": "apron", "emotion": "neutral", "detail": detail})
                if detail == "repaired_flat":
                    person.assert_not_called()
                else:
                    self.assertIs(person.call_args.kwargs["mother"], False)
                self.assertEqual(panel.called, detail != "fitting")
        for subject in ("mother", "son"):
            with patch.object(art, "_window") as window:
                art._insert(draw, {}, {"subject": subject, "emotion": "neutral"})
                self.assertIs(window.call_args.kwargs["night"], False)
        rooms = {}
        for detail in ("damp", "dry", None):
            image = Image.new("RGB", (1920, 1080))
            shot = {"subject": "room", "emotion": "neutral"}
            if detail is not None:
                shot["detail"] = detail
            art._insert(ImageDraw.Draw(image), {"id": "13"}, shot)
            rooms[detail] = image.tobytes()
        self.assertEqual(rooms["dry"], rooms[None])
        self.assertNotEqual(rooms["dry"], rooms["damp"])


if __name__ == "__main__":
    unittest.main()
