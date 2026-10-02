from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from .costs import profit_sensitivity
from .models import APPROVALS, STAGES, JobSpec, PipelineError, money_micros
from .providers import get_provider
from .runner import Runner
from .store import Store


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Story workflow simulator and explicitly authorized local video production. No YouTube integration."
    )
    result.add_argument("--workspace", type=Path, default=Path(".story-pipeline"))
    commands = result.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Create a simulated episode; no API calls.")
    plan.add_argument("episode")
    plan.add_argument("--concept", required=True)
    plan.add_argument("--minutes", type=int, default=25)
    plan.add_argument("--simulation-budget-usd", required=True, help="Virtual budget, NOT spending authorization.")
    plan.add_argument("--max-attempts", type=int, default=2)
    for name in ("run", "resume"):
        command = commands.add_parser(name)
        command.add_argument("episode")
        command.add_argument("--dry-run", action="store_true", required=True)
        command.add_argument("--provider", default="fixture")
        if name == "resume":
            command.add_argument("--recover", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("episode")
    approve = commands.add_parser("approve", help="Record a LOCAL fixture review; no publishing permission.")
    approve.add_argument("episode")
    approve.add_argument("--kind", choices=APPROVALS, required=True)
    approve.add_argument("--reviewer", required=True)
    revise = commands.add_parser("revise")
    revise.add_argument("episode")
    revise.add_argument("--stage", choices=STAGES, required=True)
    revise.add_argument("--instruction", required=True)
    costs = commands.add_parser("cost-report")
    costs.add_argument("episode")
    costs.add_argument("--monthly-cash-krw", type=int, required=True, help="Hypothetical cash cost, not a quote.")
    produce = commands.add_parser("produce-local", help="Synthesize real narration and render a LOCAL video; never upload.")
    produce.add_argument("--episode", type=Path, required=True)
    produce.add_argument("--output", type=Path, required=True)
    produce.add_argument("--allow-external-tts", action="store_true", required=True,
                         help="Explicitly allow original narration to be sent to Microsoft Edge speech.")
    produce.add_argument("--rate", default="+0%")
    produce.add_argument("--audio-only", action="store_true")
    speech_review = commands.add_parser("evaluate-speech", help="Recognize local voice excerpts; never upload audio.")
    speech_review.add_argument("--output", type=Path, required=True)
    speech_review.add_argument("--model", default="small", choices=("tiny", "base", "small"))
    speech_review.add_argument("--allow-model-download", action="store_true")
    comparison = commands.add_parser("compare-editions", help="Compare two preserved local videos; no model or upload calls.")
    comparison.add_argument("--baseline", type=Path, required=True)
    comparison.add_argument("--revised", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == "produce-local":
            from .media import produce_local
            output = asyncio.run(produce_local(
                args.episode, args.output, allow_external_tts=args.allow_external_tts,
                rate=args.rate, audio_only=args.audio_only,
            ))
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        if args.command == "evaluate-speech":
            from .evaluation import evaluate_speech
            output = evaluate_speech(
                args.output, model_name=args.model, allow_model_download=args.allow_model_download
            )
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        if args.command == "compare-editions":
            from .comparison import compare_editions
            output = compare_editions(args.baseline, args.revised)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        store = Store(args.workspace)
        if args.command == "plan":
            store.create(JobSpec(
                args.episode, args.concept, args.minutes,
                money_micros(args.simulation_budget_usd), args.max_attempts,
            ))
            output = store.report(args.episode)
        elif args.command in ("run", "resume"):
            output = Runner(store, get_provider(args.provider)).run(
                args.episode, dry_run=args.dry_run, recover=getattr(args, "recover", False)
            )
        elif args.command == "approve":
            scope = store.approve(args.episode, args.kind, args.reviewer)
            output = {
                "simulated": True, "kind": args.kind, "scope_hash": scope,
                "authority": "local_cli_annotation_not_authenticated_identity",
                "paid_execution_authorized": False, "publishing_authorized": False,
            }
        elif args.command == "revise":
            store.revise(args.episode, args.stage, args.instruction)
            output = store.report(args.episode)
        elif args.command == "cost-report":
            output = {
                "workflow": store.report(args.episode)["costs"],
                "profit_sensitivity": profit_sensitivity(args.monthly_cash_krw),
            }
        else:
            output = store.report(args.episode)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2 if output.get("status") in ("awaiting_script_approval", "awaiting_review") else 0
    except (PipelineError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error), "youtube_connection_available": False}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
