"""Learning loop: retrospectives into the playbook, critic calibration against audience
metrics, champion/challenger prompt promotion, and the monthly business report."""
from __future__ import annotations

import json
import statistics
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..models import PipelineError
from .analytics import summarize
from .gateway import check_schema
from .playbook import Playbook
from .roster import BY_NAME, SCHEMAS

DEFAULT_ROLES = frozenset(BY_NAME) | {"all", "concept-writer"}


# -- learn-retro -------------------------------------------------------------------
def apply_retrospective(playbook: Playbook, retro_output: Mapping[str, Any], episode_id: str,
                        allowed_roles: Iterable[str] | None = None) -> dict[str, Any]:
    """Validates the whole retrospective first, then applies it atomically and saves."""
    check_schema(retro_output, SCHEMAS["retrospective"])
    allowed = set(DEFAULT_ROLES if allowed_roles is None else allowed_roles)
    for index, lesson in enumerate(retro_output["lessons"]):
        unknown = set(lesson["roles"]) - allowed
        if unknown or not lesson["roles"]:
            raise PipelineError(f"Retrospective lesson {index + 1} targets unknown roles {sorted(unknown)}.")
        if not lesson["text"].strip() or not lesson["evidence"].strip():
            raise PipelineError(f"Retrospective lesson {index + 1} needs text and evidence.")
    known = {lesson["id"]: lesson for lesson in playbook.lessons}
    missing = [i for i in retro_output["retire_lesson_ids"] if i not in known]
    if missing:
        raise PipelineError(f"Retrospective retires unknown lesson ids {missing}; nothing was applied.")
    before = len(playbook.lessons)
    added, duplicates, retired, already = [], [], [], []
    for lesson in retro_output["lessons"]:
        count = len(playbook.lessons)
        identifier = playbook.add(lesson["text"], lesson["roles"], lesson["confidence"], lesson["evidence"], episode_id)
        (added if len(playbook.lessons) > count else duplicates).append(identifier)
    for identifier in retro_output["retire_lesson_ids"]:
        if known[identifier]["retired"]:
            already.append(identifier)
            continue
        playbook.retire(identifier, "retrospective found it contradicted by results", episode_id)
        retired.append(identifier)
    version = playbook.save() if added or retired else playbook.version
    return {"episode": episode_id, "added": added, "duplicates": duplicates, "retired": retired,
            "already_retired": already, "lessons_before": before, "playbook_version": version}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise PipelineError(f"Corrupt JSON in {path}: {error}") from None


