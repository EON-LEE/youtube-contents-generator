from __future__ import annotations

from typing import Any

from .models import JobSpec, PipelineError, TransientFailure


class FixtureProvider:
    version = "fixture-v1"
    simulated = True

    def __init__(self, failures: dict[str, int] | None = None):
        self.failures = dict(failures or {})
        self.calls: list[str] = []

    def usage(self, stage: str, previous: dict[str, Any] | None) -> dict[str, int]:
        if stage == "produce":
            if previous is None:
                raise PipelineError("Production requires a directed script.")
            return {
                "speech_characters": sum(len(scene["text"]) for scene in previous["scenes"]),
                "images": len(previous["scenes"]),
            }
        return {"model_input_tokens": 500, "model_output_tokens": 200}

    def generate(
        self, stage: str, spec: JobSpec, previous: dict[str, Any] | None, instruction: str
    ) -> dict[str, Any]:
        self.calls.append(stage)
        if self.failures.get(stage, 0):
            self.failures[stage] -= 1
            raise TransientFailure(f"Injected fixture failure at {stage}.")
        note = {
            "simulated": True,
            "instruction": instruction,
            "limitations": "Fixed fixture; no model reasoning, research, rights clearance or media generation.",
        }
        if stage == "research":
            return {
                **note,
                "candidates": [{"concept": spec.concept, "status": "unvalidated_hypothesis"}],
                "sources": [],
                "rights_status": "original_fixture_only",
            }
        if previous is None:
            raise PipelineError(f"{stage} requires an upstream artifact.")
        if stage == "write":
            scenes = [
                {"id": "scene-1", "speaker": "parent", "text": "나는 가족에게 내 생각을 먼저 설명하고 싶었다."},
                {"id": "scene-2", "speaker": "child", "text": "나는 같은 일을 다른 관점에서 기억하고 있었다."},
            ]
            return {
                **note,
                "concept": spec.concept,
                "script": "\n".join(scene["text"] for scene in scenes),
                "characters": [
                    {"id": "parent", "adult": True},
                    {"id": "child", "adult": True},
                ],
                "scenes": scenes,
                "target_minutes": spec.target_minutes,
                "actual_duration_seconds": None,
            }
        if stage == "edit":
            return {**previous, **note, "blocking_issues": [], "editorial_quality": "not_tested"}
        if stage == "direct":
            return {
                **previous,
                **note,
                "voice_plan": {character["id"]: "fixture-not-a-real-voice" for character in previous["characters"]},
            }
        if stage == "produce":
            return {
                **note,
                "manifest": [
                    {"scene_id": scene["id"], "audio": None, "image": None, "status": "simulated_only"}
                    for scene in previous["scenes"]
                ],
                "actual_duration_seconds": None,
                "media_created": False,
                "target_minutes": spec.target_minutes,
            }
        if stage == "qa":
            return {
                **note,
                "checks": {
                    "contract": "passed",
                    "audio": "not_tested",
                    "visuals": "not_tested",
                    "listening": "not_tested",
                    "rights": "not_tested",
                    "audience_demand": "not_tested",
                },
                "blocking_issues": [],
                "production_ready": False,
            }
        raise PipelineError(f"Unknown stage: {stage}")


def get_provider(name: str) -> FixtureProvider:
    if name != "fixture":
        raise PipelineError("Live providers are not implemented or authorized; use explicit dry-run.")
    return FixtureProvider()
