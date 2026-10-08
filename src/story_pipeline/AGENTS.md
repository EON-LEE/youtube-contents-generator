# story_pipeline

**Parent context:** `../AGENTS.md`
**Generated:** 2026-10-08 · **Updated:** 2026-10-08

## Purpose
The whole application. Entry point `python -m story_pipeline` / `story-pipeline`
dispatches to either the offline simulator or the local production path.

## Key Files
| File | Description |
|------|-------------|
| `cli.py` | argparse commands; exit codes `0` ok, `2` awaiting review, `1` error |
| `models.py` | `STAGES`, `JobSpec` (20–30 min, ko-KR only), `Artifact`, `PipelineError`, USD micros helpers |
| `providers.py` | `FixtureProvider` (fixed text, injected failures); `get_provider` rejects anything but `fixture` |
| `roles.py` | Per-stage output contracts; forbids simulated output from claiming real media/quality |
| `runner.py` | Stage loop: fingerprint → invalidate → reserve budget → generate → validate → store; review gates |
| `store.py` | SQLite (`jobs/stages/attempts/approvals`), file lock, artifact integrity, revisions, report |
| `costs.py` | Price catalog (Azure TTS public retail + hypothetical model/image prices), profit sensitivity |
| `media.py` | Real path: episode/storyboard validation, edge-tts synthesis + caching, captions (ASS/SRT), shot timeline, FFmpeg render, loudness, technical evaluation, HTML player |
| `illustrations.py` | Procedural 1920×1080 scene art for 10 fixed `visual` tags + cover |
| `storyboard_art.py` | Procedural per-shot art for fixed `SHOT_SUBJECTS`/`SHOT_DETAILS` |
| `evaluation.py` | Local faster-whisper CER check on scenes `01`, `03`, `11` |
| `comparison.py` | Before/after metrics and side-by-side `comparison.html` for two outputs |

## For AI Agents

### Working In This Directory
- Simulator and production are separate on purpose. `runner.py` refuses non-dry-run and non-simulated providers; adding a live provider requires changing `get_provider`, `Runner.run`, `roles.validate_payload` (`simulated is True` checks) and `Artifact.validate`.
- Story-specific constants are hard-coded in `media.py`: `VOICES` (`mother`, `son`), `VISUALS`, `SHOT_SUBJECTS`, `SHOT_EMOTIONS`, `SHOT_DETAILS`. A new story with other characters/props needs code changes here and matching drawing code in `illustrations.py`/`storyboard_art.py`.
- Cache keys include provider/renderer version strings (e.g. `illustrated-v4-...`). Bump them when output changes, otherwise stale caches are reused.
- Font path `C:\Windows\Fonts\malgun.ttf` is hard-coded in `media.py` (render + shot contact sheet); `illustrations.py` already has Linux fallbacks.
- Speech timing comes only from service `SentenceBoundary` events; never invent timings.

### Testing Requirements
- Pure logic (captions, storyboard, store, costs) is tested without network. Media tests skip when FFmpeg/Pillow/font are missing.
- When changing art, keep output deterministic: tests compare repeated renders.

### Common Patterns
- `write_json` writes atomically via `.tmp` + `replace`.
- Every failure path raises `PipelineError`; the CLI converts it to JSON on stderr.

## Dependencies

### Internal
- `episodes/*.json` is the input contract for `media.load_episode`.

### External
- `edge_tts`, `aiohttp`, `PIL`, `imageio_ffmpeg`, `faster_whisper` (lazy-imported).

## Manual Notes
