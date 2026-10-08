"""Episode schema v3: characters, voices, locations and shots are data, not code."""
from __future__ import annotations

import re
from typing import Any

from ..media import normalized_text
from ..models import PipelineError

MOODS = ("tender", "concerned", "tense", "sad", "hopeful", "warm", "neutral", "relieved")
EMOTIONS = ("neutral", "concerned", "softened", "tense", "sad", "joyful", "surprised")
VOICE_PATTERN = re.compile(r"ko-KR-[A-Za-z]+Neural\Z")
ID = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
RATE = re.compile(r"[+-]\d{1,2}%\Z")

EPISODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "id", "title", "subtitle", "logline", "fiction_notice", "synopsis",
                 "characters", "locations", "scenes"],
    "properties": {
        "schema_version": {"const": 3},
        "id": {"type": "string"},
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "logline": {"type": "string"},
        "fiction_notice": {"type": "string"},
        "synopsis": {"type": "string"},
        "characters": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "name", "age_band", "gender", "adult", "appearance", "voice"],
            "properties": {
                "id": {"type": "string"}, "name": {"type": "string"}, "age_band": {"type": "string"},
                "gender": {"enum": ["female", "male"]}, "adult": {"const": True},
                "appearance": {"type": "string"},
                "voice": {"type": "object", "additionalProperties": False,
                          "required": ["name", "style", "rate", "pitch"],
                          "properties": {"name": {"type": "string"}, "style": {"type": "string"},
                                         "rate": {"type": "string"}, "pitch": {"type": "string"}}},
            }}},
        "locations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "description"],
            "properties": {"id": {"type": "string"}, "description": {"type": "string"}}}},
        "scenes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "title", "pov", "location", "mood", "narration", "shots"],
            "properties": {
                "id": {"type": "string"}, "title": {"type": "string"}, "pov": {"type": "string"},
                "location": {"type": "string"}, "mood": {"enum": list(MOODS)},
                "narration": {"type": "string"},
                "shots": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "anchor", "characters", "visual_prompt", "emotion", "reason", "sfx"],
                    "properties": {
                        "id": {"type": "string"}, "anchor": {"type": "string"},
                        "characters": {"type": "array", "items": {"type": "string"}},
                        "visual_prompt": {"type": "string"}, "emotion": {"enum": list(EMOTIONS)},
                        "reason": {"type": "string"}, "sfx": {"type": "array", "items": {"type": "string"}},
                    }}},
            }}},
    },
}


def _text(value: Any, label: str, maximum: int = 20000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PipelineError(f"{label} must be nonempty text.")
    if len(value) > maximum:
        raise PipelineError(f"{label} exceeds {maximum} characters.")
    return value


def validate_shots(scene: dict[str, Any], characters: set[str]) -> None:
    shots = scene["shots"]
    if not isinstance(shots, list) or not 2 <= len(shots) <= 8:
        raise PipelineError(f"Scene {scene['id']}: a storyboard needs 2-8 shots.")
    text = normalized_text(scene["narration"])
    previous = -1
    seen = set()
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict) or not re.fullmatch(r"\d{2}", str(shot.get("id", ""))):
            raise PipelineError(f"Scene {scene['id']}: shot IDs must be two digits.")
        if shot["id"] in seen:
            raise PipelineError(f"Scene {scene['id']}: duplicate shot {shot['id']}.")
        seen.add(shot["id"])
        if shot.get("emotion") not in EMOTIONS:
            raise PipelineError(f"{scene['id']}/{shot['id']}: unknown emotion {shot.get('emotion')!r}.")
        _text(shot.get("visual_prompt"), f"{scene['id']}/{shot['id']} visual_prompt", 1500)
        _text(shot.get("reason"), f"{scene['id']}/{shot['id']} reason", 500)
        cast = shot.get("characters")
        if not isinstance(cast, list) or any(c not in characters for c in cast):
            raise PipelineError(f"{scene['id']}/{shot['id']}: shot characters must be declared characters.")
        if not isinstance(shot.get("sfx"), list) or any(not isinstance(s, str) for s in shot["sfx"]):
            raise PipelineError(f"{scene['id']}/{shot['id']}: sfx must be a list of tags.")
        anchor = normalized_text(_text(shot.get("anchor"), f"{scene['id']}/{shot['id']} anchor", 300))
        if len(anchor) < 4 or text.count(anchor) != 1:
            raise PipelineError(f"{scene['id']}/{shot['id']}: anchor is absent or ambiguous in the narration.")
        location = text.index(anchor)
        if location <= previous or (index == 0 and location != 0):
            raise PipelineError(f"Scene {scene['id']}: anchors must start the narration and proceed in order.")
        previous = location