def _compact(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + f"…(+{len(value) - 200})"
    if depth >= 4:
        return "…"
    if isinstance(value, dict):
        return {k: _compact(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        items = [_compact(v, depth + 1) for v in value[:12]]
        return items + ([f"…(+{len(value) - 12} items)"] if len(value) > 12 else [])
    return value


def read_trace(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                raise PipelineError(f"Corrupt trace line {number} in {path}.") from None
    return records


def trace_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    agents: dict[str, dict[str, Any]] = {}
    for record in records:
        entry = agents.setdefault(record["agent"], {"calls": 0, "errors": [], "max_iteration": 0,
                                                    "input_tokens": 0, "output_tokens": 0, "usd": Decimal(0),
                                                    "stages": set()})
        entry["calls"] += 1
        entry["max_iteration"] = max(entry["max_iteration"], int(record.get("iteration", 0)))
        entry["stages"].add(record.get("stage", "?"))
        if "error" in record:
            entry["errors"].append(str(record["error"])[:300])
        else:
            entry["input_tokens"] += int(record.get("input_tokens", 0))
            entry["output_tokens"] += int(record.get("output_tokens", 0))
            entry["usd"] += Decimal(str(record.get("usd", 0)))
    return {name: {**e, "usd": str(e["usd"]), "stages": sorted(e["stages"])} for name, e in sorted(agents.items())}


def run_scores(run_dir: Path, trace: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Downstream quality signals of one run (missing stages are None)."""
    stages = run_dir / "stages"
    scores: dict[str, Any] = {"concept": None, "script": None, "script_axes": None, "packaging": None, "final": None}
    if (stages / "concept.json").exists():
        scores["concept"] = _read_json(stages / "concept.json").get("score")
    if (stages / "write.json").exists():
        history = _read_json(stages / "write.json").get("history") or []
        if history:
            scores["script"], scores["script_axes"] = history[-1].get("mean"), history[-1].get("scores")
    if (stages / "packaging.json").exists():
        titles = _read_json(stages / "packaging.json").get("scores", {}).get("titles") or []
        scores["packaging"] = max((t["score"] for t in titles), default=None)
    trace = read_trace(run_dir / "team-trace.jsonl") if trace is None else trace
    finals = [r["output"]["score"] for r in trace if r.get("agent") == "final-judge" and "output" in r]
    if finals:
        scores["final"] = finals[-1]
    return scores


def retro_inputs(run_dir: Path, analytics_rows: Sequence[Mapping[str, Any]] = (),
                 video_ids: Sequence[str] = ()) -> dict[str, Any]:
    """Compact production record for the retrospective agent prompt."""
    run_dir = Path(run_dir)
    if not (run_dir / "stages").is_dir():
        raise PipelineError(f"{run_dir} has no stages/ directory; not a studio run.")
    trace = read_trace(run_dir / "team-trace.jsonl")
    ledger = None
    if (run_dir / "ledger.json").exists():
        data = _read_json(run_dir / "ledger.json")
        statuses = [e["status"] for e in data.get("entries", [])]
        ledger = {"budget_usd": data.get("budget_usd"), "committed_usd": data.get("committed_usd"),
                  "settled_usd": data.get("settled_usd"), "calls": len(statuses), "failed": statuses.count("failed")}
    return {
        "run": run_dir.name,
        "stages": {p.stem: _compact(_read_json(p)) for p in sorted((run_dir / "stages").glob("*.json"))},
        "agents": trace_summary(trace),
        "scores": run_scores(run_dir, trace),
        "ledger": ledger,
        "metrics": {v: summarize(analytics_rows, v) for v in video_ids},
    }


# -- learn-calibrate -----------------------------------------------------------------
def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 2:
        return None
    try:
        return round(statistics.correlation(xs, ys), 4)
    except statistics.StatisticsError:
        return None


def calibrate(records: Sequence[Mapping[str, Any]], min_samples: int = 8) -> dict[str, Any]:
    """Pearson r of each critic axis against retention (and ctr). Weights stay 1.0 until
    ``min_samples`` episodes exist; then weight = clamp(1 + r_retention, 0.5, 1.5)."""
    axes = sorted({axis for r in records for axis in r.get("axis_scores", {})})
    correlations, weights, not_predictive, samples = {}, {}, [], {}
    for axis in axes:
        pairs = [(float(r["axis_scores"][axis]), r["metrics"]) for r in records
                 if axis in r.get("axis_scores", {}) and r.get("metrics", {}).get("retention") is not None]
        ctr_pairs = [(s, m["ctr"]) for s, m in pairs if m.get("ctr") is not None]
        r_retention = _pearson([s for s, _ in pairs], [float(m["retention"]) for _, m in pairs])
        correlations[axis] = {"retention": r_retention,
                              "ctr": _pearson([s for s, _ in ctr_pairs], [float(c) for _, c in ctr_pairs])}
        samples[axis] = len(pairs)
        if len(pairs) < min_samples or r_retention is None:
            weights[axis] = 1.0
        else:
            weights[axis] = round(min(1.5, max(0.5, 1 + r_retention)), 4)
        if r_retention is not None and abs(r_retention) < 0.1:
            not_predictive.append(axis)
    n = sum(1 for r in records if r.get("metrics", {}).get("retention") is not None)
    return {"n": n, "min_samples": min_samples, "calibrated": n >= min_samples, "samples": samples,
            "correlations": correlations, "weights": weights, "not_predictive": not_predictive,
            "note": None if n >= min_samples else f"Fewer than {min_samples} episodes with retention; weights fixed at 1.0."}


# -- learn-optimize -----------------------------------------------------------------
def build_dataset(run_dirs: Sequence[Path], allow_missing_prompt: bool = False) -> list[dict[str, Any]]:
    """One row per successful agent call: query (prompt), response (output JSON) and the
    run's downstream scores, for Foundry evaluation / prompt optimization."""
    rows = []
    for run_dir in map(Path, run_dirs):
        trace = read_trace(run_dir / "team-trace.jsonl")
        if not trace:
            raise PipelineError(f"{run_dir} has no team-trace.jsonl records.")
        downstream = run_scores(run_dir, trace)
        for record in trace:
            if "output" not in record:
                continue
            query = record.get("prompt")
            if query is None and not allow_missing_prompt:
                raise PipelineError(
                    f"{run_dir / 'team-trace.jsonl'} records no prompt text (only prompt_chars). Log the prompt "
                    "in the gateway trace, or pass allow_missing_prompt=True for output-only rows.")
            rows.append({"run": run_dir.name, "agent": record["agent"], "stage": record.get("stage"),
                         "iteration": record.get("iteration", 0), "agent_version": record.get("agent_version"),
                         "model": record.get("model"), "query": query,
                         "response": json.dumps(record["output"], ensure_ascii=False), "downstream": downstream})
    return rows


def write_dataset(rows: Iterable[Mapping[str, Any]], out_dir: Path) -> dict[str, Path]:
    """Writes one JSONL file per agent."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["agent"], []).append(row)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for agent, items in grouped.items():
        paths[agent] = out_dir / f"{agent}.jsonl"
        paths[agent].write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
    return paths


def _axis_means(scores: Sequence[Mapping[str, float]]) -> dict[str, float]:
    axes = {axis for sample in scores for axis in sample}
    return {axis: statistics.fmean(float(s[axis]) for s in scores if axis in s) for axis in sorted(axes)}


def decide_promotion(baseline_scores: Sequence[Mapping[str, float]], candidate_scores: Sequence[Mapping[str, float]],
                     min_gain: float = 0.3, min_samples: int = 5) -> dict[str, Any]:
    """Each item is one held-out sample's ``{axis: score}``. Promote only if the mean gain is
    at least ``min_gain`` and no axis mean drops by more than 0.5."""
    if len(baseline_scores) != len(candidate_scores):
        raise PipelineError("Baseline and candidate must be scored on the same held-out samples.")
    n = len(candidate_scores)
    if n < min_samples:
        return {"promote": False, "reason": f"Only {n} held-out samples; need {min_samples}.", "n": n}
    base, cand = _axis_means(baseline_scores), _axis_means(candidate_scores)
    if set(base) != set(cand):
        raise PipelineError(f"Baseline and candidate were scored on different axes: {sorted(base)} vs {sorted(cand)}.")
    deltas = {axis: round(cand[axis] - base[axis], 4) for axis in base}
    gain = round(statistics.fmean(statistics.fmean(map(float, s.values())) for s in candidate_scores)
                 - statistics.fmean(statistics.fmean(map(float, s.values())) for s in baseline_scores), 4)
    regressed = [axis for axis, delta in deltas.items() if delta < -0.5]
    if regressed:
        reason, promote = f"Axes regressed by more than 0.5: {regressed}.", False
    elif gain < min_gain:
        reason, promote = f"Mean gain {gain} is below {min_gain}.", False
    else:
        reason, promote = f"Mean gain {gain} >= {min_gain} with no axis regression.", True
    return {"promote": promote, "reason": reason, "gain": gain, "axis_deltas": deltas, "n": n}


def _retention(item: float | Mapping[str, Any]) -> float:
    value = item if isinstance(item, (int, float)) else item.get("retention")
    if value is None:
        raise PipelineError(f"Metric entry without retention: {item!r}")
    return float(value)


def decide_rollback(recent_metrics: Sequence[float | Mapping[str, Any]],
                    previous_metrics: Sequence[float | Mapping[str, Any]],
                    drop: float = 0.15, min_episodes: int = 3) -> dict[str, Any]:
    """Roll back when mean retention of >= ``min_episodes`` recent episodes fell by >= ``drop``
    relative to the previous version's episodes."""
    if len(recent_metrics) < min_episodes:
        return {"rollback": False, "reason": f"Only {len(recent_metrics)} recent episodes; need {min_episodes}."}
    if not previous_metrics:
        return {"rollback": False, "reason": "No baseline episodes from the previous version."}
    recent = statistics.fmean(map(_retention, recent_metrics))
    previous = statistics.fmean(map(_retention, previous_metrics))
    if previous <= 0:
        raise PipelineError("Previous mean retention must be positive to compare.")
    change = round((recent - previous) / previous, 4)
    rollback = change <= -drop
    return {"rollback": rollback, "relative_change": change, "recent_mean": round(recent, 4),
            "previous_mean": round(previous, 4),
            "reason": f"Retention changed {change:+.1%} (threshold -{drop:.0%})."}


class FoundryAgentVersions:
    """Thin wrapper over azure-ai-projects 2.x ``project_client.agents``
    (create_version / get_version / list_versions). A rollback never deletes history:
    it re-publishes an older definition as the newest version."""

    def __init__(self, project_client: Any):
        self.agents = project_client.agents

    def versions(self, agent_name: str) -> list[str]:
        return [str(v.version) for v in self.agents.list_versions(agent_name=agent_name)]

    def promote(self, agent_name: str, definition: Any, metadata: Mapping[str, str] | None = None,
                description: str | None = None) -> dict[str, str]:
        try:
            created = self.agents.create_version(agent_name=agent_name, definition=definition,
                                                 metadata=dict(metadata or {}), description=description)
        except Exception as error:
            raise PipelineError(f"Creating a version of {agent_name} failed: {error}") from error
        return {"agent": agent_name, "version": str(created.version)}

    def rollback(self, agent_name: str, to_version: str) -> dict[str, str]:
        try:
            prior = self.agents.get_version(agent_name=agent_name, agent_version=str(to_version))
        except Exception as error:
            raise PipelineError(f"Cannot read {agent_name} version {to_version}: {error}") from error
        metadata = {**dict(getattr(prior, "metadata", None) or {}), "rollback_of": str(to_version)}
        result = self.promote(agent_name, prior.definition, metadata, f"Rollback to version {to_version}")
        return {**result, "restored_from": str(to_version)}


# -- learn-report -----------------------------------------------------------------
def _trend(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "first": None, "last": None, "slope_per_episode": None, "improving": None}
    slope = statistics.linear_regression(range(len(values)), values).slope if len(values) >= 2 else None
    return {"n": len(values), "first": values[0], "last": values[-1],
            "slope_per_episode": round(slope, 4) if slope is not None else None,
            "improving": None if slope is None else slope > 0}


def monthly_report(ledger_summaries: Sequence[Mapping[str, Any]], analytics_rows: Sequence[Mapping[str, Any]],
                   target_profit_krw: int = 1_000_000, *, usd_krw: float,
                   rpm_usd: float | None = None, month: str | None = None) -> dict[str, Any]:
    """``ledger_summaries``: chronological ``{**CostLedger.summary(), "episode", "final_score"?,
    "script_score"?}``. Revenue is only estimated from an explicit ``rpm_usd`` assumption."""
    if not usd_krw or usd_krw <= 0:
        raise PipelineError("Pass the USD→KRW exchange rate (usd_krw > 0) used for this report.")
    for item in ledger_summaries:
        if "episode" not in item or "committed_usd" not in item:
            raise PipelineError(f"Ledger summary needs 'episode' and 'committed_usd': {item!r}")
    rows = [r for r in analytics_rows if month is None or str(r["date"]).startswith(month)]
    cost = sum((Decimal(str(i["committed_usd"])) for i in ledger_summaries), Decimal(0))
    totals = summarize(rows)
    report: dict[str, Any] = {
        "month": month, "episodes": len(ledger_summaries), "cost_usd": str(cost),
        "cost_krw": round(float(cost) * usd_krw), "usd_krw": usd_krw, "views": totals["views"],
        "watch_hours": totals["watch_hours"], "subscribers_gained": totals["subscribers_gained"],
        "videos": len({r["video_id"] for r in rows}), "target_profit_krw": target_profit_krw,
        "trends": {key: _trend([float(i[f"{key}_score"]) for i in ledger_summaries if i.get(f"{key}_score") is not None])
                   for key in ("final", "script")},
        "cost_note": "committed_usd counts failed reservations; reconcile with Azure Cost Management.",
    }
    if rpm_usd is None:
        report.update({"revenue_usd": None, "profit_krw": None,
                       "revenue_note": "Revenue not estimated; pass rpm_usd (an assumption) or use YouTube Studio revenue."})
    else:
        revenue = totals["views"] / 1000 * rpm_usd
        profit = round((revenue - float(cost)) * usd_krw)
        report.update({"revenue_usd": round(revenue, 2), "profit_krw": profit,
                       "revenue_note": f"ASSUMPTION: revenue = views / 1000 × RPM USD {rpm_usd}; not actual earnings.",
                       "target_progress": round(profit / target_profit_krw, 4) if target_profit_krw else None})
    return report
