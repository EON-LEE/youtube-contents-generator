"""Agent roster: who is on the team, which prompt file, tools and output schema.

Model deployments come from configuration (``[agents]`` in studio.toml), so the
mix of model families can change without code changes. Writers and the critics
that judge them must use different model families; ``check_independence``
enforces it.

``critic-originality`` and ``packaging-agent`` are intentionally deployed WITHOUT
``web_search`` even though their prompts describe using it: on Azure AI Foundry,
the grok-4-1-fast-reasoning deployment rejects any Responses API call that
includes the ``web_search`` tool with a 400 ``ApiSamplingErrorUnprocessableInput``
(confirmed reproducible in isolation; the same deployment's ``file_search`` tool
works fine). Reassigning these two roles to an OpenAI model to keep web_search
would break ``check_independence`` (writers are already OpenAI). They fall back
to the model's own knowledge instead of live search grounding.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import PipelineError
from .episode_v3 import EMOTIONS, MOODS


def obj(required: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": list(required), "properties": required}


STR = {"type": "string"}
NUM = {"type": "number"}
STRS = {"type": "array", "items": STR}

SCORE = obj({"axis": STR, "score": NUM, "rationale": STR})

SCHEMAS: dict[str, dict[str, Any]] = {
    "research": obj({"topics": {"type": "array", "items": obj({
        "topic": STR, "why_now": STR, "audience_fit": STR, "sources": STRS})}}),
    "analysis": obj({"keep": STRS, "avoid": STRS, "hypotheses": STRS, "evidence": STRS}),
    "brief": obj({"episode_goal": STR, "audience_promise": STR, "constraints": STRS,
                  "chosen_topic": STR, "tone": STR}),
    "concept": obj({"logline": STR, "hook": STR, "protagonist": STR, "conflict": STR,
                    "turning_point": STR, "emotional_payoff": STR, "title_candidates": STRS}),
    "concept_scores": obj({"evaluations": {"type": "array", "items": obj({
        "concept_index": {"type": "integer"}, "scores": {"type": "array", "items": SCORE},
        "notes": STR})}}),
    "outline": obj({
        "title": STR, "subtitle": STR, "logline": STR, "synopsis": STR,
        "characters": {"type": "array", "items": obj({
            "id": STR, "name": STR, "age_band": STR, "gender": {"enum": ["female", "male"]},
            "appearance": STR, "personality": STR})},
        "locations": {"type": "array", "items": obj({"id": STR, "description": STR})},
        "scenes": {"type": "array", "items": obj({
            "id": STR, "title": STR, "pov": STR, "location": STR, "mood": {"enum": list(MOODS)},
            "beats": STRS, "target_characters": {"type": "integer"}})}}),
    "scene": obj({"id": STR, "narration": STR}),
    "critique": obj({"axis": STR, "score": NUM, "blocking": {"type": "boolean"},
                     "notes": {"type": "array", "items": obj({
                         "scene_id": STR, "issue": STR, "suggestion": STR,
                         "severity": {"enum": ["minor", "major", "blocking"]}})}}),
    "revision_plan": obj({"decisions": {"type": "array", "items": obj({
        "scene_id": STR, "action": STR, "rationale": STR, "from_axes": STRS})},
        "rejected_notes": {"type": "array", "items": obj({"issue": STR, "reason": STR})}}),
    "revision": obj({"scenes": {"type": "array", "items": obj({"id": STR, "narration": STR})}}),
    "storyboard": obj({"scene_id": STR, "shots": {"type": "array", "items": obj({
        "id": STR, "anchor": STR, "characters": STRS, "visual_prompt": STR,
        "emotion": {"enum": list(EMOTIONS)}, "reason": STR, "sfx": STRS})}}),
    "casting": obj({"voices": {"type": "array", "items": obj({
        "character_id": STR, "name": STR, "style": STR, "rate": STR, "pitch": STR})}}),
    "art_direction": obj({"style_guide": STR, "negative_guidance": STR,
                          "character_sheets": {"type": "array", "items": obj({
                              "character_id": STR, "reference_prompt": STR})}}),
    "art_review": obj({"score": NUM, "issues": STRS, "regenerate": {"type": "boolean"},
                       "revised_prompt": STR}),
    "packaging": obj({"titles": STRS, "thumbnail_texts": STRS, "description": STR, "tags": STRS,
                      "shorts": {"type": "array", "items": obj({
                          "scene_id": STR, "start_anchor": STR, "end_anchor": STR, "hook": STR})}}),
    "click_scores": obj({"titles": {"type": "array", "items": obj({"index": {"type": "integer"}, "score": NUM, "rationale": STR})},
                         "thumbnail_texts": {"type": "array", "items": obj({"index": {"type": "integer"}, "score": NUM, "rationale": STR})}}),
    "final_verdict": obj({"approve": {"type": "boolean"}, "score": NUM, "reasons": STRS,
                          "return_to_stage": {"enum": ["none", "concept", "script", "direction", "art", "packaging"]}}),
    "retrospective": obj({"lessons": {"type": "array", "items": obj({
        "text": STR, "roles": STRS, "confidence": {"enum": ["low", "medium", "high"]}, "evidence": STR})},
        "retire_lesson_ids": STRS}),
}


@dataclass(frozen=True)
class AgentSpec:
    name: str
    team: str
    prompt: str
    schema: str
    tools: tuple[str, ...] = ()
    family_group: str = ""
    max_output_tokens: int = 4000

    def instructions(self, channel_dir: Path) -> str:
        path = channel_dir / "prompts" / self.prompt
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise PipelineError(f"Missing agent prompt: {path}") from None
        shared = (channel_dir / "prompts" / "_shared.md").read_text(encoding="utf-8")
        return shared + "\n\n" + text


ROSTER: tuple[AgentSpec, ...] = (
    AgentSpec("showrunner", "planning", "showrunner.md", "brief", ("file_search",)),
    AgentSpec("trend-researcher", "planning", "trend_researcher.md", "research", ("web_search",), "", 20000),
    AgentSpec("performance-analyst", "planning", "performance_analyst.md", "analysis", ("file_search",)),
    AgentSpec("concept-writer-a", "planning", "concept_writer.md", "concept", ("file_search",), "concept_writers"),
    AgentSpec("concept-writer-b", "planning", "concept_writer.md", "concept", ("file_search",), "concept_writers"),
    AgentSpec("concept-writer-c", "planning", "concept_writer.md", "concept", ("file_search",), "concept_writers"),
    AgentSpec("greenlight-judge-a", "planning", "greenlight_judge.md", "concept_scores", (), "concept_judges"),
    AgentSpec("greenlight-judge-b", "planning", "greenlight_judge.md", "concept_scores", (), "concept_judges"),
    AgentSpec("outline-writer", "writing", "outline_writer.md", "outline", ("file_search",), "", 8000),
    AgentSpec("scene-writer", "writing", "scene_writer.md", "scene", ("file_search",), "writers", 3000),
    AgentSpec("critic-continuity", "writing", "critic_continuity.md", "critique", (), "critics", 4000),
    AgentSpec("critic-engagement", "writing", "critic_engagement.md", "critique", ("file_search",), "critics", 4000),
    AgentSpec("critic-korean", "writing", "critic_korean.md", "critique", (), "critics", 4000),
    AgentSpec("critic-originality", "writing", "critic_originality.md", "critique", (), "critics", 16000),
    AgentSpec("critic-policy", "writing", "critic_policy.md", "critique", (), "critics", 4000),
    AgentSpec("arbiter", "writing", "arbiter.md", "revision_plan", (), "", 4000),
    AgentSpec("script-doctor", "writing", "script_doctor.md", "revision", ("file_search",), "writers", 16000),
    AgentSpec("director", "direction", "director.md", "storyboard", (), "", 4000),
    AgentSpec("voice-director", "direction", "voice_director.md", "casting", (), "", 2000),
    AgentSpec("art-director", "direction", "art_director.md", "art_direction", ("file_search",), "", 4000),
    AgentSpec("art-critic", "direction", "art_critic.md", "art_review", (), "", 1500),
    AgentSpec("packaging-agent", "packaging", "packaging_agent.md", "packaging", (), "packagers", 16000),
    AgentSpec("click-judge", "packaging", "click_judge.md", "click_scores", (), "", 2000),
    AgentSpec("final-judge", "packaging", "final_judge.md", "final_verdict", (), "", 3000),
    AgentSpec("retrospective", "learning", "retrospective.md", "retrospective", ("file_search",), "", 4000),
)

BY_NAME = {spec.name: spec for spec in ROSTER}

# Groups whose members must never share a model family with the paired group.
INDEPENDENT_PAIRS = (("writers", "critics"), ("concept_writers", "concept_judges"), ("packagers", "final"))


def spec(name: str) -> AgentSpec:
    try:
        return BY_NAME[name]
    except KeyError:
        raise PipelineError(f"Unknown agent {name!r}.") from None


def model_family(deployment: str) -> str:
    lowered = deployment.lower()
    for family in ("claude", "gpt", "o1", "o3", "o4", "deepseek", "llama", "mistral", "grok", "phi", "gemini", "kimi", "qwen"):
        if family in lowered:
            return "openai" if family in ("gpt", "o1", "o3", "o4") else family
    return lowered


def check_independence(agent_models: dict[str, str]) -> None:
    """Writers and their reviewers must be different model families (no self-grading)."""
    def families(group: str) -> set[str]:
        members = [s.name for s in ROSTER if s.family_group == group]
        if group == "final":
            members = ["final-judge"]
        return {model_family(agent_models[m]) for m in members if m in agent_models}

    for producer, reviewer in INDEPENDENT_PAIRS:
        overlap = families(producer) & families(reviewer)
        if overlap:
            raise PipelineError(
                f"Model family overlap between {producer} and {reviewer}: {sorted(overlap)}. "
                "Assign reviewers a different model family."
            )
    writers = {model_family(agent_models[s.name]) for s in ROSTER if s.family_group == "concept_writers"}
    if len(writers) < 2:
        raise PipelineError("Concept writers must use at least two different model families for diversity.")
