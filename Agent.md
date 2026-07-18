# Agent.md — GifAgent Project Reference

> Quick-reference for AI agents working on this codebase. Read this first.

## What This Project Does

Local movie-scene GIF auto-tagging and preference-mining agent. Scans GIFs/videos → VLM frame analysis → LLM synthesis → FAISS vector index → RAG-enhanced clip discovery → candidate GIF export → human feedback → preference profile building.

## Current Model Stack

| Role | Model | Provider | Endpoint |
|------|-------|----------|----------|
| VLM | `llava:13b` | Ollama (local) | `http://localhost:11434` |
| LLM | `deepseek-v4-flash` | DeepSeek (cloud) | `https://api.deepseek.com/v1` |
| Embedding | `nomic-embed-text:latest` | Ollama (local) | `http://localhost:11434` |

- **LLM is cloud-based, handles NSFW content without rejection** (tested 3/3 success on adult content)
- API key env var: `DEEPSEEK_API_KEY` (sk- prefix, DeepSeek native key)
- LLM provider is `openai_compatible` (NOT anthropic_compatible — that was the old Ark setup)

## Key Parameters (scripts/test_video_adaptive.py)

```python
SAMPLE_INTERVAL = 10          # coarse sampling every N seconds
MERGE_GAP = 15                # max gap to merge adjacent frames
MERGE_SCORE_THRESHOLD = 0.50  # only merge when BOTH frames >= this
WORTHINESS_THRESHOLD = 0.50   # min score to keep a frame
REFINE_THRESHOLD = 0.65       # min score to trigger refinement sampling
REFINE_RADIUS = 10            # seconds around high-score frame
REFINE_INTERVAL = 10          # fine sampling interval
OUTPUT_RATIO = 0.45           # fraction of deduped clips to export
MAX_OUTPUT = 40               # hard cap per video
EMBED_SIM_THRESHOLD = 0.90    # text embedding duplicate threshold
TEMPORAL_DEDUP_MIN_GAP_S = 12 # keep highest score within peak-time window
POTPLAYER_PBF_ENABLED = True  # write PotPlayer bookmark file beside exports
VLM_OPTIONS = {"temperature": 0.50, "top_p": 0.90, "top_k": 40}
```

**Score-gated merge logic**: adjacent frames merge only when `gap <= MERGE_GAP` AND both frames' `gif_worthiness >= MERGE_SCORE_THRESHOLD`. This produces a mix of long multi-frame GIFs (sustained good moments) + short single-frame GIFs (isolated moments).

**Duplicate reduction**: each adaptive run clears the target video output
folder before exporting new GIFs, then applies text-embedding dedup followed by
temporal dedup. The result JSON records `embedding_deduped_clips` and final
`deduped_clips`.

**PotPlayer bookmarks**: adaptive export writes `{video_name}.pbf` in the same
export folder when `potplayer_pbf_enabled=true`. Each successful GIF contributes
one bookmark at the GIF start time, with the title carrying rank, interval,
score, merge type, and caption summary.

## How to Run

### Prerequisites
```bash
ollama pull llava:13b
ollama pull nomic-embed-text:latest
export DEEPSEEK_API_KEY=sk-...     # DeepSeek native API key
```

### Batch video processing (main workflow)
```bash
# Single video
uv run python scripts/test_video_adaptive.py --video <path>

# Batch with checkpoint resume
uv run python scripts/test_video_batch.py --dir "<video_dir>" --extensions ".ts,.mp4,.mkv"
```

Output structure: `data/exports/adaptive_test/{input_folder_name}/{video_name}/`
contains GIFs named `{video_name}@@@{seq}_{start_ms}ms-{end_ms}ms.gif` and a
PotPlayer bookmark file named `{video_name}.pbf`.

### Packaged GUI

```bash
uv run pyinstaller --noconfirm build_exe.spec
```

Output: `dist/GifAgentUI/GifAgentUI.exe`. If an older packaged GUI is running,
stop it before rebuilding because it holds `dist/GifAgentUI/data/library.db`.
Use `bash scripts/rebuild_exe.sh` for an in-place release. It preserves both
`dist/GifAgentUI/data/` and the writable `dist/GifAgentUI/configs/` directory.
These contain the runtime databases, history, exports junction, labels,
Preference Memory, and settings edited through the UI. Never replace only the
EXE: release the matching `_internal/` tree as well. Before and after building,
compare hashes for the writable config, databases, and checkpoints. A full
historical queue rerun is optional; prefer a small new-video smoke test.

