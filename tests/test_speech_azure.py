from __future__ import annotations

import json
import shutil
import unittest
import xml.etree.ElementTree as ET
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from story_pipeline.media import normalized_text
from story_pipeline.models import PipelineError
from story_pipeline.studio import speech_azure as speech
from story_pipeline.studio.ledger import CostLedger
from studio_fixtures import FakeSynthesizer, make_config, make_episode


class SsmlTests(unittest.TestCase):
    def test_escapes_text_and_applies_style_and_prosody(self):
        ssml = speech.build_ssml("A & <B>", {"name": "ko-KR-SunHiNeural", "style": "sad", "rate": "+5%", "pitch": "-2%"})
        self.assertIn("A &amp; &lt;B&gt;", ssml)
        self.assertIn('<mstts:express-as style="sad">', ssml)
        self.assertIn('<prosody rate="+5%" pitch="-2%">', ssml)
        ET.fromstring(ssml)

    def test_default_and_neutral_styles_omit_express_as(self):
        for style in ("default", "Neutral"):
            ssml = speech.build_ssml("안녕", {"name": "ko-KR-InJoonNeural", "style": style, "rate": "+0%", "pitch": "+0%"})
            self.assertNotIn("express-as", ssml)

    def test_invalid_voice_or_prosody_rejected(self):
        with self.assertRaises(PipelineError):
            speech.build_ssml("x", {"name": "en-US-Jenny", "style": "default", "rate": "+0%", "pitch": "+0%"})
        with self.assertRaises(PipelineError):
            speech.build_ssml("x", {"name": "ko-KR-InJoonNeural", "style": "a\"b", "rate": "+0%", "pitch": "+0%"})

    def test_sdk_boundary_event_conversion(self):
        event = SimpleNamespace(audio_offset=1_000_000, duration=timedelta(seconds=1.5), text="문장.")
        self.assertEqual(speech.sentence_boundary(event),
                         {"type": "SentenceBoundary", "offset": 1_000_000, "duration": 15_000_000, "text": "문장."})


class SynthesisTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f".speech-{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.config = make_config()
        self.ledger = CostLedger(self.root / "ledger.json", Decimal("10"))
        self.episode = make_episode()

    def test_episode_synthesis_records_captions_costs_and_reuses_cache(self):
        fake = FakeSynthesizer()
        created = []
        factory = lambda config: created.append(1) or fake
        records = speech.synthesize_episode_azure(self.episode, self.root / "scenes", config=self.config,
                                                  ledger=self.ledger, synthesizer_factory=factory)
        self.assertEqual(len(created), 1)
        self.assertEqual(len(fake.calls), 4)
        for scene, record in zip(self.episode["scenes"], records):
            self.assertEqual(normalized_text("".join(c["text"] for c in record["captions"])),
                             normalized_text(scene["narration"]))
            self.assertTrue(record["azure_used"])
            self.assertFalse(record["simulated"])
            self.assertTrue((self.root / "scenes" / f"{scene['id']}.ass").is_file())
        entries = self.ledger.entries
        self.assertEqual([e["status"] for e in entries], ["settled"] * 4)
        expected = Decimal(len(fake.calls[0])) * Decimal(15) / Decimal(1_000_000)
        self.assertEqual(Decimal(entries[0]["actual_usd"]), expected.quantize(Decimal("0.000001"), rounding="ROUND_CEILING"))
        again = speech.synthesize_episode_azure(self.episode, self.root / "scenes", config=self.config,
                                                ledger=self.ledger, synthesizer_factory=factory)
        self.assertEqual(again, records)
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(len(self.ledger.entries), 4)

    def test_text_coverage_mismatch_is_rejected(self):
        scene = self.episode["scenes"][0]
        with self.assertRaisesRegex(PipelineError, "do not cover"):
            speech.synthesize_scene_azure(self.episode, scene, self.root, config=self.config, ledger=self.ledger,
                                          synthesizer_factory=lambda c: FakeSynthesizer(drop_last=True))

    def test_wrong_audio_format_is_rejected(self):
        with self.assertRaisesRegex(PipelineError, "24 kHz"):
            speech.synthesize_scene_azure(self.episode, self.episode["scenes"][0], self.root, config=self.config,
                                          ledger=self.ledger, synthesizer_factory=lambda c: FakeSynthesizer(sample_rate=16000))

    def test_service_failure_fails_ledger_entry_without_fallback(self):
        with self.assertRaisesRegex(PipelineError, "no fallback"):
            speech.synthesize_scene_azure(self.episode, self.episode["scenes"][0], self.root, config=self.config,
                                          ledger=self.ledger, synthesizer_factory=lambda c: FakeSynthesizer(fail=True))
        self.assertEqual(self.ledger.entries[0]["status"], "failed")
        error = json.loads((self.root / "01.speech-error.json").read_text(encoding="utf-8"))
        self.assertFalse(error["fallback_used"])

    def test_missing_endpoint_is_explicit(self):
        with self.assertRaises(PipelineError):
            speech.AzureSpeechSynthesizer(make_config(speech_endpoint=""), credential=object())


class DurationTests(unittest.TestCase):
    def records(self, seconds, count=20):
        return [{"scene": f"{i:02}", "duration_seconds": seconds, "narration_characters": round(seconds * 6)}
                for i in range(1, count + 1)]

    def test_within_target(self):
        result = speech.measure_episode_duration(self.records(70))
        self.assertTrue(result["within_target"])
        self.assertEqual(result["action"], "none")

    def test_too_short_and_too_long_give_direction(self):
        short = speech.measure_episode_duration(self.records(50))
        self.assertEqual((short["within_target"], short["action"]), (False, "lengthen"))
        self.assertGreater(short["seconds_outside_target"], 0)
        long = speech.measure_episode_duration(self.records(100))
        self.assertEqual(long["action"], "shorten")
        low, high = long["suggested_character_range"]
        self.assertLess(low, high)


if __name__ == "__main__":
    unittest.main()
