from __future__ import annotations

import importlib.util
import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from story_pipeline.models import PipelineError
from story_pipeline.studio import audio_mix
from studio_fixtures import make_episode, make_libraries, make_speech

MEDIA = all(importlib.util.find_spec(name) for name in ("PIL", "imageio_ffmpeg"))


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f".audio-{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.music, self.sfx = make_libraries(self.root)

    def rewrite(self, mutate):
        index = self.music / "library.json"
        data = json.loads(index.read_text(encoding="utf-8"))
        mutate(data["entries"])
        index.write_text(json.dumps(data), encoding="utf-8")

    def test_valid_libraries_load(self):
        self.assertEqual(len(audio_mix.load_library(self.music, "music")), 2)
        self.assertEqual(audio_mix.load_library(self.sfx, "sfx")[0]["tags"], ["door"])

    def test_entries_without_commercial_license_are_rejected(self):
        cases = [
            lambda e: e[0]["license"].update(commercial_use=False),
            lambda e: e[0]["license"].pop("license_id"),
            lambda e: e[0].pop("license"),
            lambda e: e[0]["license"].update(proof_url_or_file="missing-proof.txt"),
            lambda e: e[1].update(file="../outside.wav"),
            lambda e: e[1].update(moods=[]),
        ]
        original = (self.music / "library.json").read_text(encoding="utf-8")
        for mutate in cases:
            (self.music / "library.json").write_text(original, encoding="utf-8")
            self.rewrite(mutate)
            with self.subTest(mutate=mutate), self.assertRaisesRegex(PipelineError, "Rejected music library"):
                audio_mix.load_library(self.music, "music")

    def test_music_choice_is_deterministic_and_mood_matched(self):
        library = audio_mix.load_library(self.music, "music")
        episode = make_episode()
        first = audio_mix.choose_music(episode, library)
        self.assertEqual(first, audio_mix.choose_music(episode, library))
        for scene in episode["scenes"]:
            self.assertIn(scene["mood"], first[scene["id"]]["moods"])
        episode["scenes"][1]["mood"] = "tense"
        with self.assertRaisesRegex(PipelineError, "no licensed music for mood 'tense'"):
            audio_mix.choose_music(episode, library)

    def test_same_mood_consecutive_scenes_share_track(self):
        episode = make_episode()
        for scene in episode["scenes"]:
            scene["mood"] = "hopeful"
        chosen = audio_mix.choose_music(episode, audio_mix.load_library(self.music, "music"))
        self.assertEqual(len({entry["file"] for entry in chosen.values()}), 1)

    def test_unknown_sfx_tag_lists_available_tags(self):
        episode = make_episode()
        episode["scenes"][0]["shots"][1]["sfx"] = ["thunder"]
        timelines = {s["id"]: [{**shot, "start": 0.0} for shot in s["shots"]] for s in episode["scenes"]}
        with self.assertRaisesRegex(PipelineError, r"\['thunder'\].*\['door'\]"):
            audio_mix.place_sfx(episode, timelines, {s["id"]: 0.0 for s in episode["scenes"]},
                                audio_mix.load_library(self.sfx, "sfx"))


@unittest.skipUnless(MEDIA, "Media extra is needed for the FFmpeg mix test.")
class MixTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f".mix-{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def test_mix_aligns_duration_normalizes_and_caches(self):
        music, sfx = make_libraries(self.root)
        episode = make_episode()
        speech = make_speech(self.root, episode)
        record = audio_mix.mix_episode(episode, speech, self.root / "scenes", self.root / "audio",
                                       music_dir=music, sfx_dir=sfx)
        expected = sum(r["duration_seconds"] + 0.35 for r in speech)
        self.assertAlmostEqual(record["duration_seconds"], expected, delta=0.05)
        self.assertAlmostEqual(record["loudness"]["output_i"], -14, delta=1.5)
        self.assertLessEqual(record["loudness"]["output_tp"], -1.0)
        self.assertEqual([s["tag"] for s in record["sfx"]], ["door"])
        self.assertEqual(record["sfx"][0]["time"], 0.0)
        self.assertIn("Calm by Tester (CC0)", record["attributions"])
        self.assertIn("Door by Tester", record["attributions"])
        self.assertEqual(list(record["scene_starts"]), ["01", "02", "03", "04"])
        mtime = Path(record["file"]).stat().st_mtime_ns
        again = audio_mix.mix_episode(episode, speech, self.root / "scenes", self.root / "audio",
                                      music_dir=music, sfx_dir=sfx)
        self.assertEqual(again["sha256"], record["sha256"])
        self.assertEqual(Path(record["file"]).stat().st_mtime_ns, mtime)


if __name__ == "__main__":
    unittest.main()