### Services
```bash
# FastAPI (port 8000)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# Gradio candidate review UI (port 7861)
uv run python app/ui/candidate_review.py

# Gradio original review UI (port 7860)
uv run python app/ui/review.py
```

## Architecture Overview

```
app/
├── main.py                    # FastAPI app (35+ endpoints)
├── routers/
│   ├── candidates.py          # GET /api/candidates/folders, GET /api/candidates, POST /api/candidates/{id}/feedback
│   ├── preference.py          # profile build/publish/evaluate endpoints
│   ├── tasks.py               # Task engine command/status API (7 endpoints)
│   └── quality_lab.py         # Quality Lab API (9 endpoints — runs, AB sessions, champions)
├── services/
│   ├── llm_client.py          # Shared LLM client (Ollama + OpenAI + Anthropic compatible)
│   ├── json_guard.py          # Unified JSON parser (strips <think>, markdown fence)
│   ├── quality.py             # Placeholder detection + Pydantic validation
│   ├── video_fingerprint.py   # Duration + keyframe pHash dedup (pre-processing)
│   ├── preference_*.py        # Preference Memory subsystem (6 tables)
│   ├── reranker.py            # Score-gated preference reranker
│   ├── provenance.py          # Provenance dataclass (git commit, config hash, model versions)
│   └── candidates.py          # Candidate GIF materialization
├── quality_lab/               # Phase 2: Quality Lab benchmarking (see below)
├── task_engine/               # Phase 1: Reliable task processing engine (see below)
├── ui/
│   ├── candidate_review.py    # Gradio UI: review + batch control (port 7861)
│   ├── review.py              # Gradio UI: original GIF review (port 7860)
│   ├── control_tab.py         # Control tab (now API-backed; legacy mode via GIFAGENT_LEGACY_QUEUE_UI=1)
│   └── quality_lab_tab.py     # Quality Lab tab (blind A/B, promotion, history)
scripts/
├── task_worker.py             # Single-writer worker process (task engine consumer)
├── import_legacy_task_state.py# Import legacy batch queue/checkpoint state
├── write_version_manifest.py  # Generate version manifest for packaged builds
├── smoke_task_engine.py       # Smoke test for task engine reliability
├── smoke_active_preference.py # Smoke test for active preference learning lifecycle
├── smoke_quality_lab.py       # Smoke test for Quality Lab lifecycle
├── test_video_adaptive.py     # Core: adaptive GIF extraction (4 phases)
├── test_video_batch.py        # Batch wrapper with checkpoint
├── pipeline_stage2.py         # Post-VLM LLM synthesis + FAISS rebuild
└── vlm_loop.py                # Production VLM processing loop
configs/
└── models.yaml                # Main config (models, paths, preference_memory flag, task_engine)
```

## Phase 1: Reliable Task Engine

The task engine (`app/task_engine/`) provides a reliable, database-backed job-processing
system for adaptive GIF extraction and pipeline stages.

### Data Model (task_state.db)

7 tables managed by `app/task_engine/schema.py`:

| Table | Purpose |
|-------|---------|
| `task_jobs` | Job catalog — one per source directory |
| `task_videos` | Videos belonging to a job |
| `task_stages` | Individual processing stages per video |
| `task_artifacts` | Immutable artifact records with SHA-256 provenance |
| `task_events` | Append-only event log |
| `task_commands` | Cancel/pause/resume commands |
| `task_migrations` | Schema migration + legacy import tracking |

### Key Components

| Module | Role |
|--------|------|
| `models.py` | Dataclasses (JobRecord, StageRecord, VideoRecord, ArtifactRef, etc.) |
| `schema.py` | DDL, migrations, `connect_task_db()` factory |
| `repository.py` | `TaskRepository` — transactional CRUD, leasing, heartbeat |
| `fingerprints.py` | `sha256_file()`, `canonical_hash()`, `canonical_json()` |
| `artifacts.py` | `commit_artifact()` with path-existence + SHA-256 validation |
| `legacy_import.py` | Import legacy `batch_queue_state.json` / checkpoint |
| `stages.py` | `StageAdapter`, `StageContext`, `StageResult` — adapter protocol |
| `adaptive_adapter.py` | `AdaptivePipelineAdapter` — wraps existing pipeline |
| `worker.py` | `TaskWorker` — single-writer lease loop with heartbeat, retry, cancellation |

