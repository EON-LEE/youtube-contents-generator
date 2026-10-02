from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
import shutil
import subprocess
import textwrap
import time
import wave
from pathlib import Path
from typing import Any

from .models import PipelineError, canonical, digest

VOICES = {"mother": "ko-KR-SunHiNeural", "son": "ko-KR-InJoonNeural"}
VISUALS = {"door", "kitchen", "letter", "bus", "workshop", "rain", "table", "garden", "station", "window"}
FRAME_RATE = 24
SHOT_SUBJECTS = {
    "wide", "mother", "son", "hands", "keys", "phone", "schedule", "payslip",
    "envelope", "sewing", "apron", "room", "bus", "calendar",
}
SHOT_EMOTIONS = {"concerned", "neutral", "softened"}
SHOT_DETAILS = {
    "hands": {"clasped_hands", "calendar_point", "bag_handle", "unpick_seam", "order_note", "key_handover", "order_complete"},
    "phone": {"message", "timetable", "face_down", "call"},
    "room": {"damp", "dry"},
    "apron": {"fitting", "repaired_flat", "repaired_worn"},
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_episode(path: Path) -> dict[str, Any]:
    episode = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(episode, dict):
        raise PipelineError("Episode must be a JSON object.")
    for name in ("id", "title", "fiction_notice"):
        if not isinstance(episode.get(name), str) or not episode[name].strip():
            raise PipelineError(f"Missing episode {name}.")
    scenes = episode.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise PipelineError("Episode must contain scenes.")
    identifiers = set()
    for scene in scenes:
        if not isinstance(scene, dict) or not re.fullmatch(r"\d{2}", str(scene.get("id", ""))):
            raise PipelineError("Scene IDs must be two digits.")
        if scene["id"] in identifiers:
            raise PipelineError("Duplicate scene ID.")
        identifiers.add(scene["id"])
        if scene.get("pov") not in VOICES or scene.get("visual") not in VISUALS:
            raise PipelineError(f"Unknown voice perspective or visual: {scene['id']}")
        for name in ("title", "narration"):
            if not isinstance(scene.get(name), str) or not scene[name].strip():
                raise PipelineError(f"Missing scene {name}: {scene['id']}")
        if "shots" in scene:
            validate_storyboard(scene)
    return episode


def ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise PipelineError("Install the media extra in the local virtual environment.") from error
    return imageio_ffmpeg.get_ffmpeg_exe()


def execute(command: list[str], *, cwd: Path, log: Path, timeout: int = 1800) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PipelineError(f"Media command failed to execute: {error}") from error
    output = result.stdout + result.stderr
    log.write_text(output, encoding="utf-8")
    if result.returncode:
        raise PipelineError(f"Media command exited {result.returncode}. See {log}: {output[-2000:]}")
    return output


def probe_duration(ffmpeg: str, path: Path) -> float:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise PipelineError(f"Could not measure media duration: {path}")
    hours, minutes, seconds = map(float, match.groups())
    return hours * 3600 + minutes * 60 + seconds


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def normalized_text(text: str) -> str:
    return "".join(char for char in html.unescape(text) if char.isalnum())


def balanced_caption(text: str) -> str:
    plain = " ".join(text.split())
    if len(plain) <= 24:
        return plain
    layouts = [
        textwrap.wrap(plain, width=width, break_long_words=True, break_on_hyphens=False)
        for width in range(12, 25)
    ]
    choices = [lines for lines in layouts if len(lines) == 2]
    if choices:
        return "\n".join(min(choices, key=lambda lines: abs(len(lines[0]) - len(lines[1]))))
    return text


def reconcile_caption_timing(captions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    adjusted = [dict(cue) for cue in captions]
    for previous, following in zip(adjusted, adjusted[1:]):
        overlap = previous["end"] - following["start"]
        if overlap > 0:
            if overlap > 0.1 or following["start"] <= previous["start"]:
                raise PipelineError("Speech boundaries overlap materially; manual timing review required.")
            previous["end"] = following["start"]
            previous["service_boundary_end_trim_seconds"] = round(overlap, 4)
    return adjusted


def improve_caption_readability(
    captions: list[dict[str, Any]], *, duration: float, minimum_seconds: float = 1.1,
) -> list[dict[str, Any]]:
    """Extend only into silence or merge adjacent short cues without losing text."""
    result = reconcile_caption_timing(captions)
    index = 0
    while index < len(result):
        cue = result[index]
        if cue["end"] - cue["start"] >= minimum_seconds - 0.001:
            index += 1
            continue
        next_start = result[index + 1]["start"] if index + 1 < len(result) else duration
        available_end = min(next_start, duration, cue["end"] + 0.6)
        if available_end - cue["start"] >= minimum_seconds:
            cue["original_end_before_readability"] = cue["end"]
            cue["end"] = round(cue["start"] + minimum_seconds, 4)
            cue["readability_adjustment"] = "extended_into_existing_silence"
            index += 1
            continue
        neighbors = [n for n in (index - 1, index + 1) if 0 <= n < len(result)]
        neighbors.sort(key=lambda n: len(result[n]["text"]))
        merged = False
        for neighbor in neighbors:
            low, high = sorted((index, neighbor))
            first, second = result[low], result[high]
            if second["start"] - first["end"] > 1.0:
                continue
            text = " ".join((first["text"] + " " + second["text"]).split())
            wrapped = textwrap.wrap(text, width=24, break_long_words=True, break_on_hyphens=False)
            if len(wrapped) > 2:
                continue
            replacement = {
                **first, "end": second["end"], "text": balanced_caption(text),
                "readability_adjustment": "merged_adjacent_cues",
                "merged_cue_count": first.get("merged_cue_count", 1) + second.get("merged_cue_count", 1),
            }
            result[low:high + 1] = [replacement]
            index = low
            merged = True
            break
        if not merged:
            cue["readability_warning"] = "short_cue_cannot_be_extended_without_timing_or_layout_damage"
            index += 1
    if normalized_text("".join(cue["text"] for cue in result)) != normalized_text("".join(cue["text"] for cue in captions)):
        raise PipelineError("Readability adjustment changed subtitle content.")
    return result


def validate_storyboard(scene: dict[str, Any]) -> None:
    shots = scene["shots"]
    if not isinstance(shots, list) or not 2 <= len(shots) <= 8:
        raise PipelineError("A storyboard must have 2-8 explicitly directed shots.")
    text = normalized_text(scene["narration"])
    previous = -1
    identifiers = set()
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict) or not re.fullmatch(r"\d{2}", str(shot.get("id", ""))):
            raise PipelineError("Shot IDs must be two digits.")
        if shot["id"] in identifiers:
            raise PipelineError("Duplicate shot ID within a scene.")
        identifiers.add(shot["id"])
        if shot.get("subject") not in SHOT_SUBJECTS or shot.get("emotion") not in SHOT_EMOTIONS:
            raise PipelineError("Unrecognized storyboard subject or emotion.")
        if shot["subject"] in SHOT_DETAILS and shot.get("detail") not in SHOT_DETAILS[shot["subject"]]:
            raise PipelineError(f"{shot['subject']}: an explicit supported prop/action detail is required.")
        if not isinstance(shot.get("anchor"), str) or not isinstance(shot.get("reason"), str) or not shot["reason"].strip():
            raise PipelineError("Every shot requires an exact narration anchor and editorial reason.")
        anchor = normalized_text(shot["anchor"])
        if len(anchor) < 4 or text.count(anchor) != 1:
            raise PipelineError(f"{scene['id']}/{shot['id']}: narration anchor is absent or ambiguous.")
        location = text.index(anchor)
        if location <= previous or (index == 0 and location != 0):
            raise PipelineError("Shot anchors must start with the narration and proceed in order.")
        previous = location


def compile_shot_timeline(scene: dict[str, Any], speech: dict[str, Any]) -> list[dict[str, Any]]:
    validate_storyboard(scene)
    cues = speech["captions"]
    source = normalized_text(scene["narration"])
    if normalized_text("".join(cue["text"] for cue in cues)) != source:
        raise PipelineError("Cannot align storyboard to different speech text.")
    total_frames = math.ceil((speech["duration_seconds"] + 0.35) * FRAME_RATE)
    positions = []
    for index, shot in enumerate(scene["shots"]):
        offset = source.index(normalized_text(shot["anchor"]))
        consumed = 0
        seconds = None
        for cue in cues:
            count = len(normalized_text(cue["text"]))
            if count and offset < consumed + count:
                seconds = cue["start"] + (cue["end"] - cue["start"]) * (offset - consumed) / count
                break
            consumed += count
        if seconds is None:
            raise PipelineError("Storyboard anchor could not be mapped to service speech timing.")
        frame = 0 if index == 0 else round(seconds * FRAME_RATE)
        if frame >= total_frames or (positions and frame <= positions[-1]):
            raise PipelineError("Storyboard shots collapsed onto the same video frame.")
        positions.append(frame)
    timeline = []
    for index, shot in enumerate(scene["shots"]):
        start = positions[index]
        end = positions[index + 1] if index + 1 < len(positions) else total_frames
        if end - start < FRAME_RATE:
            raise PipelineError("Storyboard contains a sub-second shot; revise its anchors.")
        timeline.append({
            **shot, "start": start / FRAME_RATE, "end": end / FRAME_RATE,
            "frames": end - start, "duration_seconds": (end - start) / FRAME_RATE,
            "alignment": "service caption start; proportional within cue; rounded to video frame",
        })
    return timeline


def captions_from_boundaries(boundaries: list[dict[str, Any]], *, duration: float) -> list[dict[str, Any]]:
    captions = []
    for boundary in boundaries:
        if boundary.get("type") != "SentenceBoundary":
            continue
        start = max(0.0, boundary["offset"] / 10_000_000)
        end = min(duration, (boundary["offset"] + boundary["duration"]) / 10_000_000)
        text = html.unescape(boundary["text"]).strip()
        if not text or end <= start:
            raise PipelineError("Invalid speech boundary; cannot manufacture subtitle timing.")
        lines = textwrap.wrap(text, width=24, break_long_words=True, break_on_hyphens=False)
        blocks = ["\n".join(lines[index:index + 2]) for index in range(0, len(lines), 2)]
        weights = [len(normalized_text(block)) for block in blocks]
        total = sum(weights)
        if not total:
            raise PipelineError("Subtitle boundary has no readable text.")
        cursor = start
        for index, block in enumerate(blocks):
            stop = end if index == len(blocks) - 1 else cursor + (end - start) * weights[index] / total
            captions.append({
                "start": round(cursor, 4), "end": round(stop, 4), "text": balanced_caption(block),
                "timing_method": "service_sentence_boundary; proportional subdivision within sentence",
            })
            cursor = stop
    if not captions:
        raise PipelineError("No service sentence boundaries were returned.")
    return reconcile_caption_timing(captions)


def timestamp(seconds: float, *, ass: bool = False) -> str:
    scale = 100 if ass else 1000
    ticks = round(seconds * scale)
    hours, ticks = divmod(ticks, 3600 * scale)
    minutes, ticks = divmod(ticks, 60 * scale)
    whole, fraction = divmod(ticks, scale)
    return f"{hours}:{minutes:02}:{whole:02}.{fraction:02}" if ass else f"{hours:02}:{minutes:02}:{whole:02},{fraction:03}"


def ass_text(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", r"\N")


def write_ass(path: Path, captions: list[dict[str, Any]]) -> None:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Narration,Malgun Gothic,40,&H00F3F6F8,&H00F3F6F8,&H001B2330,&H001B2330,0,0,0,0,100,100,0,0,1,2,0,2,70,70,28,1
Style: Notice,Malgun Gothic,18,&H00A7BEC8,&H00A7BEC8,&H001B2330,&H001B2330,0,0,0,0,100,100,0,0,1,0,0,7,24,24,572,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [
        f"Dialogue: 0,{timestamp(cue['start'], ass=True)},{timestamp(cue['end'], ass=True)},Narration,,0,0,0,,{ass_text(cue['text'])}"
        for cue in captions
    ]
    if captions:
        events.insert(
            0,
            f"Dialogue: 1,0:00:00.00,{timestamp(captions[-1]['end'] + 0.35, ass=True)},Notice,,0,0,0,,창작 오디오드라마 · AI 음성",
        )
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")


def write_srt(path: Path, captions: list[dict[str, Any]]) -> None:
    path.write_text("\n\n".join(
        f"{index}\n{timestamp(cue['start'])} --> {timestamp(cue['end'])}\n{cue['text']}"
        for index, cue in enumerate(captions, start=1)
    ) + "\n", encoding="utf-8")


def write_player(output: Path, episode: dict[str, Any], chapters: list[float], evaluation: dict[str, Any]) -> None:
    buttons = "\n".join(
        f'<button type="button" data-time="{start:.3f}">{html.escape(scene["id"])} · {html.escape(scene["title"])}</button>'
        for scene, start in zip(episode["scenes"], chapters, strict=True)
    )
    title = html.escape(episode["title"])
    minutes, seconds = divmod(round(evaluation["duration_seconds"]), 60)
    extra_links = "".join(
        f'<a href="{filename}">{label}</a>'
        for filename, label in (
            ("speech-evaluation.json", "음성 교차검사"), ("editorial-review.json", "내용 비평"),
            ("before-after.json", "이전 판본과 비교"), ("shot-contact-sheet.jpg", "장면별 컷 보기"),
            ("comparison.html", "두 판본 나란히 보기"),
        )
        if (output / filename).is_file()
    )
    page = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · 로컬 시사회</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#101c26;color:#f3e7cf;font-family:"Malgun Gothic",sans-serif}
main{max-width:1200px;margin:auto;padding:28px 20px}h1{font-size:clamp(24px,4vw,38px);margin:10px 0}
.eyebrow{color:#d6ad68;letter-spacing:.12em;font-size:13px}p{line-height:1.7;color:#cad4d7}
video{width:100%;display:block;border-radius:12px;background:#000;margin:24px 0}
nav,.chapters{display:flex;flex-wrap:wrap;gap:10px}a,button{color:#f3e7cf;background:#263c49;border:1px solid #49646c;border-radius:7px;padding:12px 15px;font:inherit;text-decoration:none}
a:focus-visible,button:focus-visible{outline:3px solid #d6ad68;outline-offset:3px}button{cursor:pointer;text-align:left}
h2{font-size:20px;margin-top:32px}.notice{padding:16px 20px;border-left:4px solid #d6ad68;background:#1a2c38}
</style></head><body><main>
<div class="eyebrow">두 사람의 기억 · LOCAL SCREENING</div>
<h1>__TITLE__</h1><p>__FICTION__ · __DURATION__ · 한국어 두 화자 · 자막 포함</p>
<video id="player" controls preload="metadata" playsinline poster="cover.png">
<source src="episode.mp4" type="video/mp4">브라우저가 동영상을 지원하지 않으면 아래 링크로 파일을 여세요.</video>
<p id="play-status" role="status"></p>
<nav aria-label="결과 파일"><a href="episode.mp4" download>MP4 저장</a><a href="episode.ko.srt" download>자막 저장</a>
<a href="evaluation.json">기술 평가</a><a href="episode-script.json">원작 대본</a>
<a href="contact-sheet.jpg">장면 모아보기</a>__EXTRA_LINKS__</nav>
<h2>장면 바로가기</h2><div class="chapters">__CHAPTERS__</div>
<h2>이번 결과의 범위</h2><p class="notice">유튜브 연결·업로드는 하지 않았습니다.
음성은 Microsoft Edge 온라인 음성 서비스를 사용했으며 인증된 Azure Speech 호출이 아닙니다.
삽화는 로컬에서 직접 그렸습니다. 기술 검사 통과는 시청 만족·바이럴·광고수익을 입증하지 않습니다.</p>
</main><script>
const player=document.getElementById('player');
document.querySelectorAll('[data-time]').forEach(button=>button.addEventListener('click',()=>{
player.currentTime=Number(button.dataset.time);
player.play().catch(error=>{
document.getElementById('play-status').textContent='재생을 시작하지 못했습니다. 영상의 재생 버튼을 눌러 주세요. '+error.message;
player.focus();
});
}));
</script></body></html>"""
    output.joinpath("index.html").write_text(
        page.replace("__TITLE__", title).replace("__FICTION__", html.escape(episode["fiction_notice"]))
        .replace("__DURATION__", f"{minutes}분 {seconds}초").replace("__CHAPTERS__", buttons)
        .replace("__EXTRA_LINKS__", extra_links),
        encoding="utf-8",
    )


async def synthesize_scene(
    scene: dict[str, Any], directory: Path, ffmpeg: str, *, rate: str,
) -> dict[str, Any]:
    try:
        import edge_tts
        import aiohttp
    except ImportError as error:
        raise PipelineError("Install the media extra before actual synthesis.") from error
    identifier = scene["id"]
    voice = VOICES[scene["pov"]]
    fingerprint = digest({
        "text": scene["narration"], "voice": voice, "rate": rate,
        "provider": "edge-tts", "version": edge_tts.__version__, "boundary": "SentenceBoundary",
    })
    cache = directory / f"{identifier}.speech.json"
    wav = directory / f"{identifier}.wav"
    if cache.exists() and wav.exists():
        record = json.loads(cache.read_text(encoding="utf-8"))
        if record.get("fingerprint") == fingerprint and record.get("wav_sha256") == hashlib.sha256(wav.read_bytes()).hexdigest():
            for cue in record["captions"]:
                cue["text"] = balanced_caption(cue["text"])
            record["captions"] = improve_caption_readability(record["captions"], duration=record["duration_seconds"])
            write_ass(directory / f"{identifier}.ass", record["captions"])
            write_srt(directory / f"{identifier}.srt", record["captions"])
            write_json(cache, record)
            return record
    mp3 = directory / f"{identifier}.mp3"
    partial = directory / f"{identifier}.partial.mp3"
    boundaries = []
    for attempt in range(1, 3):
        boundaries = []
        try:
            communicate = edge_tts.Communicate(
                scene["narration"], voice, rate=rate, boundary="SentenceBoundary",
                connect_timeout=15, receive_timeout=60,
            )
            with partial.open("wb") as audio:
                async for event in communicate.stream():
                    if event["type"] == "audio":
                        audio.write(event["data"])
                    elif event["type"] == "SentenceBoundary":
                        boundaries.append(event)
            if partial.stat().st_size < 1000:
                raise PipelineError(f"{identifier}: speech service returned no usable audio.")
            partial.replace(mp3)
            break
        except (aiohttp.ClientError, asyncio.TimeoutError, edge_tts.exceptions.EdgeTTSException) as error:
            write_json(directory / f"{identifier}.speech-error.json", {
                "scene": identifier, "attempt": attempt, "error": str(error),
                "provider": "edge-tts", "fallback_used": False,
            })
            if attempt == 2:
                raise PipelineError(f"{identifier}: real TTS failed; see speech-error.json: {error}") from error
            await asyncio.sleep(2)
    execute(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3), "-ar", "24000", "-ac", "1",
         "-c:a", "pcm_s16le", str(wav)],
        cwd=directory, log=directory / f"{identifier}.decode.log",
    )
    duration = wav_duration(wav)
    captions = captions_from_boundaries(boundaries, duration=duration)
    captions = improve_caption_readability(captions, duration=duration)
    expected = normalized_text(scene["narration"])
    actual = normalized_text("".join(cue["text"] for cue in captions))
    if expected != actual:
        raise PipelineError(f"{identifier}: returned subtitle text differs from narration. Review before rendering.")
    write_ass(directory / f"{identifier}.ass", captions)
    write_srt(directory / f"{identifier}.srt", captions)
    record = {
        "fingerprint": fingerprint, "scene": identifier, "voice": voice, "rate": rate,
        "provider": "Microsoft Edge online speech via unofficial edge-tts client; NOT Azure Speech account",
        "duration_seconds": duration, "narration_characters": len(scene["narration"]),
        "captions": captions, "subtitle_text_coverage": 1.0,
        "wav_sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
        "api_billed_amount_usd": 0, "azure_used": False, "simulated": False,
    }
    write_json(cache, record)
    return record


def render_clip(
    ffmpeg: str, scene: dict[str, Any], speech: dict[str, Any], directory: Path,
    timeline: list[dict[str, Any]] | None = None,
) -> float:
    identifier = scene["id"]
    duration = speech["duration_seconds"] + 0.35
    frames = math.ceil(duration * FRAME_RATE)
    video = directory / f"{identifier}.mp4"
    shot_plan = timeline if timeline is not None else [{
        "id": "wide", "frames": frames, "start": 0.0, "end": frames / FRAME_RATE,
        "duration_seconds": frames / FRAME_RATE,
    }]
    images = [
        directory / (f"{identifier}-shot-{shot['id']}.png" if timeline is not None else f"{identifier}.png")
        for shot in shot_plan
    ]
    key = digest({
        "speech": speech["fingerprint"],
        "art": [hashlib.sha256(image.read_bytes()).hexdigest() for image in images],
        "shots": shot_plan,
        "subtitles": hashlib.sha256((directory / f"{identifier}.ass").read_bytes()).hexdigest(),
        "renderer": "illustrated-v4-story-anchored-shots-1280x720-24fps-superfast-crf22",
    })
    record_path = directory / f"{identifier}.render.json"
    if video.exists() and record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("fingerprint") == key and record.get("sha256") == hashlib.sha256(video.read_bytes()).hexdigest():
            return record["duration_seconds"]
    inputs = []
    filters = []
    for index, (image, shot) in enumerate(zip(images, shot_plan, strict=True)):
        inputs.extend(["-i", image.name])
        filters.append(
            f"[{index}:v]scale=1920:1080,zoompan=z='1+0.02*on/{shot['frames']}':"
            f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={shot['frames']}:s=1280x720:fps={FRAME_RATE},"
            f"setsar=1,setpts=PTS-STARTPTS[v{index}]"
        )
    # Whole-scene captions follow concatenation; camera moves never move subtitle timing.
    joins = "".join(f"[v{index}]" for index in range(len(images)))
    filters.append(
        f"{joins}concat=n={len(images)}:v=1:a=0,"
        f"subtitles={identifier}.ass:fontsdir=fonts,format=yuv420p[vout]"
    )
    execute(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-threads", "2",
         *inputs, "-i", f"{identifier}.wav",
         "-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
         "-map", "[vout]", "-map", f"{len(images)}:a:0",
         "-af", "apad=pad_dur=0.35", "-t", f"{duration:.6f}",
         "-c:v", "libx264", "-preset", "superfast", "-crf", "22", "-threads", "2",
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart",
         f"{identifier}.rendering.mp4"],
        cwd=directory, log=directory / f"{identifier}.render.log",
    )
    (directory / f"{identifier}.rendering.mp4").replace(video)
    measured = probe_duration(ffmpeg, video)
    if abs(measured - duration) > 0.15:
        raise PipelineError(f"{identifier}: rendered scene duration differs from actual narration.")
    write_json(record_path, {
        "fingerprint": key, "duration_seconds": measured,
        "shots": shot_plan,
        "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
    })
    return measured


def loudness_stats(ffmpeg: str, video: Path, directory: Path, name: str) -> dict[str, Any]:
    output = execute(
        [ffmpeg, "-hide_banner", "-i", str(video), "-vn",
         "-af", "loudnorm=I=-18:TP=-1.5:LRA=7:print_format=json", "-f", "null", "-"],
        cwd=directory, log=directory / f"{name}.loudness.log",
    )
    matches = re.findall(r'\{\s*"input_i".*?\}', output, flags=re.S)
    if not matches:
        raise PipelineError("FFmpeg returned no loudness measurements.")
    result = json.loads(matches[-1])
    if not math.isfinite(float(result["input_i"])):
        raise PipelineError("Audio is silent or unmeasurable.")
    return result


def write_contact_sheet(output: Path, scene_count: int) -> None:
    from PIL import Image
    columns, tile_width, tile_height = 4, 480, 270
    rows = math.ceil(scene_count / columns)
    contact = Image.new("RGB", (columns * tile_width, rows * tile_height), "#172b3a")
    for index in range(scene_count):
        with Image.open(output / "review-frames" / f"{index + 1:02}.jpg") as frame:
            tile = frame.convert("RGB").resize((tile_width, tile_height), Image.Resampling.LANCZOS)
            contact.paste(tile, ((index % columns) * tile_width, (index // columns) * tile_height))
    contact.save(output / "contact-sheet.jpg", quality=92)


def write_shot_contact_sheet(output: Path, timeline: list[dict[str, Any]]) -> None:
    from PIL import Image, ImageDraw, ImageFont
    columns, tile_width, tile_height = 4, 480, 290
    contact = Image.new("RGB", (columns * tile_width, math.ceil(len(timeline) / columns) * tile_height), "#172b3a")
    draw = ImageDraw.Draw(contact)
    font = ImageFont.truetype(r"C:\Windows\Fonts\malgun.ttf", 17)
    for index, shot in enumerate(timeline):
        image = output / "scenes" / f"{shot['scene']}-shot-{shot['id']}.png"
        if not image.is_file():
            image = output / "scenes" / f"{shot['scene']}.png"
        left, top = (index % columns) * tile_width, (index // columns) * tile_height
        with Image.open(image) as frame:
            contact.paste(frame.convert("RGB").resize((480, 270), Image.Resampling.LANCZOS), (left, top))
        label = f"{shot['scene']}-{shot['id']} | {shot.get('subject', 'wide')} | {shot['duration_seconds']:.1f}s"
        draw.text((left + 8, top + 270), label, font=font, fill="#f3e7cf")
    contact.save(output / "shot-contact-sheet.jpg", quality=92)


def technical_evaluation(
    ffmpeg: str, output: Path, episode: dict[str, Any], speech: list[dict[str, Any]],
    captions: list[dict[str, Any]], chapter_starts: list[float],
) -> dict[str, Any]:
    final = output / "episode.mp4"
    duration = probe_duration(ffmpeg, final)
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(final)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    dimensions = re.search(r"Video: h264[^\r\n]*? (\d{3,5})x(\d{3,5})", probe.stderr)
    frame_rate = re.search(r"Video: h264[^\r\n]*? ([\d.]+) fps", probe.stderr)
    if not dimensions or not frame_rate or "Audio: aac" not in probe.stderr:
        raise PipelineError("Final file is missing the expected H.264 video/AAC audio streams.")
    width, height = map(int, dimensions.groups())
    fps = float(frame_rate.group(1))
    measured = loudness_stats(ffmpeg, final, output, "final")
    execute(
        [ffmpeg, "-v", "error", "-xerror", "-threads", "2", "-i", str(final), "-f", "null", "-"],
        cwd=output, log=output / "decode-check.log",
    )
    overlap = [
        index for index in range(1, len(captions))
        if captions[index]["start"] < captions[index - 1]["end"] - 0.02
    ]
    out_of_bounds = [index for index, cue in enumerate(captions) if cue["start"] < 0 or cue["end"] > duration + 0.1]
    max_cps = max(len(normalized_text(cue["text"])) / max(0.01, cue["end"] - cue["start"]) for cue in captions)
    frames = output / "review-frames"
    frames.mkdir(exist_ok=True)
    for index, start in enumerate(chapter_starts):
        stamp = min(duration - 1, start + 8)
        execute(
            [ffmpeg, "-y", "-v", "error", "-ss", str(stamp), "-i", str(final),
             "-frames:v", "1", str(frames / f"{index + 1:02}.jpg")],
            cwd=output, log=frames / f"{index + 1:02}.log",
        )
    write_contact_sheet(output, len(chapter_starts))
    shot_timelines = []
    for scene, chapter_start in zip(episode["scenes"], chapter_starts, strict=True):
        record = json.loads((output / "scenes" / f"{scene['id']}.render.json").read_text(encoding="utf-8"))
        local_shots = record.get("shots", [{
            "id": "wide", "start": 0.0, "end": record["duration_seconds"],
            "duration_seconds": record["duration_seconds"],
        }])
        for shot in local_shots:
            shot_timelines.append({
                **shot, "scene": scene["id"], "global_start": chapter_start + shot["start"],
                "global_end": chapter_start + shot["end"],
            })
    write_json(output / "shot-timeline.json", shot_timelines)
    write_shot_contact_sheet(output, shot_timelines)
    results = {
        "simulated": False,
        "file": str(final),
        "video_sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
        "title": episode["title"],
        "duration_seconds": duration,
        "video_width": width,
        "video_height": height,
        "video_fps": fps,
        "stream_format_pass": (width, height) == (1280, 720) and abs(fps - FRAME_RATE) < 0.01,
        "target_duration_pass": 1200 <= duration <= 1800,
        "decode_all_frames_pass": True,
        "measured_loudness_lufs": float(measured["input_i"]),
        "measured_true_peak_dbtp": float(measured["input_tp"]),
        "loudness_pass": -20 <= float(measured["input_i"]) <= -16,
        "true_peak_pass": float(measured["input_tp"]) <= -1,
        "subtitle_count": len(captions),
        "subtitle_text_coverage": min(item["subtitle_text_coverage"] for item in speech),
        "subtitle_overlaps": overlap,
        "subtitle_out_of_bounds": out_of_bounds,
        "small_service_boundary_overlaps_corrected": sum(
            "service_boundary_end_trim_seconds" in cue for cue in captions
        ),
        "maximum_subtitle_characters_per_second": round(max_cps, 2),
        "short_subtitles_under_one_second": sum(cue["end"] - cue["start"] < 1 for cue in captions),
        "readability_adjusted_subtitles": sum("readability_adjustment" in cue for cue in captions),
        "longest_subtitle_line_characters": max(len(line) for cue in captions for line in cue["text"].splitlines()),
        "shot_count": len(shot_timelines),
        "average_shot_seconds": round(sum(shot["duration_seconds"] for shot in shot_timelines) / len(shot_timelines), 2),
        "maximum_shot_seconds": round(max(shot["duration_seconds"] for shot in shot_timelines), 2),
        "story_anchored_shots": sum("anchor" in shot for shot in shot_timelines),
        "art_provenance": "Original locally drawn illustrations; no stock photos or external image model.",
        "script_provenance": "Original Korean fiction written by the coding-agent writer, not copied submissions.",
        "speech_provider": "Microsoft Edge online via edge-tts; not authenticated Azure Speech.",
        "youtube_connection": False,
        "human_listening_test": "not_performed",
        "audience_retention_and_revenue": "not_measured",
        "limitations": [
            "Automated decode/loudness/text checks do not prove natural acting or engagement.",
            "Subtitle timing inside a long service sentence is proportionally subdivided.",
            "Service boundary overlaps up to 100ms are explicitly trimmed to the next cue; larger overlaps are rejected.",
            "Unkeyed Edge speech access is not a contracted Azure production service or commercial clearance.",
            "No public-platform upload, ad suitability decision, or revenue validation occurred.",
        ],
    }
    write_json(output / "evaluation.json", results)
    return results


async def produce_local(
    episode_path: Path, output: Path, *, allow_external_tts: bool, rate: str = "+0%",
    audio_only: bool = False,
) -> dict[str, Any]:
    if not allow_external_tts:
        raise PipelineError("Actual speech requires explicit --allow-external-tts. No YouTube integration exists.")
    if not re.fullmatch(r"[+-]\d{1,2}%", rate):
        raise PipelineError("Speech rate must look like +0%, -5% or +10%.")
    episode = load_episode(episode_path)
    output = output.resolve()
    directory = output / "scenes"
    directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_executable()
    speech = []
    started = time.monotonic()
    for scene in episode["scenes"]:
        print(f"Speech {scene['id']}: {scene['title']}", flush=True)
        speech.append(await synthesize_scene(scene, directory, ffmpeg, rate=rate))
    narration_duration = sum(item["duration_seconds"] for item in speech)
    report = {
        "simulated": False, "stage": "audio_ready", "narration_seconds": narration_duration,
        "narration_characters": sum(item["narration_characters"] for item in speech),
        "speech_rate": rate, "azure_used": False, "youtube_connection": False,
        "api_billed_amount_usd": 0, "cost_note": "No paid API called; excludes subscription/agent credits, device and electricity.",
        "elapsed_process_seconds": round(time.monotonic() - started, 2),
        "timing_note": "This invocation only; cache reuse, prior interrupted renders, writing and environment setup are not an end-to-end production benchmark.",
    }
    write_json(output / "production.json", report)
    if audio_only:
        return report
    if not 1200 <= narration_duration <= 1800:
        raise PipelineError(
            f"Actual narration is {narration_duration / 60:.2f} minutes, outside 20-30. "
            "Revise the story or speech rate; do not pad with artificial silence."
        )
    from .illustrations import render_cover, render_scene
    font = Path(r"C:\Windows\Fonts\malgun.ttf")
    if not font.is_file():
        raise PipelineError("A Korean font is required. This renderer expects Windows Malgun Gothic.")
    fonts = directory / "fonts"
    fonts.mkdir(exist_ok=True)
    copied_font = fonts / "malgun.ttf"
    shutil.copyfile(font, copied_font)
    render_cover(episode, output / "cover.png")
    chapter_starts = []
    captions = []
    elapsed = 0.0
    for scene, audio in zip(episode["scenes"], speech, strict=True):
        print(f"Render {scene['id']}: {scene['title']}", flush=True)
        timeline = None
        if "shots" in scene:
            from .storyboard_art import render_shot
            timeline = compile_shot_timeline(scene, audio)
            for shot in timeline:
                render_shot(scene, shot, episode, directory / f"{scene['id']}-shot-{shot['id']}.png")
            shutil.copyfile(
                directory / f"{scene['id']}-shot-{timeline[0]['id']}.png",
                directory / f"{scene['id']}.png",
            )
        else:
            render_scene(scene, episode, directory / f"{scene['id']}.png")
        chapter_starts.append(elapsed)
        captions.extend({**cue, "start": cue["start"] + elapsed, "end": cue["end"] + elapsed} for cue in audio["captions"])
        elapsed += render_clip(ffmpeg, scene, audio, directory, timeline)
    copied_font.unlink()
    fonts.rmdir()
    (directory / "concat.txt").write_text(
        "\n".join(f"file '{scene['id']}.mp4'" for scene in episode["scenes"]) + "\n", encoding="utf-8"
    )
    combined = output / "combined.mp4"
    execute(
        [ffmpeg, "-y", "-v", "warning", "-f", "concat", "-safe", "1", "-i", "concat.txt",
         "-c", "copy", str(combined)],
        cwd=directory, log=output / "concat.log",
    )
    before = loudness_stats(ffmpeg, combined, output, "before")
    audio_filter = (
        f"loudnorm=I=-18:TP=-1.5:LRA=7:measured_I={before['input_i']}:"
        f"measured_TP={before['input_tp']}:measured_LRA={before['input_lra']}:"
        f"measured_thresh={before['input_thresh']}:offset={before['target_offset']}:linear=true"
    )
    metadata = [";FFMETADATA1", "title=" + episode["title"].replace("=", r"\="),
                "comment=" + episode["fiction_notice"]]
    for index, scene in enumerate(episode["scenes"]):
        stop = chapter_starts[index + 1] if index + 1 < len(chapter_starts) else elapsed
        metadata.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(chapter_starts[index] * 1000)}",
                         f"END={round(stop * 1000)}", "title=" + scene["title"].replace("=", r"\=")])
    (output / "chapters.ffmeta").write_text("\n".join(metadata) + "\n", encoding="utf-8")
    execute(
        [ffmpeg, "-y", "-v", "warning", "-i", str(combined), "-i", "chapters.ffmeta",
         "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1",
         "-c:v", "copy", "-af", audio_filter, "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
         "-movflags", "+faststart", "episode.rendering.mp4"],
        cwd=output, log=output / "final-encode.log",
    )
    (output / "episode.rendering.mp4").replace(output / "episode.mp4")
    write_srt(output / "episode.ko.srt", captions)
    write_json(output / "captions.json", captions)
    if episode_path.resolve() != (output / "episode-script.json").resolve():
        shutil.copyfile(episode_path, output / "episode-script.json")
    results = technical_evaluation(ffmpeg, output, episode, speech, captions, chapter_starts)
    report.update({
        "stage": "quality_review_pending", "video": str(output / "episode.mp4"),
        "duration_seconds": results["duration_seconds"],
        "elapsed_process_seconds": round(time.monotonic() - started, 2),
    })
    write_json(output / "production.json", report)
    for key in ("target_duration_pass", "stream_format_pass", "loudness_pass", "true_peak_pass"):
        if not results[key]:
            raise PipelineError(f"Video generated but quality gate failed: {key}; inspect evaluation.json.")
    if results["subtitle_overlaps"] or results["subtitle_out_of_bounds"]:
        raise PipelineError("Video generated but subtitle timing failed. Inspect evaluation.json.")
    write_player(output, episode, chapter_starts, results)
    report["stage"] = "local_video_complete"
    write_json(output / "production.json", report)
    return results
