"""Licensed music/SFX selection and the episode mix (ducked music bed, SFX, two-pass loudnorm)."""
from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any

from ..media import execute, ffmpeg_executable, wav_duration, write_json
from ..models import PipelineError, digest
from .render_v3 import SCENE_PAD_SECONDS, compile_shot_timeline_v3, scene_starts

MIXER = "studio-mix-v1-sidechain-loudnorm-I-14-TP-1.5"
TARGET_I, TARGET_TP, TARGET_LRA = -14.0, -1.5, 11.0
LICENSE_FIELDS = ("source", "license_id", "proof_url_or_file")


def load_library(directory: Path, kind: str) -> list[dict[str, Any]]:
    """Load ``library.json``; any entry without verifiable commercial-use licensing rejects the library."""
    if kind not in ("music", "sfx"):
        raise PipelineError("Library kind must be music or sfx.")
    labels = "moods" if kind == "music" else "tags"
    index = directory / "library.json"
    if not index.is_file():
        raise PipelineError(f"{kind} library index missing: {index}")
    raw = json.loads(index.read_text(encoding="utf-8-sig"))
    entries = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise PipelineError(f"{index}: expected a nonempty list of entries.")
    root = directory.resolve()
    problems, accepted = [], []
    for position, entry in enumerate(entries):
        name = entry.get("file") if isinstance(entry, dict) else None
        label = f"#{position} {name or '?'}"
        if not isinstance(name, str) or not name:
            problems.append(f"{label}: file missing")
            continue
        path = (root / name).resolve()
        license_ = entry.get("license")
        if not path.is_relative_to(root) or not path.is_file():
            problems.append(f"{label}: audio file not found inside the library")
        if not isinstance(license_, dict) or any(
                not isinstance(license_.get(f), str) or not license_[f].strip() for f in LICENSE_FIELDS):
            problems.append(f"{label}: license needs {', '.join(LICENSE_FIELDS)}")
        elif license_.get("commercial_use") is not True:
            problems.append(f"{label}: license.commercial_use must be true")
        elif not re.match(r"https?://", license_["proof_url_or_file"]) and not (root / license_["proof_url_or_file"]).is_file():
            problems.append(f"{label}: license proof file not found")
        if not isinstance(entry.get("attribution"), str):
            problems.append(f"{label}: attribution text required (may be empty if the license needs none)")
        values = entry.get(labels)
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
            problems.append(f"{label}: {labels} must be a nonempty list of strings")
        accepted.append({**entry, "path": str(path)})
    if problems:
        raise PipelineError(f"Rejected {kind} library {index}: " + "; ".join(problems))
    return accepted


def _rng(*parts: str) -> random.Random:
    return random.Random(int(hashlib.sha256("\x1f".join(parts).encode()).hexdigest(), 16))


