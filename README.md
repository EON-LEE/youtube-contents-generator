# Illustrated story workflow and local video production

**Moving to another machine:** see [MIGRATION.md](MIGRATION.md) for setup,
the public release containing both completed videos and production artifacts,
checksum verification, and the remaining Azure integration work.

Two deliberately separate paths are available:

- **Offline simulator:** role contracts, persistent state and virtual cost gates
  using fixed fixtures. No external calls or media.
- **Actual local production:** original Korean episode JSON → two real narration
  voices → original locally drawn illustrations → burned-in subtitles and a
  local MP4 → technical and optional local speech-recognition evaluation.

**There is no YouTube connection, authentication, upload or publishing command.**
The actual production path uses Microsoft Edge online speech through `edge-tts`
with explicit permission. It is **not an authenticated Azure Speech integration**.
No Azure credentials were available for the sample; no cloud resources were
created. Unkeyed Edge access is not a production SLA or commercial-use clearance.

The business target is **KRW 1,000,000 monthly pretax cash profit after actual
service/operating costs, excluding the owner's labor cost**. This prototype does
not establish audience demand, monetization eligibility, or profitability.

## Revised edition: narrative-directed cuts

The baseline `episodes\changed-lock.json` and its rendered output remain unchanged.
The revised input is `episodes\changed-lock-v2.json`; render it to a **different**
output directory:

```powershell
$env:PYTHONPATH = "$PWD\src"
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -m story_pipeline produce-local `
  --episode .\episodes\changed-lock-v2.json `
  --output .\outputs\changed-lock-v2 `
  --allow-external-tts
```

Revised scenes carry an explicit storyboard: each shot has a subject, emotion,
editorial reason, and a unique phrase in the narration. Shot changes follow
the corresponding service caption timing, rounded to a video frame; inside a
caption the alignment is proportional, not word-perfect forced alignment.
Missing, ambiguous or out-of-order anchors are rejected. Scenes without a
storyboard retain the single-image rendering path.

Close-ups of work schedules, pay records, keys, the letter, sewing and character
reactions replace generic location-only imagery. These are original procedural
illustrations, not photorealistic actors or generated live action. `shot-timeline.json`
records what appears, why, and when. Shot count and shot duration are measured
in `evaluation.json`; more cuts alone are **not** evidence of better retention.

Short subtitle cues are merged with an adjacent cue only if all text still fits
within two 24-character lines, or extended into an existing silence by at most
0.6 seconds. The target display time is 1.1 seconds. Cases that cannot be improved
without losing text or damaging timing remain explicitly flagged, not concealed.
The final report counts remaining cues shorter than one second.

Compare two completed outputs without calling any model or service:

```powershell
.\.venv\Scripts\python.exe -m story_pipeline compare-editions `
  --baseline .\outputs\changed-lock --revised .\outputs\changed-lock-v2
```

The command writes `before-after.json` and, for sibling output folders, a local
`comparison.html` with both players. The v2 sample measures 1,296.50 seconds,
56 narration-anchored shots (average 23.16 seconds, maximum 31.67 seconds), and
zero subtitle cues below one second. The baseline measures 1,280.44 seconds,
14 shots (average 91.46 seconds), and eight such short cues. Narrative prose is
shorter (8,042 → 7,859 characters), but the revised delivery is slightly longer;
this is not an overall-runtime reduction or proof of improved audience retention.

Shot-specific `detail` fields distinguish hand actions, messages versus calls,
dry versus damp rooms, and apron fitting versus repaired clothing. Missing or
unsupported details for those subjects are rejected rather than replaced with
a generic picture.

## Produce a real local episode

Python 3.12+ and the `media` extra are required. Use an isolated environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[media]'
$env:PYTHONPATH = "$PWD\src"
$env:PYTHONIOENCODING = "utf-8"

.\.venv\Scripts\python.exe -m story_pipeline produce-local `
  --episode .\episodes\changed-lock.json `
  --output .\outputs\changed-lock `
  --allow-external-tts
```

`--allow-external-tts` permits sending this episode's narration to the Edge speech
service. It does not authorize uploads to video platforms. The bundled FFmpeg
comes from `imageio-ffmpeg`; nothing is installed system-wide. The current subtitle
renderer requires Windows Malgun Gothic at `C:\Windows\Fonts\malgun.ttf`. The font
is copied only for local rendering, then removed from the temporary fonts folder.

If package downloads fail because of a TLS transport problem, do not disable
certificate validation or use `--trusted-host`. The development environment used
a reachable public wheel mirror, verified each wheel's SHA256 against the
official PyPI release metadata, then installed the verified wheels offline.
This was an environment workaround, not a project-wide package-index change.