### Production Stage Contract (2026-07-18)

The production adapter executes eight independent stages:
`discover -> sample -> vlm -> refine -> rank_dedup -> synthesize -> gif_clip -> materialize`.
Stages exchange versioned manifests through `task_artifacts`; they must not
re-run the full adaptive pipeline or infer success from missing artifacts.
`rank_dedup` fans out one `gif_clip` stage per clip. Each GIF is independently
retryable, and `materialize` is created only after every clip reaches a terminal
state. Partial output uses `StageResult.outcome=needs_attention` and preserves
successfully published GIFs.

Release gate: compileall, the complete pytest suite, and `git diff --check`.
The merged 2026-07-18 verified baseline is `1012 passed, 2 skipped, 3 warnings`,
covering the persistent serial folder queue plus four deterministic production
E2E scenarios: success, VLM outage, invalid VLM payload, and valid zero-clip.
Tests must use temporary data and must not mutate historical databases, exports,
labels, checkpoints, or writable configuration.

### Task API Endpoints (7 new)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/tasks/commands` | Enqueue a command (cancel/pause/resume) |
| GET | `/api/tasks/commands/pending` | Poll pending commands |
| GET | `/api/tasks/jobs` | List all jobs with status counts |
| GET | `/api/tasks/jobs/{job_id}` | Job detail + videos + stages |
| GET | `/api/tasks/stages` | Query stages by status/worker/video |
| POST | `/api/tasks/export-candidates` | Package candidate GIFs for export |
| POST | `/api/tasks/import-legacy` | Import legacy queue/checkpoint state |

### Control Tab Cutover

The Gradio Control tab now reads/writes the task engine API instead of
driving the legacy batch queue directly. Set the environment variable
`GIFAGENT_LEGACY_QUEUE_UI=1` to restore the old queue-based control panel.

The compatibility module preserves the latest persistent folder-queue
behavior from the legacy Control UI. Directory identity is canonicalized so an
active/pending path cannot be appended twice. One lease-owning worker processes
folders serially and uses explicit starting/running/draining/cleanup handoffs.
Per-video and per-GIF terminal lines are retained in the status log; an empty
or failed GIF export returns a non-zero video result. Keep the queue modules and
their regression tests when changing the Workbench entrypoint.

### Scripts

- **`task_worker.py`**: Single-writer worker daemon. Claims stages via
  `TaskWorker`, runs the `AdaptivePipelineAdapter`, and records artifacts.
  Supports `--once` for one-shot mode, configurable poll/lease/retry via
  CLI flags or the `task_engine` config section.

- **`import_legacy_task_state.py`**: One-shot migration that reads
  `batch_queue_state.json`, `batch_state.json`, and checkpoint files,
  plans job/video/stage rows, and inserts them into `task_state.db` with
  a deterministic migration ID for idempotent re-import.

- **`write_version_manifest.py`**: Generates a JSON manifest with git
  commit, Python version, config schema version, and SHA-256 hashes of
  key packaged files. Used during release builds:
  ```
  uv run python scripts/write_version_manifest.py --dist dist/GifAgentUI
  ```

- **`smoke_task_engine.py`**: End-to-end smoke test that creates a job,
  adds videos and stages, claims and completes a stage, and verifies
  cancellation. Requires `--data-dir` and rejects production data dirs.

- **`smoke_quality_lab.py`**: End-to-end smoke test for the Quality Lab.
  Creates experiment configs, a benchmark manifest, completes runs with
  injected fake stage results, runs a blind A/B session, promotes a
  champion config, rolls back, and verifies no source files changed.

### Task Engine Config

Enabled by default in `configs/models.yaml`:

```yaml
task_engine:
  enabled: true
  db_path: "data/task_state.db"
  poll_seconds: 1.0
  lease_seconds: 90
  max_attempts: 3
  base_delay_seconds: 5
  max_delay_seconds: 300
```

### Backups and Rollback