def choose_music(episode: dict[str, Any], library: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Deterministic per-scene track by mood (seeded by episode id); consecutive same-mood scenes share a track."""
    rng = _rng(episode["id"], "music")
    chosen: dict[str, dict[str, Any]] = {}
    previous: tuple[str, dict[str, Any]] | None = None
    for scene in episode["scenes"]:
        candidates = sorted((e for e in library if scene["mood"] in e["moods"]), key=lambda e: e["file"])
        if not candidates:
            moods = sorted({m for e in library for m in e["moods"]})
            raise PipelineError(f"Scene {scene['id']}: no licensed music for mood {scene['mood']!r}; library moods: {moods}.")
        track = previous[1] if previous and previous[0] == scene["mood"] else rng.choice(candidates)
        chosen[scene["id"]] = track
        previous = (scene["mood"], track)
    return chosen


def place_sfx(
    episode: dict[str, Any], timelines: dict[str, list[dict[str, Any]]], starts: dict[str, float],
    library: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    available = sorted({t for e in library for t in e["tags"]})
    unknown = sorted({tag for scene in episode["scenes"] for shot in scene["shots"] for tag in shot["sfx"]} - set(available))
    if unknown:
        raise PipelineError(f"Unknown sfx tags {unknown}; available tags: {available}.")
    placements = []
    for scene in episode["scenes"]:
        for shot in timelines[scene["id"]]:
            for tag in shot["sfx"]:
                candidates = sorted((e for e in library if tag in e["tags"]), key=lambda e: e["file"])
                entry = _rng(episode["id"], scene["id"], shot["id"], tag).choice(candidates)
                placements.append({"scene": scene["id"], "shot": shot["id"], "tag": tag,
                                   "time": round(starts[scene["id"]] + shot["start"], 3), "entry": entry})
    return placements


def _loudnorm(ffmpeg: str, source: Path, work: Path, name: str, extra: str = "") -> dict[str, Any]:
    target = f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}{extra}:print_format=json"
    output = execute(
        [ffmpeg, "-hide_banner", "-i", str(source), "-af", target, "-f", "null", "-"]
        if not extra else
        [ffmpeg, "-y", "-hide_banner", "-i", str(source), "-af", target, "-ar", "48000", "-c:a", "pcm_s16le",
         str(work / f"{name}.wav")],
        cwd=work, log=work / f"{name}.loudnorm.log",
    )
    matches = re.findall(r'\{\s*"input_i".*?\}', output, flags=re.S)
    if not matches:
        raise PipelineError("FFmpeg loudnorm returned no measurements.")
    return json.loads(matches[-1])


def mix_episode(
    episode: dict[str, Any], speech: list[dict[str, Any]], scenes_dir: Path, output: Path, *,
    music_dir: Path, sfx_dir: Path, music_gain_db: float = -18.0, sfx_gain_db: float = -6.0,
) -> dict[str, Any]:
    """Narration (+0.35 s scene pads) over a ducked, scene-faded music bed with SFX, normalized to -14 LUFS."""
    if [r["scene"] for r in speech] != [s["id"] for s in episode["scenes"]]:
        raise PipelineError("Speech records must match episode scenes in order.")
    music = choose_music(episode, load_library(music_dir, "music"))
    cast = {c["id"] for c in episode["characters"]}
    timelines = {s["id"]: compile_shot_timeline_v3(s, r, cast) for s, r in zip(episode["scenes"], speech, strict=True)}
    starts = dict(zip((s["id"] for s in episode["scenes"]), scene_starts(speech)))
    has_sfx = any(shot["sfx"] for scene in episode["scenes"] for shot in scene["shots"])
    sfx = place_sfx(episode, timelines, starts, load_library(sfx_dir, "sfx")) if has_sfx else []
    voices = [scenes_dir / f"{s['id']}.wav" for s in episode["scenes"]]
    lengths = [wav_duration(p) + SCENE_PAD_SECONDS for p in voices]
    total = sum(lengths)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    final = output / "mix.wav"
    record_path = output / "audio-mix.json"
    fingerprint = digest({
        "voices": [hashlib.sha256(p.read_bytes()).hexdigest() for p in voices],
        "music": [hashlib.sha256(Path(music[s["id"]]["path"]).read_bytes()).hexdigest() for s in episode["scenes"]],
        "sfx": [(p["time"], hashlib.sha256(Path(p["entry"]["path"]).read_bytes()).hexdigest()) for p in sfx],
        "gains": [music_gain_db, sfx_gain_db], "mixer": MIXER,
    })
    if final.exists() and record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("fingerprint") == fingerprint and record.get("sha256") == hashlib.sha256(final.read_bytes()).hexdigest():
            return record
    ffmpeg = ffmpeg_executable()
    count = len(voices)
    inputs: list[str] = []
    filters: list[str] = []
    stereo = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    for index, (path, length) in enumerate(zip(voices, lengths)):
        inputs += ["-i", str(path.resolve())]
        filters.append(f"[{index}:a]{stereo},apad=whole_dur={length:.6f},atrim=duration={length:.6f}[n{index}]")
    for index, (scene, length) in enumerate(zip(episode["scenes"], lengths)):
        inputs += ["-stream_loop", "-1", "-i", music[scene["id"]]["path"]]
        fade = min(1.5, length / 4)
        filters.append(
            f"[{count + index}:a]atrim=duration={length:.6f},asetpts=PTS-STARTPTS,{stereo},volume={music_gain_db}dB,"
            f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={length - fade:.6f}:d={fade:.3f}[m{index}]"
        )
    filters.append("".join(f"[n{i}]" for i in range(count)) + f"concat=n={count}:v=0:a=1,asplit=2[voice][key]")
    filters.append("".join(f"[m{i}]" for i in range(count)) + f"concat=n={count}:v=0:a=1[bed]")
    filters.append("[bed][key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=400[ducked]")
    labels = ["[voice]", "[ducked]"]
    for index, placement in enumerate(sfx):
        inputs += ["-i", placement["entry"]["path"]]
        delay = round(placement["time"] * 1000)
        filters.append(f"[{2 * count + index}:a]{stereo},volume={sfx_gain_db}dB,adelay=delays={delay}:all=1[s{index}]")
        labels.append(f"[s{index}]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0,"
                   f"atrim=duration={total:.6f}[mix]")
    script = output / "mix.filter"
    script.write_text(";\n".join(filters), encoding="utf-8")
    execute(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", *inputs, "-/filter_complex", str(script),
         "-map", "[mix]", "-ar", "48000", "-c:a", "pcm_s16le", str(output / "premix.wav")],
        cwd=output, log=output / "mix.log",
    )
    first = _loudnorm(ffmpeg, output / "premix.wav", output, "measure")
    second = _loudnorm(ffmpeg, output / "premix.wav", output, "mix.normalizing", (
        f":measured_I={first['input_i']}:measured_TP={first['input_tp']}:measured_LRA={first['input_lra']}"
        f":measured_thresh={first['input_thresh']}:offset={first['target_offset']}:linear=true"
    ))
    (output / "mix.normalizing.wav").replace(final)
    used = {e["file"]: e for e in [*music.values(), *(p["entry"] for p in sfx)]}
    record = {
        "fingerprint": fingerprint, "file": str(final), "sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
        "duration_seconds": round(wav_duration(final), 4), "expected_duration_seconds": round(total, 4),
        "scene_starts": starts, "mixer": MIXER,
        "music": [{"scene": s, "file": e["file"], "moods": e["moods"]} for s, e in music.items()],
        "sfx": [{k: p[k] for k in ("scene", "shot", "tag", "time")} | {"file": p["entry"]["file"]} for p in sfx],
        "loudness": {"target_i": TARGET_I, "target_tp": TARGET_TP, "first_pass": first,
                     "output_i": float(second["output_i"]), "output_tp": float(second["output_tp"]),
                     "normalization_type": second.get("normalization_type")},
        "licenses": [{"file": f, **e["license"]} for f, e in sorted(used.items())],
        "attributions": sorted({e["attribution"] for e in used.values() if e["attribution"].strip()}),
        "simulated": False,
    }
    if abs(record["duration_seconds"] - total) > 0.05:
        raise PipelineError(f"Mixed audio is {record['duration_seconds']}s; expected {total:.3f}s.")
    write_json(record_path, record)
    return record
