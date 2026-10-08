"""Versioned playbook of lessons the team carries from episode to episode."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..models import PipelineError

CONFIDENCE = ("low", "medium", "high")


class Playbook:
    """JSON-backed lesson store. Lessons are never deleted, only retired, so the
    reason a rule existed stays auditable."""

    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1:
                raise PipelineError(f"Unsupported playbook schema in {path}.")
            self.version: int = data["version"]
            self.lessons: list[dict[str, Any]] = data["lessons"]
        else:
            self.version = 0
            self.lessons = []

    def active(self, role: str | None = None) -> list[dict[str, Any]]:
        base = re.sub(r"-[abc]\Z", "", role) if role else None
        return [
            lesson for lesson in self.lessons
            if not lesson["retired"] and (
                role is None or "all" in lesson["roles"] or role in lesson["roles"] or base in lesson["roles"]
            )
        ]

    def add(self, text: str, roles: list[str], confidence: str, evidence: str, episode: str) -> str:
        text = text.strip()
        if not text or not roles or confidence not in CONFIDENCE or not evidence.strip():
            raise PipelineError("A lesson needs text, roles, a confidence level and evidence.")
        if any(lesson["text"] == text and not lesson["retired"] for lesson in self.lessons):
            return next(l["id"] for l in self.lessons if l["text"] == text and not l["retired"])
        identifier = f"L{len(self.lessons) + 1:04}"
        self.lessons.append({
            "id": identifier, "text": text, "roles": sorted(set(roles)), "confidence": confidence,
            "evidence": [{"episode": episode, "note": evidence}], "created": time.strftime("%Y-%m-%d"),
            "retired": False, "retired_reason": None,
        })
        return identifier

    def retire(self, identifier: str, reason: str, episode: str) -> None:
        for lesson in self.lessons:
            if lesson["id"] == identifier:
                if lesson["retired"]:
                    return
                lesson.update({"retired": True, "retired_reason": f"{episode}: {reason}"})
                return
        raise PipelineError(f"Unknown lesson {identifier}.")

    def save(self) -> int:
        self.version += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"schema_version": 1, "version": self.version, "lessons": self.lessons},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return self.version

    def markdown(self) -> str:
        """Rendering uploaded to the Foundry vector store for file_search."""
        lines = [f"# 제작 플레이북 v{self.version}", "", "검증된 교훈만 적용한다. 폐기된 교훈은 따르지 않는다.", ""]
        for lesson in self.active():
            lines.append(f"- [{lesson['id']}] ({lesson['confidence']}; 대상: {', '.join(lesson['roles'])}) {lesson['text']}")
        return "\n".join(lines) + "\n"