Before importing legacy state, the importer creates timestamped,
byte-for-byte backups of the source files in the configured backup
directory. Import is reversible: delete `task_state.db` (or restore
from a backup) and the legacy queue files remain untouched.

Migration tracking via `task_migrations` table prevents duplicate
imports — each import stores its migration ID (SHA-256 of source file
hashes) and full report JSON. Re-running the import on the same source
files returns the stored report immediately.

## Phase 2: Quality Lab

Phase 2 adds a systematic quality evaluation framework (`app/quality_lab/`)
for comparing experiment configurations through frozen benchmark manifests,
automated metric scorecards, blind A/B review, and champion promotion with
rollback.

### Architecture

```
app/quality_lab/
├── __init__.py       # Public exports (models, services)
├── models.py         # Dataclasses: ExperimentConfig, ExperimentRun,
│                     #   BenchmarkItem, BenchmarkManifest, ABSession, ABResult
├── schema.py         # quality_lab.db DDL (10 tables) + connect_quality_db()
├── manifests.py      # Immutable JSON manifest creation + loading
├── runner.py         # ExperimentRunner — submits items as task jobs
├── metrics.py        # NumPy metrics: ndcg_at_k, temporal_coverage,
│                     #   diversity_score, export_integrity
├── calibration.py    # VLM score calibration: reliability diagram bins
│                     #   + Pool-Adjacent-Violators isotonic regression
├── ab_review.py      # BlindReviewService — blind A/B session lifecycle
└── promotion.py      # Champion promotion (6 gates) + rollback + history
```

### Data Model (quality_lab.db)

10 tables managed by `app/quality_lab/schema.py`:

| Table | Purpose |
|-------|---------|
| `benchmark_manifests` | Frozen manifest catalog |
| `benchmark_items` | Individual benchmark items with split (tune/holdout) |
| `experiment_configs` | Experiment configurations with provenance |
| `experiment_runs` | Runs linking config to manifest split |
| `experiment_items` | Per-item results within a run |
| `metric_values` | Metric values per run/item |
| `ab_sessions` | Blind A/B review sessions |
| `ab_judgments` | Individual pair judgments |
| `ab_pairs` | Pair assignments with opaque tokens |
| `champion_history` | Append-only promotion/rollback event log |

### Quality Lab Endpoints (9 new)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/quality/runs` | List all experiment runs |
| GET | `/api/quality/runs/{run_id}` | Get a single run |
| GET | `/api/quality/runs/{run_id}/scorecard` | Run metric scorecard |
| POST | `/api/quality/ab-sessions` | Create blind A/B session |
| POST | `/api/quality/ab-sessions/{session_id}/judgments` | Record judgment |
| POST | `/api/quality/champions/{config_id}/promote` | Promote config to champion |
| POST | `/api/quality/champions/rollback` | Rollback to previous champion |
| GET | `/api/quality/champions/history` | Champion history events |
| GET | `/api/quality/champions/current` | Current champion config |

### Quality Lab Scripts

```bash
# Smoke test for the full quality-lab lifecycle
uv run python scripts/smoke_quality_lab.py --data-dir <temp-dir>
```

### Quality Lab Config

The quality-lab database path defaults to `data/quality_lab.db` and can be
overridden via the `GIFAGENT_QUALITY_DB` environment variable.

### Phase 2 Tasks (1-7) Output Summary

| Task | Output |
|------|--------|
| 1: Schema + Manifests | `quality_lab.db` DDL, `connect_quality_db()`, manifest freeze/load |
| 2: Provenance | `provenance.py` service + recording in `experiment_configs` |
| 3: Runner | `ExperimentRunner` — task-job submission per benchmark item |
| 4: Scorecards + Calibration | NumPy metrics (4), reliability diagrams, PAV calibrator |
| 5: Blind A/B | `BlindReviewService` — sessions, pairs, judgments, reveal |
| 6: Promotion | `promote_config()` with 6 gates, `rollback()`, champion history |
| 7: Release + Docs | Router, Lab tab UI, smoke script, build packaging |

### Champion Promotion Gates

1. Config exists in `experiment_configs`
2. Confirmation matches config ID
3. At least one completed tune experiment run
4. At least one completed holdout experiment run
5. At least one completed blind A/B session involving any of the config's runs
6. Average `export_integrity` metric >= 0.9

