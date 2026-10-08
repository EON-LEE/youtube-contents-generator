"""Azure AI Speech synthesis for episode schema v3 (Entra ID auth, service sentence boundaries)."""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import wave
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol
from xml.sax.saxutils import escape, quoteattr

from ..media import (
    balanced_caption, captions_from_boundaries, improve_caption_readability, normalized_text,
    wav_duration, write_ass, write_json, write_srt,
)
from ..models import PipelineError, digest
from .config import StudioConfig
from .episode_v3 import validate_episode
from .ledger import CostLedger

PROVIDER = "azure-speech"
OUTPUT_FORMAT = "Riff24Khz16BitMonoPcm"
SAMPLE_RATE = 24000
SCENE_PAD_SECONDS = 0.35
NEUTRAL_STYLES = {"", "default", "neutral"}
STYLE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}\Z")
PROSODY = re.compile(r"[+-]\d{1,2}%\Z")


class Synthesizer(Protocol):
    def synthesize(self, ssml: str) -> tuple[bytes, list[dict[str, Any]]]:
        """Return RIFF WAV bytes and SentenceBoundary dicts {type, offset, duration, text} in 100 ns ticks."""


SynthesizerFactory = Callable[[StudioConfig], Synthesizer]


def sdk_version() -> str:
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError:
        return "not-installed"
    return speechsdk.__version__


def build_ssml(text: str, voice: dict[str, Any]) -> str:
    name, style = str(voice.get("name", "")), str(voice.get("style", "")).strip()
    if not re.fullmatch(r"ko-KR-[A-Za-z]+Neural", name):
        raise PipelineError(f"Unsupported Azure voice {name!r}; expected a ko-KR neural voice.")
    for key in ("rate", "pitch"):
        if not PROSODY.fullmatch(str(voice.get(key, ""))):
            raise PipelineError(f"Voice {key} must look like +0%, got {voice.get(key)!r}.")
    body = (f"<prosody rate={quoteattr(voice['rate'])} pitch={quoteattr(voice['pitch'])}>"
            f"{escape(text)}</prosody>")
    if style.lower() not in NEUTRAL_STYLES:
        if not STYLE.fullmatch(style):
            raise PipelineError(f"Voice style {style!r} is not a valid Azure speaking style name.")
        body = f"<mstts:express-as style={quoteattr(style)}>{body}</mstts:express-as>"
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="ko-KR">'
        f"<voice name={quoteattr(name)}>{body}</voice></speak>"
    )


def billed_characters(ssml: str) -> int:
    # Conservative: the whole SSML document, although Azure bills mainly the spoken text.
    return len(ssml)


def sentence_boundary(event: Any) -> dict[str, Any]:
    """Convert an SDK SpeechSynthesisWordBoundaryEventArgs to the edge-tts style boundary dict."""
    duration = event.duration
    ticks = round(duration.total_seconds() * 10_000_000) if hasattr(duration, "total_seconds") else int(duration)
    return {"type": "SentenceBoundary", "offset": int(event.audio_offset), "duration": ticks, "text": event.text}


class AzureSpeechSynthesizer:
    """Speech SDK >= 1.44 synthesizer using Microsoft Entra ID (token_credential) on a custom-domain endpoint."""

    def __init__(self, config: StudioConfig, credential: Any = None):
        try:
            import azure.cognitiveservices.speech as speechsdk
        except ImportError as error:
            raise PipelineError("Install azure-cognitiveservices-speech to synthesize with Azure Speech.") from error
        if not config.speech_endpoint:
            raise PipelineError("Set [speech].endpoint or SPEECH_ENDPOINT to the Speech resource custom-domain endpoint.")
        if credential is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as error:
                raise PipelineError("Install azure-identity for Entra ID authentication to Azure Speech.") from error
            credential = DefaultAzureCredential()
        try:
            speech_config = speechsdk.SpeechConfig(endpoint=config.speech_endpoint, token_credential=credential)
        except TypeError as error:
            raise PipelineError("Installed Speech SDK lacks token_credential support; upgrade to >= 1.44.") from error
        speech_config.set_speech_synthesis_output_format(getattr(speechsdk.SpeechSynthesisOutputFormat, OUTPUT_FORMAT))
        speech_config.set_property(speechsdk.PropertyId.SpeechServiceResponse_RequestSentenceBoundary, "true")
        self._sdk = speechsdk
        self._config = speech_config

    def synthesize(self, ssml: str) -> tuple[bytes, list[dict[str, Any]]]:
        sdk = self._sdk
        synthesizer = sdk.SpeechSynthesizer(speech_config=self._config, audio_config=None)
        boundaries: list[dict[str, Any]] = []

        def on_boundary(event: Any) -> None:
            if event.boundary_type == sdk.SpeechSynthesisBoundaryType.Sentence:
                boundaries.append(sentence_boundary(event))

        synthesizer.synthesis_word_boundary.connect(on_boundary)
        result = synthesizer.speak_ssml_async(ssml).get()
        if result.reason != sdk.ResultReason.SynthesizingAudioCompleted:
            details = getattr(result, "cancellation_details", None)
            reason = getattr(details, "reason", result.reason)
            message = getattr(details, "error_details", "") or "no error details"
            raise PipelineError(f"Azure Speech synthesis did not complete ({reason}): {message}")
        return bytes(result.audio_data), boundaries


def scene_voice(episode: dict[str, Any], scene: dict[str, Any]) -> dict[str, Any]:
    for character in episode["characters"]:
        if character["id"] == scene["pov"]:
            return character["voice"]
    raise PipelineError(f"Scene {scene['id']}: pov {scene['pov']!r} is not a declared character.")


