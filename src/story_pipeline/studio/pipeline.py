"""One autonomous episode: team pre-production → speech → art → mix → render →
packaging → final gate → private upload → retrospective.

Media and platform operations are injected (``MediaOps``/``uploader``) so the
control flow is testable without Azure, Google or a long render.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..media import write_json
from ..models import PipelineError
from .learning import apply_retrospective, retro_inputs
from .team import Studio


@dataclass
class MediaOps:
    synthesize: Callable[..., list[dict[str, Any]]]
    measure: Callable[..., dict[str, Any]]
    references: Callable[..., dict[str, Path]]
    art_loop: Callable[..., dict[str, Any]]
    mix: Callable[..., dict[str, Any]]
    render: Callable[..., dict[str, Any]]
    timeline: Callable[..., dict[str, Any]]
    thumbnails: Callable[..., list[dict[str, Any]]]
    shorts: Callable[..., list[dict[str, Any]]]
    captions: Callable[..., dict[str, str]]
    metadata: Callable[..., dict[str, Any]]

    @classmethod
    def real(cls, config, ledger, image_client=None, synthesizer_factory=None) -> "MediaOps":
        from . import audio_mix, images, packaging_media, render_v3, speech_azure
        client = image_client or images.FoundryImageClient(config)
        return cls(
            synthesize=lambda episode, directory: speech_azure.synthesize_episode_azure(
                episode, directory, config=config, ledger=ledger, synthesizer_factory=synthesizer_factory),
            measure=speech_azure.measure_episode_duration,
            references=lambda episode, art, directory: images.generate_character_references(
                episode, art, directory, config=config, ledger=ledger, client=client),
            art_loop=lambda episode, art, refs, directory, review: images.art_loop(
                episode, art, refs, directory, config=config, ledger=ledger, client=client, review=review),
            mix=audio_mix.mix_episode, render=render_v3.render_episode_v3, timeline=render_v3.episode_timeline,
            thumbnails=packaging_media.render_thumbnail_candidates, shorts=packaging_media.render_shorts,
            captions=packaging_media.write_caption_files, metadata=packaging_media.build_metadata,
        )


@dataclass
class Production:
    studio: Studio
    media: MediaOps
    music_dir: Path
    sfx_dir: Path
    uploader: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    length_retries: int = 1
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def run_dir(self) -> Path:
        return self.studio.run_dir

    def log(self, stage: str, **detail: Any) -> None:
        self.events.append({"time": time.time(), "stage": stage, **detail})
        write_json(self.run_dir / "production-log.json", self.events)

    def review_shot(self, image: bytes, shot: dict[str, Any]) -> dict[str, Any]:
        return self.studio.gateway.call(
            "art-critic",
            "이 컷 이미지를 지시와 비교해 채점하라.\n\n```json\n" + json.dumps(shot, ensure_ascii=False) + "\n```",
            stage="art-review", images=(image,),
        )

    def _speech_within_target(self, pre: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        for attempt in range(self.length_retries + 1):
            episode = pre["episode"]
            speech = self.media.synthesize(episode, self.run_dir / "scenes")
            verdict = self.media.measure(speech)
            self.log("speech", attempt=attempt, **{k: verdict[k] for k in ("episode_seconds", "action")})
            if verdict["within_target"]:
                return pre, speech
            if attempt == self.length_retries:
                break
            # Loop back to the writing team with a measured character target, then re-direct.
            low, high = verdict["suggested_character_range"]
            self.studio.set_target_characters((low, high))
            for stage in ("write", "direction", "packaging"):
                (self.run_dir / "stages" / f"{stage}.json").unlink(missing_ok=True)
            shutil.rmtree(self.run_dir / "scenes", ignore_errors=True)
            pre = self.studio.run_preproduction()
        raise PipelineError(f"Measured speech stays outside 20-30 minutes after {self.length_retries} rewrite(s).")

    def produce(self) -> dict[str, Any]:
        previous = self.run_dir / "production.json"
        if previous.exists():
            done = json.loads(previous.read_text(encoding="utf-8"))
            status = (done.get("upload") or {}).get("status")
            if status == "uploaded":
                self.log("skip", reason="already uploaded", video_id=done["upload"].get("video_id"))
                return done
            if status == "in_progress":
                raise PipelineError(
                    "A previous upload of this run was interrupted. Check YouTube Studio for a partial private "
                    "video, then set production.json upload.status to 'uploaded' (with video_id) or remove it."
                )
        pre = self.studio.run_preproduction()
        self.log("preproduction", title=pre["packaging"]["title"], budget=pre["budget"]["committed_usd"])
        pre, speech = self._speech_within_target(pre)
        episode = pre["episode"]
        art_dir = self.run_dir / "art"
        references = self.media.references(episode, pre["art_direction"], art_dir)
        art = self.media.art_loop(episode, pre["art_direction"], references, art_dir, self.review_shot)
        self.log("art", passed=art["passed"], failed=art["failed_shots"])
        mix = self.media.mix(episode, speech, self.run_dir / "scenes", self.run_dir / "audio",
                             music_dir=self.music_dir, sfx_dir=self.sfx_dir)
        output = self.run_dir / "output"
        render = self.media.render(episode, speech, Path(mix["file"]), art_dir / "frames", output)
        plan = self.media.timeline(episode, speech)
        starts = dict(zip((s["id"] for s in episode["scenes"]), plan["scene_starts"]))
        key_scene = episode["scenes"][len(episode["scenes"]) // 2]["id"]
        key_frame = art_dir / "frames" / f"{key_scene}-shot-01.png"
        packaging = pre["packaging"]
        thumbnails = self.media.thumbnails(key_frame, [packaging["thumbnail_text"]], output / "thumbnails")
        shots = [{**shot, "image": str(art_dir / "frames" / f"{shot['scene']}-shot-{shot['id']}.png")}
                 for shot in plan["shots"]]
        shorts = self.media.shorts(episode, packaging, captions=plan["captions"], shot_timeline=shots,
                                   starts=starts, source_media=Path(mix["file"]), directory=output / "shorts")
        captions = self.media.captions(output, plan["captions"])
        chapters = [(scene["title"], starts[scene["id"]]) for scene in episode["scenes"]]
        metadata = self.media.metadata(episode, packaging, chapters, render["duration_seconds"],
                                       attributions=list(mix.get("attributions", [])))
        media_report = {
            "technical_pass": bool(render["evaluation"].get("technical_pass")) and art["passed"],
            "technical": render["evaluation"], "art_passed": art["passed"], "art_failed_shots": art["failed_shots"],
            "duration_seconds": render["duration_seconds"], "shorts": len(shorts),
        }
        verdict = self.studio.final_review(pre, media_report)
        self.log("final", approved=verdict["approved"], score=verdict["score"], back_to=verdict["return_to_stage"])
        result = {
            "run": self.run_dir.name, "title": metadata["title"], "video": render["video"], "verdict": verdict,
            "budget": self.studio.gateway.ledger.summary(), "upload": None,
        }
        if verdict["approved"] and self.uploader is not None:
            # Persist intent first: a crash mid-upload must never cause a silent duplicate upload on rerun.
            write_json(self.run_dir / "production.json", {**result, "upload": {"status": "in_progress"}})
            result["upload"] = self.uploader({
                "video": render["video"], "metadata": metadata, "thumbnail": thumbnails[0]["file"],
                "captions": [captions["srt"]], "shorts": shorts, "run_dir": str(self.run_dir),
            })
            self.log("upload", status=result["upload"].get("status"))
        elif not verdict["approved"]:
            result["not_uploaded_reason"] = verdict["reasons"]
        write_json(self.run_dir / "production.json", result)
        return result

    def retrospective(self, analytics_rows: list[dict[str, Any]] = (), video_ids: list[str] = ()) -> dict[str, Any]:
        """Runs after the gate (and again after analytics); writes lessons to the playbook."""
        return run_retrospective(self.studio, analytics_rows, video_ids)


def run_retrospective(studio: Studio, analytics_rows=(), video_ids=()) -> dict[str, Any]:
    inputs = retro_inputs(studio.run_dir, analytics_rows, video_ids)
    retro = studio.call("retrospective", "이번 편 제작 기록과 성과로 다음 편에 쓸 교훈을 정리하라.",
                        stage="retrospective", record=inputs,
                        existing_lessons=[{k: l[k] for k in ("id", "text", "roles")}
                                          for l in studio.playbook.active()])
    summary = apply_retrospective(studio.playbook, retro, studio.run_dir.name)
    summary["playbook_version"] = studio.playbook.version
    write_json(studio.run_dir / f"retrospective-{int(time.time() * 1000)}.json", summary)
    return summary
