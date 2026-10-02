from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

STAGES = ("research", "write", "edit", "direct", "produce", "qa")
APPROVALS = ("script", "review")
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
MICROS = Decimal(1_000_000)


class PipelineError(Exception):
    """An actionable, expected workflow error."""


class TransientFailure(PipelineError):
    """A fixture failure eligible for bounded retry."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def money_micros(value: str) -> int:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise PipelineError("Budget must be a finite, nonnegative USD amount.") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000"):
        raise PipelineError("USD amount must be between 0 and 1,000,000.")
    micros = amount * MICROS
    if micros != micros.to_integral_value():
        raise PipelineError("Use at most six decimal places for USD amounts.")
    return int(micros)


def usd(micros: int) -> str:
    return format(Decimal(micros) / MICROS, ".6f")


def required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PipelineError(f"{label} must be nonempty text.")
    return value


@dataclass(frozen=True)
class JobSpec:
    episode_id: str
    concept: str
    target_minutes: int
    simulation_budget_micros: int
    max_attempts: int = 2
    language: str = "ko-KR"

    def validate(self) -> None:
        if not ID_PATTERN.fullmatch(self.episode_id):
            raise PipelineError("Episode ID: 1-64 lowercase letters, digits, '-' or '_'.")
        required_text(self.concept, "Concept")
        if len(self.concept) > 5000:
            raise PipelineError("Concept must be at most 5,000 characters.")
        if type(self.target_minutes) is not int or not 20 <= self.target_minutes <= 30:
            raise PipelineError("Target duration must be 20-30 minutes.")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise PipelineError("Maximum attempts must be between 1 and 3.")
        if (
            type(self.simulation_budget_micros) is not int
            or not 0 <= self.simulation_budget_micros <= 1_000_000_000_000
        ):
            raise PipelineError("Invalid simulation budget.")
        if self.language != "ko-KR":
            raise PipelineError("This prototype supports the ko-KR fixture only.")


@dataclass(frozen=True)
class Artifact:
    episode_id: str
    stage: str
    input_hash: str
    payload: dict[str, Any]
    simulated: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "episode_id": self.episode_id,
            "stage": self.stage,
            "input_hash": self.input_hash,
            "simulated": self.simulated,
            "payload": self.payload,
        }

    def validate(self) -> None:
        if self.stage not in STAGES or self.simulated is not True:
            raise PipelineError("Only recognized, explicitly simulated artifacts are allowed.")
        if not ID_PATTERN.fullmatch(self.episode_id):
            raise PipelineError("Invalid artifact episode ID.")
        if not re.fullmatch(r"[0-9a-f]{64}", self.input_hash):
            raise PipelineError("Invalid artifact input hash.")
        if not isinstance(self.payload, dict):
            raise PipelineError("Artifact payload must be an object.")


def ceiling_micros(amount: Decimal) -> int:
    return int((amount * MICROS).to_integral_value(rounding=ROUND_CEILING))