def _check_wav(data: bytes, label: str) -> None:
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            params = (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes())
    except (wave.Error, EOFError) as error:
        raise PipelineError(f"{label}: Azure Speech returned audio that is not RIFF WAV: {error}") from error
    if params[:3] != (SAMPLE_RATE, 1, 2) or params[3] == 0:
        raise PipelineError(f"{label}: expected 24 kHz 16-bit mono WAV with audio, got {params}.")


def synthesize_scene_azure(
    episode: dict[str, Any], scene: dict[str, Any], directory: Path, *, config: StudioConfig,
    ledger: CostLedger, synthesizer_factory: SynthesizerFactory | None = None,
) -> dict[str, Any]:
    identifier = scene["id"]
    voice = scene_voice(episode, scene)
    ssml = build_ssml(scene["narration"], voice)
    version = sdk_version()
    fingerprint = digest({
        "ssml": ssml, "voice": voice, "provider": PROVIDER, "sdk_version": version,
        "format": OUTPUT_FORMAT, "boundary": "Sentence",
    })
    directory.mkdir(parents=True, exist_ok=True)
    cache = directory / f"{identifier}.speech.json"
    wav = directory / f"{identifier}.wav"
    if cache.exists() and wav.exists():
        record = json.loads(cache.read_text(encoding="utf-8"))
        if record.get("fingerprint") == fingerprint and record.get("wav_sha256") == hashlib.sha256(wav.read_bytes()).hexdigest():
            return record
    billed = billed_characters(ssml)
    price = Decimal(billed) * config.speech_usd_per_million_characters / Decimal(1_000_000)
    synthesizer = (synthesizer_factory or AzureSpeechSynthesizer)(config)
    entry = ledger.reserve(f"speech:{identifier}", price, {
        "provider": PROVIDER, "voice": voice["name"], "billed_characters": billed,
    })
    try:
        audio, boundaries = synthesizer.synthesize(ssml)
    except Exception as error:
        ledger.fail(entry, str(error))
        write_json(directory / f"{identifier}.speech-error.json", {
            "scene": identifier, "error": str(error), "provider": PROVIDER, "fallback_used": False,
        })
        raise PipelineError(f"{identifier}: Azure Speech synthesis failed; no fallback is used: {error}") from error
    ledger.settle(entry, price, {"billed_characters": billed, "audio_bytes": len(audio)})
    _check_wav(audio, identifier)
    partial = directory / f"{identifier}.partial.wav"
    partial.write_bytes(audio)
    partial.replace(wav)
    duration = wav_duration(wav)
    captions = captions_from_boundaries(boundaries, duration=duration)
    for cue in captions:
        cue["text"] = balanced_caption(cue["text"])
    captions = improve_caption_readability(captions, duration=duration)
    if normalized_text("".join(cue["text"] for cue in captions)) != normalized_text(scene["narration"]):
        raise PipelineError(f"{identifier}: Azure sentence boundaries do not cover the narration text exactly.")
    write_ass(directory / f"{identifier}.ass", captions)
    write_srt(directory / f"{identifier}.srt", captions)
    record = {
        "fingerprint": fingerprint, "scene": identifier, "voice": voice["name"], "style": voice["style"],
        "rate": voice["rate"], "pitch": voice["pitch"],
        "provider": "Azure AI Speech (Microsoft Entra ID) via azure-cognitiveservices-speech",
        "provider_id": PROVIDER, "sdk_version": version, "ssml_sha256": hashlib.sha256(ssml.encode()).hexdigest(),
        "duration_seconds": duration, "narration_characters": len(scene["narration"]),
        "billed_characters": billed, "captions": captions, "subtitle_text_coverage": 1.0,
        "wav_sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
        "api_billed_amount_usd": float(price), "azure_used": True, "simulated": False,
    }
    write_json(cache, record)
    return record


def synthesize_episode_azure(
    episode: dict[str, Any], directory: Path, *, config: StudioConfig, ledger: CostLedger,
    synthesizer_factory: SynthesizerFactory | None = None,
) -> list[dict[str, Any]]:
    validate_episode(episode)
    factory = synthesizer_factory or AzureSpeechSynthesizer
    created: list[Synthesizer] = []

    def shared(config: StudioConfig) -> Synthesizer:
        if not created:
            created.append(factory(config))
        return created[0]

    return [
        synthesize_scene_azure(episode, scene, directory, config=config, ledger=ledger, synthesizer_factory=shared)
        for scene in episode["scenes"]
    ]


def measure_episode_duration(
    speech: list[dict[str, Any]], *, minimum: float = 1200.0, maximum: float = 1800.0,
    pad_seconds: float = SCENE_PAD_SECONDS,
) -> dict[str, Any]:
    """Structured duration verdict (never raises) so the orchestrator can loop back to the script."""
    narration = sum(item["duration_seconds"] for item in speech)
    total = narration + pad_seconds * len(speech)
    characters = sum(item["narration_characters"] for item in speech)
    rate = characters / narration if narration else 0.0
    pads = pad_seconds * len(speech)
    action = "lengthen" if total < minimum else "shorten" if total > maximum else "none"
    return {
        "narration_seconds": round(narration, 3), "episode_seconds": round(total, 3),
        "minimum_seconds": minimum, "maximum_seconds": maximum, "within_target": action == "none",
        "action": action,
        "seconds_outside_target": round(minimum - total if action == "lengthen" else total - maximum if action == "shorten" else 0.0, 3),
        "narration_characters": characters, "characters_per_second": round(rate, 3),
        "suggested_character_range": [max(0, math.floor(rate * (minimum - pads))), max(0, math.floor(rate * (maximum - pads)))],
        "scenes": [{"scene": item["scene"], "seconds": item["duration_seconds"]} for item in speech],
    }