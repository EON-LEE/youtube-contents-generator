"""Publish the roster as versioned Foundry prompt agents plus the shared knowledge store.

Both operations are idempotent: knowledge files are re-uploaded only when their
SHA-256 changes, and a new agent version is created only when its model,
instructions or tool set changed since the version recorded in the manifest.
Clients are injected so the logic is testable without Azure.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ..models import PipelineError
from .config import StudioConfig
from .playbook import Playbook
from .roster import ROSTER, SCHEMAS, AgentSpec, check_independence

KNOWLEDGE_STORE = "channel-knowledge"
DEFAULT_STATE = Path(".story-pipeline") / "knowledge-state.json"
DEFAULT_MANIFEST = Path("agents-manifest.json")
SUPPORTED_TOOLS = ("web_search", "file_search")


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise PipelineError(f"Corrupt state file {path}: {error}") from None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _attr(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def knowledge_documents(channel_dir: Path, playbook: Playbook) -> dict[str, bytes]:
    """The files agents retrieve with file_search: bible, rubrics (optional), playbook."""
    bible = channel_dir / "bible.md"
    if not bible.exists():
        raise PipelineError(f"Missing channel bible: {bible}")
    documents = {"bible.md": bible.read_bytes()}
    rubrics = channel_dir / "rubrics.md"
    if rubrics.exists():
        documents["rubrics.md"] = rubrics.read_bytes()
    documents["playbook.md"] = playbook.markdown().encode("utf-8")
    return documents


def _find_store(openai_client: Any, state: dict[str, Any], name: str) -> str | None:
    """Reuse the recorded store; otherwise adopt a same-named store and rebuild local
    state from the ``name``/``sha256`` attributes stored on each uploaded file (so a
    fresh CI checkout stays idempotent). Files without attributes are removed."""
    known = state.get("vector_store_id")
    if known:
        try:
            openai_client.vector_stores.retrieve(known)
            return known
        except Exception:  # noqa: BLE001 - a deleted store is located or rebuilt below
            pass
    state["files"] = {}
    for store in openai_client.vector_stores.list():
        if _attr(store, "name") != name:
            continue
        store_id = _attr(store, "id")
        for item in list(openai_client.vector_stores.files.list(vector_store_id=store_id)):
            attributes = _attr(item, "attributes") or {}
            file_name, digest = attributes.get("name"), attributes.get("sha256")
            if file_name and digest and file_name not in state["files"]:
                state["files"][file_name] = {"sha256": digest, "file_id": _attr(item, "id")}
            else:
                openai_client.vector_stores.files.delete(_attr(item, "id"), vector_store_id=store_id)
        return store_id
    return None


def sync_knowledge(openai_client: Any, channel_dir: Path, playbook: Playbook, *,
                   state_path: Path = DEFAULT_STATE, store_name: str = KNOWLEDGE_STORE) -> str:
    """Create or update the shared vector store and return its id.

    Changed files are deleted from the store (and the files API) and re-uploaded;
    unchanged files are left alone; files that no longer exist locally are removed.
    """
    documents = knowledge_documents(Path(channel_dir), playbook)
    state = _read_json(state_path, {"vector_store_id": None, "files": {}})
    state.setdefault("files", {})
    store_id = _find_store(openai_client, state, store_name)
    if store_id is None:
        store_id = _attr(openai_client.vector_stores.create(name=store_name), "id")
        state["files"] = {}
    state["vector_store_id"] = store_id
    _write_json(state_path, state)

    def remove(name: str) -> None:
        file_id = state["files"].pop(name, {}).get("file_id")
        if not file_id:
            return
        for delete in (lambda: openai_client.vector_stores.files.delete(file_id, vector_store_id=store_id),
                       lambda: openai_client.files.delete(file_id)):
            try:
                delete()
            except Exception:  # noqa: BLE001 - already gone is the desired end state
                pass

    for name in sorted(set(state["files"]) - set(documents)):
        remove(name)
        _write_json(state_path, state)
    for name, content in documents.items():
        digest = sha256(content)
        if state["files"].get(name, {}).get("sha256") == digest:
            continue
        remove(name)
        uploaded = openai_client.vector_stores.files.upload_and_poll(
            vector_store_id=store_id, file=(name, content), attributes={"name": name, "sha256": digest})
        status = _attr(uploaded, "status")
        if status != "completed":
            raise PipelineError(f"Knowledge file {name} ended with status {status!r}: {_attr(uploaded, 'last_error')}")
        state["files"][name] = {"sha256": digest, "file_id": _attr(uploaded, "id")}
        _write_json(state_path, state)
    return store_id


def build_tools(tool_names: Iterable[str], vector_store_id: str | None, models: Any = None) -> list[Any]:
    """Map roster tool names onto azure-ai-projects tool models."""
    if models is None:
        import azure.ai.projects.models as models

    tools: list[Any] = []
    for name in tool_names:
        if name == "web_search":
            tools.append(models.WebSearchTool())
        elif name == "file_search":
            if not vector_store_id:
                raise PipelineError("file_search requires the channel knowledge vector store id.")
            tools.append(models.FileSearchTool(vector_store_ids=[vector_store_id]))
        else:
            raise PipelineError(f"Unsupported agent tool {name!r}; supported: {', '.join(SUPPORTED_TOOLS)}.")
    return tools


def _tool_fingerprint(tool_names: Iterable[str], vector_store_id: str | None) -> list[str]:
    return [f"file_search:{vector_store_id}" if name == "file_search" else name for name in tool_names]


def _definition_sha256(entry: dict[str, Any]) -> str:
    return sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")))


def _remote_version(project_client: Any, agent_name: str, fingerprint: str) -> str | None:
    """Latest Foundry version of the agent if it was published from the same definition."""
    try:
        details = project_client.agents.get(agent_name=agent_name)
    except Exception as error:  # noqa: BLE001 - only "not found" means "create it"
        if type(error).__name__ == "ResourceNotFoundError" or getattr(error, "status_code", None) == 404:
            return None
        raise
    latest = _attr(_attr(details, "versions"), "latest")
    metadata = _attr(latest, "metadata") or {}
    if metadata.get("definition_sha256") == fingerprint and _attr(latest, "version"):
        return str(_attr(latest, "version"))
    return None


def deploy_agents(project_client: Any, openai_client: Any, config: StudioConfig, *,
                  channel_dir: Path | None = None, playbook: Playbook | None = None,
                  manifest_path: Path = DEFAULT_MANIFEST, state_path: Path = DEFAULT_STATE,
                  roster: Iterable[AgentSpec] = ROSTER, force: bool = False,
                  models: Any = None) -> dict[str, Any]:
    """Create a new prompt-agent version for each roster member whose definition changed.

    "Unchanged" means same model, instructions SHA-256 and tool set (including the
    knowledge vector store id) as the manifest entry or, when the manifest has no
    match (for example a fresh CI checkout), as the latest Foundry version's
    ``definition_sha256`` metadata. Returns the manifest written to
    ``manifest_path`` with ``changed`` marking newly created versions.
    ``models`` defaults to ``azure.ai.projects.models`` (injectable for tests).
    """
    if models is None:
        import azure.ai.projects.models as models

    channel_dir = Path(channel_dir or config.channel_dir)
    playbook = playbook if playbook is not None else Playbook(channel_dir / "playbook.json")
    roster = tuple(roster)
    check_independence(config.agent_models)
    unsupported = {tool for spec in roster for tool in spec.tools if tool not in SUPPORTED_TOOLS}
    if unsupported:
        raise PipelineError(f"Roster uses unsupported tools: {sorted(unsupported)}")
    store_id = None
    if any("file_search" in spec.tools for spec in roster):
        store_id = sync_knowledge(openai_client, channel_dir, playbook, state_path=state_path)

    previous = _read_json(manifest_path, {"agents": {}}).get("agents", {})
    manifest: dict[str, Any] = {"vector_store_id": store_id, "playbook_version": playbook.version, "agents": {}}
    for spec in roster:
        model = config.model_for(spec.name)
        instructions = spec.instructions(channel_dir)
        entry = {
            "model": model,
            "instructions_sha256": sha256(instructions),
            "tools": _tool_fingerprint(spec.tools, store_id),
            "schema": spec.schema,
        }
        fingerprint = _definition_sha256(entry)
        if not force:
            before = previous.get(spec.name, {})
            version = before.get("version") if all(before.get(k) == v for k, v in entry.items()) else None
            version = version or _remote_version(project_client, spec.name, fingerprint)
            if version:
                manifest["agents"][spec.name] = {"name": spec.name, "version": str(version), **entry,
                                                 "changed": False}
                continue
        # The structured-output schema is part of the agent DEFINITION, not the call: the
        # Responses API rejects a per-call `text` parameter once `agent_reference` names an
        # agent (`gateway.FoundryTransport` relies on this being baked in here).
        text_options = models.PromptAgentDefinitionTextOptions(
            format=models.TextResponseFormatJsonSchema(name=spec.schema, schema=SCHEMAS[spec.schema], strict=True)
        )
        details = project_client.agents.create_version(
            agent_name=spec.name,
            definition=models.PromptAgentDefinition(model=model, instructions=instructions,
                                                    tools=build_tools(spec.tools, store_id, models),
                                                    text=text_options),
            description=f"{spec.team} team; output schema {spec.schema}",
            metadata={"definition_sha256": fingerprint, "instructions_sha256": entry["instructions_sha256"],
                      "team": spec.team},
        )
        version = _attr(details, "version")
        if not version:
            raise PipelineError(f"Foundry returned no version for agent {spec.name}.")
        manifest["agents"][spec.name] = {"name": spec.name, "version": str(version), **entry, "changed": True}
        _write_json(manifest_path, {**manifest, "agents": {**previous, **manifest["agents"]}})
    _write_json(manifest_path, manifest)
    return manifest


def connect(config: StudioConfig) -> tuple[Any, Any]:
    """Keyless (Entra ID) project and OpenAI clients for the configured Foundry project."""
    if not config.project_endpoint:
        raise PipelineError("FOUNDRY_PROJECT_ENDPOINT is not configured.")
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError as error:
        raise PipelineError("Install the studio extra: pip install -e .[studio]") from error
    project = AIProjectClient(endpoint=config.project_endpoint, credential=DefaultAzureCredential())
    return project, project.get_openai_client()
