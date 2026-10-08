# episodes

**Parent context:** `../AGENTS.md`
**Generated:** 2026-10-08 · **Updated:** 2026-10-08

## Purpose
Hand-authored episode inputs for `produce-local`. Written by an agent session,
not generated at runtime.

## Key Files
| File | Description |
|------|-------------|
| `changed-lock.json` | Baseline "내 집 열쇠를 돌려받던 날": 14 scenes, one image per scene |
| `changed-lock-v2.json` | Revised edition: same story, 7,859 narration characters, per-scene storyboards (56 shots total) |

## For AI Agents

### Working In This Directory
- Top-level keys: `id`, `title`, `subtitle`, `fiction_notice`, `synopsis`, `characters`, `scenes`, `editorial_notes`, `revision_notes`.
- Scene: `id` (two digits), `title`, `pov` (`mother`|`son`), `visual` (one of `media.VISUALS`), `narration`, optional `shots`.
- Shot: `id`, `anchor` (unique exact substring of the narration, in order, first at position 0), `subject`, `emotion`, `reason`, and `detail` for `hands`/`phone`/`room`/`apron`.
- Allowed values live in `src/story_pipeline/media.py`. New values need matching art code.
- Never edit `changed-lock.json`; it is the comparison baseline. Render each edition to its own output directory.
- Total narration must measure 20–30 minutes after synthesis or production stops.

## Manual Notes
