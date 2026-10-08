from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from story_pipeline.models import PipelineError
from story_pipeline.studio import images
from story_pipeline.studio.config import Loops
from story_pipeline.studio.ledger import CostLedger
from studio_fixtures import make_config, make_episode

PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
if PIL_AVAILABLE:
    from PIL import Image
    from studio_fixtures import png_bytes

ART = {
    "style_guide": "Warm watercolor storybook style.",
    "negative_guidance": "photorealism",
    "character_sheets": [{"character_id": "minji", "reference_prompt": "front view"},
                         {"character_id": "junho", "reference_prompt": "three-quarter view"}],
}


class FakeImageClient:
    def __init__(self, refuse_words=()):
        self.calls = []
        self.refuse_words = refuse_words

    def _image(self, prompt):
        if any(word in prompt for word in self.refuse_words):
            raise images.ContentFilterRefusal("content_filter: blocked")
        return png_bytes("#%06x" % (len(self.calls) * 997 % 0xFFFFFF), (150, 100))

    def generate(self, prompt, size):
        self.calls.append(("generate", prompt, 0, size))
        return self._image(prompt)

    def edit(self, prompt, reference_images, size):
        self.calls.append(("edit", prompt, len(reference_images), size))
        return self._image(prompt)


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is not installed")
class ImageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f".images-{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.config = make_config()
        self.ledger = CostLedger(self.root / "ledger.json", Decimal("10"))
        self.episode = make_episode()

    def references(self, client):
        return images.generate_character_references(self.episode, ART, self.root, config=self.config,
                                                    ledger=self.ledger, client=client)

    def test_character_references_are_cached_and_billed(self):
        client = FakeImageClient()
        refs = self.references(client)
        self.assertEqual(set(refs), {"minji", "junho"})
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call[0] == "generate" and call[3] == "1536x1024" for call in client.calls))
        self.assertIn("짧은 회색 머리", client.calls[1][1])
        self.references(client)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([e["status"] for e in self.ledger.entries], ["settled", "settled"])
        self.assertEqual(Decimal(self.ledger.entries[0]["actual_usd"]), Decimal("0.07"))

    def test_sheets_must_match_cast(self):
        art = {**ART, "character_sheets": ART["character_sheets"][:1]}
        with self.assertRaises(PipelineError):
            images.generate_character_references(self.episode, art, self.root, config=self.config,
                                                 ledger=self.ledger, client=FakeImageClient())

    def test_shot_prompt_uses_edit_with_references_and_generate_without(self):
        client = FakeImageClient()
        refs = self.references(client)
        scene = self.episode["scenes"][1]
        first = images.generate_shot_image(self.episode, ART, scene, scene["shots"][0], refs, self.root,
                                           config=self.config, ledger=self.ledger, client=client)
        self.assertEqual(client.calls[-1][0], "edit")
        self.assertEqual(client.calls[-1][2], 2)
        for fragment in ("Warm watercolor", "작은 아파트 거실", "현관 앞", "남색 점퍼", "Emotion: concerned"):
            self.assertIn(fragment, first["prompt"])
        images.generate_shot_image(self.episode, ART, scene, scene["shots"][1], refs, self.root,
                                   config=self.config, ledger=self.ledger, client=client)
        self.assertEqual(client.calls[-1][0], "generate")

    def test_content_filter_retries_with_softened_prompt_then_fails(self):
        client = FakeImageClient(refuse_words=("현관",))
        refs = self.references(FakeImageClient())
        scene = self.episode["scenes"][0]
        softened = images.generate_shot_image(
            self.episode, ART, scene, scene["shots"][0], refs, self.root, config=self.config, ledger=self.ledger,
            client=client, soften=lambda prompt, shot, error: prompt.replace("현관", "문"))
        self.assertEqual(len(softened["content_filter_refusals"]), 1)
        self.assertIn("failed", [e["status"] for e in self.ledger.entries])
        config = make_config(loops=Loops(art_retries_per_shot=1))
        with self.assertRaisesRegex(PipelineError, "content filter refused 2"):
            images.generate_shot_image(
                self.episode, ART, scene, scene["shots"][0], refs, self.root / "x", config=config,
                ledger=self.ledger, client=client, soften=lambda prompt, shot, error: prompt + " gentle")

    def test_art_loop_regenerates_only_failed_shots_and_writes_frames(self):
        client = FakeImageClient()
        refs = self.references(client)
        seen = []

        def review(data, shot):
            key = f"{shot['scene_id']}/{shot['id']}"
            seen.append(key)
            bad = key == "02/01" and seen.count(key) == 1
            return {"score": 4 if bad else 8.5, "issues": ["hands"] if bad else [], "regenerate": bad,
                    "revised_prompt": "현관 앞, 손을 자연스럽게" if bad else ""}

        before = len(client.calls)
        report = images.art_loop(self.episode, ART, refs, self.root, config=self.config, ledger=self.ledger,
                                 client=client, review=review)
        self.assertTrue(report["passed"])
        self.assertEqual(len(client.calls) - before, 9)
        self.assertEqual(seen.count("02/01"), 2)
        self.assertIn("손을 자연스럽게", client.calls[-1][1])
        self.assertEqual(report["shots"]["02/01"]["chosen_attempt"], 2)
        with Image.open(report["shots"]["01/01"]["frame"]) as frame:
            self.assertEqual(frame.size, (1920, 1080))
        saved = json.loads((self.root / "art-report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["shots"]["02/01"]["attempts"]), 2)

    def test_art_loop_reports_failure_after_cap(self):
        client = FakeImageClient()
        refs = self.references(client)
        report = images.art_loop(
            self.episode, ART, refs, self.root, config=self.config, ledger=self.ledger, client=client,
            review=lambda data, shot: {"score": 3 if shot["scene_id"] == "03" else 9, "issues": [],
                                       "regenerate": shot["scene_id"] == "03", "revised_prompt": ""})
        self.assertFalse(report["passed"])
        self.assertEqual(sorted(report["failed_shots"]), ["03/01", "03/02"])
        self.assertEqual(len(report["shots"]["03/01"]["attempts"]), 3)


class FoundryClientTests(unittest.TestCase):
    def test_openai_client_calls_and_refusal_mapping(self):
        encoded = base64.b64encode(b"png-bytes").decode()
        calls = []

        class Images:
            def generate(self, **kwargs):
                calls.append(("generate", kwargs))
                return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

            def edit(self, **kwargs):
                calls.append(("edit", kwargs))
                error = Exception("Your request was rejected")
                error.code = "content_policy_violation"
                raise error

        client = images.FoundryImageClient(make_config(), openai_client=SimpleNamespace(images=Images()))
        self.assertEqual(client.generate("p", "1536x1024"), b"png-bytes")
        self.assertEqual(calls[0][1]["model"], "gpt-image-1")
        with self.assertRaises(images.ContentFilterRefusal):
            client.edit("p", [b"a"], "1536x1024")
        self.assertEqual(calls[1][1]["image"][0], ("reference-0.png", b"a", "image/png"))


if __name__ == "__main__":
    unittest.main()
