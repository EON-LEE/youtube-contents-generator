from __future__ import annotations

from typing import Any

from .models import PipelineError, required_text

ROLE_CONTRACTS = {
    "research": {
        "role": "researcher",
        "tools": [],
        "output": ("candidates", "rights_status", "sources"),
    },
    "write": {
        "role": "writer",
        "tools": [],
        "output": ("script", "characters", "scenes"),
    },
    "edit": {
        "role": "critical_editor",
        "tools": [],
        "output": ("script", "characters", "scenes", "blocking_issues"),
    },
    "direct": {
        "role": "director",
        "tools": [],
        "output": ("script", "characters", "scenes", "voice_plan"),
    },
    "produce": {
        "role": "deterministic_production_simulator",
        "tools": [],
        "output": ("manifest", "actual_duration_seconds", "media_created"),
    },
    "qa": {
        "role": "quality_reviewer",
        "tools": [],
        "output": ("checks", "production_ready", "blocking_issues"),
    },
}


def validate_payload(stage: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or payload.get("simulated") is not True:
        raise PipelineError("Role output must be an explicitly simulated object.")
    contract = ROLE_CONTRACTS[stage]
    for field in contract["output"]:
        if field not in payload:
            raise PipelineError(f"{stage}: missing required output field {field!r}.")
    if stage == "research":
        if payload["rights_status"] != "original_fixture_only":
            raise PipelineError("Fixture rights must not imply clearance of external materials.")
        if (
            payload["sources"] != []
            or not isinstance(payload["candidates"], list)
            or not payload["candidates"]
        ):
            raise PipelineError("Research fixture must have candidates and no fabricated sources.")
        for candidate in payload["candidates"]:
            if not isinstance(candidate, dict):
                raise PipelineError("Research candidates must be objects.")
            required_text(candidate.get("concept"), "Candidate concept")
    if stage in ("write", "edit", "direct"):
        required_text(payload["script"], f"{stage} script")
        characters = payload["characters"]
        if not isinstance(characters, list) or not characters:
            raise PipelineError("Characters must be a nonempty list.")
        ids: set[str] = set()
        for character in characters:
            if not isinstance(character, dict):
                raise PipelineError("Each character must be an object.")
            identifier = required_text(character.get("id"), "Character ID")
            if identifier in ids or character.get("adult") is not True:
                raise PipelineError("Fixture characters must have unique IDs and be adults.")
            ids.add(identifier)
        scenes = payload["scenes"]
        if not isinstance(scenes, list) or not scenes:
            raise PipelineError("Scenes must be a nonempty list.")
        scene_ids: set[str] = set()
        for scene in scenes:
            if not isinstance(scene, dict):
                raise PipelineError("Each scene must be an object.")
            identifier = required_text(scene.get("id"), "Scene ID")
            speaker = required_text(scene.get("speaker"), "Scene speaker")
            if identifier in scene_ids or speaker not in ids:
                raise PipelineError("Scenes require unique IDs and known speakers.")
            required_text(scene.get("text"), "Scene text")
            scene_ids.add(identifier)
        if payload["script"] != "\n".join(scene["text"] for scene in scenes):
            raise PipelineError("Script and ordered scene text do not match.")
        if stage == "direct":
            voice_plan = payload["voice_plan"]
            if not isinstance(voice_plan, dict) or set(voice_plan) != ids:
                raise PipelineError("Every character needs exactly one fixture voice assignment.")
    if stage in ("edit", "qa"):
        if payload["blocking_issues"] != []:
            raise PipelineError(f"{stage}: unresolved blocking issues.")
    if stage == "produce":
        if payload["media_created"] is not False or payload["actual_duration_seconds"] is not None:
            raise PipelineError("A fixture cannot claim actual media or measured duration.")
        if not isinstance(payload["manifest"], list) or not payload["manifest"]:
            raise PipelineError("Production manifest must be a nonempty list.")
        for scene in payload["manifest"]:
            if (
                not isinstance(scene, dict)
                or scene.get("audio") is not None
                or scene.get("image") is not None
                or scene.get("status") != "simulated_only"
            ):
                raise PipelineError("Production fixture must not claim real media.")
            required_text(scene.get("scene_id"), "Manifest scene ID")
    if stage == "qa":
        if payload["production_ready"] is not False:
            raise PipelineError("Simulated QA must never mark output production-ready.")
        if not isinstance(payload["checks"], dict):
            raise PipelineError("QA checks must be an object.")
        for check in ("audio", "visuals", "listening", "rights", "audience_demand"):
            if payload["checks"].get(check) != "not_tested":
                raise PipelineError(f"QA cannot claim a real {check} check.")
