# Development handoff and artifact recovery

## Scope of this recovery guide

This guide restores the **historical 2026-10-02 Edge/Pillow samples**, not the
current Azure-produced pilot. Current capabilities and production status are
maintained in [README.md](README.md#current-production-status-2026-10-14);
deployment prerequisites are in [infra/README.md](infra/README.md).

- Original edition: `episodes\changed-lock.json`, 1,280.44 seconds.
- Revised edition: `episodes\changed-lock-v2.json`, 1,296.50 seconds, 56 directed
  shots, 336 captions, zero captions shorter than one second.
- Both videos are 1280 x 720 at 24 fps, H.264/AAC.

Newer files in `outputs\senior-pilot-2026-10-12\` and `.story-pipeline\` are
local, ignored artifacts. They are not in this archive or a Git clone.
Back them up separately, with their provenance and checksums, before removing
or migrating the existing workspace. Never include credentials in an archive.

## Release assets

Release: [local-video-handoff-2026-10-02](https://github.com/EON-LEE/youtube-contents-generator/releases/tag/local-video-handoff-2026-10-02).

The repository became public on 2026-10-08, so anyone can download this release,
including the `research\` folder. Its ZIP keeps large media out of Git history.
A clone alone does not download it.

Assets:

- `local-video-handoff-2026-10-02.zip`
- `local-video-handoff-2026-10-02.sha256`

ZIP contents:

- `outputs\changed-lock\`: original MP4, SRT, cover, script, voice and scene
  caches, render metadata/logs, review frames, evaluation, and local player.
- `outputs\changed-lock-v2\`: revised equivalents, shot artwork/timeline,
  before/after report, side-by-side player and editorial review.
- `research\`: captured market metadata, LLM quality review, and historical
  planning record. These are dated evidence, not current verified forecasts.
- `migration-manifest.json`: SHA256 and size of every archived file.

Machine-specific workspace paths in textual artifacts are replaced with portable
relative paths or a historical session-path marker. These transformations change
text artifact hashes, not the MP4/audio/image bytes. The migration manifest is the
integrity record for this archive. Historical plan decisions are superseded by
the current README; do not treat old spending proposals as authorization.

Excluded deliberately: `.venv`, downloaded wheels, Whisper model cache, scratch
browser captures/downloads, local simulator DB, font binaries and credentials.
These are not required to recover the source or completed videos. Models and
dependencies can be downloaded afresh; test state can be recreated.

## Clone and set up

Windows with Python 3.12 and Malgun Gothic matches the archived samples.
The renderer also supports Noto Sans CJK and Nanum Gothic, or explicit
`STORY_FONT`/`STORY_FONT_BOLD` paths. Install a supported Korean font on the
target machine before rendering.

```powershell
gh repo clone EON-LEE/youtube-contents-generator
Set-Location .\youtube-contents-generator
git switch main
git pull --ff-only

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -c requirements-windows-py312.txt -e ".[media,evaluation]"
$env:PYTHONPATH = "$PWD\src"
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

For direct MP4 playback, the archive and a compatible media player are sufficient.
Python is needed only for the optional local HTTP player below; no TTS,
recognition model or package installation is needed for playback.

The constraints file records the environment used for these results, including
transitive media/evaluation dependencies. It is not a security audit, hash lock,
or a guarantee of every OS/Python combination. If an exact package is unavailable,
record and test a deliberate replacement rather than silently omitting it.
Never disable TLS verification to get downloads working.

## Download and verify the archive

Run from the repository root:

```powershell
New-Item -ItemType Directory -Force .migration | Out-Null
gh release download local-video-handoff-2026-10-02 `
  --repo EON-LEE/youtube-contents-generator `
  --pattern "local-video-handoff-2026-10-02.*" --dir .migration
if ($LASTEXITCODE -ne 0) { throw "Release download failed" }

$archive = ".migration\local-video-handoff-2026-10-02.zip"
$expected = ((Get-Content ".migration\local-video-handoff-2026-10-02.sha256").Trim() -split "\s+")[0]
if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
    throw "Archive checksum mismatch"
}
Expand-Archive -LiteralPath $archive -DestinationPath .migration\restored

$root = (Resolve-Path .migration\restored).Path
$manifest = Get-Content -Raw -Encoding UTF8 "$root\migration-manifest.json" | ConvertFrom-Json
foreach ($file in $manifest.files) {
    $path = Join-Path $root $file.path
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) {
        throw "Artifact checksum mismatch: $($file.path)"
    }
}
if (Test-Path .\outputs) { throw "Existing outputs found; preserve them before restoring" }
Copy-Item -LiteralPath "$root\outputs" -Destination .\outputs -Recurse
```

The two `outputs` subdirectories must remain siblings for the comparison page.
Research files remain available under `.migration\restored\research`.

## Play the existing videos without generating anything

```powershell
python -m http.server 8000 --bind 127.0.0.1 --directory .\outputs
```

Open:

- `http://127.0.0.1:8000/changed-lock-v2/index.html`
- `http://127.0.0.1:8000/changed-lock-v2/comparison.html`

The old session's ephemeral ports are not durable links. Keep the server bound
to loopback. Stop it with Ctrl+C when finished. A local `episode.mp4` can also be
opened directly in a media player.

## Resume production carefully

The README documents `produce-local`, `evaluate-speech`, and `compare-editions`.
Do not regenerate merely to view the existing videos.

`produce-local --allow-external-tts` sends the episode narration to Microsoft's
Edge service. It is not Azure, not an offline-only command, and not proof of
commercial rights or a service guarantee. Restored matching WAV/scene caches
can be reused; do not edit cached artifacts manually and claim they are still
verified. The short ASR excerpts are cached as audio, but the recognition model
must be downloaded separately if rerunning ASR evaluation.

## Current studio migration

Use the README and infrastructure guide rather than the archive's historical
development plans. Install the extras required by the intended studio stage and
verify authorized resources, model access and identity on the destination.
Do not copy credentials or assume the old machine's login remains available.
Missing credentials or failed providers must stop explicitly, never fall back
to Edge, fixtures or fake output. Uploads require separate authorization.

Technical integrity checks do not establish target-audience satisfaction,
monetization eligibility or profit.
