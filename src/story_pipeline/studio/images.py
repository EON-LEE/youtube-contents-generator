"""Foundry image generation: character references, per-shot art, and the art-critic loop."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Callable, Protocol

from ..media import write_json
from ..models import PipelineError, digest
from .config import StudioConfig
from .ledger import CostLedger

SIZE = "1536x1024"
FRAME_SIZE = (1920, 1080)
PROVIDER = "foundry-image"
VERSION = "studio-images-v1"
REFUSAL_MARKERS = ("content_filter", "content_policy", "moderation_blocked", "responsibleaipolicyviolation")

Softener = Callable[[str, dict[str, Any], str], str]
Reviewer = Callable[[bytes, dict[str, Any]], dict[str, Any]]


class ContentFilterRefusal(PipelineError):
    """The image service refused the prompt under its content policy."""


class ImageClient(Protocol):
    def generate(self, prompt: str, size: str) -> bytes: ...

    def edit(self, prompt: str, reference_images: list[bytes], size: str) -> bytes: ...


class FoundryImageClient:
    """gpt-image deployment reached through the Foundry project's OpenAI-compatible client."""

    def __init__(self, config: StudioConfig, *, credential: Any = None, openai_client: Any = None):
        if not config.image_deployment:
            raise PipelineError("Set [foundry].image_deployment or FOUNDRY_IMAGE_DEPLOYMENT.")
        if openai_client is None:
            if not config.project_endpoint:
                raise PipelineError("Set [foundry].project_endpoint or FOUNDRY_PROJECT_ENDPOINT.")
            try:
                from azure.ai.projects import AIProjectClient
                from azure.identity import DefaultAzureCredential
            except ImportError as error:
                raise PipelineError("Install azure-ai-projects and azure-identity for Foundry images.") from error
            project = AIProjectClient(endpoint=config.project_endpoint, credential=credential or DefaultAzureCredential())
            openai_client = project.get_openai_client()
        self.client = openai_client
        self.deployment = config.image_deployment

    def generate(self, prompt: str, size: str) -> bytes:
        return self._call(lambda: self.client.images.generate(model=self.deployment, prompt=prompt, size=size, n=1))

    def edit(self, prompt: str, reference_images: list[bytes], size: str) -> bytes:
        files = [(f"reference-{index}.png", data, "image/png") for index, data in enumerate(reference_images)]
        return self._call(lambda: self.client.images.edit(
            model=self.deployment, image=files, prompt=prompt, size=size, n=1))

    @staticmethod
    def _call(request: Callable[[], Any]) -> bytes:
        try:
            response = request()
        except Exception as error:
            text = f"{getattr(error, 'code', '')} {getattr(error, 'body', '')} {error}".lower()
            if any(marker in text for marker in REFUSAL_MARKERS):
                raise ContentFilterRefusal(f"Image prompt refused by content filter: {error}") from error
            raise PipelineError(f"Foundry image request failed: {error}") from error
        data = getattr(response.data[0], "b64_json", None) if getattr(response, "data", None) else None
        if not data:
            raise PipelineError("Foundry image response contained no base64 image.")
        return base64.b64decode(data)


