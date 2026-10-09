"""Calling Foundry prompt agents with budget reservation and schema validation."""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol

from ..models import PipelineError
from .config import StudioConfig
from .ledger import CostLedger
from .roster import SCHEMAS, spec


@dataclass(frozen=True)
class AgentReply:
    agent: str
    output: dict[str, Any]
    model: str
    agent_version: str
    input_tokens: int
    output_tokens: int


def _balanced_json_objects(text: str):
    """Yield every top-level {...} span in ``text``, in reverse (rightmost/last first),
    using brace-depth matching so embedded example snippets with stray braces can't
    merge with the real answer."""
    closers = [i for i, ch in enumerate(text) if ch == "}"]
    for end in reversed(closers):
        depth = 0
        for start in range(end, -1, -1):
            ch = text[start]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    yield text[start:end + 1]
                    break


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output. Some non-OpenAI model families (e.g. DeepSeek,
    Grok) are not grammar-constrained to the agent's structured-output schema and instead
    surround the real answer with chain-of-thought prose, an embedded example of the schema
    itself, and/or a ```json ... ``` fence. Prefer, in order: the whole text as JSON; the last
    fenced ```json block; the last balanced {...} span scanning from the end of the text
    backward (so an earlier schema example can't merge with the real trailing answer)."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    for candidate in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)):
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    for candidate in _balanced_json_objects(text):
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    raise ValueError("no JSON object found in model output")


class Transport(Protocol):
    def respond(self, agent: str, prompt: str, schema_name: str, schema: dict[str, Any],
                max_output_tokens: int, images: tuple[bytes, ...] = ()) -> AgentReply: ...


class FoundryTransport:
    """Invokes a versioned Foundry prompt agent through the project Responses API."""

    def __init__(self, project_endpoint: str):
        if not project_endpoint:
            raise PipelineError("FOUNDRY_PROJECT_ENDPOINT is not configured.")
        try:
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential
        except ImportError as error:
            raise PipelineError("Install the studio extra: pip install -e .[studio]") from error
        self._project = AIProjectClient(endpoint=project_endpoint, credential=DefaultAzureCredential())
        self._openai = self._project.get_openai_client()

    def respond(self, agent, prompt, schema_name, schema, max_output_tokens, images=()):
        content: Any = prompt
        if images:
            content = [{"role": "user", "content": [{"type": "input_text", "text": prompt}] + [
                {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}
                for image in images]}]
        # The structured-output schema is baked into the agent's own definition
        # (deploy_agents.py sets PromptAgentDefinition.text); the Responses API rejects a
        # `text` parameter on the call itself once `agent_reference` names an agent.
        response = self._openai.responses.create(
            input=content,
            max_output_tokens=max_output_tokens,
            extra_body={"agent_reference": {"name": agent, "type": "agent_reference"}},
        )
        if getattr(response, "status", "completed") != "completed":
            raise PipelineError(f"{agent}: response status {response.status}; no output accepted.")
        try:
            output = extract_json_object(response.output_text)
        except (TypeError, ValueError) as error:
            raise PipelineError(f"{agent}: returned non-JSON output despite the schema.") from error
        usage = response.usage
        reference = getattr(response, "agent_reference", None)
        version = reference.get("version") if isinstance(reference, dict) else None
        return AgentReply(
            agent=agent, output=output, model=getattr(response, "model", "unknown"),
            agent_version=str(version or "active"),
            input_tokens=int(usage.input_tokens), output_tokens=int(usage.output_tokens),
        )


class ScriptedTransport:
    """Deterministic transport for tests and local rehearsals. Never used implicitly."""

    def __init__(self, handlers: dict[str, Callable[[str], dict[str, Any]]], model: str = "scripted"):
        self.handlers = handlers
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def respond(self, agent, prompt, schema_name, schema, max_output_tokens, images=()):
        self.calls.append((agent, prompt))
        if agent not in self.handlers:
            raise PipelineError(f"No scripted handler for {agent}.")
        return AgentReply(agent, self.handlers[agent](prompt), self.model, "scripted",
                          max(1, len(prompt) // 2), 200)


# Some non-OpenAI model families (e.g. DeepSeek) are not grammar-constrained to the
# agent's structured-output schema and drift toward natural-language synonyms for enum
# fields. Normalize known synonyms before strict validation instead of hard-failing.
_SEVERITY_SYNONYMS = {
    "info": "minor", "informational": "minor", "low": "minor", "trivial": "minor",
    "nit": "minor", "nitpick": "minor", "cosmetic": "minor",
    "medium": "major", "mid": "major", "moderate": "major", "significant": "major", "warning": "major",
    "high": "blocking", "critical": "blocking", "severe": "blocking", "urgent": "blocking", "fatal": "blocking",
}


def normalize_known_enum_synonyms(value: Any) -> None:
    """Mutates nested dicts/lists in place, mapping known synonym values for the
    ``severity`` field onto the schema's actual enum members."""
    if isinstance(value, dict):
        raw = value.get("severity")
        if isinstance(raw, str) and raw.lower() in _SEVERITY_SYNONYMS:
            value["severity"] = _SEVERITY_SYNONYMS[raw.lower()]
        for child in value.values():
            normalize_known_enum_synonyms(child)
    elif isinstance(value, list):
        for item in value:
            normalize_known_enum_synonyms(item)


def check_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Minimal strict JSON-schema check for the subset the roster uses."""
    if "const" in schema and value != schema["const"]:
        raise PipelineError(f"{path}: expected {schema['const']!r}.")
    if "enum" in schema and value not in schema["enum"]:
        raise PipelineError(f"{path}: {value!r} is not one of {schema['enum']}.")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise PipelineError(f"{path}: expected object.")
        missing = [key for key in schema.get("required", []) if key not in value]
        extra = [key for key in value if key not in schema.get("properties", {})]
        if missing or (extra and schema.get("additionalProperties") is False):
            raise PipelineError(f"{path}: missing {missing} or unexpected {extra}.")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                check_schema(value[key], child, f"{path}.{key}")
    elif kind == "array":
        if not isinstance(value, list):
            raise PipelineError(f"{path}: expected array.")
        for index, item in enumerate(value):
            check_schema(item, schema["items"], f"{path}[{index}]")
    elif kind == "string" and not isinstance(value, str):
        raise PipelineError(f"{path}: expected string.")
    elif kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise PipelineError(f"{path}: expected number.")
    elif kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise PipelineError(f"{path}: expected integer.")
    elif kind == "boolean" and not isinstance(value, bool):
        raise PipelineError(f"{path}: expected boolean.")


class AgentGateway:
    """Budgeted, traced agent calls. Every call is reserved, validated and logged."""

    def __init__(self, config: StudioConfig, ledger: CostLedger, transport: Transport, trace_path: Path):
        self.config = config
        self.ledger = ledger
        self.transport = transport
        self.trace_path = trace_path

    def estimate(self, agent: str, prompt: str, max_output_tokens: int) -> Decimal:
        price = self.config.price_for(self.config.model_for(agent))
        # Korean text can tokenize at up to ~1 token per character; reserve conservatively,
        # plus headroom for agent instructions and retrieved file-search context.
        input_tokens = len(prompt) + 6000
        return (Decimal(input_tokens) * price.input_usd_per_million_tokens
                + Decimal(max_output_tokens) * price.output_usd_per_million_tokens) / Decimal(1_000_000)

    def call(self, agent: str, prompt: str, *, stage: str, iteration: int = 0,
             images: tuple[bytes, ...] = ()) -> dict[str, Any]:
        agent_spec = spec(agent)
        schema = SCHEMAS[agent_spec.schema]
        # Each image is reserved as roughly 2,000 input tokens.
        estimate = self.estimate(agent, prompt + " " * (2000 * len(images)), agent_spec.max_output_tokens)
        # Non-OpenAI model families (DeepSeek, Grok) are not grammar-constrained to the
        # agent's structured-output schema via the Responses API and occasionally drift
        # into a differently-shaped JSON object. Retry a bounded number of times before
        # giving up, since most attempts do conform.
        max_attempts = 5
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            # On retry, reinforce the exact required JSON shape in the call itself: the
            # schema baked into the agent's definition at deploy time is not always
            # enough grip for a model family that isn't grammar-constrained to it.
            call_prompt = prompt if attempt == 1 else (
                prompt + "\n\n[필수] 반드시 다음 JSON 스키마의 필드명과 구조를 정확히 그대로 사용해 답하라. "
                "다른 필드명이나 구조를 쓰면 안 된다:\n```json\n" + json.dumps(schema, ensure_ascii=False) + "\n```")
            entry = self.ledger.reserve(f"{stage}:{agent}", estimate,
                                        {"agent": agent, "iteration": iteration, "attempt": attempt})
            started = time.time()
            try:
                reply = self.transport.respond(agent, call_prompt, agent_spec.schema, schema,
                                               agent_spec.max_output_tokens, images)
                normalize_known_enum_synonyms(reply.output)
                check_schema(reply.output, schema)
                if agent_spec.schema == "critique" and not 0 <= reply.output["score"] <= 10:
                    raise PipelineError(f"{agent}: score {reply.output['score']!r} outside 0-10.")
                break
            except Exception as error:
                self.ledger.fail(entry, str(error))
                self._trace({"stage": stage, "agent": agent, "iteration": iteration, "attempt": attempt,
                             "error": str(error), "seconds": round(time.time() - started, 2)})
                last_error = error if isinstance(error, PipelineError) else PipelineError(
                    f"{agent} call failed: {error}")
                if attempt == max_attempts:
                    raise last_error from error
        price = self.config.price_for(self.config.model_for(agent))
        actual = (Decimal(reply.input_tokens) * price.input_usd_per_million_tokens
                  + Decimal(reply.output_tokens) * price.output_usd_per_million_tokens) / Decimal(1_000_000)
        self.ledger.settle(entry, actual, {"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                                           "model": reply.model})
        self._trace({
            "stage": stage, "agent": agent, "iteration": iteration, "model": reply.model,
            "agent_version": reply.agent_version, "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens, "usd": str(actual), "seconds": round(time.time() - started, 2),
            "prompt_chars": len(prompt), "prompt": prompt, "output": reply.output,
        })
        return reply.output

    def _trace(self, record: dict[str, Any]) -> None:
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": time.time(), **record}, ensure_ascii=False) + "\n")
