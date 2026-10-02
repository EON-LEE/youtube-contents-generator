from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .media import execute, ffmpeg_executable, normalized_text, write_json
from .models import PipelineError


def edit_distance(reference: str, actual: str) -> int:
    previous = list(range(len(actual) + 1))
    for row, left in enumerate(reference, start=1):
        current = [row]
        for column, right in enumerate(actual, start=1):
            current.append(min(
                current[-1] + 1, previous[column] + 1,
                previous[column - 1] + (left != right),
            ))
        previous = current
    return previous[-1]


def evaluate_speech(
    output: Path, *, model_name: str = "small", allow_model_download: bool = False,
) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise PipelineError("Install the evaluation extra to run local speech recognition.") from error
    output = output.resolve()
    samples = output / "speech-review"
    samples.mkdir(exist_ok=True)
    model = WhisperModel(
        model_name, device="cpu", compute_type="int8", cpu_threads=2, num_workers=1,
        download_root=str(output.parent / ".models"), local_files_only=not allow_model_download,
        use_auth_token=False,
    )
    ffmpeg = ffmpeg_executable()
    results = []
    for identifier in ("01", "03", "11"):
        source = output / "scenes" / f"{identifier}.speech.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        cues = [cue for cue in record["captions"] if cue["end"] <= 35]
        if not cues:
            raise PipelineError(f"No short evaluation excerpt in scene {identifier}.")
        stop = cues[-1]["end"]
        clip = samples / f"{identifier}.wav"
        execute(
            [ffmpeg, "-y", "-v", "error", "-i", str(output / "scenes" / f"{identifier}.wav"),
             "-t", str(stop), "-ar", "16000", "-ac", "1", str(clip)],
            cwd=samples, log=samples / f"{identifier}.extract.log",
        )
        segments, info = model.transcribe(
            str(clip), language="ko", beam_size=5, vad_filter=False,
            condition_on_previous_text=False, temperature=0,
        )
        # No reference text prompt: supplying it would bias this cross-check.
        transcript = " ".join(segment.text.strip() for segment in segments)
        reference = " ".join(cue["text"].replace("\n", " ") for cue in cues)
        expected = normalized_text(reference)
        observed = normalized_text(transcript)
        errors = edit_distance(expected, observed)
        results.append({
            "scene": identifier, "voice": record["voice"], "seconds": stop,
            "reference": reference, "transcript": transcript, "language": info.language,
            "normalized_character_error_rate": round(errors / max(1, len(expected)), 4),
            "normalized_character_count": len(expected), "edit_distance": errors,
        })
    total_characters = sum(item["normalized_character_count"] for item in results)
    report = {
        "method": "Local faster-whisper recognition; audio is not uploaded.",
        "model": model_name, "device": "cpu", "compute_type": "int8",
        "reference_prompt_supplied": False,
        "samples": results,
        "sampled_seconds": round(sum(item["seconds"] for item in results), 2),
        "weighted_normalized_character_error_rate": round(
            sum(item["edit_distance"] for item in results) / max(1, total_characters), 4
        ),
        "limitations": [
            "ASR errors include recognizer mistakes, especially names and homophones.",
            "Sampled original narration WAV, not proof of every word or final-video lip/beat timing.",
            "This does not measure natural acting, emotional delivery, human satisfaction or viral potential.",
        ],
    }
    write_json(output / "speech-evaluation.json", report)
    return report
