"""Packaging media: thumbnails, vertical Shorts, caption files and YouTube description metadata."""
from __future__ import annotations

import hashlib
import io
import shutil
import textwrap
from pathlib import Path
from typing import Any

from ..fonts import font_family, korean_font
from ..media import ass_text, execute, ffmpeg_executable, timestamp, write_json, write_srt
from ..models import PipelineError
from .render_v3 import anchor_seconds

THUMBNAIL_SIZE = (1280, 720)
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
SHORT_SIZE = (1080, 1920)
SHORT_RANGE = (15.0, 59.0)
AI_DISCLOSURE = "이 영상의 음성·삽화는 AI로 생성되었으며, 이야기는 창작된 허구입니다."


def _wrap(draw: Any, text: str, font: Any, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = ""
        for char in word:
            if draw.textlength(current + char, font=font) > width and current:
                lines.append(current)
                current = ""
            current += char
    return lines + ([current] if current else [])


def render_thumbnail(image: Path, text: str, target: Path, *, maximum_size: int = 150, minimum_size: int = 56) -> dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    text = " ".join(text.split())
    if not text:
        raise PipelineError("Thumbnail text is empty.")
    width, height = THUMBNAIL_SIZE
    margin = 64
    with Image.open(image) as source:
        canvas = ImageOps.fit(source.convert("RGB"), THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
    shade = Image.linear_gradient("L").resize(THUMBNAIL_SIZE).point(lambda v: int(v * 0.75))
    canvas = Image.composite(Image.new("RGB", THUMBNAIL_SIZE, "black"), canvas, shade)
    draw = ImageDraw.Draw(canvas)
    font_path = korean_font(bold=True)
    for size in range(maximum_size, minimum_size - 1, -4):
        font = ImageFont.truetype(str(font_path), size)
        lines = _wrap(draw, text, font, width - 2 * margin)
        line_height = round(size * 1.18)
        if len(lines) <= 2 and line_height * len(lines) <= height // 2:
            break
    else:
        raise PipelineError(f"Thumbnail text {text!r} does not fit in two lines at {minimum_size}px; shorten it.")
    stroke = max(4, size // 12)
    top = height - margin - line_height * len(lines)
    for index, line in enumerate(lines):
        draw.text((margin, top + index * line_height), line, font=font, fill="#ffe14d" if index == 0 else "white",
                  stroke_width=stroke, stroke_fill="black")
    target.parent.mkdir(parents=True, exist_ok=True)
    for quality in (92, 85, 75, 65):
        buffer = io.BytesIO()
        canvas.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() < THUMBNAIL_MAX_BYTES:
            target.write_bytes(buffer.getvalue())
            return {"file": str(target), "text": text, "lines": lines, "font_size": size, "quality": quality,
                    "bytes": buffer.tell(), "source_image": str(image), "size": list(THUMBNAIL_SIZE)}
    raise PipelineError("Thumbnail could not be compressed below 2 MB.")


def render_thumbnail_candidates(image: Path, texts: list[str], directory: Path) -> list[dict[str, Any]]:
    if not texts:
        raise PipelineError("No thumbnail texts supplied.")
    results = [render_thumbnail(image, text, directory / f"thumbnail-{index + 1:02}.jpg") for index, text in enumerate(texts)]
    write_json(directory / "thumbnails.json", results)
    return results


def short_window(episode: dict[str, Any], short: dict[str, Any], captions: list[dict[str, Any]],
                 starts: dict[str, float]) -> tuple[float, float]:
    """Episode-global start/end seconds for a Shorts segment; captions carry their ``scene`` id."""
    scenes = {scene["id"]: scene for scene in episode["scenes"]}
    if short.get("scene_id") not in scenes:
        raise PipelineError(f"Short refers to unknown scene {short.get('scene_id')!r}.")
    scene = scenes[short["scene_id"]]
    offset = starts[scene["id"]]
    local = [{**c, "start": c["start"] - offset, "end": c["end"] - offset} for c in captions if c.get("scene") == scene["id"]]
    begin = offset + anchor_seconds(scene["narration"], local, short["start_anchor"])
    end = offset + anchor_seconds(scene["narration"], local, short["end_anchor"], end=True)
    length = end - begin
    if not SHORT_RANGE[0] <= length <= SHORT_RANGE[1]:
        raise PipelineError(
            f"Short in scene {scene['id']} is {length:.1f}s; Shorts segments must be {SHORT_RANGE[0]:.0f}-{SHORT_RANGE[1]:.0f}s."
        )
    return round(begin, 3), round(end + 0.25, 3)


def _short_ass(path: Path, captions: list[dict[str, Any]], hook: str, duration: float, font_name: str) -> None:
    def wrap(text: str, width: int) -> str:
        return "\n".join(textwrap.wrap(" ".join(text.split()), width=width, break_long_words=True, break_on_hyphens=False))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {SHORT_SIZE[0]}
PlayResY: {SHORT_SIZE[1]}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font_name},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,5,0,2,60,60,420,1
Style: Hook,{font_name},76,&H004DE1FF,&H004DE1FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,0,8,60,60,230,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [f"Dialogue: 1,{timestamp(0, ass=True)},{timestamp(duration, ass=True)},Hook,,0,0,0,,{ass_text(wrap(hook, 12))}"]
    events += [
        f"Dialogue: 0,{timestamp(c['start'], ass=True)},{timestamp(c['end'], ass=True)},Caption,,0,0,0,,{ass_text(wrap(c['text'], 15))}"
        for c in captions
    ]
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")


def render_short(
    episode: dict[str, Any], short: dict[str, Any], *, captions: list[dict[str, Any]],
    shot_timeline: list[dict[str, Any]], starts: dict[str, float], source_media: Path, target: Path,
) -> dict[str, Any]:
    """Vertical 1080x1920 cut: blurred full-bleed background, centered 16:9 art, big captions, hook on top."""
    if not str(short.get("hook", "")).strip():
        raise PipelineError("Each short needs a hook line.")
    begin, end = short_window(episode, short, captions, starts)
    duration = end - begin
    pieces = []
    for shot in shot_timeline:
        low, high = max(begin, shot["global_start"]), min(end, shot["global_end"])
        if high - low > 0.01:
            pieces.append((Path(shot["image"]).resolve(), high - low))
    if not pieces:
        raise PipelineError("No shots overlap the requested short segment.")
    window = [{**c, "start": max(0.0, c["start"] - begin), "end": min(duration, c["end"] - begin)}
              for c in captions if c["end"] > begin and c["start"] < end]
    work = target.parent / f"{target.stem}.work"
    fonts = work / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    font = korean_font(bold=True)
    shutil.copyfile(font, fonts / font.name)
    _short_ass(work / "short.ass", window, short["hook"], duration, font_family(font))
    ffmpeg = ffmpeg_executable()
    inputs, filters = [], []
    width, height = SHORT_SIZE
    for index, (image, seconds) in enumerate(pieces):
        inputs += ["-loop", "1", "-framerate", "24", "-t", f"{seconds:.4f}", "-i", str(image)]
        filters.append(
            f"[{index}:v]split[a{index}][b{index}];"
            f"[a{index}]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=24:2[bg{index}];"
            f"[b{index}]scale={width}:-2[fg{index}];"
            f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=24[v{index}]"
        )
    filters.append("".join(f"[v{i}]" for i in range(len(pieces))) +
                   f"concat=n={len(pieces)}:v=1:a=0,subtitles=short.ass:fontsdir=fonts,format=yuv420p[vout]")
    target.parent.mkdir(parents=True, exist_ok=True)
    execute(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", *inputs,
         "-ss", f"{begin:.3f}", "-t", f"{duration:.3f}", "-i", str(source_media.resolve()),
         "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", f"{len(pieces)}:a:0",
         "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(target.resolve())],
        cwd=work, log=work / "render.log",
    )
    shutil.rmtree(work, ignore_errors=True)
    return {"file": str(target), "scene_id": short["scene_id"], "start": begin, "end": end,
            "duration_seconds": round(duration, 3), "hook": short["hook"], "size": list(SHORT_SIZE),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def render_shorts(
    episode: dict[str, Any], packaging: dict[str, Any], *, captions: list[dict[str, Any]],
    shot_timeline: list[dict[str, Any]], starts: dict[str, float], source_media: Path, directory: Path,
) -> list[dict[str, Any]]:
    results = [
        render_short(episode, short, captions=captions, shot_timeline=shot_timeline, starts=starts,
                     source_media=source_media, target=directory / f"short-{index + 1:02}.mp4")
        for index, short in enumerate(packaging.get("shorts", []))
    ]
    write_json(directory / "shorts.json", results)
    return results


def write_vtt(path: Path, captions: list[dict[str, Any]]) -> None:
    def stamp(seconds: float) -> str:
        return timestamp(seconds).replace(",", ".")
    path.write_text("WEBVTT\n\n" + "\n\n".join(
        f"{index}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}" for index, c in enumerate(captions, start=1)
    ) + "\n", encoding="utf-8")


def write_caption_files(directory: Path, captions: list[dict[str, Any]], language: str = "ko") -> dict[str, str]:
    srt, vtt = directory / f"episode.{language}.srt", directory / f"episode.{language}.vtt"
    write_srt(srt, captions)
    write_vtt(vtt, captions)
    return {"srt": str(srt), "vtt": str(vtt)}


def chapter_stamp(seconds: float) -> str:
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def tags_length(tags: list[str]) -> int:
    """YouTube counts commas between tags and quotes around tags containing spaces."""
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def build_metadata(
    episode: dict[str, Any], packaging: dict[str, Any], chapters: list[tuple[str, float]], duration: float, *,
    attributions: list[str] = (), disclosure: str = AI_DISCLOSURE,
) -> dict[str, Any]:
    title = " ".join(str(packaging.get("title") or episode["title"]).split())
    tags = [" ".join(str(t).split()) for t in packaging.get("tags", []) if str(t).strip()]
    if not title or len(title) > 100:
        raise PipelineError(f"YouTube title must be 1-100 characters; got {len(title)}.")
    if len(chapters) < 3 or chapters[0][1] != 0:
        raise PipelineError("YouTube chapters need at least 3 entries and the first must start at 0:00.")
    bounds = [start for _, start in chapters] + [duration]
    short = [name for (name, _), a, b in zip(chapters, bounds, bounds[1:]) if b - a < 10]
    if short:
        raise PipelineError(f"Chapters shorter than 10 seconds: {short}.")
    if tags_length(tags) > 500:
        raise PipelineError(f"Tags total {tags_length(tags)} characters; YouTube allows 500. Drop some tags.")
    sections = [
        str(packaging.get("description") or "").strip(), episode["synopsis"].strip(),
        "\n".join(f"{chapter_stamp(start)} {name}" for name, start in chapters),
        disclosure, episode["fiction_notice"].strip(),
    ]
    if attributions:
        sections.append("음악·효과음\n" + "\n".join(attributions))
    description = "\n\n".join(section for section in sections if section)
    for label, value in (("title", title), ("description", description)):
        if "<" in value or ">" in value:
            raise PipelineError(f"YouTube {label} cannot contain angle brackets.")
    size = len(description.encode("utf-8"))
    if size > 5000:
        raise PipelineError(f"Description is {size} bytes; YouTube allows 5000. Shorten the synopsis or description.")
    return {"title": title, "description": description, "tags": tags, "description_bytes": size,
            "tags_characters": tags_length(tags), "chapters": [{"title": n, "start": s} for n, s in chapters],
            "contains_synthetic_media": True}
