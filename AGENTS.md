# youtube-contents-generator

**Generated:** 2026-10-08 · **Updated:** 2026-10-08

## Purpose
Prototype for producing Korean illustrated audio-drama episodes (20–30 minute,
two-narrator family fiction) for a YouTube channel. Python package
`story_pipeline` with two deliberately separate paths:

1. **Offline simulator** (`plan/run/approve/revise/resume/status/cost-report`):
   role contracts, SQLite state, virtual cost ledger, review gates. Fixture
   provider only; never creates media or calls a network service.
2. **Local production** (`produce-local`, `evaluate-speech`, `compare-editions`):
   hand-authored episode JSON → Microsoft Edge TTS (`edge-tts`) → procedural
   Pillow illustrations → FFmpeg MP4 with burned-in subtitles → technical checks.

The two paths are not connected: the simulator's `produce` stage never feeds
`produce-local`. There is no LLM, Azure, image-model, or YouTube integration.

## Key Files
| File | Description |
|------|-------------|
| `README.md` | User guide for both paths, cost evidence, scope boundaries |
| `MIGRATION.md` | Machine handoff, release-archive restore, planned Azure phase |
| `pyproject.toml` | Package metadata; extras `media` (edge-tts, Pillow, imageio-ffmpeg) and `evaluation` (faster-whisper) |
| `requirements-windows-py312.txt` | Constraints file from the working Windows/Python 3.12 environment |
| `.gitignore` | Excludes `outputs/`, `.story-pipeline/`, `.migration/`, secrets |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `src/` | Package source (see `src/AGENTS.md`) |
| `tests/` | `unittest` suite (see `tests/AGENTS.md`) |
| `episodes/` | Hand-authored episode JSON inputs (see `episodes/AGENTS.md`) |
| `.github/workflows/` | `tests.yml`: Windows + Python 3.12, installs `.[media]`, runs unittest on every push/PR |

## For AI Agents

### Working In This Directory
- Set `$env:PYTHONPATH = "$PWD\src"` and `$env:PYTHONIOENCODING = "utf-8"` before running anything.
- Use a venv: `py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -c requirements-windows-py312.txt -e ".[media]"`.
- Real rendering is Windows-only today: it requires `C:\Windows\Fonts\malgun.ttf`.
- `produce-local --allow-external-tts` sends narration to Microsoft's Edge service. Do not run it without the user's permission.
- Never add a silent fallback (Edge, fixture, fake output) when a real provider fails; the codebase fails explicitly by design.
- Do not commit `outputs/`; finished videos live in the GitHub release `local-video-handoff-2026-10-02`.
- Git/PR accounts: the app runs as `eonlee_microsoft` (read-only on this repo). Push works through `~/.gitconfig-eonlee-auth`; create and merge PRs with `$env:GH_TOKEN = gh auth token --user EON-LEE`.

### Testing Requirements
- `python -m unittest discover -s tests -v` (needs the `media` extra; without Pillow `test_storyboard_art` errors on import).
- CI must stay green on `windows-latest`.

### Common Patterns
- Every artifact states whether it is `simulated`; provenance and limitations are written into JSON outputs.
- Content-addressed caching: SHA-256 fingerprints over inputs + provider/renderer version decide reuse.
- Validation raises `PipelineError` with an actionable message instead of guessing.

## Dependencies

### External
- `edge-tts` 7.2 — unofficial Microsoft Edge online speech client (no SLA, not Azure)
- `Pillow` 12 — procedural illustration drawing
- `imageio-ffmpeg` 0.6 — bundled FFmpeg binary
- `faster-whisper` 1.2 — optional local ASR check

## Manual Notes