### Promotion & Rollback

Promotion writes a versioned config snapshot to `data/config_versions/` and
atomically updates `data/current_config.json`. Rollback finds the previous
promote event in `champion_history` and reverts `current_config.json`.
The `configs/models.yaml` file is never modified.

### Metric Definitions

| Metric | Range | Description |
|--------|-------|-------------|
| `export_integrity` | [0, 1] | Fraction of successful exports |
| `temporal_coverage` | [0, 1] | Fraction of timeline covered by exported clips |
| `ndcg_at_k` | [0, 1] | NDCG at position k for ranked relevance |
| `diversity_score` | [0, 1] | Average pairwise cosine distance of clip vectors |

### VLM Score Calibration

Calibration produces reliability-diagram bins: for each bin the mean predicted
score is compared to the actual positive rate. The PAV (pool-adjacent-violators)
algorithm fits a monotonic step-function that maps raw scores to calibrated
probabilities.

```python
# Compute reliability curve
from app.quality_lab.calibration import calibration_curve
bins = calibration_curve(scores=[0.1, 0.5, 0.9], labels=[0, 1, 1], bins=5)

# Fit monotonic calibrator
from app.quality_lab.calibration import fit_monotonic_calibrator
cal = fit_monotonic_calibrator(scores, labels)
```

### Task 3: Benchmark Experiment Runner

`ExperimentRunner` creates and manages experiment runs by submitting each
benchmark item as a task job through the task engine. It never reads batch
logs — state is derived solely from the task client. Key methods:

- `create_run(manifest_id, config_id, split)` — creates a pending run
- `submit(run_id)` — submits all items as task jobs (idempotent)
- `refresh(run_id)` — queries task engine for latest item state
- `cancel(run_id)` — cancels all running jobs

## test_video_adaptive.py — 4 Phases

1. **Probe + sample**: ffprobe duration → sample at SAMPLE_INTERVAL → dark filter
2. **VLM scoring**: llava:13b scores each frame (0.0-1.0) → refinement around high-score regions
3. **RAG + LLM synthesis**: FAISS search per clip → DeepSeek synthesizes summary/tags (non-fatal)
3.5. **9-grid thumbnail**: select top-9 scored frames with pHash dedup (Hamming > 10) → export 9 individual JPEGs + 3x3 grid to `Sample/` subfolder
4. **GIF export**: ffmpeg palette two-pass, ranked by gif_worthiness

**Non-fatal LLM**: if LLM fails, GIFs still export. Synthesis metadata is skipped.

## Preference Memory Subsystem

6 tables: `candidate_gifs`, `candidate_vectors`, `candidate_vector_exclusions`, `favorite_gifs`, `preference_events`, `preference_profile_builds`, `preference_profiles`, `preference_profile_current`, `preference_profile_publications`

Flow: candidate materialize → human feedback (like/dislike/neutral/skip/quality_reject/favorite) → profile build (7 gates) → holdout evaluation → source-grouped evaluation → explicit publish → reranker (behind `preference_memory.enabled` flag, default false)

### All 6 feedback ratings

| Rating | Meaning | Profile usage |
|--------|---------|---------------|
| `like` | Positive | Liked centroid, weight=1.0 |
| `dislike` | Negative | Disliked centroid, weight=1.0 |
| `neutral` | Neutral | Ignored |
| `skip` | Skipped | Ignored |
| `quality_reject` | Technical defect | Ignored |
| `favorite` | Strong positive | Liked centroid, weight=2.0 |

Candidate vectors are required before profile builds can pass. Backfill existing
reviewed candidates with:

```bash
uv run python scripts/backfill_candidate_vectors.py --db dist/GifAgentUI/data/library.db
```

By default the script embeds only candidates with effective like/dislike
feedback. Use `--all-candidates` to fill every `candidate_gifs` row and
`--dry-run` to count missing vectors without calling Ollama.

Profile publishing is available in the Candidate Review Profile panel. Click
`Refresh Profiles`, choose a completed profile version, then click
`Publish Selected Profile`. The panel calls
`POST /api/preference/profiles/{version}/publish` and updates
`preference_profile_current`; reranking uses only this published version.
Preference API endpoints use short-lived SQLite connections with a 30s
`busy_timeout`; publish lock contention is surfaced as retryable 503 instead of
an internal 500.
Profile builds require matching `candidate_vectors` for every effective
like/dislike feedback target; partial vector coverage blocks the build.

