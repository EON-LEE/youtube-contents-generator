"""``python -m story_pipeline studio <command>``: the operational entry points used by
the Container Apps Job, the Logic Apps workflows and CI.

Commands: check-models, deploy-agents, job, collect-analytics, learn.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from ..models import PipelineError
from .config import load_config
from .playbook import Playbook
from .state import StateStore


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _context():
    config = load_config()
    channel = Path(_env("STUDIO_CHANNEL_DIR", str(config.channel_dir)))
    state = StateStore(Path(_env("STUDIO_STATE_DIR", ".story-pipeline/state")))
    state.pull(seed_playbook=channel / "playbook.json")
    return config, channel, state, Playbook(state.path("playbook.json"))


def _read(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _youtube_services():
    from .youtube import EnvSecretProvider, KeyVaultSecretProvider, build_services, youtube_credentials
    vault = _env("KEY_VAULT_URL") or _env("STUDIO_KEY_VAULT_URL")
    provider = KeyVaultSecretProvider(vault) if vault else EnvSecretProvider()
    return build_services(youtube_credentials(provider))


def make_uploader(state: StateStore, run_id: str = "", services: Callable[[], tuple[Any, Any]] = _youtube_services):
    """Uploads inside a state transaction so the quota ledger and video registry are
    updated atomically and recorded the moment the upload succeeds."""
    from .youtube import QuotaLedger, upload_episode

    def upload(package: dict[str, Any]) -> dict[str, Any]:
        youtube, _ = services()
        title = package["metadata"]["title"]
        shorts = [{"path": short["file"], "title": f"{short.get('hook', '').strip()} | {title}"[:90],
                   "description": short.get("hook", "")} for short in package["shorts"]]
        with state.transaction():
            result = upload_episode(youtube, Path(package["video"]), package["metadata"], Path(package["thumbnail"]),
                                    [Path(p) for p in package["captions"]], shorts,
                                    QuotaLedger(state.path("youtube-quota.json")))
            if result.get("status") == "uploaded":
                videos = _read(state.path("videos.json"), [])
                videos.append({"video_id": result["video_id"], "run": run_id, "title": title,
                               "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                               "shorts": result.get("shorts", []), "post_analytics_retro": False})
                _write(state.path("videos.json"), videos)
        return result
    return upload


def _studio_for_run(config, channel: Path, playbook: Playbook, run_dir: Path, transport=None,
                    history: list[str] | None = None):
    from .gateway import AgentGateway, FoundryTransport
    from .ledger import CostLedger
    from .team import Studio
    ledger = CostLedger(run_dir / "ledger.json", config.episode_budget_usd)
    gateway = AgentGateway(config, ledger, transport or FoundryTransport(config.project_endpoint),
                           run_dir / "team-trace.jsonl")
    request = _read(run_dir / "request.json", {})
    return Studio(config, gateway, run_dir, playbook, (channel / "bible.md").read_text(encoding="utf-8"),
                  request.get("performance", []), request.get("history", history or []))


def _sync_knowledge(config, channel: Path, playbook: Playbook, state: StateStore) -> str | None:
    if not config.project_endpoint:
        return None
    from .deploy_agents import connect, sync_knowledge
    _, openai_client = connect(config)
    return sync_knowledge(openai_client, channel, playbook, state_path=state.path("knowledge-state.json"))


def _retrospective_in_transaction(state: StateStore, studio, rows=(), video_ids=()) -> dict[str, Any]:
    """Apply lessons to the freshest playbook under the state lock."""
    from .pipeline import run_retrospective
    with state.transaction():
        studio.playbook = Playbook(state.path("playbook.json"))
        return run_retrospective(studio, rows, video_ids)


def command_job(run_id: str, *, media=None, transport=None) -> dict[str, Any]:
    from .audio_mix import load_library
    from .hosted import RunStore, runs_root
    from .pipeline import MediaOps, Production
    from .state import sync_library
    config, channel, state, playbook = _context()
    library = Path(_env("STUDIO_LIBRARY_DIR", "library"))
    sync_library(library)
    load_library(library / "music", "music")  # fail before any billed call if the library is unusable
    load_library(library / "sfx", "sfx")
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    runs = RunStore()
    runs.download(run_id, run_dir)
    titles = [v["title"] for v in _read(state.path("videos.json"), [])]
    studio = _studio_for_run(config, channel, playbook, run_dir, transport, history=titles)
    uploader = make_uploader(state, run_id) if _env("STUDIO_UPLOAD") == "private" else None
    production = Production(studio, media or MediaOps.real(config, studio.gateway.ledger), library / "music",
                            library / "sfx", uploader)
    try:
        result = production.produce()
        try:
            result["retrospective"] = _retrospective_in_transaction(state, studio)
        except PipelineError as error:
            # The episode result (and any upload record) is already safe; learning can retry later.
            result["retrospective"] = {"error": str(error)}
        result["knowledge_store"] = _sync_knowledge(config, channel, studio.playbook, state)
        return result
    finally:
        runs.upload(run_id, run_dir)


def command_collect_analytics(today: dt.date | None = None, services=_youtube_services) -> dict[str, Any]:
    from .analytics import JsonAnalyticsStore, collect
    _, _, state, _ = _context()
    today = today or dt.date.today()
    end = today - dt.timedelta(days=2)  # YouTube Analytics data lags 2-3 days.
    with state.transaction():
        videos = _read(state.path("videos.json"), [])
        if not videos:
            return {"collected": 0, "reason": "No uploaded videos yet."}
        start = min(dt.date.fromisoformat(v["uploaded_at"][:10]) for v in videos)
        if start > end:
            return {"collected": 0, "reason": "Analytics not available yet (2-day lag)."}
        _, analytics = services()
        store = JsonAnalyticsStore(state.path("analytics.json"))
        return collect(analytics, store, [{"video_id": v["video_id"], "episode": v["run"]} for v in videos],
                       start.isoformat(), end.isoformat())


def command_learn(today: dt.date | None = None, transport=None, min_age_days: int = 7) -> dict[str, Any]:
    from .analytics import JsonAnalyticsStore, summarize
    from .hosted import RunStore, runs_root
    from .learning import build_dataset, calibrate, decide_rollback, run_scores, write_dataset
    from .pipeline import run_retrospective
    config, channel, state, _ = _context()
    today = today or dt.date.today()
    runs = RunStore()
    report: dict[str, Any] = {"retrospectives": [], "calibration": None, "rollback": None}
    records, run_dirs = [], []
    with state.transaction():
        playbook = Playbook(state.path("playbook.json"))
        videos = _read(state.path("videos.json"), [])
        rows = JsonAnalyticsStore(state.path("analytics.json")).rows()
        for video in videos:
            run_dir = runs_root() / video["run"]
            runs.download(video["run"], run_dir)
            run_dirs.append(run_dir)
            video_rows = [r for r in rows if r["video_id"] == video["video_id"]]
            metrics = summarize(video_rows, video["video_id"]) if video_rows else {}
            age = (today - dt.date.fromisoformat(video["uploaded_at"][:10])).days
            if video_rows and age >= min_age_days and not video.get("post_analytics_retro"):
                studio = _studio_for_run(config, channel, playbook, run_dir, transport)
                summary = run_retrospective(studio, video_rows, [video["video_id"]])
                video["post_analytics_retro"] = True
                _write(state.path("videos.json"), videos)
                report["retrospectives"].append({"run": video["run"], **summary})
                runs.upload(video["run"], run_dir)
            scores = run_scores(run_dir)
            if scores.get("script_axes") and metrics.get("retention") is not None:
                records.append({"episode": video["run"], "axis_scores": scores["script_axes"],
                                "metrics": {"retention": metrics["retention"], "ctr": metrics.get("ctr")}})
        report["calibration"] = calibrate(records)
        _write(state.path("calibration.json"), report["calibration"])
    retention = [r["metrics"] for r in records]
    report["rollback"] = decide_rollback(retention[-3:], retention[-6:-3])
    dataset = runs_root() / "_datasets"
    report["dataset_files"] = {k: str(v) for k, v in write_dataset(build_dataset(run_dirs, allow_missing_prompt=True),
                                                                   dataset).items()} if run_dirs else {}
    runs.upload("_datasets", dataset)
    report["knowledge_store"] = _sync_knowledge(config, channel, playbook, state)
    return report


def command_deploy_agents(force: bool) -> dict[str, Any]:
    from .deploy_agents import connect, deploy_agents
    config, channel, state, playbook = _context()
    project, openai_client = connect(config)
    return deploy_agents(project, openai_client, config, channel_dir=channel, playbook=playbook,
                         state_path=state.path("knowledge-state.json"), force=force)


def command_check_models() -> dict[str, Any]:
    from .check_models import check_models
    from .deploy_agents import connect
    config = load_config()
    project, _ = connect(config)
    return check_models(config, project)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="story_pipeline studio")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-models")
    deploy = commands.add_parser("deploy-agents")
    deploy.add_argument("--force", action="store_true")
    job = commands.add_parser("job")
    job.add_argument("--run-id", default=_env("STUDIO_RUN_ID"))
    commands.add_parser("collect-analytics")
    commands.add_parser("learn")
    args = parser.parse_args(argv)
    try:
        from .telemetry import configure
        configure()
        if args.command == "check-models":
            output = command_check_models()
        elif args.command == "deploy-agents":
            output = command_deploy_agents(args.force)
        elif args.command == "job":
            if not args.run_id:
                raise PipelineError("Pass --run-id or set STUDIO_RUN_ID.")
            output = command_job(args.run_id)
        elif args.command == "collect-analytics":
            output = command_collect_analytics()
        else:
            output = command_learn()
    except (PipelineError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as error:  # Azure/Google SDK errors: keep the traceback for operators.
        import traceback
        traceback.print_exc()
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0
