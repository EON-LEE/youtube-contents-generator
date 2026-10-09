"""The production team: bounded, multi-agent loops instead of one-shot generation.

Deterministic code owns control flow, budgets, thresholds and validation; agents
own creative judgement. Each completed stage is checkpointed to the run
directory so an interrupted run resumes without paying for finished work.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from ..models import PipelineError
from .config import StudioConfig
from .episode_v3 import narration_characters, validate_episode, validate_shots, VOICE_PATTERN, RATE
from .gateway import AgentGateway
from .playbook import Playbook

CRITICS = ("critic-continuity", "critic-engagement", "critic-korean", "critic-originality", "critic-policy")
CONCEPT_WRITERS = ("concept-writer-a", "concept-writer-b", "concept-writer-c")
CONCEPT_JUDGES = ("greenlight-judge-a", "greenlight-judge-b")


def expand_scene_ids(raw: str, known_scenes: set[str]) -> list[str]:
    """Some non-OpenAI model families collapse multiple scene IDs into one field,
    e.g. ``"06,09,10"`` or a range like ``"10-12"``, instead of one decision per
    scene. Expand those into the individual zero-padded IDs the rest of the
    pipeline expects."""
    ids: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and part not in known_scenes:
            start, _, end = part.partition("-")
            start, end = start.strip(), end.strip()
            if start.isdigit() and end.isdigit():
                width = len(start)
                ids.extend(str(i).zfill(width) for i in range(int(start), int(end) + 1))
                continue
        ids.append(part)
    return ids


def prompt(task: str, **context: Any) -> str:
    return task.strip() + "\n\n```json\n" + json.dumps(context, ensure_ascii=False, indent=1) + "\n```"


class Studio:
    def __init__(self, config: StudioConfig, gateway: AgentGateway, run_dir: Path, playbook: Playbook,
                 bible: str, performance: list[dict[str, Any]] | None = None, history: list[str] | None = None):
        self.config = config
        self.gateway = gateway
        self.run_dir = run_dir
        self.playbook = playbook
        self.bible = bible
        self.performance = performance or []
        self.history = history or []
        (run_dir / "stages").mkdir(parents=True, exist_ok=True)
        override = run_dir / "stages" / "length-target.json"
        self.target_characters = (tuple(json.loads(override.read_text(encoding="utf-8")))
                                  if override.exists() else config.target_characters)

    def set_target_characters(self, target: tuple[int, int]) -> None:
        """Measured-speech feedback: rewrite toward a character range derived from real audio."""
        low, high = int(target[0]), int(target[1])
        if not 1000 <= low < high:
            raise PipelineError(f"Invalid measured character target {target}.")
        self.target_characters = (low, high)
        (self.run_dir / "stages" / "length-target.json").write_text(json.dumps([low, high]), encoding="utf-8")

    # -- checkpointing -------------------------------------------------
    def _stage(self, name: str, build) -> Any:
        path = self.run_dir / "stages" / f"{name}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        value = build()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return value

    def call(self, agent: str, task: str, *, stage: str, iteration: int = 0, **context: Any) -> dict[str, Any]:
        return self.gateway.call(agent, prompt(task, **context), stage=stage, iteration=iteration)

    def lessons(self, role: str) -> list[str]:
        return [lesson["text"] for lesson in self.playbook.active(role)]

    # -- planning ------------------------------------------------------
    def plan(self) -> dict[str, Any]:
        def build():
            research = self.call("trend-researcher", "현재 시청자 관심 주제를 조사하라. 모든 주제에 출처 URL을 붙여라.",
                                 stage="plan", channel_bible=self.bible, recent_titles=self.history[-30:])
            topics = [t for t in research["topics"] if t["sources"]]
            if not topics:
                raise PipelineError("Trend research returned no sourced topics; refusing to plan on unsourced claims.")
            analysis = self.call("performance-analyst", "지난 편 성과와 플레이북에서 유지할 점, 피할 점, 가설을 정리하라.",
                                 stage="plan", performance=self.performance[-20:],
                                 lessons=self.lessons("performance-analyst"))
            brief = self.call("showrunner", "이번 편의 제작 브리프를 정하라. 주제는 조사된 주제 중 하나여야 한다.",
                              stage="plan", topics=topics, analysis=analysis, channel_bible=self.bible,
                              recent_titles=self.history[-30:], lessons=self.lessons("showrunner"))
            if brief["chosen_topic"] not in {t["topic"] for t in topics}:
                raise PipelineError("Showrunner chose a topic outside the sourced research.")
            return {"research": {"topics": topics}, "analysis": analysis, "brief": brief}
        return self._stage("plan", build)

    def concept(self, plan: dict[str, Any]) -> dict[str, Any]:
        def build():
            feedback: list[str] = []
            rounds = []
            for round_index in range(1, self.config.loops.concept_rounds + 1):
                concepts = [
                    self.call(writer, "브리프에 맞는 독창적인 기획안을 하나 제안하라. 다른 작가와 겹치지 않게 과감하게.",
                              stage="concept", iteration=round_index, brief=plan["brief"],
                              judge_feedback=feedback, lessons=self.lessons("concept-writer"),
                              avoid_titles=self.history[-30:])
                    for writer in CONCEPT_WRITERS
                ]
                totals = {index: [] for index in range(len(concepts))}
                notes = []
                for judge in CONCEPT_JUDGES:
                    verdict = self.call(judge, "각 기획안을 루브릭 축별 0-10점으로 채점하라.", stage="concept",
                                        iteration=round_index, brief=plan["brief"],
                                        concepts=[{"concept_index": i, **c} for i, c in enumerate(concepts)])
                    for evaluation in verdict["evaluations"]:
                        index = evaluation["concept_index"]
                        if index not in totals or not evaluation["scores"]:
                            raise PipelineError(f"{judge} scored an unknown concept or no axes.")
                        totals[index].append(statistics.fmean(s["score"] for s in evaluation["scores"]))
                        notes.append(f"[{judge} → 기획 {index}] {evaluation['notes']}")
                if any(len(v) != len(CONCEPT_JUDGES) for v in totals.values()):
                    raise PipelineError("Every judge must score every concept.")
                averages = {index: round(statistics.fmean(v), 3) for index, v in totals.items()}
                best = max(averages, key=averages.get)
                rounds.append({"round": round_index, "concepts": concepts, "scores": averages, "notes": notes})
                if averages[best] >= self.config.thresholds.concept:
                    return {"chosen": concepts[best], "score": averages[best], "rounds": rounds}
                feedback = notes
            raise PipelineError(
                f"Concept gate failed after {len(rounds)} rounds (best {max(rounds[-1]['scores'].values())}); "
                f"see {self.run_dir / 'stages'}."
            )
        return self._stage("concept", build)

    # -- writing -------------------------------------------------------
    def outline(self, plan: dict[str, Any], concept: dict[str, Any]) -> dict[str, Any]:
        def build():
            low, high = self.target_characters
            outline = self.call("outline-writer", "선택된 기획안으로 장면 구성안을 작성하라.", stage="outline",
                                brief=plan["brief"], concept=concept["chosen"],
                                total_characters=[low, high], lessons=self.lessons("outline-writer"))
            ids = [scene["id"] for scene in outline["scenes"]]
            if ids != [f"{i:02}" for i in range(1, len(ids) + 1)] or not 4 <= len(ids) <= 24:
                raise PipelineError("Outline scenes must be 4-24 consecutive two-digit IDs.")
            cast = {c["id"] for c in outline["characters"]}
            places = {l["id"] for l in outline["locations"]}
            for scene in outline["scenes"]:
                if scene["pov"] not in cast or scene["location"] not in places:
                    raise PipelineError(f"Outline scene {scene['id']} references undeclared cast or location.")
            planned = sum(scene["target_characters"] for scene in outline["scenes"])
            if not low <= planned <= high:
                raise PipelineError(f"Outline plans {planned} characters, outside {low}-{high}.")
            return outline
        return self._stage("outline", build)

    def draft(self, outline: dict[str, Any]) -> dict[str, str]:
        def build():
            scenes: dict[str, str] = {}
            for scene in outline["scenes"]:
                previous = list(scenes.values())[-1][-500:] if scenes else ""
                reply = self.call("scene-writer", "이 장면의 내레이션을 쓰라. 목표 글자 수를 지켜라.", stage="draft",
                                  outline={k: outline[k] for k in ("title", "logline", "synopsis", "characters")},
                                  scene=scene, previous_scene_ending=previous,
                                  lessons=self.lessons("scene-writer"))
                text = reply["narration"].strip()
                if reply["id"] != scene["id"] or not text:
                    raise PipelineError(f"Scene writer returned the wrong or empty scene for {scene['id']}.")
                if not 0.5 * scene["target_characters"] <= len(text) <= 1.7 * scene["target_characters"]:
                    raise PipelineError(f"Scene {scene['id']} length {len(text)} is far from its target.")
                scenes[scene["id"]] = text
            return scenes
        return self._stage("draft", build)

    def _critique(self, outline: dict[str, Any], scenes: dict[str, str], round_index: int) -> dict[str, Any]:
        script = [{"id": s["id"], "title": s["title"], "pov": s["pov"], "narration": scenes[s["id"]]}
                  for s in outline["scenes"]]
        results = {}
        for critic in CRITICS:
            review = self.call(critic, "대본을 네 축으로만 평가하라. 고칠 점은 장면 ID와 함께 구체적으로.",
                               stage="critique", iteration=round_index,
                               outline={k: outline[k] for k in ("title", "logline", "characters")},
                               script=script, lessons=self.lessons(critic))
            if not 0 <= review["score"] <= 10:
                raise PipelineError(f"{critic} returned a score outside 0-10.")
            results[critic] = review
        low, high = self.target_characters
        total = sum(len(text) for text in scenes.values())
        if not low <= total <= high:
            results["length-check"] = {
                "axis": "length", "score": 0, "blocking": True,
                "notes": [{"scene_id": "all", "severity": "blocking", "issue": f"총 {total}자, 목표 {low}-{high}자",
                           "suggestion": "장면 분량을 조정해 목표 범위에 맞춰라."}],
            }
        return results

    def write(self, outline: dict[str, Any], draft: dict[str, str]) -> dict[str, Any]:
        def build():
            scenes = dict(draft)
            history = []
            previous_mean = None
            for round_index in range(1, self.config.loops.script_rounds + 1):
                reviews = self._critique(outline, scenes, round_index)
                axis_scores = {name: review["score"] for name, review in reviews.items()}
                blocking = [name for name, review in reviews.items()
                            if review["blocking"] or any(n["severity"] == "blocking" for n in review["notes"])]
                mean = statistics.fmean(axis_scores.values())
                passed = not blocking and min(axis_scores.values()) >= self.config.thresholds.script_axis
                history.append({"round": round_index, "scores": axis_scores, "mean": round(mean, 3),
                                "blocking": blocking, "passed": passed, "characters": sum(map(len, scenes.values()))})
                if passed:
                    return {"scenes": scenes, "history": history, "passed": True}
                if previous_mean is not None and mean - previous_mean < self.config.loops.minimum_improvement and not blocking:
                    history[-1]["stopped"] = "plateau"
                    break
                previous_mean = mean
                if round_index == self.config.loops.script_rounds:
                    break
                plan = self.call("arbiter", "비평가 의견을 통합해 수정 계획을 세워라. 충돌하는 의견은 근거를 들어 하나로 정하라.",
                                 stage="arbitrate", iteration=round_index, reviews=reviews,
                                 lessons=self.lessons("arbiter"))
                targets = {scene_id for d in plan["decisions"] for scene_id in expand_scene_ids(d["scene_id"], set(scenes))}
                if "all" in targets:
                    targets = set(scenes)
                unknown = targets - set(scenes)
                if unknown or not targets:
                    raise PipelineError(f"Arbiter targeted unknown or no scenes: {sorted(unknown)}.")
                revision = self.call("script-doctor", "수정 계획대로 대상 장면만 다시 써라. 다른 장면과 연결을 유지하라.",
                                     stage="revise", iteration=round_index, plan=plan,
                                     scenes=[{"id": i, "narration": scenes[i]} for i in sorted(targets)],
                                     neighbors={i: scenes[i][:300] for i in scenes if i not in targets},
                                     lessons=self.lessons("script-doctor"))
                returned = {s["id"]: s["narration"].strip() for s in revision["scenes"]}
                if set(returned) != targets or not all(returned.values()):
                    raise PipelineError("Script doctor must return exactly the targeted scenes, nonempty.")
                scenes.update(returned)
            raise PipelineError(
                f"Script gate failed after {len(history)} rounds: last scores {history[-1]['scores']}, "
                f"blocking {history[-1]['blocking']}. Not uploading; see stages/write.json."
            )
        return self._stage("write", build)

    # -- direction -----------------------------------------------------
    def direct(self, plan, concept, outline, script) -> dict[str, Any]:
        def build():
            casting = self.call("voice-director", "인물마다 ko-KR 신경망 음성을 배정하라. 인물끼리 음성이 겹치지 않게.",
                                stage="direction", characters=outline["characters"])
            voices = {v["character_id"]: v for v in casting["voices"]}
            if set(voices) != {c["id"] for c in outline["characters"]}:
                raise PipelineError("Voice director must cast every character exactly once.")
            if len({v["name"] for v in voices.values()}) != len(voices):
                raise PipelineError("Two characters share one voice; casting rejected.")
            for v in voices.values():
                if not VOICE_PATTERN.fullmatch(v["name"]) or not RATE.fullmatch(v["rate"]) or not RATE.fullmatch(v["pitch"]):
                    raise PipelineError(f"Invalid voice settings for {v['character_id']}.")
            art = self.call("art-director", "채널 화풍에 맞는 스타일 가이드와 인물 참조 시트 프롬프트를 만들라.",
                            stage="direction", characters=outline["characters"], locations=outline["locations"],
                            channel_bible=self.bible, lessons=self.lessons("art-director"))
            cast_ids = {c["id"] for c in outline["characters"]}
            if {s["character_id"] for s in art["character_sheets"]} != cast_ids:
                raise PipelineError("Art director must provide one reference sheet per character.")
            scenes = []
            for scene in outline["scenes"]:
                record = {**{k: scene[k] for k in ("id", "title", "pov", "location", "mood")},
                          "narration": script["scenes"][scene["id"]]}
                error = None
                for attempt in range(1, self.config.loops.direction_retries + 2):
                    board = self.call("director", "이 장면을 2-8컷으로 연출하라. 각 컷 앵커는 내레이션의 정확한 부분 문자열이며 순서대로, 첫 컷은 내레이션 맨 앞에서 시작한다.",
                                      stage="direction", iteration=attempt, scene=record,
                                      characters=outline["characters"], style_guide=art["style_guide"],
                                      previous_error=error, lessons=self.lessons("director"))
                    candidate = {**record, "shots": board["shots"]}
                    try:
                        if board["scene_id"] != scene["id"]:
                            raise PipelineError("storyboard for the wrong scene")
                        validate_shots(candidate, cast_ids)
                        break
                    except PipelineError as problem:
                        error = str(problem)
                else:
                    raise PipelineError(f"Director could not produce a valid storyboard for {scene['id']}: {error}")
                scenes.append(candidate)
            chosen = concept["chosen"]
            episode = {
                "schema_version": 3, "id": self.run_dir.name.lower().replace("-", "_")[:32],
                "title": outline["title"], "subtitle": outline["subtitle"], "logline": outline["logline"],
                "fiction_notice": "이 이야기는 창작된 허구이며 AI 음성과 AI 생성 그림을 사용했습니다.",
                "synopsis": outline["synopsis"],
                "characters": [{
                    **{k: c[k] for k in ("id", "name", "age_band", "gender", "appearance")}, "adult": True,
                    "voice": {k: voices[c["id"]][k] for k in ("name", "style", "rate", "pitch")},
                } for c in outline["characters"]],
                "locations": outline["locations"], "scenes": scenes,
            }
            validate_episode(episode, target_characters=self.target_characters)
            return {"episode": episode, "art_direction": art, "hook": chosen["hook"]}
        return self._stage("direction", build)

    # -- packaging -----------------------------------------------------
    def package(self, direction: dict[str, Any]) -> dict[str, Any]:
        def build():
            episode = direction["episode"]
            summary = {k: episode[k] for k in ("title", "subtitle", "logline", "synopsis")}
            feedback: list[str] = []
            for round_index in range(1, self.config.loops.concept_rounds + 1):
                candidates = self.call("packaging-agent", "제목, 썸네일 문구, 설명, 태그, 쇼츠 구간 후보를 만들라.",
                                       stage="packaging", iteration=round_index, episode=summary,
                                       scenes=[{"id": s["id"], "title": s["title"], "opening": s["narration"][:200]}
                                               for s in episode["scenes"]],
                                       feedback=feedback, lessons=self.lessons("packaging-agent"))
                if not candidates["titles"] or not candidates["thumbnail_texts"]:
                    raise PipelineError("Packaging agent returned no title or thumbnail candidates.")
                scores = self.call("click-judge", "시청자 페르소나로 각 제목과 썸네일 문구를 0-10점 채점하라.",
                                   stage="packaging", iteration=round_index, episode=summary,
                                   titles=candidates["titles"], thumbnail_texts=candidates["thumbnail_texts"])
                title = max(scores["titles"], key=lambda s: s["score"])
                thumb = max(scores["thumbnail_texts"], key=lambda s: s["score"])
                if not (0 <= title["index"] < len(candidates["titles"]) and 0 <= thumb["index"] < len(candidates["thumbnail_texts"])):
                    raise PipelineError("Click judge referenced a nonexistent candidate.")
                if min(title["score"], thumb["score"]) >= self.config.thresholds.packaging:
                    return {"title": candidates["titles"][title["index"]],
                            "thumbnail_text": candidates["thumbnail_texts"][thumb["index"]],
                            "description": candidates["description"], "tags": candidates["tags"][:30],
                            "shorts": candidates["shorts"][:3], "scores": scores, "round": round_index}
                feedback = [s["rationale"] for s in scores["titles"] + scores["thumbnail_texts"]]
            raise PipelineError("Packaging gate failed; no title/thumbnail reached the threshold.")
        return self._stage("packaging", build)

    def run_preproduction(self) -> dict[str, Any]:
        plan = self.plan()
        concept = self.concept(plan)
        outline = self.outline(plan, concept)
        draft = self.draft(outline)
        script = self.write(outline, draft)
        direction = self.direct(plan, concept, outline, script)
        packaging = self.package(direction)
        result = {"episode": direction["episode"], "art_direction": direction["art_direction"],
                  "packaging": packaging, "script_history": script["history"],
                  "concept_score": concept["score"], "budget": self.gateway.ledger.summary()}
        (self.run_dir / "episode.json").write_text(json.dumps(direction["episode"], ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
        return result

    # -- final gate ----------------------------------------------------
    def final_review(self, preproduction: dict[str, Any], media_report: dict[str, Any],
                     attempt: int = 1) -> dict[str, Any]:
        verdict = self.call("final-judge", "완성본을 독립적으로 평가하고 업로드 승인 여부를 결정하라.",
                            stage="final", iteration=attempt,
                            episode={k: preproduction["episode"][k] for k in ("title", "logline", "synopsis")},
                            packaging={k: preproduction["packaging"][k] for k in ("title", "thumbnail_text", "description")},
                            script_history=preproduction["script_history"], media_report=media_report)
        technical_ok = bool(media_report.get("technical_pass"))
        approved = verdict["approve"] and technical_ok and verdict["score"] >= self.config.thresholds.final
        return {**verdict, "technical_pass": technical_ok, "approved": approved}
