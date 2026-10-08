"""Verify that every deployment named in studio.toml exists in the Foundry project."""
from __future__ import annotations

from typing import Any

from ..models import PipelineError
from .config import StudioConfig
from .roster import check_independence


def _deployment_record(deployment: Any) -> dict[str, Any]:
    def field(name: str) -> Any:
        if isinstance(deployment, dict):
            return deployment.get(name)
        return getattr(deployment, name, None)

    return {
        "name": field("name"),
        "type": field("type"),
        "model_name": field("model_name"),
        "model_version": field("model_version"),
        "model_publisher": field("model_publisher"),
    }


def check_models(config: StudioConfig, project_client: Any) -> dict[str, Any]:
    """List project deployments and confirm the configured agent and image models exist.

    ``project_client`` is an ``azure.ai.projects.AIProjectClient`` (or any object
    exposing ``deployments.list()``). Raises ``PipelineError`` when the agent
    model mix violates writer/critic independence or any deployment is missing.
    """
    check_independence(config.agent_models)
    deployments = [_deployment_record(item) for item in project_client.deployments.list()]
    available = {record["name"] for record in deployments if record["name"]}
    required: dict[str, list[str]] = {}
    for agent, model in sorted(config.agent_models.items()):
        required.setdefault(model, []).append(agent)
    if config.image_deployment:
        required.setdefault(config.image_deployment, []).append("image")
    else:
        raise PipelineError("No image deployment configured (foundry.image_deployment / FOUNDRY_IMAGE_DEPLOYMENT).")
    missing = {model: users for model, users in required.items() if model not in available}
    report = {
        "project_endpoint": config.project_endpoint,
        "deployments": deployments,
        "required": required,
        "missing": missing,
        "independence": "ok",
    }
    if missing:
        details = "; ".join(f"{model} (used by {', '.join(users)})" for model, users in sorted(missing.items()))
        raise PipelineError(
            f"Foundry project is missing {len(missing)} deployment(s): {details}. "
            f"Available: {', '.join(sorted(available)) or 'none'}. Deploy them (infra/main.bicep) "
            "or update [agents]/image_deployment in studio.toml."
        )
    return report
