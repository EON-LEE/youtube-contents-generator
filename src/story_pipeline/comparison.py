from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any

from .media import write_json
from .models import PipelineError


def compare_editions(baseline: Path, revised: Path) -> dict[str, Any]:
    """Compare measured outputs, not invented audience or subjective quality scores."""
    first = json.loads((baseline / "evaluation.json").read_text(encoding="utf-8"))
    second = json.loads((revised / "evaluation.json").read_text(encoding="utf-8"))
    story1 = json.loads((baseline / "episode-script.json").read_text(encoding="utf-8"))
    story2 = json.loads((revised / "episode-script.json").read_text(encoding="utf-8"))
    if baseline.resolve() == revised.resolve():
        raise PipelineError("Revision comparison requires separate preserved output directories.")
    if first.get("simulated") is not False or second.get("simulated") is not False:
        raise PipelineError("Only actual rendered videos can be compared.")
    hashes = {}
    for directory, evaluation in ((baseline, first), (revised, second)):
        if not (directory / "episode.mp4").is_file():
            raise PipelineError(f"Missing comparison video: {directory}")
        hashes[directory] = hashlib.sha256((directory / "episode.mp4").read_bytes()).hexdigest()
        if evaluation.get("video_sha256") and evaluation["video_sha256"] != hashes[directory]:
            raise PipelineError("The video changed after its technical evaluation.")
    base_shots = first.get("shot_count", len(story1["scenes"]))
    new_shots = second["shot_count"]
    report = {
        "baseline": str(baseline.resolve()),
        "revised": str(revised.resolve()),
        "simulated": False,
        "youtube_connection": False,
        "method": "Measured media outputs and explicit script/storyboard changes; not a viewer experiment.",
        "measurements": {
            "duration_seconds": {"before": first["duration_seconds"], "after": second["duration_seconds"]},
            "shot_count": {"before": base_shots, "after": new_shots},
            "average_shot_seconds": {
                "before": first.get("average_shot_seconds", round(first["duration_seconds"] / base_shots, 2)),
                "after": second["average_shot_seconds"],
            },
            "short_subtitles_under_one_second": {
                "before": first["short_subtitles_under_one_second"],
                "after": second["short_subtitles_under_one_second"],
            },
            "narration_characters": {
                "before": sum(len(scene["narration"]) for scene in story1["scenes"]),
                "after": sum(len(scene["narration"]) for scene in story2["scenes"]),
            },
            "loudness_lufs": {"before": first["measured_loudness_lufs"], "after": second["measured_loudness_lufs"]},
            "true_peak_dbtp": {"before": first["measured_true_peak_dbtp"], "after": second["measured_true_peak_dbtp"]},
        },
        "technical_gates": {
            key: second[key]
            for key in ("decode_all_frames_pass", "target_duration_pass", "stream_format_pass", "loudness_pass", "true_peak_pass")
        },
        "storyboard": {
            "story_anchored_shots": second["story_anchored_shots"],
            "timing_note": "Speech-cue timing; within-cue proportional mapping is approximate.",
        },
        "revision_notes": story2.get("revision_notes", []),
        "not_demonstrated": [
            "Higher watch time, click-through rate, return visits, viral reach or revenue",
            "Human emotional listening assessment",
            "Photorealistic acting or motion beyond illustrated camera moves",
        ],
        "integrity": {
            "baseline_video_sha256": hashes[baseline],
            "revised_video_sha256": hashes[revised],
        },
    }
    write_json(revised / "before-after.json", report)
    if baseline.parent.resolve() == revised.parent.resolve():
        rows = "".join(
            f"<tr><th>{html.escape(label)}</th><td>{values['before']}</td><td>{values['after']}</td></tr>"
            for key, label in (
                ("duration_seconds", "길이(초)"), ("shot_count", "편집 컷 수"),
                ("average_shot_seconds", "평균 컷 길이(초)"),
                ("short_subtitles_under_one_second", "1초 미만 자막"),
                ("narration_characters", "내레이션 글자 수"),
            )
            for values in (report["measurements"][key],)
        )
        baseline_folder = html.escape(baseline.name, quote=True)
        page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>내 집 열쇠를 돌려받던 날 · 개선 전후</title>
<style>
body{{background:#101c26;color:#f3e7cf;font-family:"Malgun Gothic",sans-serif;margin:0;padding:24px}}
main{{max-width:1300px;margin:auto}}.players{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
video{{width:100%;background:#000;border-radius:10px}}p{{line-height:1.7}}a{{color:#d6ad68}}
table{{border-collapse:collapse;width:100%;margin-top:24px}}td,th{{padding:14px;text-align:left;border-bottom:1px solid #49646c}}
@media(max-width:800px){{.players{{grid-template-columns:1fr}}}}
</style></head><body><main><h1>개선 전후 비교</h1>
<p>같은 원작의 두 판본입니다. 컷 수와 자막은 실제 파일 기준입니다.
이 수치는 재미·시청 지속률·매출의 향상을 입증하지 않습니다. 한 번에 한 영상만 재생하세요.</p>
<div class="players"><section><h2>기존 판본</h2>
<video controls preload="none" poster="../{baseline_folder}/cover.png" src="../{baseline_folder}/episode.mp4"></video>
</section><section><h2>개선 판본</h2><video controls preload="none" poster="cover.png" src="episode.mp4"></video></section></div>
<table><thead><tr><th>실측 항목</th><th>기존</th><th>개선</th></tr></thead><tbody>{rows}</tbody></table>
<p><a href="index.html">개선판 시사회</a> · <a href="shot-contact-sheet.jpg">{new_shots}컷 구성 확인</a> ·
<a href="before-after.json">상세 비교 자료</a></p>
<script>
document.querySelectorAll('video').forEach(active=>active.addEventListener('play',()=>{{
document.querySelectorAll('video').forEach(other=>{{if(other!==active)other.pause();}});
}}));
</script></main></body></html>"""
        (revised / "comparison.html").write_text(page, encoding="utf-8")
    return report
