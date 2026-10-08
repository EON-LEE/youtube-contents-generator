"""Optional Azure Monitor (Application Insights) telemetry for the hosted agent and job.

Nothing is imported or exported unless ``APPLICATIONINSIGHTS_CONNECTION_STRING``
is set; Foundry hosted agents receive that variable from the platform, and the
Container Apps Job receives it from infrastructure (infra/modules/jobs.bicep).
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from ..models import PipelineError

CONNECTION_VARIABLE = "APPLICATIONINSIGHTS_CONNECTION_STRING"
_configured = False


def configure(service_name: str = "story-studio", environ: Mapping[str, str] | None = None) -> bool:
    """Export traces, metrics and logs to Application Insights when configured.

    Returns ``True`` when telemetry is active and ``False`` (a no-op) when no
    connection string is set. Raises ``PipelineError`` when a connection string
    is set but ``azure-monitor-opentelemetry`` is missing, so a misconfigured
    deployment fails loudly instead of running unobserved.
    """
    global _configured
    environ = os.environ if environ is None else environ
    connection_string = environ.get(CONNECTION_VARIABLE, "").strip()
    if not connection_string:
        return False
    if _configured:
        return True
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError as error:
        raise PipelineError(
            f"{CONNECTION_VARIABLE} is set but azure-monitor-opentelemetry is not installed; "
            "install the studio extra."
        ) from error
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    configure_azure_monitor(connection_string=connection_string)
    try:
        from agent_framework.observability import enable_instrumentation
    except ImportError:
        pass
    else:
        enable_instrumentation()
    _configured = True
    return True