### Sample and artifacts

`episodes\changed-lock.json` is an original fictional family story:
**내 집 열쇠를 돌려받던 날**. Fourteen scenes alternate between a mother and her
adult son. All named characters are adults. The script was authored by a writing
agent and reviewed for continuity; the runtime does not autonomously create new
scripts or scrape other creators' stories.

The output directory contains:

| File | Content |
| --- | --- |
| `episode.mp4` | Actual 1280×720, 24 fps H.264/AAC local video, Korean subtitles, chapters |
| `episode.ko.srt` | Separate timed Korean subtitle track |
| `cover.png` | Original cover illustration |
| `episode-script.json` | Copy of the actual input episode |
| `production.json` | Real/simulated status, providers, measured duration, cost limitations |
| `evaluation.json` | Full decode check, duration, loudness/peak and subtitle checks |
| `review-frames\` | Actual frames extracted from the final video |
| `shot-timeline.json` | Actual cuts, narration anchors and scene/shot durations |
| `scenes\` | Cached real audio, service timings, original artwork, scene videos and logs |

Speech is cached by narration, voice, rate and provider version, with a WAV hash
check. Rerunning reuses matching speech and matching encoded scenes. Generated
boundaries must cover the narration exactly after punctuation/whitespace
normalization; missing service timings are an error, not replaced with invented
alignment. Long sentence boundaries are proportionally subdivided for readable
two-line captions, so their within-sentence timing is approximate.

`--audio-only` measures narration before rendering. A complete production run
rejects narration outside 20–30 minutes rather than filling the video with
silence. `--rate=+5%` explicitly changes the speech rate and invalidates speech
caches. The renderer adds only a short inter-scene pause, a subtle camera move,
and final loudness normalization; no copyrighted music or stock images are used.

The production API-billed amount is zero for the unkeyed speech/local artwork
path. **That is not a claim of zero total cost:** agent/app credits, existing
subscriptions, hardware, electricity, connectivity and setup effort are excluded.
Before commercial operation, confirm service terms and use a supported paid
provider where needed.

### Evaluate speech locally

Optional local recognition needs the `evaluation` extra and a model download.
Only model files are downloaded; episode audio stays on the machine.

```powershell
.\.venv\Scripts\python.exe -m pip install '.[evaluation]'
.\.venv\Scripts\python.exe -m story_pipeline evaluate-speech `
  --output .\outputs\changed-lock --model small --allow-model-download
```

This recognizes short excerpts from scenes 01, 03 and 11 without feeding it the
reference script as a prompt. `speech-evaluation.json` compares normalized
characters against the expected narration. Recognition errors can be ASR errors
rather than pronunciation errors. They do not prove emotional acting quality,
human enjoyment or suitability for advertising. The measurements apply to the
sampled narration WAVs; the final MP4 is separately decoded and measured.

## Offline simulator

`research → write → edit → direct → script review → produce → QA → final review`

Roles have distinct output contracts. The fixture provider supplies short,
fixed original test material rather than generated stories. Every artifact is
marked `simulated`; QA explicitly leaves listening, audio, visuals, rights, and
audience demand **untested**. The target duration is a requested duration, not a
measured or achieved runtime.

SQLite stores stage state, input versions, attempts, approvals, and virtual
costs. A workspace lock allows one writer at a time. Completed artifacts are
integrity-checked and reused. Revisions invalidate the selected stage and its
successors; earlier artifacts and spent virtual costs remain in the history.
Retries are bounded, including across process restarts.

Python + SQLite is the initial execution layer. Hermes could later provide
planning/memory; OpenClaw could provide messaging; n8n could provide an operations
UI. None is installed, required, or connected by this prototype. Role names do
not represent independently running live model agents yet.

### Run the simulator on Windows

Requires Python 3.12+. The simulator has no runtime dependencies and does not
require installation or FFmpeg.

From this repository, in PowerShell:

```powershell
$env:PYTHONPATH = "$PWD\src"
$env:PYTHONIOENCODING = "utf-8"

python -m story_pipeline plan demo --concept "Two adult relatives remember one decision differently." --minutes 25 --simulation-budget-usd 1
python -m story_pipeline run demo --dry-run
```