### Holdout Evaluation

`PreferenceEvaluationService.evaluate()` runs publish gates and NDCG metrics.
Phase 3 adds `evaluate_source_grouped()` with:

- **Source-video integrity**: asserts no `source_video_sha256` appears in both train and holdout
- **Base vs preference NDCG**: compares `base_rag_similarity` ranking vs `final_score` ranking
- **Pairwise win rate**: fraction of liked holdout candidates where preference outranks base
- **Exploration diversity**: distinct source videos and scenario keys in holdout
- **Vector coverage**: fraction of holdout candidates with embedding vectors
- **Inactive fallbacks**: fraction of holdout candidates falling back to base RAG (no preference score)

### Config parameters (preference_memory section)

```yaml
preference_memory:
  enabled: false                         # Master switch
  base_score_weight: 0.50                # RAG similarity blend weight
  preference_score_weight: 0.50          # Preference score blend weight
  recency_enabled: true                  # Enable recency decay
  recency_half_life_days: 90.0           # Weight half-life in days
  favorite_weight: 2.0                   # Favorite rating multiplier
  like_weight: 1.0                       # Like rating multiplier
  dislike_weight: 1.0                    # Dislike rating multiplier
  scenario_min_feedback: 8               # Min events for scenario profile
```

### Smoke test

```bash
uv run python scripts/smoke_active_preference.py
```

Verifies: candidate seeding, all 6 feedback ratings, profile build and publish, source-grouped evaluation, reranker explanations, and rollback. Runs entirely in an in-memory SQLite database.

### Candidate Review UI (2026-07-04)

- `GET /api/candidates` is paginated and filtered server-side. Defaults:
  `status=candidate`, `limit=24`, `offset=0`; callers can use `status=all`
  and pass `folder` for exact-folder review.
- `GET /api/candidates/folders` discovers recursive candidate folders under a
  selected root directory and returns per-folder totals, missing counts, and
  status counts. Folders with `.gif` files but no `candidate_gifs` rows are
  still listed with `unmaterialized_count` and treated as new candidates.
- The Review tab does not auto-load every candidate on open. Users first choose
  a data root, click `Load Folders`, then choose the exact folder to review from
  the recursive folder list.
- Selecting a folder with unmaterialized GIFs imports only that exact folder's
  direct `.gif` files into `candidate_gifs`; child folders are listed separately
  and are not imported until selected.
- `candidate_review.py` uses `PAGE_SIZE=12` for the Gradio gallery.
- Gallery items use cached static thumbnails under `data/thumbs/candidates/`;
  full animated GIFs load only in the selected preview pane.
- Selection uses the current page's `gr.State` item list. Do not reintroduce
  index math against a freshly fetched unfiltered list, or filtered-page clicks
  can rate the wrong candidate.
- Candidate display and feedback require the GIF file to still exist at its
  original `artifact_path`. If the path changed or the file is missing, the API
  returns 409 instead of showing or rating stale data.

## Known Gotchas

1. **`safe_worth()` vs `validate_frame_analysis`**: VLM sometimes returns string labels ("AVERAGE - ...") instead of numbers for `gif_worthiness`. `safe_worth()` handles this, but `validate_frame_analysis()` in quality.py tries `float()` first and can raise. The exception is caught (frame skipped after 3 retries), but it's not clean.

2. **LLM synthesis was silently failing**: `parse_json(raw)` was a NameError — the function is `parse_json_response(raw)` which returns a `JsonParseResult` object (use `.ok` and `.data`). Fixed 2026-07-04.

3. **DeepSeek model name is lowercase**: `deepseek-v4-flash` works, `DeepSeek-V4-Flash` returns 400. The API is OpenAI-compatible, not Anthropic-compatible.

4. **Export dir nesting**: `test_video_batch.py` now nests output as `adaptive_test/{input_folder}/{video}/`. Old runs have flat `adaptive_test/{video}/` structure.

5. **VLM scoring distribution**: llava:13b tends to score everything 0.5-0.7 regardless of temperature. The `temperature=0.65` setting gives the best balance — lower (0.5) makes it too conservative (0 frames above 0.7), higher (1.0) spreads but doesn't help merge count.

