"""Episode v3 renderer: generated shot frames + mixed audio + burned captions -> YouTube-ready MP4."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..fonts import font_family, korean_font
from ..media import (
    FRAME_RATE, execute, ffmpeg_executable, normalized_text, probe_duration, write_ass, write_json, write_srt,
)
from ..models import PipelineError, digest
from .episode_v3 import validate_shots

SCENE_PAD_SECONDS = 0.35
RESOLUTIONS = {(1280, 720), (1920, 1080)}
RENDERER = "studio-v3-kenburns-24fps-x264-veryfast-crf20"
TARGET_LUFS = -14.0


def anchor_seconds(narration: str, captions: list[dict[str, Any]], anchor: str, *, end: bool = False) -> float:
    """Map a narration anchor to speech time: service cue start, proportional within the cue."""
    source = normalized_text(narration)
    if normalized_text("".join(cue["text"] for cue in captions)) != source:
        raise PipelineError("Cannot align anchors to speech whose text differs from the narration.")
    needle = normalized_text(anchor)
    if not needle or source.count(needle) != 1:
        raise PipelineError(f"Anchor {anchor!r} is absent or ambiguous in the narration.")
    offset = source.index(needle) + (len(needle) if end else 0)
    consumed = 0
    for cue in captions:
        count = len(normalized_text(cue["text"]))
        if count and (offset < consumed + count or (end and offset == consumed + count)):
            return cue["start"] + (cue["end"] - cue["start"]) * (offset - consumed) / count
        consumed += count
    raise PipelineError(f"Anchor {anchor!r} could not be mapped to speech timing.")


def compile_shot_timeline_v3(
    scene: dict[str, Any], speech: dict[str, Any], characters: set[str] | None = None,
) -> list[dict[str, Any]]:
    """v3 equivalent of media.compile_shot_timeline (scene-local seconds, frame-rounded)."""
    validate_shots(scene, characters if characters is not None else
                   {c for shot in scene["shots"] for c in shot.get("characters", [])})
    total_frames = math.ceil((speech["duration_seconds"] + SCENE_PAD_SECONDS) * FRAME_RATE)
    positions: list[int] = []
    for index, shot in enumerate(scene["shots"]):
        seconds = anchor_seconds(scene["narration"], speech["captions"], shot["anchor"])
        frame = 0 if index == 0 else round(seconds * FRAME_RATE)
        if frame >= total_frames or (positions and frame <= positions[-1]):
            raise PipelineError(f"Scene {scene['id']}: storyboard shots collapsed onto the same video frame.")
        positions.append(frame)
    timeline = []
    for index, shot in enumerate(scene["shots"]):
        start = positions[index]
        end = positions[index + 1] if index + 1 < len(positions) else total_frames
        if end - start < FRAME_RATE:
            raise PipelineError(f"{scene['id']}/{shot['id']}: sub-second shot; revise its anchors.")
        timeline.append({
            **shot, "scene": scene["id"], "start": start / FRAME_RATE, "end": end / FRAME_RATE,
            "frames": end - start, "duration_seconds": (end - start) / FRAME_RATE,
            "alignment": "service caption start; proportional within cue; rounded to video frame",
        })
    return timeline


def scene_starts(speech: list[dict[str, Any]]) -> list[float]:
    starts, elapsed = [], 0.0
    for record in speech:
        starts.append(elapsed)
        elapsed += record["duration_seconds"] + SCENE_PAD_SECONDS
    return starts


def episode_timeline(episode: dict[str, Any], speech: list[dict[str, Any]]) -> dict[str, Any]:
    """Global shot timeline whose shot boundaries are rounded once on the episode frame grid."""
    if [r["scene"] for r in speech] != [s["id"] for s in episode["scenes"]]:
        raise PipelineError("Speech records must match episode scenes in order.")
    cast = {c["id"] for c in episode["characters"]}
    starts = scene_starts(speech)
    total = starts[-1] + speech[-1]["duration_seconds"] + SCENE_PAD_SECONDS
    shots, captions = [], []
    for scene, record, start in zip(episode["scenes"], speech, starts, strict=True):
        for shot in compile_shot_timeline_v3(scene, record, cast):
            shots.append({**shot, "global_start": start + shot["start"]})
        captions.extend({**cue, "scene": scene["id"], "start": round(cue["start"] + start, 4),
                         "end": round(cue["end"] + start, 4)} for cue in record["captions"])
    total_frames = round(total * FRAME_RATE)
    frames = [round(shot["global_start"] * FRAME_RATE) for shot in shots] + [total_frames]
    for index, shot in enumerate(shots):
        shot["global_frame"] = frames[index]
        shot["global_frames"] = frames[index + 1] - frames[index]
        shot["global_end"] = frames[index + 1] / FRAME_RATE
        if shot["global_frames"] < FRAME_RATE:
            raise PipelineError(f"{shot['scene']}/{shot['id']}: sub-second shot after frame rounding.")
    return {"shots": shots, "captions": captions, "scene_starts": starts, "duration_seconds": total_frames / FRAME_RATE}


def _motion(index: int, frames: int) -> str:
    """Deterministic Ken Burns variety: zoom in, zoom out, pan right, pan left."""
    progress = f"on/{max(1, frames - 1)}"
    return [
        f"z='1+0.06*{progress}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'",
        f"z='1.06-0.06*{progress}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'",
        f"z='1.06':x='(iw-iw/zoom)*{progress}':y='ih/2-ih/zoom/2'",
        f"z='1.06':x='(iw-iw/zoom)*(1-{progress})':y='ih/2-ih/zoom/2'",
    ][index % 4]


def _render_scene(
    ffmpeg: str, scene_id: str, shots: list[dict[str, Any]], frames_dir: Path, work: Path,
    size: tuple[int, int], preset: str,
) -> Path:
    width, height = size
    images = [frames_dir / f"{scene_id}-shot-{shot['id']}.png" for shot in shots]
    missing = [str(p) for p in images if not p.is_file()]
    if missing:
        raise PipelineError(f"Scene {scene_id}: missing shot frames {missing}.")
    ass = work / f"{scene_id}.ass"
    key = digest({
        "art": [hashlib.sha256(p.read_bytes()).hexdigest() for p in images],
        "shots": [(s["id"], s["global_frames"]) for s in shots], "size": size, "preset": preset,
        "subtitles": hashlib.sha256(ass.read_bytes()).hexdigest(), "renderer": RENDERER,
    })
    video, record_path = work / f"{scene_id}.mp4", work / f"{scene_id}.render.json"
    if video.exists() and record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("fingerprint") == key and record.get("sha256") == hashlib.sha256(video.read_bytes()).hexdigest():
            return video
    inputs, filters = [], []
    source = f"{width * 3 // 2}:{height * 3 // 2}"
    for index, (image, shot) in enumerate(zip(images, shots, strict=True)):
        inputs.extend(["-i", str(image)])
        filters.append(
            f"[{index}:v]scale={source},zoompan={_motion(index, shot['global_frames'])}:"
            f"d={shot['global_frames']}:s={width}x{height}:fps={FRAME_RATE},setsar=1,setpts=PTS-STARTPTS[v{index}]"
        )
    joins = "".join(f"[v{index}]" for index in range(len(images)))
    filters.append(f"{joins}concat=n={len(images)}:v=1:a=0,subtitles={scene_id}.ass:fontsdir=fonts,format=yuv420p[vout]")
    frames = sum(shot["global_frames"] for shot in shots)
    execute(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", *inputs,
         "-filter_complex", ";".join(filters), "-map", "[vout]", "-frames:v", str(frames), "-r", str(FRAME_RATE),
         "-c:v", "libx264", "-preset", preset, "-crf", "20", "-pix_fmt", "yuv420p", "-an",
         f"{scene_id}.rendering.mp4"],
        cwd=work, log=work / f"{scene_id}.render.log",
    )
    (work / f"{scene_id}.rendering.mp4").replace(video)
    write_json(record_path, {"fingerprint": key, "frames": frames, "sha256": hashlib.sha256(video.read_bytes()).hexdigest()})
    return video


def render_episode_v3(
    episode: dict[str, Any], speech: list[dict[str, Any]], audio: Path, frames_dir: Path, output: Path, *,
    resolution: tuple[int, int] = (1920, 1080), preset: str = "veryfast",
    duration_range: tuple[float, float] = (1200.0, 1800.0),
) -> dict[str, Any]:
    if tuple(resolution) not in RESOLUTIONS:
        raise PipelineError(f"Resolution must be one of {sorted(RESOLUTIONS)}.")
    output = output.resolve()
    work = output / "render"
    fonts = work / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    font = korean_font()
    family = font_family(font)
    shutil.copyfile(font, fonts / font.name)
    ffmpeg = ffmpeg_executable()
    plan = episode_timeline(episode, speech)
    audio_seconds = probe_duration(ffmpeg, audio)
    if abs(audio_seconds - plan["duration_seconds"]) > 0.1:
        raise PipelineError(
            f"Mixed audio is {audio_seconds:.2f}s but the speech timeline is {plan['duration_seconds']:.2f}s; remix."
        )
    segments = []
    for scene, record in zip(episode["scenes"], speech, strict=True):
        shots = [shot for shot in plan["shots"] if shot["scene"] == scene["id"]]
        offset = shots[0]["global_frame"] / FRAME_RATE
        local = [{**cue, "start": max(0.0, cue["start"] - offset), "end": cue["end"] - offset}
                 for cue in plan["captions"] if cue["scene"] == scene["id"]]
        write_ass(work / f"{scene['id']}.ass", local, font_name=family)
        segments.append(_render_scene(ffmpeg, scene["id"], shots, frames_dir.resolve(), work, tuple(resolution), preset))
    (work / "concat.txt").write_text("\n".join(f"file '{p.name}'" for p in segments) + "\n", encoding="utf-8")
    execute([ffmpeg, "-y", "-v", "warning", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy",
             "video.mp4"], cwd=work, log=work / "concat.log")
    metadata = [";FFMETADATA1", "title=" + _meta(episode["title"]), "comment=" + _meta(episode["fiction_notice"])]
    starts = plan["scene_starts"] + [plan["duration_seconds"]]
    for index, scene in enumerate(episode["scenes"]):
        metadata.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(starts[index] * 1000)}",
                         f"END={round(starts[index + 1] * 1000)}", "title=" + _meta(scene["title"])])
    (work / "chapters.ffmeta").write_text("\n".join(metadata) + "\n", encoding="utf-8")
    execute(
        [ffmpeg, "-y", "-v", "warning", "-i", "video.mp4", "-i", str(audio.resolve()), "-i", "chapters.ffmeta",
         "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "2", "-map_chapters", "2", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{plan['duration_seconds']:.3f}",
         "-movflags", "+faststart", str(output / "episode.rendering.mp4")],
        cwd=work, log=work / "mux.log",
    )
    (output / "episode.rendering.mp4").replace(output / "episode.mp4")
    write_srt(output / "episode.ko.srt", plan["captions"])
    write_json(output / "captions.json", plan["captions"])
    write_json(output / "shot-timeline.json", [
        {**shot, "image": str((frames_dir / f"{shot['scene']}-shot-{shot['id']}.png").resolve())} for shot in plan["shots"]
    ])
    shutil.rmtree(fonts, ignore_errors=True)
    evaluation = technical_checks(ffmpeg, output / "episode.mp4", plan["captions"], output,
                                  resolution=tuple(resolution), duration_range=duration_range)
    return {"video": str(output / "episode.mp4"), "scene_starts": plan["scene_starts"],
            "duration_seconds": plan["duration_seconds"], "evaluation": evaluation,
            "font_family": family, "renderer": RENDERER}


def _meta(text: str) -> str:
    return re.sub(r"([=;#\\])", r"\\\1", text).replace("\n", " ")


def measure_loudness(ffmpeg: str, media: Path, directory: Path, name: str) -> dict[str, Any]:
    output = execute(
        [ffmpeg, "-hide_banner", "-i", str(media), "-vn",
         "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        cwd=directory, log=directory / f"{name}.loudness.log",
    )
    matches = re.findall(r'\{\s*"input_i".*?\}', output, flags=re.S)
    if not matches:
        raise PipelineError("FFmpeg returned no loudness measurements.")
    result = json.loads(matches[-1])
    if not math.isfinite(float(result["input_i"])):
        raise PipelineError("Audio is silent or unmeasurable.")
    return result


def technical_checks(
    ffmpeg: str, video: Path, captions: list[dict[str, Any]], output: Path, *,
    resolution: tuple[int, int] = (1920, 1080), duration_range: tuple[float, float] = (1200.0, 1800.0),
) -> dict[str, Any]:
    duration = probe_duration(ffmpeg, video)
    probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30, check=False).stderr
    dimensions = re.search(r"Video: h264[^\r\n]*? (\d{3,5})x(\d{3,5})", probe)
    rate = re.search(r"Video: h264[^\r\n]*? ([\d.]+) fps", probe)
    streams_ok = bool(dimensions and rate and "Audio: aac" in probe)
    width, height = map(int, dimensions.groups()) if dimensions else (0, 0)
    try:
        execute([ffmpeg, "-v", "error", "-xerror", "-i", str(video), "-f", "null", "-"],
                cwd=output, log=output / "decode-check.log")
        decoded = True
    except PipelineError:
        decoded = False
    loudness = measure_loudness(ffmpeg, video, output, "final")
    integrated, peak = float(loudness["input_i"]), float(loudness["input_tp"])
    overlaps = [i for i in range(1, len(captions)) if captions[i]["start"] < captions[i - 1]["end"] - 0.02]
    outside = [i for i, cue in enumerate(captions) if cue["start"] < 0 or cue["end"] > duration + 0.1]
    checks = {
        "stream_format_pass": streams_ok and (width, height) == tuple(resolution)
                              and abs(float(rate.group(1)) - FRAME_RATE) < 0.01,
        "decode_all_frames_pass": decoded,
        "target_duration_pass": duration_range[0] <= duration <= duration_range[1],
        "loudness_pass": abs(integrated - TARGET_LUFS) <= 1.5,
        "true_peak_pass": peak <= -1.0,
        "subtitle_timing_pass": not overlaps and not outside,
    }
    result = {
        "simulated": False, "file": str(video), "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "duration_seconds": duration, "duration_range_seconds": list(duration_range),
        "video_width": width, "video_height": height, "measured_loudness_lufs": integrated,
        "measured_true_peak_dbtp": peak, "target_loudness_lufs": TARGET_LUFS,
        "subtitle_count": len(captions), "subtitle_overlaps": overlaps, "subtitle_out_of_bounds": outside,
        **checks, "technical_pass": all(checks.values()),
        "limitations": ["Technical checks do not judge story quality, acting, or audience retention."],
    }
    write_json(output / "evaluation.json", result)
    return result
