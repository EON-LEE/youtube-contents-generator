from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..models import PipelineError

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "studio.toml"


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal


@dataclass(frozen=True)
class Loops:
    concept_rounds: int = 2
    script_rounds: int = 4
    minimum_improvement: float = 0.2
    direction_retries: int = 2
    art_retries_per_shot: int = 2
    final_retries: int = 1


@dataclass(frozen=True)
class Thresholds:
    concept: float = 7.0
    script_axis: float = 7.0
    art: float = 7.0
    packaging: float = 7.0
    final: float = 7.5


@dataclass(frozen=True)
class StudioConfig:
    project_endpoint: str
    speech_endpoint: str
    speech_region: str
    image_deployment: str
    episode_budget_usd: Decimal
    agent_models: dict[str, str]
    model_prices: dict[str, ModelPrice]
    image_usd_per_call: Decimal
    speech_usd_per_million_characters: Decimal
    loops: Loops = field(default_factory=Loops)
    thresholds: Thresholds = field(default_factory=Thresholds)
    target_characters: tuple[int, int] = (7300, 10500)
    channel_dir: Path = Path("channel")

    def model_for(self, agent: str) -> str:
        try:
            return self.agent_models[agent]
        except KeyError:
            raise PipelineError(f"No model deployment configured for agent {agent!r}.") from None

    def price_for(self, model: str) -> ModelPrice:
        try:
            return self.model_prices[model]
        except KeyError:
            raise PipelineError(f"No price configured for model {model!r}; execution blocked.") from None


def _decimal(value: Any, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PipelineError(f"{label} must be a number.") from None
    if not amount.is_finite() or amount < 0:
        raise PipelineError(f"{label} must be a finite, nonnegative number.")
    return amount


def _positive_int(value: Any, label: str, maximum: int = 20) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise PipelineError(f"{label} must be an integer between 1 and {maximum}.")
    return value


def load_config(path: Path | None = None, environ: dict[str, str] | None = None) -> StudioConfig:
    environ = dict(os.environ if environ is None else environ)
    path = path or Path(environ.get("STUDIO_CONFIG", DEFAULT_CONFIG))
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PipelineError(f"Studio configuration not found: {path}") from None
    except tomllib.TOMLDecodeError as error:
        raise PipelineError(f"Invalid studio configuration {path}: {error}") from None
    foundry = raw.get("foundry", {})
    speech = raw.get("speech", {})
    budget = raw.get("budget", {})
    prices = raw.get("prices", {})
    loops = raw.get("loops", {})
    thresholds = raw.get("thresholds", {})
    agents = raw.get("agents", {})
    if not isinstance(agents, dict) or not agents:
        raise PipelineError("Configure [agents] with one model deployment per agent.")
    model_prices = {}
    for model, entry in prices.get("models", {}).items():
        model_prices[model] = ModelPrice(
            _decimal(entry.get("input_per_million"), f"{model} input price"),
            _decimal(entry.get("output_per_million"), f"{model} output price"),
        )
    for agent, model in agents.items():
        if model not in model_prices:
            raise PipelineError(f"Agent {agent!r} uses {model!r}, which has no configured price.")
    budget_usd = _decimal(budget.get("episode_usd", 10), "Episode budget")
    if budget_usd <= 0 or budget_usd > 100:
        raise PipelineError("Episode budget must be greater than 0 and at most USD 100.")
    minimum, maximum = raw.get("episode", {}).get("target_characters", [7300, 10500])
    if not (isinstance(minimum, int) and isinstance(maximum, int) and 1000 <= minimum < maximum):
        raise PipelineError("episode.target_characters must be [minimum, maximum] integers.")
    loop_values = Loops(
        concept_rounds=_positive_int(loops.get("concept_rounds", 2), "concept_rounds"),
        script_rounds=_positive_int(loops.get("script_rounds", 4), "script_rounds"),
        minimum_improvement=float(loops.get("minimum_improvement", 0.2)),
        direction_retries=_positive_int(loops.get("direction_retries", 2), "direction_retries"),
        art_retries_per_shot=_positive_int(loops.get("art_retries_per_shot", 2), "art_retries_per_shot"),
        final_retries=_positive_int(loops.get("final_retries", 1), "final_retries"),
    )
    threshold_values = Thresholds(**{
        name: float(thresholds.get(name, getattr(Thresholds, name)))
        for name in ("concept", "script_axis", "art", "packaging", "final")
    })
    for name, value in vars(threshold_values).items():
        if not 0 <= value <= 10:
            raise PipelineError(f"Threshold {name} must be on the 0-10 scale.")
    return StudioConfig(
        project_endpoint=environ.get("FOUNDRY_PROJECT_ENDPOINT", foundry.get("project_endpoint", "")),
        speech_endpoint=environ.get("SPEECH_ENDPOINT", speech.get("endpoint", "")),
        speech_region=environ.get("SPEECH_REGION", speech.get("region", "")),
        image_deployment=environ.get("FOUNDRY_IMAGE_DEPLOYMENT", foundry.get("image_deployment", "")),
        episode_budget_usd=budget_usd,
        agent_models=dict(agents),
        model_prices=model_prices,
        image_usd_per_call=_decimal(prices.get("image_per_call", 0), "Image price"),
        speech_usd_per_million_characters=_decimal(prices.get("speech_per_million_characters", 15), "Speech price"),
        loops=loop_values,
        thresholds=threshold_values,
        target_characters=(minimum, maximum),
        channel_dir=Path(raw.get("channel", {}).get("directory", "channel")),
    )