6. **Merge count vs threshold**: Lowering `MERGE_SCORE_THRESHOLD` from 0.6 to 0.55 barely increased merges (92→90 clips) because low-score frames (0.3-0.5) interspersed in the timeline break merge chains. The bottleneck is score distribution, not threshold.

7. **Video fingerprint dedup**: `test_video_batch.py` computes a content fingerprint (duration + 5 keyframe pHashes at 10%/30%/50%/70%/90%) for each video before processing. If Hamming distance ≤ 5 vs any already-processed video, it's marked `dedup_skipped` with `duplicate_of` field. Robust to re-encode/container/filename changes; NOT robust to crop/watermark. Stored in `data/batch_checkpoint.json` under each video's `fingerprint` key. Use `--force` to bypass dedup.

## API Endpoints (26+ total)

Core endpoints (pre-Phase 1):

Key ones (preference/candidate):
- `GET /api/candidates/folders` - recursive candidate-folder discovery below a
  selected root directory
- `GET /api/candidates` - paginated candidate list (`status`, `limit`, `offset`,
  optional exact `folder`); defaults to `status=candidate`
- `POST /api/candidates/{id}/feedback` — rate (like/neutral/dislike/skip)
- `GET /api/preference/profiles` — list profile builds
- `POST /api/preference/profiles/build` — build new profile
- `POST /api/preference/profiles/{version}/publish` — publish
- `POST /api/preference/evaluate` — holdout eval

## Test Suite

```bash
uv run pytest tests/ -v   # 400+ tests across all modules
```

Covers: JSON parsing, placeholder detection, FAISS manifest, reset safety, candidate materialization, feedback events, preference profiles, holdout evaluation, reranker, task engine repository, artifacts, legacy import, stage adapters, fault injection, worker, version manifest, smoke script, quality lab API, quality lab UI, metrics, calibration, blind A/B, promotion.

## Phase 4: Library Workbench (7-Tab Management UI)

Phase 4 delivers the Library Workbench — a unified Gradio interface with 7 tabs
replacing the separate candidate-review and control-panel UIs. Backed by new
services for search, timeline, relink, collections, taste map, narrative curation,
and attention inbox.

### Workbench Structure

```
app/ui/
├── workbench.py           # Shell — 7-tab gr.Blocks
├── api_client.py          # GifAgentApiClient (HTTP)
├── components/
│   ├── common.py          # Shared UI components
│   └── timeline.py        # Timeline renderer
└── tabs/
    ├── today.py           # Attention inbox
    ├── control.py         # Task queue control
    ├── review.py          # Candidate GIF review
    ├── search.py          # Semantic + filtered search
    ├── collections.py     # Smart collections
    ├── lab.py             # Quality Lab
    ├── settings.py        # Config editor
    └── profile.py         # Profile publish
```

### New Services

| Service | File | Key API |
|---------|------|---------|
| Library Search | `app/services/library_search.py` | `LibrarySearchService.search(query, limit, offset)` — FTS5 + vector |
| Workbench Schema | `app/services/workbench_schema.py` | FTS5 DDL, `SearchQuery`, `SearchPage`, `CollectionSpec` |
| Timeline | `app/services/timeline.py` | `load_timeline_window(conn, video_id, start, end, max_thumbnails=60)` |
| Media Relink | `app/services/media_relink.py` | `propose_relinks(conn)`, `apply_relink(conn, proposal)` |
| Collections | `app/services/collections.py` | `CollectionService` — create, refresh (farthest-first diversity), freeze, export |
| Taste Map | `app/services/taste_map.py` | `project_taste_map(vectors, ids)` — centred SVD, no sklearn |
| Narrative Curation | `app/services/narrative_curation.py` | `curate_narrative(candidates, beats)` — greedy beat assignment |
| Attention | `app/services/attention.py` | `list_attention_items(...)` — cross-DB aggregation |

