# tests

**Parent context:** `../AGENTS.md`
**Generated:** 2026-10-08 · **Updated:** 2026-10-08

## Purpose
`unittest` suite (60 tests). Runs fully offline; no TTS, model download, or upload.

## Key Files
| File | Description |
|------|-------------|
| `test_pipeline.py` | Simulator: reviews, resume, revisions, budget, retries, recovery, locking, integrity, CLI exit codes |
| `test_media.py` | Caption building/overlap handling, SRT/ASS output, episode validation |
| `test_editing.py` | Caption readability merge/extend, storyboard anchors, multi-shot render + cache reuse |
| `test_illustrations.py` | Scene/cover art determinism, wrapping, font errors, expressions |
| `test_storyboard_art.py` | Shot art subjects/details, determinism, no private text on props |
| `test_comparison.py` | Edition comparison and invalidation on changed video |

## For AI Agents

### Working In This Directory
- Run from repo root: `$env:PYTHONPATH="$PWD\src"; python -m unittest discover -s tests -v`.
- Tests that need FFmpeg, Pillow, or a Korean font skip when they are missing.
- Studio tests share builders in `studio_fixtures.py` (v3 episode, fake Azure synthesizer, licensed music/SFX libraries, frames); no Azure call is made.
- Add a test for every new validation rule; tests assert that invalid input fails explicitly.

## Manual Notes
