"""Foundry hosted agent: the deterministic preproduction pipeline as an Agent Framework workflow.

``team.Studio.run_preproduction`` (bounded multi-agent loops with budgets and
gates) is wrapped in a Microsoft Agent Framework *functional workflow*
(``@workflow`` + ``@step``) with file checkpoint storage, exposed as an agent via
``.as_agent(name="story-studio")`` and served on the Foundry hosted-agent
**Invocations** protocol (``POST /invocations``, port 8088).

A preproduction run takes far longer than an HTTP request may stay open, so the
invoke handler is asynchronous and idempotent per ``run_id``: the first call
starts the run and answers ``202 {"status": "running"}``; repeating the same
call (or ``GET /invocations/{run_id}``) reports ``running``, ``completed`` (with
the summary) or ``failed``. Callers should pass ``agent_session_id=<run_id>`` so
every poll reaches the same hosted sandbox.

Request body: ``{"message": "{\\"run_id\\": \\"2026-10-09\\"}"}`` (the message may
also be a JSON object). Optional fields: ``performance``, ``history``, ``retry``.

All Agent Framework / hosting / Azure imports are lazy so the core package
imports without them. Needed extra: ``agent-framework-core``,
``agent-framework-foundry-hosting``, ``azure-identity``, ``azure-ai-projects``,
``azure-storage-blob`` (only when ``STUDIO_STORAGE_ACCOUNT_URL`` is set).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..models import PipelineError

AGENT_NAME = "story-studio"
RUN_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
STATUS_FILE = "hosted-status.json"
DEFAULT_RUNS_DIR = Path(".story-pipeline") / "studio-runs"


@dataclass(frozen=True)
class StudioRequest:
    run_id: str
    performance: list[dict[str, Any]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    retry: bool = False


def _message_text(message: Any) -> Any:
    """Unwrap the shapes Agent Framework and the hosting servers deliver."""
    if isinstance(message, (list, tuple)):
        if not message:
            raise PipelineError("Empty request; send {\"run_id\": ...}.")
        return _message_text(message[-1])
    if isinstance(message, (dict, str, bytes)):
        return message
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text
    raise PipelineError(f"Unsupported request message type {type(message).__name__}.")


def parse_request(message: Any) -> StudioRequest:
    value = _message_text(message)
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise PipelineError("Request message must be JSON like {\"run_id\": \"2026-10-09\"}.") from None
    if isinstance(value, dict) and "message" in value and "run_id" not in value:
        return parse_request(value["message"])
    if not isinstance(value, dict):
        raise PipelineError("Request message must be a JSON object with run_id.")
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.match(run_id):
        raise PipelineError("run_id must be 1-64 characters of letters, digits, '.', '_' or '-'.")
    performance = value.get("performance", [])
    history = value.get("history", [])
    if not isinstance(performance, list) or not all(isinstance(item, dict) for item in performance):
        raise PipelineError("performance must be a list of objects.")
    if not isinstance(history, list) or not all(isinstance(item, str) for item in history):
        raise PipelineError("history must be a list of strings.")
    return StudioRequest(run_id, performance, history, bool(value.get("retry", False)))


def runs_root(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("STUDIO_RUNS_DIR", str(DEFAULT_RUNS_DIR)))


# -- durable run storage (optional) ---------------------------------------------
class RunStore:
    """Mirrors a run directory to blob storage (container ``runs``) with Entra ID auth,
    so a recycled sandbox resumes from finished stages and the render job can read
    the preproduction output. Disabled when ``STUDIO_STORAGE_ACCOUNT_URL`` is unset."""

    def __init__(self, environ: Mapping[str, str] | None = None):
        environ = os.environ if environ is None else environ
        self.account_url = environ.get("STUDIO_STORAGE_ACCOUNT_URL", "").strip()
        self.container = environ.get("STUDIO_RUNS_CONTAINER", "runs")

    @property
    def enabled(self) -> bool:
        return bool(self.account_url)

    def _client(self):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import ContainerClient
        except ImportError as error:
            raise PipelineError("STUDIO_STORAGE_ACCOUNT_URL is set; install azure-storage-blob.") from error
        return ContainerClient(self.account_url, self.container, credential=DefaultAzureCredential())

    def download(self, run_id: str, run_dir: Path) -> int:
        if not self.enabled:
            return 0
        count = 0
        with self._client() as client:
            for blob in client.list_blobs(name_starts_with=f"{run_id}/"):
                relative = blob.name[len(run_id) + 1:]
                target = (run_dir / relative).resolve()
                if run_dir.resolve() not in target.parents:
                    raise PipelineError(f"Refusing blob outside the run directory: {blob.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(client.download_blob(blob.name).readall())
                count += 1
        return count

    def upload(self, run_id: str, run_dir: Path) -> int:
        if not self.enabled or not run_dir.exists():
            return 0
        count = 0
        with self._client() as client:
            for path in sorted(p for p in run_dir.rglob("*") if p.is_file() and p.suffix != ".tmp"):
                with path.open("rb") as handle:
                    client.upload_blob(f"{run_id}/{path.relative_to(run_dir).as_posix()}", handle, overwrite=True)
                count += 1
        return count


# -- the deterministic pipeline -----------------------------------------------------
def run_preproduction(request: StudioRequest, *, environ: Mapping[str, str] | None = None,
                      transport: Any = None) -> dict[str, Any]:
    """Build config/ledger/gateway/playbook from the environment and run preproduction."""
    from .config import load_config
    from .gateway import AgentGateway, FoundryTransport
    from .ledger import CostLedger
    from .playbook import Playbook
    from .team import Studio

    environ = dict(os.environ if environ is None else environ)
    config = load_config(environ=environ)
    run_dir = runs_root(environ) / request.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    channel_dir = Path(environ.get("STUDIO_CHANNEL_DIR", str(config.channel_dir)))
    bible_path = channel_dir / "bible.md"
    if not bible_path.exists():
        raise PipelineError(f"Missing channel bible: {bible_path}")
    from .state import StateStore
    state = StateStore(Path(environ.get("STUDIO_STATE_DIR", ".story-pipeline/state")), environ)
    state.pull(seed_playbook=channel_dir / "playbook.json")
    playbook = Playbook(state.path("playbook.json"))
    ledger = CostLedger(run_dir / "ledger.json", config.episode_budget_usd)
    gateway = AgentGateway(config, ledger, transport or FoundryTransport(config.project_endpoint),
                           run_dir / "team-trace.jsonl")
    studio = Studio(config, gateway, run_dir, playbook, bible_path.read_text(encoding="utf-8"),
                    request.performance, request.history)
    result = studio.run_preproduction()
    (run_dir / "preproduction.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    episode = result.get("episode", {})
    packaging = result.get("packaging", {})
    return {
        "run_id": request.run_id,
        "status": "completed",
        "title": episode.get("title"),
        "packaging_title": packaging.get("title"),
        "thumbnail_text": packaging.get("thumbnail_text"),
        "concept_score": result.get("concept_score"),
        "script_rounds": len(result.get("script_history", [])),
        "budget": result.get("budget"),
        "playbook_version": playbook.version,
        "outputs": ["episode.json", "preproduction.json", "ledger.json", "team-trace.jsonl"],
    }


# -- Agent Framework workflow ---------------------------------------------------------
Runner = Callable[[StudioRequest], dict[str, Any]]


def build_workflow(runner: Runner | None = None, store: RunStore | None = None,
                   environ: Mapping[str, str] | None = None):
    """Return the ``@workflow`` definition: restore run -> preproduction -> persist run."""
    from agent_framework import step, workflow

    runner = runner or (lambda request: run_preproduction(request, environ=environ))
    store = store or RunStore(environ)
    root = runs_root(environ)

    @step(name="restore-run", replay_key=lambda run_id: f"restore:{run_id}")
    async def restore(run_id: str) -> int:
        return await asyncio.to_thread(store.download, run_id, root / run_id)

    @step(name="preproduction", replay_key=lambda payload: f"preproduction:{payload}")
    async def preproduction(payload: str) -> dict[str, Any]:
        return await asyncio.to_thread(runner, StudioRequest(**json.loads(payload)))

    @step(name="persist-run", replay_key=lambda run_id: f"persist:{run_id}:{time.time_ns()}")
    async def persist(run_id: str) -> int:
        return await asyncio.to_thread(store.upload, run_id, root / run_id)

    @workflow(name=AGENT_NAME, description="Plans, writes, critiques and packages one episode.")
    async def studio_workflow(message: Any) -> str:
        request = parse_request(message)
        await restore(request.run_id)
        try:
            summary = await preproduction(json.dumps(asdict(request), ensure_ascii=False, sort_keys=True))
        finally:
            await persist(request.run_id)
        return json.dumps(summary, ensure_ascii=False)

    return studio_workflow


def build_agent(run_id: str, *, runner: Runner | None = None, store: RunStore | None = None,
                environ: Mapping[str, str] | None = None):
    """One workflow instance (and checkpoint store) per run, exposed as an agent."""
    from agent_framework import FileCheckpointStorage

    checkpoints = runs_root(environ) / run_id / "workflow-checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    definition = build_workflow(runner, store, environ)
    return definition.build(checkpoint_storage=FileCheckpointStorage(checkpoints)).as_agent(name=AGENT_NAME)


# -- Invocations host -------------------------------------------------------------------
class RunTracker:
    """Starts at most one background run per run_id and persists its status."""

    def __init__(self, agent_factory: Callable[[str], Any] = build_agent,
                 environ: Mapping[str, str] | None = None):
        self.agent_factory = agent_factory
        self.environ = environ
        self.tasks: dict[str, asyncio.Task] = {}

    def _status_path(self, run_id: str) -> Path:
        return runs_root(self.environ) / run_id / STATUS_FILE

    def status(self, run_id: str) -> dict[str, Any] | None:
        path = self._status_path(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, run_id: str, value: dict[str, Any]) -> dict[str, Any]:
        path = self._status_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return value

    async def _execute(self, request: StudioRequest) -> None:
        try:
            agent = self.agent_factory(request.run_id)
            response = await agent.run([json.dumps(asdict(request), ensure_ascii=False)])
            summary = json.loads(response.text)
            self._write(request.run_id, {**summary, "status": "completed", "finished": time.time()})
        except Exception as error:  # noqa: BLE001 - reported to the caller, never swallowed
            self._write(request.run_id, {"run_id": request.run_id, "status": "failed",
                                         "error": f"{type(error).__name__}: {error}", "finished": time.time()})

    async def submit(self, request: StudioRequest) -> tuple[int, dict[str, Any]]:
        task = self.tasks.get(request.run_id)
        if task is not None and not task.done():
            return 202, {"run_id": request.run_id, "status": "running"}
        current = self.status(request.run_id)
        if current and current.get("status") == "completed":
            return 200, current
        if current and current.get("status") == "failed" and not request.retry:
            return 200, current
        self._write(request.run_id, {"run_id": request.run_id, "status": "running", "started": time.time()})
        self.tasks[request.run_id] = asyncio.create_task(self._execute(request))
        return 202, {"run_id": request.run_id, "status": "running"}

    def lookup(self, run_id: str) -> tuple[int, dict[str, Any]]:
        if not RUN_ID.match(run_id):
            return 400, {"status": "rejected", "error": "invalid run_id"}
        task = self.tasks.get(run_id)
        if task is not None and not task.done():
            return 202, {"run_id": run_id, "status": "running"}
        current = self.status(run_id)
        if current is None:
            return 404, {"run_id": run_id, "status": "unknown"}
        if current.get("status") == "running":
            # Status file left by a recycled sandbox: nothing is running here any more.
            return 200, {**current, "status": "interrupted"}
        return 200, current


def create_host(tracker: RunTracker | None = None):
    """Invocations-protocol host (``azure-ai-agentserver-invocations``, the base of
    ``agent_framework_foundry_hosting.InvocationsHostServer``) with an async run handler."""
    from azure.ai.agentserver.invocations import InvocationAgentServerHost
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    tracker = tracker or RunTracker()
    host = InvocationAgentServerHost()

    @host.invoke_handler
    async def invoke(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            studio_request = parse_request(body.get("message", body) if isinstance(body, dict) else body)
        except (PipelineError, ValueError) as error:
            return JSONResponse({"status": "rejected", "error": str(error)}, status_code=400)
        code, payload = await tracker.submit(studio_request)
        return JSONResponse(payload, status_code=code)

    @host.get_invocation_handler
    async def get_invocation(request: Request) -> JSONResponse:
        code, payload = tracker.lookup(request.path_params.get("invocation_id", ""))
        return JSONResponse(payload, status_code=code)

    host.studio_tracker = tracker
    return host


def serve() -> None:
    """Container entry point for the Foundry hosted agent (port 8088)."""
    from .telemetry import configure

    configure(service_name=AGENT_NAME)
    create_host().run()


# -- deployment ---------------------------------------------------------------------------
def deploy_hosted_agent(project_client: Any, image: str, *, name: str = AGENT_NAME, cpu: str = "2",
                        memory: str = "4Gi", environment: Mapping[str, str] | None = None) -> Any:
    """Create a hosted-agent version that serves the Invocations protocol 2.0.0."""
    from azure.ai.projects.models import (
        AgentEndpointProtocol,
        ContainerConfiguration,
        HostedAgentDefinition,
        ProtocolVersionRecord,
    )

    reserved = [key for key in (environment or {}) if key.startswith("FOUNDRY_")]
    if reserved:
        raise PipelineError(f"FOUNDRY_* variables are injected by the platform: {reserved}")
    return project_client.agents.create_version(
        agent_name=name,
        definition=HostedAgentDefinition(
            protocol_versions=[ProtocolVersionRecord(protocol=AgentEndpointProtocol.INVOCATIONS, version="2.0.0")],
            cpu=cpu,
            memory=memory,
            container_configuration=ContainerConfiguration(image=image),
            environment_variables=dict(environment or {}),
        ),
        description="Story studio preproduction workflow (Agent Framework functional workflow).",
    )


if __name__ == "__main__":
    serve()