def validate_episode(episode: Any, *, target_characters: tuple[int, int] | None = None) -> dict[str, Any]:
    if not isinstance(episode, dict) or episode.get("schema_version") != 3:
        raise PipelineError("Episode must be a schema_version 3 object.")
    if not ID.fullmatch(str(episode.get("id", "")).replace("-", "_")):
        raise PipelineError("Episode id must be a short lowercase slug.")
    for name in ("title", "subtitle", "logline", "fiction_notice", "synopsis"):
        _text(episode.get(name), f"Episode {name}", 2000)
    characters = episode.get("characters")
    if not isinstance(characters, list) or not 1 <= len(characters) <= 6:
        raise PipelineError("Episode needs 1-6 characters.")
    cast = set()
    for character in characters:
        if not isinstance(character, dict) or not ID.fullmatch(str(character.get("id", ""))):
            raise PipelineError("Character IDs must be lowercase identifiers.")
        if character["id"] in cast:
            raise PipelineError(f"Duplicate character {character['id']}.")
        if character.get("adult") is not True:
            raise PipelineError(f"Character {character['id']} must be an adult.")
        for name in ("name", "age_band", "appearance"):
            _text(character.get(name), f"Character {character['id']} {name}", 600)
        if character.get("gender") not in ("female", "male"):
            raise PipelineError(f"Character {character['id']}: gender must be female or male for voice casting.")
        voice = character.get("voice")
        if not isinstance(voice, dict) or not VOICE_PATTERN.fullmatch(str(voice.get("name", ""))):
            raise PipelineError(f"Character {character['id']}: voice must be a ko-KR neural voice name.")
        for name in ("rate", "pitch"):
            if not RATE.fullmatch(str(voice.get(name, ""))):
                raise PipelineError(f"Character {character['id']}: voice {name} must look like +0%.")
        _text(voice.get("style"), f"Character {character['id']} voice style", 40)
        cast.add(character["id"])
    locations = episode.get("locations")
    if not isinstance(locations, list) or not locations:
        raise PipelineError("Episode needs at least one location.")
    places = set()
    for location in locations:
        if not isinstance(location, dict) or not ID.fullmatch(str(location.get("id", ""))):
            raise PipelineError("Location IDs must be lowercase identifiers.")
        _text(location.get("description"), f"Location {location['id']} description", 800)
        places.add(location["id"])
    scenes = episode.get("scenes")
    if not isinstance(scenes, list) or not 4 <= len(scenes) <= 24:
        raise PipelineError("Episode needs 4-24 scenes.")
    seen = set()
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict) or scene.get("id") != f"{index:02}":
            raise PipelineError("Scene IDs must be consecutive two-digit numbers starting at 01.")
        seen.add(scene["id"])
        _text(scene.get("title"), f"Scene {scene['id']} title", 80)
        _text(scene.get("narration"), f"Scene {scene['id']} narration", 4000)
        if scene.get("pov") not in cast:
            raise PipelineError(f"Scene {scene['id']}: pov must be a declared character.")
        if scene.get("location") not in places:
            raise PipelineError(f"Scene {scene['id']}: location must be declared.")
        if scene.get("mood") not in MOODS:
            raise PipelineError(f"Scene {scene['id']}: unknown mood {scene.get('mood')!r}.")
        validate_shots(scene, cast)
    if target_characters is not None:
        total = narration_characters(episode)
        low, high = target_characters
        if not low <= total <= high:
            raise PipelineError(
                f"Narration has {total} characters; target is {low}-{high} for a 20-30 minute episode."
            )
    return episode


def narration_characters(episode: dict[str, Any]) -> int:
    return sum(len(scene["narration"]) for scene in episode["scenes"])
