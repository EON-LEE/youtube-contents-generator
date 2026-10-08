# studio

**Parent context:** `../AGENTS.md`
**Generated:** 2026-10-08 · **Updated:** 2026-10-08

## Purpose
Autonomous, self-improving production of one episode per run. A team of 25 Foundry
prompt agents on mixed model families plans, writes, critiques and revises in bounded
loops; deterministic code owns control flow, budgets, schemas and gates. Approved
episodes are uploaded to YouTube as **private** videos; analytics and retrospectives
feed a versioned playbook that every agent reads on the next run.

## Key Files
| File | Description |
|------|-------------|
| `config.py` | Loads `studio.toml`: per-agent model deployment, prices, loop caps, thresholds, USD budget |
| `roster.py` | 25 `AgentSpec`s (prompt file, tools, output JSON schema); `check_independence` forbids writers and their reviewers sharing a model family |
| `gateway.py` | `AgentGateway.call`: reserve budget → call agent (`FoundryTransport` Responses API + `agent_reference`, optional images) → strict schema check → settle → trace JSONL (with prompt) |
| `ledger.py` | Real-money `CostLedger`; failed calls keep their reservation; `BudgetExceeded` stops the run |
| `team.py` | `Studio`: plan (sourced research) → concept competition → outline → draft → critique/arbitrate/revise loop → direction → packaging; each stage checkpointed in `stages/*.json`; `final_review` gate |
| `episode_v3.py` | Schema v3 validator + JSON schema (characters, voices, locations, shots with anchors) |
| `playbook.py` | Versioned lessons (never deleted, only retired); role-scoped; markdown for file_search |
| `pipeline.py` | `Production.produce`: preproduction → Azure speech (measured-length loop back) → art + critic loop → mix → render → thumbnails/shorts/captions/metadata → final gate → uploader; `run_retrospective` |
| `speech_azure.py`, `images.py`, `audio_mix.py`, `render_v3.py`, `packaging_media.py` | Media stages (see each module docstring) |
| `youtube.py`, `analytics.py` | Private resumable upload, quota ledger, Analytics v2 collection |
| `learning.py` | Retrospective application, calibration of rubric axes vs retention, champion/challenger promotion/rollback, optimizer datasets, monthly report |
| `state.py` | Durable state (playbook, videos, quota, analytics, calibration) mirrored to blob container `state` |
| `cli.py` | `studio check-models | deploy-agents | job | collect-analytics | learn` |
| `hosted.py` | Foundry hosted-agent wrapper running preproduction (Agent Framework workflow, async invocations) |
| `deploy_agents.py`, `check_models.py`, `telemetry.py` | Idempotent agent/knowledge deployment, deployment verification, App Insights |

## For AI Agents

### Working In This Directory
- Never add a fallback when a provider fails or a gate fails: raise `PipelineError`; a failed final gate means "do not upload".
- Every billable call goes through `AgentGateway.call` or reserves via `CostLedger.reserve` first.
- Uploads are private only (`youtube.PRIVACY`); do not add public/scheduled publishing.
- Changing a roster schema changes deployed agent contracts: update the prompt in `channel/prompts/` and redeploy (`studio deploy-agents`).
- Stage checkpoints make resume free; delete a stage file only when its inputs changed (see `Production._speech_within_target`).
- Deployment names and prices in `studio.toml` are placeholders until `studio check-models` passes against the real project.

### Testing Requirements
- All tests use `ScriptedTransport` and fake media/Google clients; no network. `tests/test_studio_team.py` has the reusable `FakeTeam`.

## Dependencies

### Internal
- `channel/` (bible, rubrics, prompts, seed playbook), `media.py` helpers, `fonts.py`.

### External
- Extras `studio` (Azure SDKs), `youtube` (Google API), `hosted` (Agent Framework, pre-release).

## Manual Notes