def _character_index(episode: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {character["id"]: character for character in episode["characters"]}


def character_prompt(art_direction: dict[str, Any], sheet: dict[str, Any], character: dict[str, Any]) -> str:
    parts = [
        art_direction["style_guide"],
        f"Character reference sheet for {character['name']} ({character['age_band']}, {character['gender']}, adult).",
        f"Appearance: {character['appearance']}",
        sheet["reference_prompt"],
        "Single character, face and full outfit clearly visible, plain neutral background, no text or watermark.",
    ]
    if art_direction.get("negative_guidance"):
        parts.append(f"Avoid: {art_direction['negative_guidance']}")
    return "\n".join(part.strip() for part in parts if part and part.strip())


def shot_prompt(
    episode: dict[str, Any], art_direction: dict[str, Any], scene: dict[str, Any], shot: dict[str, Any],
    visual_override: str | None = None,
) -> str:
    places = {location["id"]: location["description"] for location in episode["locations"]}
    cast = _character_index(episode)
    parts = [
        art_direction["style_guide"],
        f"Location: {places[scene['location']]}",
        f"Shot: {visual_override or shot['visual_prompt']}",
        *(f"{cast[c]['name']} (adult, {cast[c]['age_band']}): {cast[c]['appearance']}. Match the reference image."
          for c in shot["characters"]),
        f"Emotion: {shot['emotion']}. Scene mood: {scene['mood']}.",
        "Cinematic 3:2 composition, no text, captions, logos or watermarks.",
    ]
    if art_direction.get("negative_guidance"):
        parts.append(f"Avoid: {art_direction['negative_guidance']}")
    return "\n".join(part.strip() for part in parts if part and part.strip())


def to_frame(source: Path, target: Path, size: tuple[int, int] = FRAME_SIZE) -> Path:
    """Scale and center-crop to the render frame (1536x1024 -> 1920x1080 crops 4% top and bottom)."""
    from PIL import Image, ImageOps
    with Image.open(source) as image:
        framed = ImageOps.fit(image.convert("RGB"), size, Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp.png")
    framed.save(temporary, format="PNG")
    temporary.replace(target)
    return target


def _normalize_png(data: bytes, target: Path, label: str) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError as error:
        raise PipelineError("Install the media extra (Pillow) to store generated images.") from error
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            converted = image.convert("RGB")
    except Exception as error:
        raise PipelineError(f"{label}: image service returned undecodable data: {error}") from error
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp.png")
    converted.save(temporary, format="PNG")
    temporary.replace(target)
    return converted.size


def cached_image(
    name: str, prompt: str, references: list[bytes], directory: Path, *, config: StudioConfig,
    ledger: CostLedger, client: ImageClient, label: str, attempt: int = 1,
) -> dict[str, Any]:
    """One image call, content-addressed by prompt, references, deployment and attempt number."""
    mode = "edit" if references else "generate"
    fingerprint = digest({
        "prompt": prompt, "references": [hashlib.sha256(r).hexdigest() for r in references], "size": SIZE,
        "deployment": config.image_deployment, "provider": PROVIDER, "version": VERSION, "mode": mode,
        "attempt": attempt,
    })
    png = directory / f"{name}.png"
    meta = directory / f"{name}.image.json"
    if png.exists() and meta.exists():
        record = json.loads(meta.read_text(encoding="utf-8"))
        if record.get("fingerprint") == fingerprint and record.get("sha256") == hashlib.sha256(png.read_bytes()).hexdigest():
            return {**record, "cached": True}
    entry = ledger.reserve(label, config.image_usd_per_call, {
        "provider": PROVIDER, "deployment": config.image_deployment, "mode": mode, "size": SIZE,
    })
    try:
        data = client.edit(prompt, references, SIZE) if references else client.generate(prompt, SIZE)
    except ContentFilterRefusal as error:
        ledger.fail(entry, str(error))
        raise
    except Exception as error:
        ledger.fail(entry, str(error))
        if isinstance(error, PipelineError):
            raise
        raise PipelineError(f"{label}: image call failed; no fallback is used: {error}") from error
    ledger.settle(entry, config.image_usd_per_call, {"images": 1, "size": SIZE, "mode": mode})
    width, height = _normalize_png(data, png, label)
    record = {
        "fingerprint": fingerprint, "name": name, "prompt": prompt, "mode": mode, "attempt": attempt,
        "reference_sha256": [hashlib.sha256(r).hexdigest() for r in references],
        "requested_size": SIZE, "returned_size": [width, height], "deployment": config.image_deployment,
        "provider": PROVIDER, "sha256": hashlib.sha256(png.read_bytes()).hexdigest(), "simulated": False,
    }
    write_json(meta, record)
    return {**record, "cached": False}


def _with_refusal_retries(
    build: Callable[[str], dict[str, Any]], prompt: str, subject: dict[str, Any], *, config: StudioConfig,
    soften: Softener | None, label: str,
) -> dict[str, Any]:
    refusals = []
    for retry in range(config.loops.art_retries_per_shot + 1):
        try:
            record = build(prompt)
            return {**record, "content_filter_refusals": refusals}
        except ContentFilterRefusal as error:
            refusals.append({"prompt": prompt, "error": str(error)})
            if soften is None or retry == config.loops.art_retries_per_shot:
                break
            prompt = soften(prompt, subject, str(error))
    raise PipelineError(
        f"{label}: content filter refused {len(refusals)} prompt(s); revise the visual direction. "
        f"Last error: {refusals[-1]['error']}"
    )


def generate_character_references(
    episode: dict[str, Any], art_direction: dict[str, Any], directory: Path, *, config: StudioConfig,
    ledger: CostLedger, client: ImageClient, soften: Softener | None = None,
) -> dict[str, Path]:
    cast = _character_index(episode)
    sheets = {sheet["character_id"]: sheet for sheet in art_direction["character_sheets"]}
    if set(sheets) != set(cast):
        raise PipelineError(f"Character sheets {sorted(sheets)} must match the cast {sorted(cast)} exactly.")
    folder = directory / "characters"
    references = {}
    for identifier in sorted(sheets):
        prompt = character_prompt(art_direction, sheets[identifier], cast[identifier])
        _with_refusal_retries(
            lambda text, identifier=identifier: cached_image(
                identifier, text, [], folder, config=config, ledger=ledger, client=client,
                label=f"image:character:{identifier}"),
            prompt, sheets[identifier], config=config, soften=soften, label=f"character {identifier}",
        )
        references[identifier] = folder / f"{identifier}.png"
    return references


def generate_shot_image(
    episode: dict[str, Any], art_direction: dict[str, Any], scene: dict[str, Any], shot: dict[str, Any],
    references: dict[str, Path], directory: Path, *, config: StudioConfig, ledger: CostLedger,
    client: ImageClient, attempt: int = 1, visual_override: str | None = None, soften: Softener | None = None,
) -> dict[str, Any]:
    missing = [c for c in shot["characters"] if c not in references]
    if missing:
        raise PipelineError(f"{scene['id']}/{shot['id']}: no reference image for {missing}.")
    images = [references[c].read_bytes() for c in shot["characters"]]
    name = f"{scene['id']}-shot-{shot['id']}-a{attempt}"
    prompt = shot_prompt(episode, art_direction, scene, shot, visual_override)
    record = _with_refusal_retries(
        lambda text: cached_image(name, text, images, directory / "shots", config=config, ledger=ledger,
                                  client=client, label=f"image:shot:{scene['id']}/{shot['id']}", attempt=attempt),
        prompt, shot, config=config, soften=soften, label=f"shot {scene['id']}/{shot['id']}",
    )
    return {**record, "path": str(directory / "shots" / f"{name}.png")}


def _check_review(verdict: Any, key: str) -> dict[str, Any]:
    if not isinstance(verdict, dict) or not isinstance(verdict.get("score"), (int, float)) \
            or not isinstance(verdict.get("regenerate"), bool):
        raise PipelineError(f"{key}: art review must return score (number) and regenerate (bool).")
    return {"score": float(verdict["score"]), "issues": list(verdict.get("issues", [])),
            "regenerate": verdict["regenerate"], "revised_prompt": str(verdict.get("revised_prompt", "") or "")}


def art_loop(
    episode: dict[str, Any], art_direction: dict[str, Any], references: dict[str, Path], directory: Path, *,
    config: StudioConfig, ledger: CostLedger, client: ImageClient, review: Reviewer,
    soften: Softener | None = None,
) -> dict[str, Any]:
    """Generate every shot, review each, and regenerate only failed shots up to the retry cap.

    Accepted (or best-scoring, when the cap is hit) attempts are written as 1920x1080 frames
    ``frames/{scene}-shot-{shot}.png``; ``passed`` is False if any shot never cleared the gate.
    """
    shots = {f"{scene['id']}/{shot['id']}": (scene, shot) for scene in episode["scenes"] for shot in scene["shots"]}
    history: dict[str, list[dict[str, Any]]] = {key: [] for key in shots}
    overrides: dict[str, str | None] = {key: None for key in shots}
    pending = list(shots)
    for attempt in range(1, config.loops.art_retries_per_shot + 2):
        failed = []
        for key in pending:
            scene, shot = shots[key]
            record = generate_shot_image(
                episode, art_direction, scene, shot, references, directory, config=config, ledger=ledger,
                client=client, attempt=attempt, visual_override=overrides[key], soften=soften,
            )
            verdict = _check_review(review(Path(record["path"]).read_bytes(), {**shot, "scene_id": scene["id"]}), key)
            accepted = not verdict["regenerate"] and verdict["score"] >= config.thresholds.art
            history[key].append({
                "attempt": attempt, "path": record["path"], "prompt": record["prompt"], "cached": record["cached"],
                "content_filter_refusals": record["content_filter_refusals"], **verdict, "accepted": accepted,
            })
            if not accepted:
                failed.append(key)
                overrides[key] = verdict["revised_prompt"] or overrides[key]
        pending = failed
        if not pending:
            break
    frames = directory / "frames"
    report_shots = {}
    for key, attempts in history.items():
        chosen = next((a for a in attempts if a["accepted"]), None) or max(attempts, key=lambda a: a["score"])
        scene_id, shot_id = key.split("/")
        frame = to_frame(Path(chosen["path"]), frames / f"{scene_id}-shot-{shot_id}.png")
        report_shots[key] = {"attempts": attempts, "chosen_attempt": chosen["attempt"],
                             "accepted": chosen["accepted"], "frame": str(frame)}
    report = {
        "passed": not pending, "failed_shots": pending, "threshold": config.thresholds.art,
        "max_attempts_per_shot": config.loops.art_retries_per_shot + 1, "frame_size": list(FRAME_SIZE),
        "shots": report_shots, "provider": PROVIDER, "deployment": config.image_deployment, "simulated": False,
    }
    write_json(directory / "art-report.json", report)
    return report