### Workbench Router Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/workbench/search` | Candidate search |
| GET | `/api/workbench/timeline` | Timeline window |
| GET | `/api/workbench/attention` | Attention inbox |
| POST | `/api/workbench/relinks/scan` | Scan relink opportunities |
| POST | `/api/workbench/relinks/apply` | Apply relink |
| POST | `/api/workbench/collections` | Create collection |
| POST | `/api/workbench/collections/{id}/refresh` | Refresh collection |
| POST | `/api/workbench/collections/{id}/freeze` | Freeze collection |
| POST | `/api/workbench/collections/{id}/export` | Export collection |

### Key Performance Numbers

- Search: <5 s for first page on 10,000 candidates; max 24 items/page.
- Timeline: max 60 thumbnails per viewport window; paths only, no GIF bytes in API.
- UI chain: search → select → create collection = 3 primary actions.
- Lazy loading: gallery thumbnails (`PAGE_SIZE=12`), full GIF only on selection.

### Smoke Test

```bash
uv run python scripts/smoke_library_workbench.py
```

Validates search, timeline, relink, collections, taste map, narrative curation,
and attention. Runs entirely in-memory with synthetic vectors; no Ollama needed.

### New Test File

```bash
uv run pytest tests/test_workbench_performance.py -v
```

10k-row seed, search <5s, <=24 items/page, timeline <=60 thumbnails, no GIF bytes.

## Recent Tuning History (2026-07-04)

| Change | Before | After | Effect |
|--------|--------|-------|--------|
| SAMPLE_INTERVAL | 15 | 10 | +50% sample density |
| MERGE_GAP | 6 | 12 | enables adjacent-frame merge |
| MERGE_SCORE_THRESHOLD | (none) | 0.55 | score-gated merge |
| VLM temperature | default (~0.8) | 0.65 | consistent scoring |
| LLM provider | anthropic_compatible (Ark) | openai_compatible (DeepSeek) | native DeepSeek API |
| parse_json bug | broken | fixed | LLM synthesis works |
| Candidate review loading | full candidate list + full GIF gallery | paged API + cached static thumbnails | much faster Web/GUI review |
| Candidate click mapping | page index remapped into unfiltered list | current-page `gr.State` item list | Like/Dislike updates the clicked candidate |
| **GIF output per 3 videos** | **5** | **306** | **61x increase** |

## Production Release Gate (2026-07-18)

Eight-stage split production pipeline. Build EXE or re-run historical
queues only after passing the full gate:

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py -s
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Four E2E scenarios must pass:
1. **full success chain** — refine>0, real GIF export, LLM stub called with
   response in synthesize manifest, all 8 stages succeeded.
2. **VLM outage (503)** — VLM exhausts 3 stage attempts (needs_attention),
   request count = frame_count × max_attempts × 3 HTTP retries,
   job/video needs_attention, no downstream stages/artifacts/GIFs.
3. **invalid VLM payload ({})** — same exhaustion, no vlm_manifest written,
   no 0.5 default-score clips.
4. **valid low scores (0.1)** — genuine zero-clip via strict
   `_assert_zero_clip_proven`, job/video succeeded, no gif_clip/gif_file/GIFs.

Key invariants:
- **LLM/VLM configs come from the SAME frozen Job snapshot** (not global YAML).
  Success E2E: `llm_requests == 0` is a hard failure.
- `gif_worthiness` must be a finite number in [0,1]; parse errors/bools/missing
  values never produce a 0.5 default.
- VLM outage or all-frames-parse-failure raises RuntimeError in both
  `_stage_vlm` and `_stage_refine`.
- **Failure E2E chains must exhaust max_attempts retries (zero-delay
  RetryPolicy), reach needs_attention, and have request counts that exactly
  match frame_count × max_attempts × 3 HTTP retries.**
- Zero-clip requires rank_dedup manifest with `clip_count==0`, validated
  through `validate_artifact_strict()` + `validate_manifest_json()`.
  Empty synthesize must register `synthesize_manifest` artifact.
- `manage_lifecycle` defaults to `False` (not inferred from URL).
  `launch_mode`: `none`|`native`|`wsl`; unknown modes raise immediately.
  `stop_model`/`wait_model` consume explicit `VlmRuntimeConfig`.
  Six lifecycle tests lock command arrays, endpoint URLs, and mode
  semantics; no real models or network accessed.

Historical data in `data/` (task_state.db, quality_lab.db, library.db,
exports, labels, Preference Memory) must be preserved unchanged across
all tests; use `Get-Item data/*.db` to verify before/after.
