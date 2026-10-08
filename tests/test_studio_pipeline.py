from __future__ import annotations

import json
import unittest
from pathlib import Path

from story_pipeline.models import PipelineError
from story_pipeline.studio.pipeline import MediaOps, Production
from test_studio_team import FakeTeam, StudioTestCase


class FakeMedia:
    def __init__(self, durations=(1500.0,), technical=True, art_passed=True):
        self.durations = list(durations)
        self.technical = technical
        self.art_passed = art_passed
        self.synth_calls = 0
        self.reviews = []

    def ops(self) -> MediaOps:
        def synthesize(episode, directory):
            self.synth_calls += 1
            return [{"scene": s["id"], "duration_seconds": 1.0, "narration_characters": len(s["narration"]),
                     "captions": []} for s in episode["scenes"]]

        def measure(speech):
            total = self.durations[min(self.synth_calls - 1, len(self.durations) - 1)]
            action = "none" if 1200 <= total <= 1800 else ("lengthen" if total < 1200 else "shorten")
            return {"episode_seconds": total, "within_target": action == "none", "action": action,
                    "suggested_character_range": [1900, 2500]}

        def art_loop(episode, art, refs, directory, review):
            first = episode["scenes"][0]
            self.reviews.append(review(b"\x89PNG fake", {**first["shots"][0], "scene_id": first["id"]}))
            return {"passed": self.art_passed, "failed_shots": [] if self.art_passed else ["01-01"]}

        def timeline(episode, speech):
            return {"scene_starts": [i * 300.0 for i in range(len(episode["scenes"]))], "captions": [], "shots": []}

        return MediaOps(
            synthesize=synthesize, measure=measure,
            references=lambda episode, art, directory: {c["id"]: directory / f"{c['id']}.png" for c in episode["characters"]},
            art_loop=art_loop,
            mix=lambda episode, speech, scenes, out, music_dir, sfx_dir: {"file": str(out / "mix.wav"),
                                                                          "music": [{"scene": "01", "file": "a.mp3", "moods": ["tender"]}],
                                                                          "attributions": ["Artist - Song (CC BY)"]},
            render=lambda episode, speech, audio, frames, out: {"video": str(out / "episode.mp4"), "duration_seconds": 1500.0,
                                                                "evaluation": {"technical_pass": self.technical}},
            timeline=timeline,
            thumbnails=lambda image, texts, directory: [{"file": str(directory / "thumb-01.jpg"), "text": texts[0]}],
            shorts=lambda episode, packaging, **kw: [],
            captions=lambda out, captions: {"srt": str(out / "episode.ko.srt"), "vtt": str(out / "episode.ko.vtt")},
            metadata=lambda episode, packaging, chapters, duration, attributions: {
                "title": packaging["title"], "description": "d", "tags": packaging["tags"], "attributions": attributions},
        )


def team_with_extras() -> FakeTeam:
    team = FakeTeam()
    base = team.handlers

    def handlers():
        result = base()
        result["art-critic"] = lambda p: {"score": 8.5, "issues": [], "regenerate": False, "revised_prompt": ""}
        result["retrospective"] = lambda p: {"lessons": [{"text": "첫 장면에 갈등을 바로 보여라", "roles": ["scene-writer"],
                                                          "confidence": "low", "evidence": "engagement 6→8"}],
                                              "retire_lesson_ids": []}
        return result
    team.handlers = handlers
    return team


class ProductionTests(StudioTestCase):
    def production(self, media: FakeMedia, uploads: list | None = None):
        studio, transport = self.studio(team_with_extras())
        uploader = (lambda package: uploads.append(package) or {"status": "uploaded", "video_id": "abc"}) \
            if uploads is not None else None
        return Production(studio, media.ops(), self.root / "music", self.root / "sfx", uploader), transport

    def test_approved_episode_is_uploaded_privately_with_critic_reviewed_art(self):
        uploads = []
        production, transport = self.production(FakeMedia(), uploads)
        result = production.produce()
        self.assertTrue(result["verdict"]["approved"])
        self.assertEqual(result["upload"]["status"], "uploaded")
        self.assertEqual(len(uploads), 1)
        self.assertIn("Artist - Song (CC BY)", uploads[0]["metadata"]["attributions"])
        self.assertIn("art-critic", [agent for agent, _ in transport.calls])
        saved = json.loads((production.run_dir / "production.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["upload"]["video_id"], "abc")

    def test_failed_technical_gate_blocks_upload(self):
        uploads = []
        production, _ = self.production(FakeMedia(technical=False), uploads)
        result = production.produce()
        self.assertFalse(result["verdict"]["approved"])
        self.assertEqual(uploads, [])
        self.assertIsNone(result["upload"])

    def test_failed_art_loop_blocks_upload(self):
        uploads = []
        production, _ = self.production(FakeMedia(art_passed=False), uploads)
        self.assertFalse(production.produce()["verdict"]["approved"])
        self.assertEqual(uploads, [])

    def test_measured_speech_outside_target_loops_back_to_writers_once(self):
        media = FakeMedia(durations=(1000.0, 1500.0))
        production, transport = self.production(media)
        production.produce()
        self.assertEqual(media.synth_calls, 2)
        target = json.loads((production.run_dir / "stages" / "length-target.json").read_text(encoding="utf-8"))
        self.assertEqual(target, [1900, 2500])
        self.assertGreaterEqual([a for a, _ in transport.calls].count("director"), 8)

    def test_speech_still_outside_target_stops(self):
        production, _ = self.production(FakeMedia(durations=(1000.0, 1000.0)))
        with self.assertRaisesRegex(PipelineError, "outside 20-30 minutes"):
            production.produce()

    def test_retrospective_adds_lessons_to_playbook(self):
        production, _ = self.production(FakeMedia())
        production.produce()
        summary = production.retrospective()
        self.assertEqual(summary["playbook_version"], 1)
        self.assertEqual([l["text"] for l in production.studio.playbook.active("scene-writer")],
                         ["첫 장면에 갈등을 바로 보여라"])

    def test_rerun_after_upload_never_uploads_twice(self):
        uploads = []
        production, _ = self.production(FakeMedia(), uploads)
        first = production.produce()
        again, transport = self.production(FakeMedia(), uploads)
        self.assertEqual(again.produce()["upload"], first["upload"])
        self.assertEqual(len(uploads), 1)
        self.assertEqual(transport.calls, [])

    def test_interrupted_upload_requires_manual_check(self):
        def crash(package):
            raise RuntimeError("network died mid-upload")
        studio, _ = self.studio(team_with_extras())
        production = Production(studio, FakeMedia().ops(), self.root / "m", self.root / "s", crash)
        with self.assertRaises(RuntimeError):
            production.produce()
        retry = Production(studio, FakeMedia().ops(), self.root / "m", self.root / "s", lambda p: {"status": "uploaded"})
        with self.assertRaisesRegex(PipelineError, "interrupted"):
            retry.produce()

    def test_shorts_receive_frame_paths_for_every_shot(self):
        seen = {}
        media = FakeMedia()
        ops = media.ops()
        ops.timeline = lambda episode, speech: {"scene_starts": [i * 300.0 for i in range(len(episode["scenes"]))],
                                                "captions": [], "shots": [{"scene": "01", "id": "02"}]}
        ops.shorts = lambda episode, packaging, **kw: seen.update(kw) or []
        studio, _ = self.studio(team_with_extras())
        Production(studio, ops, self.root / "m", self.root / "s").produce()
        self.assertTrue(seen["shot_timeline"][0]["image"].endswith(str(Path("frames") / "01-shot-02.png")))


if __name__ == "__main__":
    unittest.main()