The run stops at `awaiting_script_approval`. Inspect the JSON artifacts under
`.story-pipeline\artifacts\demo\`, then record a **fixture review**:

```powershell
python -m story_pipeline approve demo --kind script --reviewer owner
python -m story_pipeline resume demo --dry-run
```

This stops at `awaiting_review`. Inspect the simulated production manifest and QA
report before recording the second review:

```powershell
python -m story_pipeline approve demo --kind review --reviewer owner
python -m story_pipeline resume demo --dry-run
python -m story_pipeline status demo
python -m story_pipeline cost-report demo --monthly-cash-krw 170000
```

Final status is `simulated_complete`, **not production-ready**. There is no upload
or publish command. CLI exits: `0` success, `2` waiting for review (or argparse
usage error), `1` actionable execution error.

The `--simulation-budget-usd` amount is a **virtual per-episode test limit**,
not permission to spend money. Actual external spend always remains zero.
Pass `--workspace PATH` before the subcommand to select another state directory.
Do not share a workspace with untrusted users.

### Revisions and interrupted attempts

```powershell
python -m story_pipeline revise demo --stage write --instruction "Give the adult child a different motivation."
python -m story_pipeline resume demo --dry-run
```

The fixture records this instruction; it does **not** perform a creative rewrite.
Changed artifact versions require new reviews. A repeated, unchanged instruction
does not reset the attempt limit. Revisions do not refund earlier virtual usage.

If the process was interrupted during a stage, inspect `status`, ensure no
worker is active, and use:

```powershell
python -m story_pipeline resume demo --dry-run --recover
```

Recovery retains the interrupted attempt and conservatively consumes its virtual
reservation. It does not reset its attempt count. On-disk artifact modification
causes an explicit integrity error rather than silent regeneration; inspect the
change and use `revise` if regeneration is intended.

`approve` is a local CLI annotation attributed to the supplied reviewer string,
**not authenticated proof of a person's identity**. A process with access to
this directory can modify the database. Before real service use, independent
authorization, secret handling, real usage reconciliation, and publication
permissions need a separate design and approval.

## Costs: evidence versus assumptions

The virtual ledger records estimates, reservations, failed attempts and simulated
settlements. Failed fixture attempts consume their estimated virtual costs; this
is conservative test behavior, not a claim about a provider's billing rules.

The default catalog includes:

| Item | Price | Evidence |
| --- | --- | --- |
| Azure S1 Neural TTS, East US | USD 15 per 1,000,000 billable characters | Public retail API observed 2026-09-20 |
| Unselected model input | USD 1 per 1,000,000 tokens | Hypothetical fixture assumption |
| Unselected model output | USD 2 per 1,000,000 tokens | Hypothetical fixture assumption |
| Unselected image model | USD 0.05 per image | Hypothetical fixture assumption |

Speech source: [Azure Retail Prices API](https://prices.azure.com/api/retail/prices),
product `Azure Speech`, region `eastus`, meter
`0f98e708-a16c-407b-8089-a0ed9e14ab49`, Consumption, tier minimum 0. The response
marks `isPrimaryMeterRegion=false`. Recheck current prices and region/voice
availability before actual use. This price does not cover Custom Voice or the
legacy Long Audio meter, and does not imply every voice supports every SSML style.

For example, 30,000 billable characters at that price cost USD 0.45 for one
synthesis before taxes/retries. That character count does **not** establish
20–30 minutes of natural narration. The fixture uses very short text and fixed
token/image quantities; its tiny virtual total is **not a full-video quote**.
Compute, storage, transmission, licensing, taxes, exchange rates, model selection
and actual retry rates remain to be priced. USD usage is not implicitly converted
to the KRW business scenarios.

At a **hypothetical** KRW 170,000 monthly cash cost, the KRW 1,000,000 profit
target needs KRW 1,170,000 creator ad revenue. Hypothetical ad-only creator receipts
of KRW 1,000 / 3,000 / 5,000 per 1,000 total views require respectively
1,170,000 / 390,000 / 234,000 monthly catalog views. These rates are not measured
genre RPM. They are after platform revenue sharing, so do not subtract the
platform share again. Before YPP ad-revenue eligibility, creator ad revenue is
zero. Labor time is an operational measure, not a deducted cash cost.

## Tests

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
```

Coverage includes offline execution with network calls prohibited, explicit
reviews, persistence, bounded retries, interrupted recovery, artifact integrity,
revision invalidation, workspace locking, missing prices, virtual budget blocking,
cash-only economics and rejection of live providers.

## Scope boundaries

Actual **local** production and evaluation are now authorized and implemented
separately from the simulator. Cloud provisioning, paid provider billing and
platform publishing are not implicitly enabled. There is no implemented YouTube
adapter. The sample's quality and technical measurements do not establish
revenue, retention, target-age demographics or viral potential.

YouTube API uploads from unverified projects created after 2020-07-28 are
restricted to private viewing until the project passes an audit. A local review
does not lift that restriction. See [videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert).
Scheduling also has privacy and prior-publication restrictions; no publishing
workaround is provided here.