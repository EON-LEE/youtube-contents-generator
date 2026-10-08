# Foundry hosted agent: `story-studio`

`src/story_pipeline/studio/hosted.py` wraps `team.Studio.run_preproduction` in a Microsoft
Agent Framework functional workflow (`@workflow` + `@step`, `FileCheckpointStorage` per run),
exposes it with `.as_agent(name="story-studio")`, and serves it on the hosted-agent
**Invocations** protocol (port 8088) through `azure-ai-agentserver-invocations`.

`azure.ai.agentserver.agentframework.from_agent_framework` belongs to the retired protocol 1.0.0
packages; the current hosting package is `agent-framework-foundry-hosting`
(`ResponsesHostServer` / `InvocationsHostServer`, protocol 2.0.0). Its `InvocationsHostServer`
waits for the agent inside the request, which is too long for a preproduction run, so
`hosted.create_host()` registers an asynchronous handler on the same
`InvocationAgentServerHost` base:

| Request | Response |
| --- | --- |
| `POST /invocations` `{"message": {"run_id": "episode-2026-10-09"}}` (first time) | `202 {"status": "running"}` |
| same request while running | `202 {"status": "running"}` |
| same request after finishing | `200 {"status": "completed", "title": ..., "budget": ...}` or `200 {"status": "failed", "error": ...}` |
| add `"retry": true` after a failure | restarts; finished stages are reused from checkpoints |
| `GET /invocations/{run_id}` | current status |

Pass `agent_session_id=<run_id>` on every call so polls reach the same sandbox. With
`STUDIO_STORAGE_ACCOUNT_URL` set, the run directory is restored from and saved to the `runs`
blob container, so a recycled sandbox resumes from finished stages.

## Deploy (verified paths)

Python SDK (used by `infra/deploy.ps1 -DeployHostedAgent` and `.github/workflows/deploy.yml`):

```powershell
az acr build -r <acr> -t story-studio-agent:<tag> -f infra/hosted-agent/Dockerfile .
python -c "from azure.ai.projects import AIProjectClient; from azure.identity import DefaultAzureCredential; from story_pipeline.studio.hosted import deploy_hosted_agent; p=AIProjectClient(endpoint='<project endpoint>', credential=DefaultAzureCredential()); print(deploy_hosted_agent(p, '<acr>.azurecr.io/story-studio-agent:<tag>', environment={'STUDIO_STORAGE_ACCOUNT_URL': 'https://<storage>.blob.core.windows.net/'}).version)"
```

Requirements (from *Deploy a hosted agent*): Foundry Project Manager on the project, AcrPull
(granted by `infra/modules/registry.bicep`) for the project identity, linux/amd64 image.

Azure Developer CLI alternative: `azd ext install azure.ai.agents`, then
`azd ai agent init -m infra/hosted-agent/agent.manifest.yaml` and `azd deploy`.

Local test: `python -m story_pipeline.studio.hosted`, then
`Invoke-RestMethod -Method Post http://localhost:8088/invocations -ContentType application/json -Body '{"message":{"run_id":"local-1"}}'`.

Platform-injected variables: `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME`,
`FOUNDRY_AGENT_VERSION`, `APPLICATIONINSIGHTS_CONNECTION_STRING`. The `FOUNDRY_*` prefix is
reserved, so `FOUNDRY_IMAGE_DEPLOYMENT` cannot be set on the hosted agent; preproduction does
not need it (the image deployment comes from `studio.toml`).
