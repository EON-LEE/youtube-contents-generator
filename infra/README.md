# Azure infrastructure

Managed services only, keyless (Microsoft Entra ID) everywhere. Nothing in this folder
has been deployed by the repository; run it yourself after reviewing the what-if.

```
Logic Apps Standard ──(MI, https://ai.azure.com)──► Foundry hosted agent "story-studio"
  daily-episode                                      (Agent Framework workflow → 25 prompt agents)
  daily-learning   ──(MI, ARM jobs/start)──────────► Container Apps Job (render / upload /
                                                     collect-analytics / learn)
Storage (runs, library, outputs, table analytics) · Key Vault (RBAC) · ACR · Log Analytics +
Application Insights · Consumption budget · failed-run metric alert → email action group
```

| File | Purpose |
| --- | --- |
| `main.bicep` | Entry point; parameters for regions, name prefix, model deployments, budget, job trigger, orchestrator schedule |
| `modules/foundry.bicep` | Foundry resource (`Microsoft.CognitiveServices/accounts`, kind `AIServices`, `allowProjectManagement`, `disableLocalAuth`) + project + model deployments (also serves Azure Speech) |
| `modules/storage.bicep` | StorageV2 with shared-key access disabled; containers `runs`, `library`, `outputs`; table `analytics` |
| `modules/keyvault.bicep` | Key Vault in RBAC mode (YouTube OAuth token etc.) |
| `modules/registry.bicep` | ACR Basic, admin user off, AcrPull for the workload identity and the Foundry project identity |
| `modules/jobs.bicep` | Container Apps environment (logs via diagnostic settings, no workspace key) + job (user-assigned identity, ACR pull by identity, Manual or Schedule trigger) |
| `modules/logicapp.bicep` | Logic Apps Standard (WS1) with system identity, keyless host storage, role assignments, failed-run alert |
| `modules/monitoring.bicep` | Log Analytics, workspace-based Application Insights, email action group |
| `modules/budget.bicep` | Monthly Consumption budget on the resource group (80 %/100 % actual, 100 % forecast) |
| `deploy.ps1` | what-if → confirm → deploy; optional ACR builds, workflow zip deploy, hosted-agent version; writes `.env.azure` |
| `logicapp/` | Logic Apps Standard project: `daily-episode`, `daily-learning`, `parameters.json` (reads app settings) |
| `hosted-agent/` | Dockerfile and agent manifest for the Foundry hosted agent |

## Identities and roles

| Principal | Role | Scope |
| --- | --- | --- |
| `<prefix>-workload-id` (job) | Foundry User | Foundry project |
| | Cognitive Services User | Foundry account (OpenAI, image, Speech data planes) |
| | Storage Blob Data Contributor, Storage Table Data Contributor | storage account |
| | Key Vault Secrets User | Key Vault |
| | AcrPull | registry |
| Foundry project system identity | AcrPull | registry (pulls the hosted-agent image) |
| Logic App system identity | Container Apps Jobs Operator + Reader | job |
| | Foundry User | Foundry project (invokes the hosted agent) |
| `<prefix>-orchestrator-host-id` | Storage Blob/Queue/Table Data Contributor | storage account (Logic Apps runtime state) |
| `deployerPrincipalId` (optional) | Foundry Project Manager | Foundry project (publishes agents) |
| `hostedAgentPrincipalId` (optional) | Storage Blob Data Contributor | storage account (run state) |

## Deploy

```powershell
Copy-Item infra/main.parameters.example.json infra/main.parameters.json   # fill alertEmail, anthropicProviderData
./infra/deploy.ps1 -ResourceGroup rg-story-studio                          # what-if, confirm, deploy
./infra/deploy.ps1 -ResourceGroup rg-story-studio -BuildImages -DeployWorkflows -DeployHostedAgent
python -m story_pipeline studio check-models
python -m story_pipeline studio deploy-agents
```

