# channel

**Parent context:** `../AGENTS.md`
**Generated:** 2026-10-08 · **Updated:** 2026-10-08

## Purpose
The team's shared knowledge, uploaded to the Foundry vector store `channel-knowledge`
for `file_search`, and the instructions of every prompt agent.

## Key Files
| File | Description |
|------|-------------|
| `bible.md` | Channel identity, viewer persona, story/language/art/packaging rules. Overrides playbook lessons |
| `rubrics.md` | 0–10 scoring axes for concept, script (one critic per axis), art, packaging, final |
| `playbook.json` | Seed lessons (from the v1→v2 revision). Production copy lives in durable state and grows via retrospectives |
| `prompts/_shared.md` | Prepended to every agent's instructions |
| `prompts/*.md` | One file per role; mapped in `src/story_pipeline/studio/roster.py` |

## For AI Agents

### Working In This Directory
- Prompts are in Korean; output must match the roster JSON schema exactly, so do not ask for prose outside it.
- After editing anything here, run `python -m story_pipeline studio deploy-agents` (new agent versions only for changed definitions).
- Do not edit `playbook.json` lesson history by hand in production; use retrospectives (lessons are retired, not deleted).

## Manual Notes