`deploy.ps1` prints `$env:` lines and writes `.env.azure` (ignored by git; endpoints only, no secrets).
CI does the same through `.github/workflows/deploy.yml` (OIDC federated credential; secrets
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`; variable `AZURE_RESOURCE_GROUP`).

## Before the first deployment (manual)

1. **Model versions and regions.** Every `modelDeployments` entry is a placeholder to confirm
   against the live catalog for `foundryLocation` (default `eastus2`, because the Claude families
   are offered in `eastus2`/`swedencentral`; the rest of the stack defaults to `koreacentral`).
   Defaults: `gpt-5.5` 2026-04-24, `gpt-5-mini` 2025-08-07, `gpt-image-1` 2025-04-15 (from the
   Foundry Models region tables); `claude-opus-5-5`/`claude-sonnet-5` version `2` (Hosted on Azure);
   `DeepSeek-V3.2` version `1`; `grok-4-1-fast-reasoning` version `1`. Deployment *names* match
   `studio.toml [agents]`; `check-models` fails if any is missing.
2. **Partner model terms.** Claude deployments send `modelProviderData`
   (`organizationName`, `countryCode`, `industry`), which accepts the Anthropic Azure Marketplace
   offer on your behalf. Review Anthropic's terms first, and make sure the subscription is
   eligible (paid billing account in a supported country, Marketplace purchases allowed).
   Grok (xAI) and DeepSeek are Foundry Models sold by Azure; confirm quota.
3. **Image model access.** `gpt-image-1` may require an access request; set `imageDeployment.name`
   to `''` to skip it until approved.
4. **Web search.** The `web_search` agent tool (`WebSearchTool`) needs no Bing resource. Only
   domain-restricted search (Bing Custom Search) or the classic Grounding with Bing tool need a
   Bing resource and project connection.
5. **Hosted agent identity.** Foundry creates a dedicated Entra identity per hosted agent at
   deployment. After the first `-DeployHostedAgent`, redeploy with
   `hostedAgentPrincipalId=<that object id>` so it can persist run state in the `runs` container.
6. **Logic Apps Standard cost.** WS1 is billed per hour for an always-on instance whether or not
   workflows run, which is a large share of a USD 400 monthly budget. Standard was chosen per the
   plan (source-controlled stateful workflows, keyless host storage, VNET-ready). If cost matters
   more, set `deployOrchestrator=false` and `jobTriggerType=Schedule` (the job then runs on
   `jobCronExpression` without Logic Apps), or port the two workflow definitions to Logic Apps
   Consumption (same workflow definition language; pay per action).
7. **Speech region.** Speech uses the Foundry account, so `SPEECH_REGION` equals `foundryLocation`
   (the job receives it as an environment variable, overriding `studio.toml`).

## Verified API facts

| Fact | Source |
| --- | --- |
| Foundry account + project resource shape; Anthropic deployments use `format: 'Anthropic'`, `GlobalStandard`, `modelProviderData` (API `2025-10-01-preview`) | Claude starter kit Bicep, https://learn.microsoft.com/azure/developer/ai/how-to/deploy-claude-foundry |
| xAI format `xAI`, Grok model ids/versions | https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-grok |
| DeepSeek-V3.2 version `1`, Foundry Models sold by Azure | https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure |
| gpt-5.5 / gpt-5-mini / gpt-image-1 versions | https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability |
| Web search tool needs no Bing resource for general search | https://learn.microsoft.com/azure/foundry/agents/how-to/tools/web-search |
| Container Apps Jobs - Start: `POST .../jobs/{job}/start`, body = execution template (`containers[]`), 200 returns `{name, id}`, 202 + `Location`; caller needs `Microsoft.App/jobs/start/action` (Container Apps Jobs Operator) | https://learn.microsoft.com/rest/api/resource-manager/containerapps/jobs/start, https://learn.microsoft.com/azure/container-apps/jobs |
| Logic Apps Standard keyless host storage (`AzureWebJobsStorage__managedIdentityResourceId`, `__credential=managedIdentity`, service URIs) | https://learn.microsoft.com/azure/logic-apps/create-single-tenant-workflows-azure-portal#set-up-managed-identity-access-to-your-storage-account |
| `WorkflowRunsCompleted` metric with `status` dimension | https://learn.microsoft.com/azure/azure-monitor/reference/supported-metrics/microsoft-web-sites-metrics |
| Built-in role ids (Foundry User, Foundry Project Manager, Cognitive Services User, Container Apps Jobs Operator/Reader) | https://learn.microsoft.com/azure/role-based-access-control/built-in-roles |
| Hosted agents: Invocations protocol 2.0.0, port 8088, `HostedAgentDefinition`, invoke `{project}/agents/{name}/endpoint/protocols/invocations?api-version=v1` with audience `https://ai.azure.com` | https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent |

`az bicep build --file infra/main.bicep` and `az bicep lint` complete without warnings
(Bicep CLI 0.48.1). The REST API version used by the workflows for Container Apps is
`2024-03-01` (parameter `containerAppsApiVersion` in `logicapp/parameters.json`).
