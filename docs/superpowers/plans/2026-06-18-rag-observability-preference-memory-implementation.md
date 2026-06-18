# GifAgent RAG Observability and Preference Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a WSL2-hosted Web workbench that can run and compare video RAG tests, visualize retrieval evidence and preference space, and learn global plus scenario-specific preferences without mutating historical runs or automatically polluting the main library.

**Architecture:** Keep `library.db` as the stable library plus long-term candidate/preference store, and add an independent `runs.db` for immutable run evidence and progress events. A serial worker executes a refactored test-video pipeline against fixed FAISS and Preference Profile versions; React consumes REST/SSE APIs for run playback, comparison, UMAP exploration, feedback, and explicit promotion. FastAPI, the worker, and React/Nginx run under Docker Compose in WSL2 while Ollama remains on the WSL host.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLite/WAL, FAISS, NumPy, Ollama HTTP API, ffmpeg/ffprobe, UMAP/scikit-learn, React, TypeScript, Vite, TanStack Query, Apache ECharts, Vitest, Playwright, Docker Compose

**Authoritative design:** `docs/superpowers/specs/2026-06-18-rag-observability-workbench-design.md`

---

## Execution Rules

1. Execute tasks in order. A task may depend only on committed work from earlier tasks.
2. Use a dedicated worktree created with `superpowers:using-git-worktrees` before Task 1.
3. Do not run schema migration against the production `data/library.db` while a long ingest process is writing it.
4. Use temporary SQLite and FAISS directories in every automated test.
5. Never stage `data/*.db*`, `data/*.log`, generated run artifacts, `.superpowers/`, or unrelated user changes.
6. Keep `preference_memory.enabled=false` until Task 16's holdout gate passes.
7. Every task follows red-green-refactor and ends with a focused commit.
8. At each phase gate, run the complete backend test suite before continuing.

## Phase Gates

| Gate | Required evidence |
|---|---|
| G0 Baseline | Existing tests pass; production DB backup and FAISS verification recorded outside Git |
| G1 Contracts | Candidate, event, Profile, run, and version schemas pass on fresh and existing-style databases |
| G2 Baseline pipeline | New pipeline reproduces test-video behavior with Preference Memory disabled |
| G3 Preference core | Event rebuild is deterministic; absent Profile equals baseline exactly |
| G4 A/B | Same video/parameters/index can compare baseline and memory runs with score explanations |
| G5 Web | Run, compare, map, feedback, and inspector workflows pass component and Playwright tests |
| G6 Deployment | Docker Compose starts on WSL2 and reaches host Ollama; end-to-end smoke test passes |

## File Map

### Backend files to create

```text
app/runs/__init__.py                  Run package exports
app/runs/models.py                    Run states, parameters, records, score snapshots
app/runs/schema.py                    runs.db DDL
app/runs/db.py                        runs.db connections and initialization
app/runs/repository.py                Run/step/frame/hit/candidate persistence and claiming
app/runs/events.py                    Append/replay run events
app/runs/artifacts.py                 Atomic run artifact paths and writes
app/runs/media.py                     ffprobe/ffmpeg and pure sampling/merge functions
app/runs/inference.py                 Injectable VLM/LLM/Embedding interfaces and Ollama adapter
app/runs/pipeline.py                  Stage orchestration
app/runs/worker.py                    Serial worker, heartbeat, cancel, resume
app/runs/comparison.py                Frame, retrieval, and candidate alignment
app/services/preference_schema.py     Candidate/event/Profile DDL
app/services/scenario.py              Scenario-key normalization
app/services/candidates.py            Candidate materialization and queries
app/services/preference_memory.py     Effective-event and immutable Profile builds
app/services/reranker.py              Availability-aware score calculation
app/services/promotion.py             Quality/dedup/promotion transaction orchestration
app/services/index_versions.py        Immutable FAISS versions and current pointer
app/services/preference_map.py        UMAP/cache/statistics
app/services/preference_evaluation.py Holdout metrics and default-enable gate
app/routers/runs.py                    Run and SSE API
app/routers/candidates.py              Candidate feedback/rerank/promote API
app/routers/preference.py              Profile build/query API
app/routers/preference_map.py          Map and statistics API
app/routers/system.py                  Existing status/media/review endpoints
scripts/run_worker.py                  Worker entrypoint
scripts/preference_memory.py           Status/rebuild CLI
scripts/pipeline.py                    Run/candidate operational CLI
scripts/preflight_workbench.py         Production preflight and SQLite backup
scripts/import_run_json.py             Idempotent legacy result importer
scripts/evaluate_preference.py         Holdout evaluation CLI
scripts/benchmark_workbench.py         Local performance probes
scripts/seed_e2e_workbench.py          Isolated deterministic E2E state
Dockerfile                             API/worker runtime image
docker-compose.yml                     WSL2 service topology
docker-compose.e2e.yml                 Isolated fake-inference test override
docker/nginx.conf                      Web proxy and SSE settings
docs/runbook-rag-workbench.md          Backup/deploy/recovery procedures
```

### Existing backend files to modify

```text
app/config.py                          Dynamic paths and environment overrides
app/db.py                              Injectable connection path and preference schema hook
app/main.py                            App factory and router registration
app/services/indexer.py                Read fixed versions and publish batches atomically
app/services/embedding.py              Injectable base URL/model and stateless text helper
app/services/scanner.py                Reusable duplicate checks for promotion
configs/models.yaml                    Run, path, map, and Preference Memory defaults
pyproject.toml                         UMAP/testing dependencies
scripts/test_video_adaptive.py         Thin CLI over the new pipeline
scripts/test_video_rag_v2.py           Compatibility CLI over the new pipeline
README.md                              Workbench entry points
.gitignore                             Generated state exclusions
.dockerignore                          Image build exclusions
```

### Frontend files to create

```text
web/package.json
web/package-lock.json
web/vite.config.ts
web/playwright.config.ts
web/Dockerfile
web/src/main.tsx
web/src/app/App.tsx
web/src/api/client.ts
web/src/api/types.ts
web/src/layout/WorkbenchLayout.tsx
web/src/runs/RunList.tsx
web/src/runs/RunForm.tsx
web/src/runs/RunWorkspace.tsx
web/src/runs/RunTimeline.tsx
web/src/runs/RetrievalEvidence.tsx
web/src/runs/RunComparison.tsx
web/src/map/PreferenceMap.tsx
web/src/map/MapStats.tsx
web/src/candidates/ReviewQueue.tsx
web/src/inspector/MediaInspector.tsx
web/src/styles.css
web/src/test/setup.ts
web/tests/workbench.spec.ts
```

### Test files to create

```text
tests/conftest.py
tests/test_preflight_workbench.py
tests/test_db_paths.py
tests/test_candidate_schema.py
tests/test_preference_schema.py
tests/test_run_models.py
tests/test_run_repository.py
tests/test_run_events.py
tests/test_index_versions.py
tests/test_run_media.py
tests/test_run_artifacts.py
tests/test_run_inference.py
tests/test_run_pipeline.py
tests/test_run_worker.py
tests/test_runs_api.py
tests/test_scenario.py
tests/test_candidates.py
tests/test_preference_events.py
tests/test_preference_profiles.py
tests/test_reranker.py
tests/test_run_comparison.py
tests/test_candidate_api.py
tests/test_promotion.py
tests/test_preference_map.py
tests/test_preference_api.py
tests/test_docker_config.py
tests/test_e2e_closed_loop.py
tests/test_import_run_json.py
tests/test_preference_evaluation.py
tests/test_acceptance_invariants.py
tests/test_workbench_benchmark.py
```

---

## Phase 0: Baseline and Safety

### Task 1: Add a Repeatable Production Preflight

**Files:**
- Create: `scripts/preflight_workbench.py`
- Create: `tests/test_preflight_workbench.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write the failing backup test**

```python
# tests/test_preflight_workbench.py
import sqlite3

from scripts.preflight_workbench import backup_database


def test_backup_database_creates_consistent_copy(tmp_path):
    source = tmp_path / "library.db"
    target = tmp_path / "backup" / "library.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE sample(value TEXT)")
    conn.execute("INSERT INTO sample VALUES ('ok')")
    conn.commit()
    conn.close()

    backup_database(source, target)

    copied = sqlite3.connect(target)
    assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copied.execute("SELECT value FROM sample").fetchone()[0] == "ok"
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run: `uv run pytest tests/test_preflight_workbench.py::test_backup_database_creates_consistent_copy -v`

Expected: FAIL with `ModuleNotFoundError` or missing `backup_database`.

- [ ] **Step 3: Implement the preflight helper and guarded CLI**

```python
# scripts/preflight_workbench.py
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def backup_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"backup integrity_check failed: {result}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/library.db")
    parser.add_argument("--backup")
    parser.add_argument("--ack-writers-stopped", action="store_true")
    args = parser.parse_args()
    path = Path(args.database)
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    if args.backup:
        if not args.ack_writers_stopped:
            raise SystemExit("--backup requires --ack-writers-stopped")
        backup_database(path, Path(args.backup))
        print("backup:", args.backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Ignore generated workbench state**

Append exactly:

```gitignore
.superpowers/
web/node_modules/
web/dist/
data/runs/
data/maps/
backups/
```

- [ ] **Step 5: Verify the helper and current baseline**

Run:

```powershell
uv run pytest tests/test_preflight_workbench.py -v
uv run pytest -q
uv run python scripts/preflight_workbench.py --database data/library.db
```

Expected: tests PASS; production command prints `integrity: ok` and performs no write.

- [ ] **Step 6: Record the operator-only backup command**

Run only after the ingest writer is stopped:

```powershell
uv run python scripts/preflight_workbench.py --database data/library.db --backup backups/library-pre-workbench.db --ack-writers-stopped
```

Expected: `backups/library-pre-workbench.db` exists and is ignored by Git.

- [ ] **Step 7: Commit**

```powershell
git add .gitignore scripts/preflight_workbench.py tests/test_preflight_workbench.py
git commit -m "chore: add workbench preflight and database backup"
```

---

## Phase 1: Shared Contracts and Schemas

### Task 2: Make Database and Config Paths Injectable

**Files:**
- Modify: `app/config.py:1-26`
- Modify: `app/db.py:1-15`
- Modify: `configs/models.yaml`
- Create: `tests/test_db_paths.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write failing path-isolation tests**

```python
# tests/test_db_paths.py
from app.db import get_connection, init_db


def test_get_connection_uses_explicit_path(tmp_path):
    path = tmp_path / "nested" / "library.db"
    init_db(path)
    conn = get_connection(path)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
```

- [ ] **Step 2: Verify the test fails against the current fixed `DB_PATH` API**

Run: `uv run pytest tests/test_db_paths.py -v`

Expected: FAIL because `init_db()` and `get_connection()` do not accept a path.

- [ ] **Step 3: Implement dynamic path resolution without breaking callers**

```python
# app/db.py
from pathlib import Path


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    return Path(db_path or os.environ.get("GIFAGENT_LIBRARY_DB") or get("database.path", "data/library.db"))


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    conn = get_connection(db_path)
    # Keep the existing executescript and migration call unchanged here.
```

Preserve the existing `save_checkpoint()` and `load_checkpoint()` signatures and route their connections through `resolve_db_path()` so environment overrides apply consistently.

- [ ] **Step 4: Add configuration blocks with safe defaults**

```yaml
runs:
  database_path: "data/runs/runs.db"
  artifacts_dir: "data/runs/artifacts"
  heartbeat_seconds: 5
  interrupted_after_seconds: 30

preference_memory:
  enabled: false
  auto_rebuild_enabled: false
  auto_rebuild_every_events: 10
  min_total_samples: 5
  min_like_samples: 3
  min_dislike_samples: 2
  min_profile_confidence: 0.25
  dislike_hard_penalty_threshold: 0.85
  dislike_soft_penalty_threshold: 0.75
  time_decay_enabled: false
  half_life_days: 180
  weights:
    base_rag_similarity: 0.45
    global_like_similarity: 0.20
    scenario_like_similarity: 0.15
    dislike_avoidance: 0.15
    diversity_bonus: 0.05

paths:
  media_root: "/media"
  maps_dir: "data/maps"
```

Merge the new `paths` keys into the existing block rather than creating a second YAML key.

- [ ] **Step 5: Add reusable temporary DB fixtures**

```python
# tests/conftest.py
import pytest

from app.db import init_db


@pytest.fixture
def library_db(tmp_path):
    path = tmp_path / "library.db"
    init_db(path)
    return path
```

- [ ] **Step 6: Run focused and regression tests**

Run:

```powershell
uv run pytest tests/test_db_paths.py -v
uv run pytest -q
```

Expected: all tests PASS; no test writes `data/library.db`.

- [ ] **Step 7: Commit**

```powershell
git add app/config.py app/db.py configs/models.yaml tests/conftest.py tests/test_db_paths.py
git commit -m "refactor: make GifAgent database paths injectable"
```

### Task 3: Add Candidate Tables

**Files:**
- Create: `app/services/preference_schema.py`
- Modify: `app/db.py:15-175`
- Create: `tests/test_candidate_schema.py`

- [ ] **Step 1: Write failing schema tests**

```python
# tests/test_candidate_schema.py
from app.db import get_connection


def test_candidate_schema_has_idempotency_and_vector_constraints(library_db):
    conn = get_connection(library_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"candidate_gifs", "candidate_vectors"} <= tables
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(candidate_gifs)")}
    assert "idx_candidate_source_run" in indexes
    assert "idx_candidate_status_score" in indexes
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_candidate_schema.py -v`

Expected: FAIL because candidate tables do not exist.

- [ ] **Step 3: Define candidate DDL in one focused module**

```python
# app/services/preference_schema.py
CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_gifs (
    candidate_id TEXT PRIMARY KEY,
    source_run_id TEXT,
    source_run_candidate_id TEXT,
    source_video_id TEXT,
    source_video_path TEXT NOT NULL,
    source_video_sha256 TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    duration REAL NOT NULL,
    representative_frame_path TEXT,
    exported_gif_path TEXT,
    export_status TEXT NOT NULL DEFAULT 'not_exported'
      CHECK(export_status IN ('not_exported','exported','failed')),
    caption TEXT,
    summary TEXT,
    emotional_core TEXT,
    aesthetic_notes_json TEXT,
    why_i_like_it TEXT,
    tags_json TEXT,
    scene_type TEXT,
    scenario_keys_json TEXT NOT NULL,
    base_rag_score_raw REAL,
    base_rag_score REAL NOT NULL,
    profile_score REAL,
    dislike_similarity REAL,
    dislike_penalty_multiplier REAL,
    final_score REAL NOT NULL,
    score_json TEXT NOT NULL,
    score_profile_version TEXT,
    status TEXT NOT NULL DEFAULT 'candidate'
      CHECK(status IN ('candidate','liked','disliked','neutral','promoted','rejected','archived')),
    promoted_media_id TEXT,
    model_info_json TEXT,
    quality_status TEXT NOT NULL DEFAULT 'unchecked'
      CHECK(quality_status IN ('unchecked','passed','failed')),
    quality_errors_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_run_id, source_run_candidate_id)
);
CREATE TABLE IF NOT EXISTS candidate_vectors (
    vector_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    vector_type TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    source_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidate_gifs(candidate_id),
    UNIQUE(candidate_id, vector_type, embedding_model)
);
CREATE INDEX IF NOT EXISTS idx_candidate_source_run
  ON candidate_gifs(source_run_id, source_run_candidate_id);
CREATE INDEX IF NOT EXISTS idx_candidate_status_score
  ON candidate_gifs(status, final_score DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_video
  ON candidate_gifs(source_video_sha256);
CREATE INDEX IF NOT EXISTS idx_candidate_vectors_candidate
  ON candidate_vectors(candidate_id, vector_type);
"""


def ensure_preference_schema(conn) -> None:
    conn.executescript(CANDIDATE_SCHEMA)
    conn.commit()
```

- [ ] **Step 4: Call the schema hook from `init_db()`**

```python
from app.services.preference_schema import ensure_preference_schema

# after the existing _migrate(conn)
ensure_preference_schema(conn)
```

- [ ] **Step 5: Add an idempotency test**

```python
def test_candidate_schema_init_is_idempotent(library_db):
    from app.db import init_db
    init_db(library_db)
    init_db(library_db)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_candidate_schema.py tests/test_db_paths.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/db.py app/services/preference_schema.py tests/test_candidate_schema.py
git commit -m "feat: add long-term candidate schema"
```

### Task 4: Add Preference Event and Immutable Profile Tables

**Files:**
- Modify: `app/services/preference_schema.py`
- Create: `tests/test_preference_schema.py`

- [ ] **Step 1: Write failing Profile schema tests**

```python
# tests/test_preference_schema.py
import pytest

from app.db import get_connection


def test_preference_schema_contains_event_and_version_tables(library_db):
    conn = get_connection(library_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "preference_events",
        "preference_profile_builds",
        "preference_profiles",
        "preference_profile_current",
        "promotion_attempts",
    } <= tables
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_preference_schema.py -v`

Expected: FAIL because the event and Profile tables are not yet installed.

- [ ] **Step 3: Replace `PREFERENCE_SCHEMA` with complete DDL**

```python
PREFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS preference_events (
    event_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('media','candidate_gif')),
    target_id TEXT NOT NULL,
    rating TEXT NOT NULL CHECK(rating IN ('like','dislike','neutral')),
    supersedes_event_id TEXT,
    reason TEXT,
    corrected_tags_json TEXT,
    scenario_keys_json TEXT NOT NULL,
    embedding_model TEXT,
    embedding_dim INTEGER,
    target_vector_json TEXT,
    score_snapshot_json TEXT NOT NULL,
    model_info_json TEXT,
    source TEXT NOT NULL
      CHECK(source IN ('web_workbench','review_ui','api','import','script')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(supersedes_event_id) REFERENCES preference_events(event_id)
);
CREATE TABLE IF NOT EXISTS preference_profile_builds (
    profile_version TEXT PRIMARY KEY,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    source_event_count INTEGER NOT NULL,
    source_event_max_created_at TEXT,
    source_event_watermark_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('building','completed','failed')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    error_json TEXT
);
CREATE TABLE IF NOT EXISTS preference_profiles (
    profile_id TEXT PRIMARY KEY,
    profile_version TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('global','scenario')),
    scenario_key TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    liked_centroid_json TEXT,
    disliked_centroid_json TEXT,
    tag_weights_json TEXT NOT NULL,
    emotion_weights_json TEXT NOT NULL,
    scene_type_weights_json TEXT NOT NULL,
    sample_count_like INTEGER NOT NULL,
    sample_count_dislike INTEGER NOT NULL,
    sample_count_neutral INTEGER NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(profile_version) REFERENCES preference_profile_builds(profile_version),
    UNIQUE(profile_version, scope, scenario_key)
);
CREATE TABLE IF NOT EXISTS preference_profile_current (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    profile_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(profile_version) REFERENCES preference_profile_builds(profile_version)
);
CREATE TABLE IF NOT EXISTS promotion_attempts (
    attempt_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    media_id TEXT NOT NULL UNIQUE,
    base_index_version TEXT NOT NULL,
    prepared_index_version TEXT,
    state TEXT NOT NULL
      CHECK(state IN ('claimed','validated','index_prepared','media_written','index_activated','completed','failed')),
    active_slot INTEGER CHECK(active_slot IS NULL OR active_slot = 1),
    heartbeat_at TEXT NOT NULL,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidate_gifs(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_preference_events_target
  ON preference_events(target_type, target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_preference_events_rating
  ON preference_events(rating, created_at);
CREATE INDEX IF NOT EXISTS idx_preference_events_supersedes
  ON preference_events(supersedes_event_id);
CREATE INDEX IF NOT EXISTS idx_preference_profiles_lookup
  ON preference_profiles(profile_version, scope, scenario_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_single_active_promotion
  ON promotion_attempts(active_slot) WHERE active_slot IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS trg_profile_current_insert_completed
BEFORE INSERT ON preference_profile_current
WHEN NOT EXISTS (
  SELECT 1 FROM preference_profile_builds
  WHERE profile_version=NEW.profile_version AND status='completed'
)
BEGIN
  SELECT RAISE(ABORT, 'current profile must reference completed build');
END;
CREATE TRIGGER IF NOT EXISTS trg_profile_current_update_completed
BEFORE UPDATE OF profile_version ON preference_profile_current
WHEN NOT EXISTS (
  SELECT 1 FROM preference_profile_builds
  WHERE profile_version=NEW.profile_version AND status='completed'
)
BEGIN
  SELECT RAISE(ABORT, 'current profile must reference completed build');
END;
"""


def ensure_preference_schema(conn) -> None:
    conn.executescript(CANDIDATE_SCHEMA + PREFERENCE_SCHEMA)
    conn.commit()
```

- [ ] **Step 4: Test the singleton and foreign-key constraints**

```python
def test_current_profile_requires_completed_build_row(library_db):
    import sqlite3
    from app.db import get_connection
    conn = get_connection(library_db)
    conn.execute(
        """INSERT INTO preference_profile_builds(
             profile_version,embedding_model,embedding_dim,source_event_count,
             source_event_watermark_json,config_json,status,created_at
           ) VALUES ('building','model-a',3,0,'{}','{}','building','2026-01-01T00:00:00Z')"""
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO preference_profile_current VALUES (1, 'building', '2026-01-01T00:00:00Z')"
        )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_preference_schema.py tests/test_candidate_schema.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/services/preference_schema.py tests/test_preference_schema.py
git commit -m "feat: add preference event and profile version schema"
```

### Task 5: Add Run Models and `runs.db` Schema

**Files:**
- Create: `app/runs/__init__.py`
- Create: `app/runs/models.py`
- Create: `app/runs/schema.py`
- Create: `app/runs/db.py`
- Create: `tests/test_run_models.py`

- [ ] **Step 1: Write failing parameter and schema tests**

```python
# tests/test_run_models.py
import pytest
from pydantic import ValidationError

from app.runs.db import init_run_db, get_run_connection
from app.runs.models import RunParameters


def test_run_parameter_defaults_preserve_current_test_values():
    params = RunParameters()
    assert params.sample_interval == 20
    assert params.refine_interval == 10
    assert params.worthiness_threshold == 0.4
    assert params.preference_memory_enabled is False


def test_run_parameter_cross_field_validation():
    with pytest.raises(ValidationError):
        RunParameters(min_duration=6, max_duration=5)


def test_run_schema_is_created(tmp_path):
    path = tmp_path / "runs.db"
    init_run_db(path)
    conn = get_run_connection(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"rag_runs", "rag_run_steps", "rag_run_frames", "rag_retrieval_hits", "rag_run_candidates", "rag_run_events"} <= tables
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_run_models.py -v`

Expected: FAIL because `app.runs` does not exist.

- [ ] **Step 3: Define typed parameters and states**

```python
# app/runs/models.py
from enum import StrEnum
from pydantic import BaseModel, Field, model_validator


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class RunParameters(BaseModel):
    sample_interval: int = Field(20, ge=5, le=120)
    refine_interval: int = Field(10, ge=1, le=30)
    refine_radius: int = Field(20, ge=0, le=120)
    refine_threshold: float = Field(0.5, ge=0, le=1)
    worthiness_threshold: float = Field(0.4, ge=0, le=1)
    min_duration: float = Field(1.5, ge=0.5, le=10)
    max_duration: float = Field(5.0, ge=1, le=20)
    merge_gap: int = Field(10, ge=0, le=60)
    embedding_dedup_threshold: float = Field(0.95, ge=0.5, le=1)
    top_k: int = Field(5, ge=1, le=20)
    output_ratio: float = Field(1.0, gt=0, le=1)
    max_output: int = Field(500, ge=1, le=500)
    gif_max_width: int = Field(1920, ge=320, le=1920)
    preference_memory_enabled: bool = False
    preference_profile_version: str | None = None

    @model_validator(mode="after")
    def validate_duration(self):
        if self.min_duration > self.max_duration:
            raise ValueError("min_duration must not exceed max_duration")
        if not self.preference_memory_enabled:
            self.preference_profile_version = None
        return self
```

- [ ] **Step 4: Define the complete run schema and connection**

```python
# app/runs/schema.py
RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_runs (
    run_id TEXT PRIMARY KEY,
    source_video_path TEXT NOT NULL,
    source_video_sha256 TEXT NOT NULL,
    status TEXT NOT NULL
      CHECK(status IN ('queued','running','cancel_requested','cancelled','completed','failed','interrupted')),
    progress REAL NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 1),
    current_phase TEXT,
    parameters_json TEXT NOT NULL,
    model_snapshot_json TEXT NOT NULL,
    index_version TEXT NOT NULL,
    preference_memory_enabled INTEGER NOT NULL DEFAULT 0
      CHECK(preference_memory_enabled IN (0,1)),
    preference_profile_version TEXT,
    base_run_id TEXT,
    parent_run_id TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    heartbeat_at TEXT,
    finished_at TEXT,
    CHECK(preference_memory_enabled = 1 OR preference_profile_version IS NULL),
    FOREIGN KEY(base_run_id) REFERENCES rag_runs(run_id),
    FOREIGN KEY(parent_run_id) REFERENCES rag_runs(run_id)
);

CREATE TABLE IF NOT EXISTS rag_run_steps (
    step_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL
      CHECK(status IN ('pending','running','completed','failed','cancelled')),
    completed_items INTEGER NOT NULL DEFAULT 0 CHECK(completed_items >= 0),
    total_items INTEGER CHECK(total_items IS NULL OR total_items >= 0),
    started_at TEXT,
    finished_at TEXT,
    error_json TEXT,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(run_id) REFERENCES rag_runs(run_id) ON DELETE CASCADE,
    UNIQUE(run_id, phase)
);

CREATE TABLE IF NOT EXISTS rag_run_frames (
    frame_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL CHECK(timestamp_ms >= 0),
    sample_source TEXT NOT NULL CHECK(sample_source IN ('coarse','refine')),
    frame_path TEXT NOT NULL,
    frame_sha256 TEXT NOT NULL,
    vlm_raw_json TEXT,
    vlm_normalized_json TEXT,
    quality_status TEXT NOT NULL DEFAULT 'pending'
      CHECK(quality_status IN ('pending','valid','invalid','repaired')),
    quality_errors_json TEXT,
    gif_worthiness REAL CHECK(gif_worthiness IS NULL OR (gif_worthiness >= 0 AND gif_worthiness <= 1)),
    emotional_core TEXT,
    caption TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK(status IN ('pending','running','completed','failed','skipped')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES rag_runs(run_id) ON DELETE CASCADE,
    UNIQUE(run_id, timestamp_ms, sample_source)
);

CREATE TABLE IF NOT EXISTS rag_retrieval_hits (
    hit_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK(rank >= 1),
    media_id TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    vector_type TEXT NOT NULL,
    query_text TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    index_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES rag_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(frame_id) REFERENCES rag_run_frames(frame_id) ON DELETE CASCADE,
    UNIQUE(frame_id, rank)
);

CREATE TABLE IF NOT EXISTS rag_run_candidates (
    run_candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK(rank >= 1),
    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
    representative_frame_id TEXT NOT NULL,
    merge_reason TEXT NOT NULL,
    contributing_frame_ids_json TEXT NOT NULL,
    annotation_snapshot_json TEXT NOT NULL,
    base_rag_score REAL NOT NULL,
    global_preference_score REAL,
    scenario_preference_score REAL,
    dislike_similarity REAL,
    dislike_penalty_multiplier REAL NOT NULL DEFAULT 1,
    diversity_bonus REAL,
    active_weights_json TEXT NOT NULL,
    inactive_reasons_json TEXT NOT NULL,
    preference_profile_version TEXT,
    score_breakdown_json TEXT NOT NULL,
    final_score REAL NOT NULL,
    exported_gif_path TEXT,
    candidate_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES rag_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(representative_frame_id) REFERENCES rag_run_frames(frame_id),
    UNIQUE(run_id, rank)
);

CREATE TABLE IF NOT EXISTS rag_run_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES rag_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rag_runs_status_created
  ON rag_runs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_rag_frames_run_timestamp
  ON rag_run_frames(run_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_rag_hits_run_frame
  ON rag_retrieval_hits(run_id, frame_id);
CREATE INDEX IF NOT EXISTS idx_rag_candidates_run_score
  ON rag_run_candidates(run_id, final_score DESC);
CREATE INDEX IF NOT EXISTS idx_rag_events_run_id
  ON rag_run_events(run_id, event_id);
"""
```

```python
# app/runs/db.py
def get_run_connection(path=None):
    resolved = Path(path or os.environ.get("GIFAGENT_RUN_DB") or get("runs.database_path"))
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_run_db(path=None):
    conn = get_run_connection(path)
    conn.executescript(RUN_SCHEMA)
    conn.commit()
    conn.close()
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_run_models.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/runs tests/test_run_models.py
git commit -m "feat: add run models and observability database"
```

### Task 6: Implement Run Repository, State Machine, and Event Replay

**Files:**
- Create: `app/runs/repository.py`
- Create: `app/runs/events.py`
- Create: `tests/test_run_repository.py`
- Create: `tests/test_run_events.py`

- [ ] **Step 1: Write failing atomic-claim and transition tests**

```python
# tests/test_run_repository.py
import pytest

from app.runs.models import RunParameters, RunStatus
from app.runs.repository import RunRepository


def test_only_one_queued_run_is_claimed(tmp_path):
    repo = RunRepository(tmp_path / "runs.db")
    first = repo.create_run("/media/a.mp4", "sha-a", RunParameters(), "idx-1", None)
    second = repo.create_run("/media/b.mp4", "sha-b", RunParameters(), "idx-1", None)
    assert repo.claim_next_run() == first
    assert repo.claim_next_run() is None
    assert repo.get_run(second)["status"] == RunStatus.QUEUED


def test_completed_run_cannot_return_to_running(tmp_path):
    repo = RunRepository(tmp_path / "runs.db")
    run_id = repo.create_run("/media/a.mp4", "sha-a", RunParameters(), "idx-1", None)
    repo.transition(run_id, RunStatus.RUNNING)
    repo.transition(run_id, RunStatus.COMPLETED)
    with pytest.raises(ValueError, match="invalid run transition"):
        repo.transition(run_id, RunStatus.RUNNING)
```

- [ ] **Step 2: Write failing event replay test**

```python
# tests/test_run_events.py
from app.runs.events import EventStore
from app.runs.models import RunParameters
from app.runs.repository import RunRepository


def test_replay_returns_only_events_after_last_id(tmp_path):
    path = tmp_path / "runs.db"
    repo = RunRepository(path)
    run_id = repo.create_run("/media/a.mp4", "sha-a", RunParameters(), "idx-1", None)
    store = EventStore(path)
    first = store.append(run_id, "run.started", {"progress": 0})
    second = store.append(run_id, "step.progress", {"progress": 1})
    events = store.list_after(run_id, first)
    assert [event["event_id"] for event in events] == [second]
```

- [ ] **Step 3: Run and verify failures**

Run: `uv run pytest tests/test_run_repository.py tests/test_run_events.py -v`

Expected: FAIL because repository and event store do not exist.

- [ ] **Step 4: Implement explicit transitions and transactional claim**

```python
# app/runs/repository.py
ALLOWED_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"completed", "failed", "cancel_requested", "interrupted"},
    "cancel_requested": {"cancelled", "failed"},
    "interrupted": {"running", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


def claim_next_run(self) -> str | None:
    conn = self._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT 1 FROM rag_runs WHERE status IN ('running','cancel_requested') LIMIT 1"
        ).fetchone()
        if active:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT run_id FROM rag_runs WHERE status='queued' ORDER BY created_at, run_id LIMIT 1"
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        now = utc_now()
        conn.execute(
            "UPDATE rag_runs SET status='running', started_at=?, heartbeat_at=? WHERE run_id=? AND status='queued'",
            (now, now, row["run_id"]),
        )
        conn.commit()
        return row["run_id"]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

Complete this repository surface with parameterized SQL only:

```python
def create_run(
    self,
    source_video_path: str,
    source_video_sha256: str,
    parameters: RunParameters,
    index_version: str,
    preference_profile_version: str | None,
    *,
    model_snapshot: dict | None = None,
    base_run_id: str | None = None,
    parent_run_id: str | None = None,
) -> str:
    if parameters.preference_memory_enabled and preference_profile_version is None:
        raise ValueError("memory-enabled run requires a profile version")
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    now = utc_now()
    conn = self._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO rag_runs(
                 run_id,source_video_path,source_video_sha256,status,progress,
                 parameters_json,model_snapshot_json,index_version,
                 preference_memory_enabled,preference_profile_version,
                 base_run_id,parent_run_id,created_at
               ) VALUES (?,?,?,'queued',0,?,?,?,?,?,?,?,?)""",
            (
                run_id, source_video_path, source_video_sha256,
                parameters.model_dump_json(), json.dumps(model_snapshot or {}), index_version,
                int(parameters.preference_memory_enabled), preference_profile_version,
                base_run_id, parent_run_id, now,
            ),
        )
        conn.executemany(
            """INSERT INTO rag_run_steps(step_id,run_id,phase,status,checkpoint_json)
               VALUES (?,?,?,'pending','{}')""",
            [(f"step_{uuid.uuid4().hex[:16]}", run_id, phase) for phase in RUN_PHASES],
        )
        conn.commit()
        return run_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_run(self, run_id: str) -> dict:
    row = self._fetch_one("SELECT * FROM rag_runs WHERE run_id=?", (run_id,))
    if row is None:
        raise KeyError(run_id)
    return dict(row)


def transition(self, run_id: str, target: RunStatus) -> None:
    conn = self._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT status FROM rag_runs WHERE run_id=?", (run_id,)).fetchone()
        if current is None or target.value not in ALLOWED_TRANSITIONS[current["status"]]:
            raise ValueError("invalid run transition")
        finished_at = utc_now() if target in TERMINAL_STATUSES else None
        conn.execute(
            "UPDATE rag_runs SET status=?,finished_at=COALESCE(?,finished_at) WHERE run_id=?",
            (target.value, finished_at, run_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

Define `RUN_PHASES` once in `app/runs/models.py` and reuse it in repository and pipeline. Add `update_heartbeat(run_id)`, `request_cancel(run_id)`, `start_step`, `update_step_progress`, `complete_step`, `insert_frame`, `replace_frame_hits`, and `replace_run_candidates`; each opens a short transaction, validates row counts, commits business rows and the corresponding event on the same connection, and closes in `finally`. Replacement methods delete only rows for the active non-terminal run and then bulk insert; reject mutation after a terminal status.

- [ ] **Step 5: Implement append-after-business-commit event storage**

```python
# app/runs/events.py
class EventStore:
    def append(self, run_id: str, event_type: str, payload: dict, conn=None) -> int:
        owned = conn is None
        conn = conn or get_run_connection(self.path)
        try:
            cursor = conn.execute(
                "INSERT INTO rag_run_events(run_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
                (run_id, event_type, json.dumps(payload), utc_now()),
            )
            if owned:
                conn.commit()
            return int(cursor.lastrowid)
        except Exception:
            if owned:
                conn.rollback()
            raise
        finally:
            if owned:
                conn.close()

    def list_after(self, run_id: str, last_event_id: int, limit: int = 200) -> list[dict]:
        conn = get_run_connection(self.path)
        try:
            rows = conn.execute(
                "SELECT * FROM rag_run_events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                (run_id, last_event_id, limit),
            ).fetchall()
            return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]
        finally:
            conn.close()
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_run_repository.py tests/test_run_events.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/runs/repository.py app/runs/events.py tests/test_run_repository.py tests/test_run_events.py
git commit -m "feat: add transactional run repository and event replay"
```

### Task 7: Introduce Immutable FAISS Versions

**Files:**
- Create: `app/services/index_versions.py`
- Modify: `app/services/indexer.py:19-229`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/test_indexer_manifest.py`
- Create: `tests/test_index_versions.py`

- [ ] **Step 1: Write failing publish-and-resolve tests**

Add the cross-platform publication lock dependency first: `uv add portalocker`.

```python
# tests/test_index_versions.py
import json
import numpy as np
import faiss

from app.services.index_versions import IndexVersionStore


def test_publish_creates_immutable_version_and_current_pointer(tmp_path):
    index = faiss.IndexFlatIP(3)
    index.add(np.array([[1.0, 0.0, 0.0]], dtype="float32"))
    store = IndexVersionStore(tmp_path)
    version = store.publish(index, {0: "media-1"}, "model-a")
    resolved = store.resolve("current")
    assert resolved.version == version
    assert resolved.manifest["vector_count"] == 1
    assert json.loads((tmp_path / "current.json").read_text())["index_version"] == version
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_index_versions.py -v`

Expected: FAIL because `IndexVersionStore` does not exist.

- [ ] **Step 3: Implement canonical hashing and atomic publish**

```python
# app/services/index_versions.py
@dataclass(frozen=True)
class ResolvedIndexVersion:
    version: str
    directory: Path
    manifest: dict


class IndexVersionStore:
    def __init__(self, root: Path):
        self.root = root

    def publish(self, index, id_map: dict[int, str], embedding_model: str) -> str:
        version = self.prepare(index, id_map, embedding_model)
        self.activate(version)
        return version

    def prepare(self, index, id_map: dict[int, str], embedding_model: str) -> str:
        manifest = {
            "schema_version": 2,
            "embedding_model": embedding_model,
            "dim": index.d,
            "metric": "cosine",
            "vector_count": index.ntotal,
        }
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        id_bytes = json.dumps({str(k): v for k, v in id_map.items()}, sort_keys=True).encode()
        version = hashlib.sha256(canonical.encode() + id_bytes + faiss.serialize_index(index).tobytes()).hexdigest()[:16]
        final_dir = self.root / "versions" / version
        if not final_dir.exists():
            tmp_dir = self.root / "versions" / f".{version}.{uuid.uuid4().hex}.tmp"
            tmp_dir.mkdir(parents=True, exist_ok=False)
            try:
                faiss.write_index(index, str(tmp_dir / "media_index.faiss"))
                (tmp_dir / "id_map.json").write_text(json.dumps({str(k): v for k, v in id_map.items()}), encoding="utf-8")
                (tmp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                self._verify_directory(tmp_dir)
                os.replace(tmp_dir, final_dir)
            except Exception:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise
        return version

    def activate(self, version: str, expected_current: str | None = None) -> None:
        self.resolve(version)
        pointer_path = self.root / "current.json"
        with portalocker.Lock(self.root / ".publication.lock", timeout=30):
            if expected_current is not None:
                current = json.loads(pointer_path.read_text(encoding="utf-8"))["index_version"]
                if current != expected_current:
                    raise RuntimeError("current index changed while version was prepared")
            atomic_json(pointer_path, {"index_version": version})

    def resolve(self, version: str) -> ResolvedIndexVersion:
        if version == "current":
            pointer = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
            version = pointer["index_version"]
        directory = self.root / "versions" / version
        manifest = self._verify_directory(directory)
        return ResolvedIndexVersion(version=version, directory=directory, manifest=manifest)

    def open_index(self, version: str):
        resolved = self.resolve(version)
        index = faiss.read_index(str(resolved.directory / "media_index.faiss"))
        id_map = json.loads((resolved.directory / "id_map.json").read_text(encoding="utf-8"))
        return resolved, index, {int(key): value for key, value in id_map.items()}

    def import_legacy(self, index_path: Path, id_map_path: Path, embedding_model: str) -> str:
        index = faiss.read_index(str(index_path))
        raw_map = json.loads(id_map_path.read_text(encoding="utf-8"))
        id_map = {int(key): value for key, value in raw_map.items()}
        if index.ntotal != len(id_map):
            raise ValueError("legacy index/id-map count mismatch")
        return self.publish(index, id_map, embedding_model)

    def _verify_directory(self, directory: Path) -> dict:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        index = faiss.read_index(str(directory / "media_index.faiss"))
        id_map = json.loads((directory / "id_map.json").read_text(encoding="utf-8"))
        if index.ntotal != manifest["vector_count"] or index.ntotal != len(id_map):
            raise ValueError("index manifest/id-map count mismatch")
        if index.d != manifest["dim"]:
            raise ValueError("index dimension does not match manifest")
        return manifest


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)
```

Resolve the store root from `GIFAGENT_FAISS_DIR`, then `paths.faiss_dir`. Add tests for missing files, dimension/count mismatch, repeated publish, corrupt current pointer, and failed publication leaving the old pointer unchanged.

- [ ] **Step 4: Refactor indexing to publish once per batch**

Replace per-item persisted `MediaIndex.add()` use in `index_all_annotated()` with:

```python
builder = MediaIndexBuilder.from_current(store)
for row in rows:
    embedding = compute_media_embedding(row["media_id"])
    if embedding:
        builder.add(embedding, row["media_id"])
version = builder.publish()
```

`MediaIndex(version="current")` becomes read-only for search. Promotion will use the same builder in Task 18.

- [ ] **Step 5: Keep legacy tests isolated**

Update `tests/test_indexer_manifest.py` to use `tmp_path` and dependency injection. Remove tests that inspect production FAISS state.

- [ ] **Step 6: Run tests**

Run:

```powershell
uv run pytest tests/test_index_versions.py tests/test_indexer_manifest.py -v
uv run pytest -q
```

Expected: PASS; no files under production `data/faiss` change.

- [ ] **Step 7: Commit**

```powershell
git add pyproject.toml uv.lock app/services/index_versions.py app/services/indexer.py tests/test_index_versions.py tests/test_indexer_manifest.py
git commit -m "feat: version FAISS indexes atomically"
```

---

## Phase 2: Baseline Run Pipeline

### Task 8: Extract Pure Video Sampling and Clip Functions

**Files:**
- Create: `app/runs/media.py`
- Create: `tests/test_run_media.py`

- [ ] **Step 1: Write failing pure-function tests**

```python
# tests/test_run_media.py
from app.runs.media import coarse_timestamps, refinement_timestamps, merge_scored_frames


def test_coarse_timestamps_respect_duration_and_clip_tail():
    assert coarse_timestamps(65, interval=20, max_duration=5) == [20, 40]


def test_refinement_timestamps_are_unique_and_in_bounds():
    result = refinement_timestamps({20}, duration=60, radius=20, interval=10, existing={20})
    assert result == [0, 10, 30, 40]


def test_merge_frames_uses_gap_and_best_score():
    frames = [
        {"timestamp": 10, "gif_worthiness": 0.6},
        {"timestamp": 17, "gif_worthiness": 0.9},
        {"timestamp": 40, "gif_worthiness": 0.7},
    ]
    clips = merge_scored_frames(frames, merge_gap=10)
    assert len(clips) == 2
    assert clips[0]["best_frame"]["gif_worthiness"] == 0.9
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_run_media.py -v`

Expected: FAIL because `app.runs.media` does not exist.

- [ ] **Step 3: Implement pure sampling and merge functions**

```python
# app/runs/media.py
def coarse_timestamps(duration: float, interval: int, max_duration: float) -> list[int]:
    return list(range(interval, max(interval, int(duration) - int(max_duration)), interval))


def refinement_timestamps(high: set[int], duration: float, radius: int, interval: int, existing: set[int]) -> list[int]:
    values = {
        timestamp + offset
        for timestamp in high
        for offset in range(-radius, radius + interval, interval)
        if 0 <= timestamp + offset <= duration - 1 and timestamp + offset not in existing
    }
    return sorted(values)


def merge_scored_frames(frames: list[dict], merge_gap: int) -> list[dict]:
    if not frames:
        return []
    ordered = sorted(frames, key=lambda item: item["timestamp"])
    groups = [[ordered[0]]]
    for frame in ordered[1:]:
        if frame["timestamp"] - groups[-1][-1]["timestamp"] <= merge_gap:
            groups[-1].append(frame)
        else:
            groups.append([frame])
    return [
        {
            "start_ts": group[0]["timestamp"],
            "end_ts": group[-1]["timestamp"],
            "frame_count": len(group),
            "frames": group,
            "best_frame": max(group, key=lambda item: item["gif_worthiness"]),
        }
        for group in groups
    ]
```

- [ ] **Step 4: Add subprocess-safe ffprobe/ffmpeg wrappers**

```python
from pathlib import Path
import os
import subprocess


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def probe_video(path: Path) -> float:
    result = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
    )
    return float(result.stdout.strip())


def extract_frame(video: Path, timestamp: float, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.stem}.partial{output.suffix}")
    _run([
        "ffmpeg", "-v", "error", "-y", "-ss", f"{timestamp:.3f}",
        "-i", str(video), "-frames:v", "1", "-q:v", "2", str(temporary),
    ])
    os.replace(temporary, output)
    return output


def export_gif(video: Path, start: float, end: float, output: Path, max_width: int, fps: int = 12) -> Path:
    if end <= start:
        raise ValueError("end must be greater than start")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.stem}.partial.gif")
    filter_graph = (
        f"[0:v]fps={fps},scale='min({max_width},iw)':-2:flags=lanczos,split[a][b];"
        "[a]palettegen[p];[b][p]paletteuse"
    )
    _run([
        "ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}",
        "-t", f"{end - start:.3f}", "-i", str(video),
        "-filter_complex", filter_graph, str(temporary),
    ])
    os.replace(temporary, output)
    return output
```

Add mocked subprocess tests asserting every command is a list, contains no `shell=True`, and publishes only after the subprocess succeeds. Accept output paths from `ArtifactStore`; do not construct Windows paths internally.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_run_media.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/runs/media.py tests/test_run_media.py
git commit -m "refactor: extract reusable video sampling primitives"
```

### Task 9: Add Atomic Run Artifacts and Injectable Inference

**Files:**
- Create: `app/runs/artifacts.py`
- Create: `app/runs/inference.py`
- Modify: `app/services/embedding.py:1-126`
- Create: `tests/test_run_artifacts.py`
- Create: `tests/test_run_inference.py`

- [ ] **Step 1: Write failing atomic-write and fake-inference tests**

```python
# tests/test_run_artifacts.py
from app.runs.artifacts import ArtifactStore


def test_json_is_published_atomically(tmp_path):
    store = ArtifactStore(tmp_path, "run-1")
    path = store.write_json("reports/result.json", {"ok": True})
    assert path.read_text(encoding="utf-8") == '{\n  "ok": true\n}'
    assert not list(path.parent.glob("*.tmp"))
```

```python
# tests/test_run_inference.py
from app.runs.inference import FakeRunInference


def test_fake_inference_is_deterministic():
    fake = FakeRunInference()
    first = fake.analyze_frame("frame-a.jpg")
    second = fake.analyze_frame("frame-a.jpg")
    assert first == second
    assert 0 <= first["normalized"]["gif_worthiness"] <= 1
```

- [ ] **Step 2: Run and verify failures**

Run: `uv run pytest tests/test_run_artifacts.py tests/test_run_inference.py -v`

Expected: FAIL because artifact and inference modules do not exist.

- [ ] **Step 3: Implement atomic artifact storage**

```python
# app/runs/artifacts.py
class ArtifactStore:
    def __init__(self, root: Path, run_id: str):
        self.run_dir = (root / run_id).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def path(self, relative: str) -> Path:
        candidate = (self.run_dir / relative).resolve()
        if self.run_dir not in candidate.parents and candidate != self.run_dir:
            raise ValueError("artifact path escapes run directory")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def write_json(self, relative: str, payload: dict) -> Path:
        target = self.path(relative)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)
        return target
```

- [ ] **Step 4: Define the inference protocol and production adapter**

```python
# app/runs/inference.py
RUN_FRAME_PROMPT = """Analyze this video frame as a potential GIF moment. Return only JSON with:
caption, emotional_core, aesthetic_notes (2-4 strings), why_i_like_it,
gif_worthiness (0.0-1.0), and reason. Describe only visible evidence."""


class InferenceQualityError(ValueError):
    def __init__(self, message: str, *, raw: str | dict, errors: list[str]):
        super().__init__(message)
        self.raw = raw
        self.errors = errors


class RunInference(Protocol):
    def analyze_frame(self, image_path: str) -> dict:
        raise NotImplementedError

    def embed_text(self, text: str) -> list[float]:
        raise NotImplementedError

    def synthesize(self, prompt: str) -> dict:
        raise NotImplementedError


class OllamaRunInference:
    def __init__(self, base_url: str, vlm_model: str, llm_model: str, embedding_model: str, client=None):
        self.base_url = base_url.rstrip("/")
        self.vlm_model = vlm_model
        self.llm_model = llm_model
        self.embedding_model = embedding_model
        self.client = client or httpx.Client(timeout=120)

    def _generate(self, *, model: str, prompt: str, images: list[str] | None = None) -> str:
        payload = {"model": model, "prompt": prompt, "stream": False}
        if images:
            payload["images"] = images
        response = self.client.post(f"{self.base_url}/api/generate", json=payload)
        response.raise_for_status()
        body = response.json()
        text = body.get("response") or body.get("thinking") or ""
        if not text.strip():
            raise ValueError("Ollama returned an empty generation")
        return text

    def analyze_frame(self, image_path: str) -> dict:
        encoded = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        raw = self._generate(model=self.vlm_model, prompt=RUN_FRAME_PROMPT, images=[encoded])
        parsed = parse_json_response(raw)
        if not parsed.ok or parsed.data is None:
            raise InferenceQualityError("invalid VLM JSON", raw=raw, errors=[parsed.error or "parse failed"])
        cleaned, errors = validate_frame_analysis(parsed.data)
        try:
            normalized = ClipScore.model_validate(cleaned).model_dump()
        except ValidationError as exc:
            raise InferenceQualityError("invalid VLM payload", raw=parsed.data, errors=errors + [str(exc)]) from exc
        return {
            "raw": parsed.data,
            "normalized": normalized,
            "quality_status": "repaired" if errors else "valid",
            "quality_errors": errors,
        }

    def synthesize(self, prompt: str) -> dict:
        raw = self._generate(model=self.llm_model, prompt=prompt)
        parsed = parse_json_response(raw)
        if not parsed.ok or parsed.data is None:
            raise InferenceQualityError("invalid LLM JSON", raw=raw, errors=[parsed.error or "parse failed"])
        cleaned, errors = validate_media_annotation(parsed.data)
        try:
            normalized = MediaAnnotation.model_validate(cleaned).model_dump()
        except ValidationError as exc:
            raise InferenceQualityError("invalid LLM payload", raw=parsed.data, errors=errors + [str(exc)]) from exc
        return {
            "raw": parsed.data,
            "normalized": normalized,
            "quality_status": "repaired" if errors else "valid",
            "quality_errors": errors,
        }

    def embed_text(self, text: str) -> list[float]:
        response = self.client.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.embedding_model, "prompt": text},
        )
        response.raise_for_status()
        return response.json()["embedding"]


class FakeRunInference:
    def analyze_frame(self, image_path: str) -> dict:
        value = int(hashlib.sha256(image_path.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        normalized = {
            "caption": f"A cinematic frame identified by {Path(image_path).name}",
            "emotional_core": "serenity",
            "aesthetic_notes": ["balanced practical lighting", "clear foreground separation"],
            "why_i_like_it": "the restrained composition gives the moment emotional clarity",
            "gif_worthiness": value,
            "reason": "the frame has a stable visual beat",
        }
        return {"raw": normalized, "normalized": normalized, "quality_status": "valid", "quality_errors": []}

    def embed_text(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        vector = np.array([digest[0] + 1, digest[1] + 1, digest[2] + 1], dtype=np.float32)
        return (vector / np.linalg.norm(vector)).tolist()

    def synthesize(self, prompt: str) -> dict:
        normalized = {
            "summary": "A concise cinematic moment with controlled visual rhythm",
            "emotional_core": "serenity",
            "aesthetic_notes": ["balanced lighting", "stable composition"],
            "why_i_like_it": "the visual restraint keeps attention on the emotional beat",
            "tags": ["cinematic", "quiet", "balanced"],
            "scene_type": "other",
        }
        return {"raw": normalized, "normalized": normalized, "quality_status": "valid", "quality_errors": []}
```

Import `ClipScore`, `MediaAnnotation`, Pydantic `ValidationError`, the JSON parser, and quality validators from their existing service modules. Add HTTP fake tests for parse failure, empty output, repaired output, validation failure, and embedding model selection; no test may call Ollama. The pipeline catches `InferenceQualityError`, persists its raw payload and errors with frame status `invalid`, then follows the configured retry policy; invalid output is never silently converted into a successful candidate.

- [ ] **Step 5: Make the existing embedding helper dynamically configurable**

Change `compute_text_embedding` to accept optional `base_url`, `model`, and `client`, while preserving no-argument caller behavior:

```python
def compute_text_embedding(text: str, model: str | None = None, base_url: str | None = None, client=None) -> list[float]:
    http = client or httpx
    response = http.post(
        f"{(base_url or get('embedding.base_url')).rstrip('/')}/api/embeddings",
        json={"model": model or get("embedding.text_model"), "prompt": text},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["embedding"]
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_run_artifacts.py tests/test_run_inference.py tests/test_quality.py tests/test_json_guard.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/runs/artifacts.py app/runs/inference.py app/services/embedding.py tests/test_run_artifacts.py tests/test_run_inference.py
git commit -m "feat: add atomic run artifacts and inference adapters"
```

### Task 10: Implement the Baseline Run Pipeline

**Files:**
- Create: `app/runs/pipeline.py`
- Create: `tests/test_run_pipeline.py`
- Modify: `scripts/test_video_adaptive.py`
- Modify: `scripts/test_video_rag_v2.py`

- [ ] **Step 1: Write a failing pipeline integration test with fakes**

```python
# tests/test_run_pipeline.py
from pathlib import Path

from app.runs.artifacts import ArtifactStore
from app.runs.models import RunParameters
from app.runs.pipeline import RunPipeline
from app.runs.repository import RunRepository


class FakeMediaBackend:
    def __init__(self, duration: float):
        self.duration = duration

    def probe(self, video: Path) -> float:
        return self.duration

    def extract_frame(self, video: Path, timestamp: float, output: Path) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"frame:{timestamp}".encode())
        return output

    def export_gif(self, video: Path, start: float, end: float, output: Path, max_width: int) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"gif:{start}:{end}:{max_width}".encode())
        return output


def test_pipeline_persists_frames_hits_and_candidates(tmp_path, fake_index, fake_inference, monkeypatch):
    repo = RunRepository(tmp_path / "runs.db")
    run_id = repo.create_run("/media/test.mp4", "video-sha", RunParameters(max_output=2), "idx-1", None)
    media = FakeMediaBackend(duration=65)
    pipeline = RunPipeline(repo, media, fake_inference, fake_index, ArtifactStore(tmp_path / "artifacts", run_id))

    pipeline.execute(run_id)

    assert repo.get_run(run_id)["status"] == "completed"
    assert len(repo.list_frames(run_id)) > 0
    assert len(repo.list_retrieval_hits(run_id)) > 0
    assert len(repo.list_candidates(run_id)) == 2
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_run_pipeline.py -v`

Expected: FAIL because `RunPipeline` and fake fixtures do not exist.

- [ ] **Step 3: Add deterministic fake fixtures**

```python
# append to tests/conftest.py
@pytest.fixture
def fake_inference():
    return FakeRunInference()


@pytest.fixture
def fake_index():
    class FakeIndex:
        version = "idx-1"
        def search(self, vector, top_k=5):
            return [
                {"media_id": f"media-{i}", "score": 0.9 - i * 0.05, "summary": f"item {i}", "emotional_core": "intimacy", "tags": ["warm_lighting"]}
                for i in range(top_k)
            ]
    return FakeIndex()
```

- [ ] **Step 4: Implement stage orchestration with explicit dependencies**

```python
# app/runs/pipeline.py
class RunPipeline:
    PHASES = (
        "probe_video", "coarse_sampling", "vlm_analysis", "refine_sampling",
        "embedding", "retrieval", "candidate_merge", "preference_rerank",
        "gif_export", "finalize",
    )

    def execute(self, run_id: str) -> None:
        run = self.repo.get_run(run_id)
        params = RunParameters.model_validate_json(run["parameters_json"])
        try:
            duration = self.media.probe(Path(run["source_video_path"]))
            frames = self._sample_and_analyze(run_id, duration, params)
            hits = self._retrieve(run_id, frames, params.top_k)
            clips = merge_scored_frames(frames, params.merge_gap)
            candidates = self._build_baseline_candidates(run_id, clips, hits, params)
            self._export(run_id, candidates, params)
            self.repo.transition(run_id, RunStatus.COMPLETED)
            self.events.append(run_id, "run.completed", {"candidate_count": len(candidates)})
        except CancelledRun:
            self.repo.transition(run_id, RunStatus.CANCELLED)
            self.events.append(run_id, "run.cancelled", {})
        except Exception as exc:
            self.repo.fail(run_id, classify_error(exc), str(exc))
            self.events.append(run_id, "run.failed", {"error": str(exc)})
            raise
```

Each private stage must commit rows before appending its event. With Preference Memory disabled, write `final_score=base_rag_similarity`, `preference_profile_version=NULL`, and `dislike_penalty_multiplier=1.0`.

- [ ] **Step 5: Convert legacy scripts into thin CLIs**

Both scripts must parse `--video`, `--config`, and output directory, construct `RunParameters`, call the shared pipeline, and print the resulting `run_id`. Remove direct `wsl ollama`, inline HTTP, and top-level execution logic. Keep old default video only behind an explicit CLI default so imports have no side effects.

```python
def main() -> int:
    args = build_parser().parse_args()
    run_id = create_and_execute_local_run(Path(args.video), RunParameters())
    print(run_id)
    return 0
```

- [ ] **Step 6: Run tests and CLI import checks**

Run:

```powershell
uv run pytest tests/test_run_pipeline.py -v
uv run python -c "import scripts.test_video_adaptive; import scripts.test_video_rag_v2; print('imports clean')"
```

Expected: PASS and `imports clean`; no video processing starts on import.

- [ ] **Step 7: Commit**

```powershell
git add app/runs/pipeline.py scripts/test_video_adaptive.py scripts/test_video_rag_v2.py tests/conftest.py tests/test_run_pipeline.py
git commit -m "refactor: run test videos through observable pipeline"
```

### Task 11: Add Serial Worker, Cancellation, Heartbeat, and Recovery

**Files:**
- Create: `app/runs/worker.py`
- Create: `scripts/run_worker.py`
- Create: `tests/test_run_worker.py`

- [ ] **Step 1: Write failing recovery and cancellation tests**

```python
# tests/test_run_worker.py
import pytest

from app.runs.db import get_run_connection
from app.runs.models import RunParameters, RunStatus
from app.runs.repository import RunRepository
from app.runs.worker import CancellationToken, CancelledRun, RunWorker


def test_worker_marks_stale_run_interrupted(tmp_path):
    path = tmp_path / "runs.db"
    repo = RunRepository(path)
    run_id = repo.create_run("/media/a.mp4", "sha-a", RunParameters(), "idx-1", None)
    repo.transition(run_id, RunStatus.RUNNING)
    conn = get_run_connection(path)
    conn.execute("UPDATE rag_runs SET heartbeat_at=? WHERE run_id=?", ("2026-01-01T00:00:00+00:00", run_id))
    conn.commit()
    conn.close()
    worker = RunWorker(repo, pipeline_factory=lambda _: None, interrupted_after=30)
    worker.recover_stale_runs(now="2026-01-01T00:01:00+00:00")
    assert repo.get_run(run_id)["status"] == "interrupted"


def test_cancel_requested_is_observed_between_frames(tmp_path):
    token = CancellationToken(lambda: True)
    with pytest.raises(CancelledRun):
        token.check()
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_run_worker.py -v`

Expected: FAIL because worker classes do not exist.

- [ ] **Step 3: Implement cancellation and worker loop**

```python
# app/runs/worker.py
class CancellationToken:
    def __init__(self, is_cancel_requested):
        self.is_cancel_requested = is_cancel_requested

    def check(self) -> None:
        if self.is_cancel_requested():
            raise CancelledRun()


class RunWorker:
    def run_forever(self, poll_seconds: float = 1.0) -> None:
        self.recover_stale_runs()
        while not self.stop_event.is_set():
            run_id = self.repo.claim_next_run()
            if not run_id:
                self.stop_event.wait(poll_seconds)
                continue
            self._execute_claimed(run_id)
```

Start a daemon heartbeat thread only while a run is active. Stop and join it in `finally`. Resume an interrupted run only from a completed step whose artifact hashes match `checkpoint_json`; otherwise mark failed with `CHECKPOINT_INVALID`.

- [ ] **Step 4: Add a worker entrypoint**

```python
# scripts/run_worker.py
def main() -> int:
    load_config()
    init_db()
    init_run_db()
    build_worker_from_config().run_forever()
    return 0
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_run_worker.py tests/test_run_repository.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/runs/worker.py scripts/run_worker.py tests/test_run_worker.py
git commit -m "feat: add serial run worker with recovery"
```

### Task 12: Add Run REST and SSE APIs

**Files:**
- Create: `app/routers/__init__.py`
- Create: `app/routers/runs.py`
- Create: `app/routers/system.py`
- Modify: `app/main.py:1-193`
- Create: `tests/test_runs_api.py`

- [ ] **Step 1: Write failing API tests**

```python
# tests/test_runs_api.py
def test_create_run_returns_202_and_snapshots_versions(api_client, test_media):
    response = api_client.post("/api/runs", json={"source_video_path": str(test_media), "parameters": {}})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["index_version"] == "idx-1"


def test_sse_replays_after_last_event_id(api_client, seeded_run):
    response = api_client.get(
        f"/api/runs/{seeded_run}/events?follow=false",
        headers={"Last-Event-ID": "1"},
    )
    assert response.status_code == 200
    assert "id: 2" in response.text
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_runs_api.py -v`

Expected: FAIL with 404 routes.

- [ ] **Step 3: Refactor FastAPI into an app factory**

```python
# app/main.py
@dataclass(frozen=True)
class AppSettings:
    config_path: Path = Path("configs/models.yaml")
    library_db: Path | None = None
    run_db: Path | None = None
    media_root: Path | None = None
    faiss_root: Path | None = None


def create_app(settings: AppSettings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        load_config(settings.config_path if settings else "configs/models.yaml")
        init_db(settings.library_db if settings else None)
        init_run_db(settings.run_db if settings else None)
        app.state.settings = settings or AppSettings()
        yield

    app = FastAPI(title="GifAgent", lifespan=lifespan)
    app.include_router(system.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    return app


app = create_app()
```

Move existing status, media, scan, preprocessing, scoring, feedback, and review endpoints unchanged into `app/routers/system.py`. Do not silently swallow FAISS exceptions in the moved review endpoint; log a structured warning.

- [ ] **Step 4: Implement path validation and run endpoints**

```python
class CreateRunRequest(BaseModel):
    source_video_path: str
    parameters: RunParameters = Field(default_factory=RunParameters)


def resolve_media_path(raw: str, media_root: Path) -> Path:
    path = Path(raw).resolve()
    root = media_root.resolve()
    if root not in path.parents:
        raise HTTPException(400, "MEDIA_OUTSIDE_ALLOWED_ROOT")
    if not path.is_file():
        raise HTTPException(404, "MEDIA_NOT_FOUND")
    return path
```

Add these routes:

```text
POST /api/runs
GET  /api/runs
GET  /api/runs/{run_id}
POST /api/runs/{run_id}/cancel
POST /api/runs/{run_id}/retry
GET  /api/runs/{run_id}/events
GET  /api/runs/{run_id}/steps
GET  /api/runs/{run_id}/frames
GET  /api/runs/{run_id}/frames/{frame_id}/retrievals
GET  /api/runs/{run_id}/candidates
GET  /api/runs/{run_id}/artifacts/{artifact_path:path}
GET  /api/run-comparisons?left=<id>&right=<id>
```

Artifact responses must use `ArtifactStore.path()` and reject traversal. List endpoints use `limit` and opaque `cursor`; cap `limit` at 200.

- [ ] **Step 5: Implement database-backed SSE replay**

```python
async def stream_run_events(run_id: str, last_id: int, follow: bool = True):
    cursor = last_id
    while True:
        events = event_store.list_after(run_id, cursor)
        for event in events:
            cursor = event["event_id"]
            yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event['payload'])}\n\n"
        if not follow:
            return
        yield ": heartbeat\n\n"
        await asyncio.sleep(15)
```

The endpoint defaults `follow=true` for browsers and accepts `follow=false` for finite replay tests and diagnostics. Read `Last-Event-ID` as an integer, reject malformed values with HTTP 400, and return 404 before opening the stream when the run is unknown.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_runs_api.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/main.py app/routers app/runs tests/test_runs_api.py
git commit -m "feat: expose run control and replayable SSE APIs"
```

---

## Phase 3: Preference Memory Core

### Task 13: Build Scenario Keys and Materialize Candidates

**Files:**
- Create: `app/services/scenario.py`
- Create: `app/services/candidates.py`
- Create: `tests/test_scenario.py`
- Create: `tests/test_candidates.py`

- [ ] **Step 1: Write failing scenario tests**

```python
# tests/test_scenario.py
from app.services.scenario import build_scenario_keys


def test_scenario_keys_are_bounded_and_normalized():
    keys = build_scenario_keys({
        "emotional_core": "Intimacy",
        "scene_type": "Close Up",
        "tags": ["Warm Lighting", "soft/focus", "Film Grain", "ignored"],
    })
    assert keys == ["global", "emotion:intimacy", "scene_type:close_up", "tag:warm_lighting", "tag:soft_focus", "tag:film_grain"]
```

- [ ] **Step 2: Write failing materialization idempotency test**

```python
# tests/test_candidates.py
def test_materialize_run_candidate_is_idempotent(library_db, run_db):
    service = CandidateService(library_db, run_db)
    first = service.materialize("run-candidate-1")
    second = service.materialize("run-candidate-1")
    assert first == second
```

- [ ] **Step 3: Run and verify failures**

Run: `uv run pytest tests/test_scenario.py tests/test_candidates.py -v`

Expected: FAIL because scenario and candidate services do not exist.

- [ ] **Step 4: Implement deterministic key normalization**

```python
def normalize_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return normalized.strip("_")


def build_scenario_keys(annotation: dict) -> list[str]:
    keys = ["global"]
    if emotion := normalize_key(annotation.get("emotional_core") or ""):
        keys.append(f"emotion:{emotion}")
    if scene := normalize_key(annotation.get("scene_type") or ""):
        keys.append(f"scene_type:{scene}")
    for tag in (annotation.get("tags") or [])[:3]:
        if value := normalize_key(str(tag)):
            keys.append(f"tag:{value}")
    return list(dict.fromkeys(keys))
```

- [ ] **Step 5: Implement transactional candidate materialization**

```python
class CandidateService:
    def materialize(self, run_candidate_id: str) -> str:
        run_conn = get_run_connection(self.run_db)
        row = run_conn.execute(
            """SELECT c.*, r.source_video_path, r.source_video_sha256, f.frame_path
               FROM rag_run_candidates c
               JOIN rag_runs r ON r.run_id=c.run_id
               JOIN rag_run_frames f ON f.frame_id=c.representative_frame_id
               WHERE c.run_candidate_id=?""",
            (run_candidate_id,),
        ).fetchone()
        run_conn.close()
        if row is None:
            raise KeyError(run_candidate_id)

        annotation = json.loads(row["annotation_snapshot_json"])
        quality_errors = annotation.get("quality_errors") or []
        scenario_keys = build_scenario_keys(annotation)
        candidate_text = "\n".join(filter(None, [
            annotation.get("summary"),
            annotation.get("emotional_core"),
            annotation.get("why_i_like_it"),
            " ".join(annotation.get("tags") or []),
        ]))
        vector = np.asarray(self.inference.embed_text(candidate_text), dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if vector.ndim != 1 or norm == 0:
            raise ValueError("candidate embedding must be a non-zero vector")
        vector = vector / norm

        candidate_id = f"cand_{uuid.uuid4().hex[:16]}"
        vector_id = f"cvec_{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection(self.library_db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO candidate_gifs(
                     candidate_id,source_run_id,source_run_candidate_id,
                     source_video_path,source_video_sha256,start,end,duration,
                     representative_frame_path,caption,summary,emotional_core,
                     aesthetic_notes_json,why_i_like_it,tags_json,scene_type,
                     scenario_keys_json,base_rag_score_raw,base_rag_score,final_score,
                     score_json,status,quality_status,quality_errors_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'candidate',?,?,?,?)
                   ON CONFLICT(source_run_id,source_run_candidate_id) DO NOTHING""",
                (
                    candidate_id, row["run_id"], row["run_candidate_id"],
                    row["source_video_path"], row["source_video_sha256"],
                    row["start_ms"] / 1000, row["end_ms"] / 1000,
                    (row["end_ms"] - row["start_ms"]) / 1000, row["frame_path"],
                    annotation.get("caption"), annotation.get("summary"),
                    annotation.get("emotional_core"),
                    json.dumps(annotation.get("aesthetic_notes") or [], ensure_ascii=False),
                    annotation.get("why_i_like_it"),
                    json.dumps(annotation.get("tags") or [], ensure_ascii=False),
                    annotation.get("scene_type"), json.dumps(scenario_keys),
                    row["base_rag_score"], row["base_rag_score"], row["final_score"],
                    row["score_breakdown_json"], "failed" if quality_errors else "passed",
                    json.dumps(quality_errors, ensure_ascii=False), now, now,
                ),
            )
            winner = conn.execute(
                """SELECT candidate_id FROM candidate_gifs
                   WHERE source_run_id=? AND source_run_candidate_id=?""",
                (row["run_id"], row["run_candidate_id"]),
            ).fetchone()["candidate_id"]
            if winner == candidate_id:
                conn.execute(
                    """INSERT INTO candidate_vectors(
                         vector_id,candidate_id,vector_type,embedding_model,
                         embedding_dim,vector_json,source_text,created_at
                       ) VALUES (?,?, 'candidate_text',?,?,?,?,?)""",
                    (vector_id, candidate_id, self.embedding_model, len(vector),
                     json.dumps(vector.tolist()), candidate_text, now),
                )
            conn.commit()
            return winner
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
```

The insert has 25 bound values plus the literal `candidate` status: verify this in the test by exercising the real SQLite statement, not by mocking the connection. Do not hold the `library.db` transaction while calling Ollama; the vector is computed before `BEGIN IMMEDIATE`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_scenario.py tests/test_candidates.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/services/scenario.py app/services/candidates.py tests/test_scenario.py tests/test_candidates.py
git commit -m "feat: materialize run candidates with scenario keys"
```

### Task 14: Record Append-Only Feedback with Latest-Effective Semantics

**Files:**
- Create: `app/services/preference_memory.py`
- Create: `tests/test_preference_events.py`
- Modify: `app/routers/system.py`

- [ ] **Step 1: Write failing supersession tests**

```python
# tests/test_preference_events.py
def test_latest_event_supersedes_previous_rating(library_db):
    memory = PreferenceMemory(library_db)
    first = memory.record_event(target_type="candidate_gif", target_id="c1", rating="like", scenario_keys=["global"], score_snapshot={})
    second = memory.record_event(target_type="candidate_gif", target_id="c1", rating="neutral", scenario_keys=["global"], score_snapshot={})
    events = memory.list_effective_events()
    assert [event["event_id"] for event in events] == [second]
    assert memory.get_event(second)["supersedes_event_id"] == first
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_preference_events.py -v`

Expected: FAIL because `PreferenceMemory` does not exist.

- [ ] **Step 3: Implement append-only writes and effective-event query**

```python
def record_event(self, *, target_type, target_id, rating, scenario_keys, score_snapshot, reason=None, corrected_tags=None, vector=None, embedding_model=None, source="api") -> str:
    if target_type not in {"media", "candidate_gif"}:
        raise ValueError("invalid target_type")
    if rating not in {"like", "dislike", "neutral"}:
        raise ValueError("invalid rating")
    vector_values = None if vector is None else [float(value) for value in vector]
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(self.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous_rows = conn.execute(
            """SELECT e.event_id FROM preference_events e
               WHERE e.target_type=? AND e.target_id=?
                 AND NOT EXISTS (
                   SELECT 1 FROM preference_events newer
                   WHERE newer.supersedes_event_id=e.event_id
                 )""",
            (target_type, target_id),
        ).fetchall()
        if len(previous_rows) > 1:
            raise RuntimeError("preference event chain has multiple effective leaves")
        previous = previous_rows[0] if previous_rows else None
        event_id = f"pref_{uuid.uuid4().hex[:16]}"
        conn.execute(
            """INSERT INTO preference_events(
                 event_id,target_type,target_id,rating,supersedes_event_id,reason,
                 corrected_tags_json,scenario_keys_json,embedding_model,embedding_dim,
                 target_vector_json,score_snapshot_json,model_info_json,source,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, target_type, target_id, rating,
                previous["event_id"] if previous else None, reason,
                json.dumps(corrected_tags, ensure_ascii=False) if corrected_tags is not None else None,
                json.dumps(scenario_keys, ensure_ascii=False), embedding_model,
                len(vector_values) if vector_values is not None else None,
                json.dumps(vector_values) if vector_values is not None else None,
                json.dumps(score_snapshot, ensure_ascii=False), None, source, now,
            ),
        )
        conn.commit()
        return event_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_effective_events(self) -> list[sqlite3.Row]:
    conn = get_connection(self.db_path)
    try:
        return conn.execute(
            """SELECT e.*
               FROM preference_events e
               WHERE NOT EXISTS (
                 SELECT 1 FROM preference_events newer
                 WHERE newer.supersedes_event_id=e.event_id
               )
               ORDER BY e.created_at,e.event_id"""
        ).fetchall()
    finally:
        conn.close()
```

A latest neutral event remains visible for audit but is excluded from centroid input. The supersession leaf, not wall-clock ordering, determines the effective event; add tests where two events have the same timestamp and confirm the event that supersedes the other is effective.

- [ ] **Step 4: Dual-write existing media feedback in one transaction**

Refactor the existing `/api/feedback` handler to call a service that inserts `feedback` and `preference_events(target_type='media')` using the same `library.db` connection and commit. If either insert fails, roll back both.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_preference_events.py tests/test_runs_api.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/services/preference_memory.py app/routers/system.py tests/test_preference_events.py
git commit -m "feat: record append-only preference feedback"
```

### Task 15: Build Immutable Global and Scenario Profiles

**Files:**
- Modify: `app/services/preference_memory.py`
- Create: `tests/test_preference_profiles.py`
- Create: `scripts/preference_memory.py`

- [ ] **Step 1: Write failing centroid, threshold, and current-pointer tests**

```python
# tests/test_preference_profiles.py
import pytest


def test_rebuild_creates_global_and_eligible_scenario_profiles(preference_memory_factory):
    memory = preference_memory_factory(likes=3, dislikes=2, scenario="emotion:intimacy")
    version = memory.rebuild_profiles(embedding_model="model-a", embedding_dim=3)
    profiles = memory.list_profiles(version)
    assert {p["scenario_key"] for p in profiles} == {"global", "emotion:intimacy"}
    assert memory.current_version() == version


def test_failed_build_does_not_replace_current(preference_memory_factory, monkeypatch):
    memory = preference_memory_factory(likes=3, dislikes=2, scenario="emotion:intimacy")
    first = memory.rebuild_profiles("model-a", 3)
    monkeypatch.setattr(memory, "_build_profile_rows", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        memory.rebuild_profiles("model-a", 3)
    assert memory.current_version() == first


def test_neutral_latest_event_is_not_in_centroid(preference_memory_factory):
    memory = preference_memory_factory(likes=3, dislikes=2, scenario="emotion:intimacy")
    memory.rate_then_neutral("neutral-target")
    version = memory.rebuild_profiles("model-a", 3)
    global_profile = memory.get_profile(version, "global")
    assert global_profile["sample_count_neutral"] == 1
    assert global_profile["sample_count_like"] == 3
```

- [ ] **Step 2: Run and verify failures**

Run: `uv run pytest tests/test_preference_profiles.py -v`

Expected: FAIL because Profile rebuild is not implemented.

- [ ] **Step 3: Implement normalized centroids and confidence**

```python
def l2_normalize(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    if norm == 0:
        raise ValueError("zero vector cannot form a preference profile")
    return vector / norm


def centroid(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    matrix = np.asarray(vectors, dtype=np.float32)
    matrix = np.vstack([l2_normalize(row) for row in matrix])
    return l2_normalize(matrix.mean(axis=0)).tolist()


def profile_confidence(like_count: int, dislike_count: int) -> float:
    return min(1.0, (like_count + dislike_count) / 20.0)


def signed_weight(likes: int, dislikes: int) -> float:
    return float(np.clip((likes - 1.5 * dislikes) / max(likes + dislikes, 1), -1, 1))
```

- [ ] **Step 4: Implement deterministic versioning and atomic current switch**

Build steps:

1. Start a read transaction and capture watermark `(created_at,event_id)`.
2. Select latest-effective events not newer than the watermark.
3. Exclude incompatible model/dimension vectors and report their count.
4. Build `global` plus scenario groups meeting `min_total_samples=5`.
5. Include liked centroid only with at least 3 likes and disliked centroid only with at least 2 dislikes.
6. Hash canonical config, watermark, active target/rating pairs, and vector hashes into `profile_version`.
7. Insert a `building` row and all Profile rows in one transaction.
8. Validate dimensions, counts, and unique keys.
9. Mark build `completed` and upsert singleton current pointer in the same final transaction.
10. On exception, mark only the build `failed`; never update current.

```python
conn.execute(
    """INSERT INTO preference_profile_current(singleton_id,profile_version,updated_at)
       VALUES (1,?,?)
       ON CONFLICT(singleton_id) DO UPDATE SET profile_version=excluded.profile_version, updated_at=excluded.updated_at""",
    (version, utc_now()),
)
```

- [ ] **Step 5: Add status and rebuild CLI**

```python
# scripts/preference_memory.py
def build_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("--apply", action="store_true")
    return parser
```

`rebuild` without `--apply` prints event counts and exits non-zero without writing. `status` prints current version, build status, event watermark, and eligible scenario count.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_preference_profiles.py tests/test_preference_events.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/services/preference_memory.py scripts/preference_memory.py tests/test_preference_profiles.py
git commit -m "feat: build immutable global and scenario profiles"
```

### Task 16: Implement Availability-Aware Preference Reranking

**Files:**
- Create: `app/services/reranker.py`
- Create: `tests/test_reranker.py`

- [ ] **Step 1: Write failing baseline identity and penalty tests**

```python
# tests/test_reranker.py
import pytest

from app.services.reranker import PreferenceReranker, ScoreContext


def test_no_profiles_is_exactly_baseline():
    result = PreferenceReranker().score(ScoreContext(base_rag_similarity=0.73))
    assert result.final_score == 0.73
    assert result.profile_score is None


def test_available_weights_are_renormalized():
    result = PreferenceReranker().score(ScoreContext(base_rag_similarity=0.6, global_like_similarity=0.9))
    expected = (0.45 * 0.6 + 0.20 * 0.9) / 0.65
    assert result.raw_score == pytest.approx(expected)


@pytest.mark.parametrize((similarity,multiplier), [(0.74, 1.0), (0.75, 0.7), (0.85, 0.4)])
def test_dislike_thresholds(similarity, multiplier):
    result = PreferenceReranker().score(ScoreContext(base_rag_similarity=0.8, dislike_similarity=similarity))
    assert result.penalty_multiplier == multiplier
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_reranker.py -v`

Expected: FAIL because reranker does not exist.

- [ ] **Step 3: Define complete score input/output models**

```python
class ScoreContext(BaseModel):
    base_rag_similarity: float = Field(ge=0, le=1)
    global_like_similarity: float | None = Field(None, ge=0, le=1)
    scenario_like_similarity: float | None = Field(None, ge=0, le=1)
    dislike_similarity: float | None = Field(None, ge=0, le=1)
    diversity_bonus: float | None = Field(None, ge=0, le=1)
    profile_version: str | None = None
    matched_profiles: list[dict] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    base_rag_similarity: float
    profile_score: float | None
    raw_score: float
    final_score: float
    penalty_multiplier: float
    active_weights: dict[str, float]
    inactive_reasons: dict[str, str]
    profile_version: str | None
    matched_profiles: list[dict]
```

- [ ] **Step 4: Implement normalization and explicit inactive reasons**

```python
def score(self, context: ScoreContext) -> ScoreBreakdown:
    components = {"base_rag_similarity": context.base_rag_similarity}
    inactive = {}
    for name in ("global_like_similarity", "scenario_like_similarity", "diversity_bonus"):
        value = getattr(context, name)
        if value is None:
            inactive[name] = "unavailable"
        else:
            components[name] = float(np.clip(value, 0, 1))
    if context.dislike_similarity is None:
        inactive["dislike_avoidance"] = "no_valid_disliked_centroid"
    else:
        components["dislike_avoidance"] = 1 - context.dislike_similarity
    active_weights = {name: self.weights[name] for name in components}
    raw = sum(components[name] * active_weights[name] for name in components) / sum(active_weights.values())
    penalty = 0.4 if (context.dislike_similarity or 0) >= 0.85 else 0.7 if (context.dislike_similarity or 0) >= 0.75 else 1.0
    memory_names = set(components) - {"base_rag_similarity"}
    profile_score = None if not memory_names else sum(components[n] * active_weights[n] for n in memory_names) / sum(active_weights[n] for n in memory_names)
    return ScoreBreakdown(
        base_rag_similarity=context.base_rag_similarity,
        profile_score=profile_score,
        raw_score=raw,
        final_score=float(np.clip(raw * penalty, 0, 1)),
        penalty_multiplier=penalty,
        active_weights=active_weights,
        inactive_reasons=inactive,
        profile_version=context.profile_version,
        matched_profiles=context.matched_profiles,
    )
```

Round only in API serialization, not during computation.

- [ ] **Step 5: Add scenario confidence aggregation tests**

Test that only profiles with `confidence >= 0.25` participate and that scenario similarity is `sum(similarity * confidence) / sum(confidence)`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_reranker.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/services/reranker.py tests/test_reranker.py
git commit -m "feat: add explainable preference reranker"
```

---

## Phase 4: Preference Integration and A/B Comparison

### Task 17: Integrate Fixed Profile Versions into Runs

**Files:**
- Modify: `app/runs/pipeline.py`
- Modify: `app/runs/repository.py`
- Create: `app/runs/comparison.py`
- Create: `tests/test_run_comparison.py`

- [ ] **Step 1: Write failing fixed-version and comparison tests**

```python
# tests/test_run_comparison.py
def test_run_keeps_profile_version_when_current_changes(run_harness):
    first = run_harness.build_profile("profile-1")
    run_id = run_harness.create_run(memory=True)
    run_harness.build_profile("profile-2")
    run_harness.execute(run_id)
    assert run_harness.repo.get_run(run_id)["preference_profile_version"] == first


def test_compare_aligns_frames_hits_and_candidates(run_harness):
    left, right = run_harness.seed_comparable_runs()
    report = compare_runs(run_harness.repo, left, right)
    assert report["same_source"] is True
    assert "top_k_jaccard" in report["frames"][0]
    assert "rank_delta" in report["candidates"][0]
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_run_comparison.py -v`

Expected: FAIL because profile integration and comparison do not exist.

- [ ] **Step 3: Resolve versions at run creation, never inside a stage**

In the create-run service:

```python
profile_version = None
if parameters.preference_memory_enabled:
    profile_version = parameters.preference_profile_version or memory.current_version()
    memory.assert_compatible(profile_version, embedding_model, embedding_dim)
return repo.create_run(source, sha256, parameters, index_version, profile_version)
```

The pipeline loads only `rag_runs.preference_profile_version`. It must not call `current_version()` after the run row is created.

- [ ] **Step 4: Build score contexts and persist full snapshots**

For each run candidate:

1. Compute baseline Top-K score.
2. Resolve global and matching scenario Profiles from the fixed version.
3. Compute cosine similarities and confidence aggregation.
4. Call `PreferenceReranker.score()`.
5. Persist the complete `ScoreBreakdown.model_dump_json()` and Profile version.

When memory is disabled, call the same reranker with only baseline; assert exact identity before persistence.

- [ ] **Step 5: Implement deterministic comparison**

```python
def compare_runs(repo, left_id: str, right_id: str, timestamp_tolerance_ms: int = 500) -> dict:
    left, right = repo.get_run(left_id), repo.get_run(right_id)
    if left["source_video_sha256"] != right["source_video_sha256"]:
        return compare_summary_only(left, right)
    return {
        "same_source": True,
        "frames": align_frames(repo.list_frames(left_id), repo.list_frames(right_id), timestamp_tolerance_ms),
        "candidates": align_candidates(repo.list_candidates(left_id), repo.list_candidates(right_id), min_iou=0.5),
        "summary": compare_metrics(left_id, right_id),
    }
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_run_comparison.py tests/test_run_pipeline.py tests/test_reranker.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/runs/pipeline.py app/runs/repository.py app/runs/comparison.py tests/test_run_comparison.py
git commit -m "feat: compare runs with fixed preference profiles"
```

### Task 18: Add Candidate, Profile, Rerank, and Promotion APIs

**Files:**
- Create: `app/routers/candidates.py`
- Create: `app/routers/preference.py`
- Create: `app/services/promotion.py`
- Create: `scripts/pipeline.py`
- Create: `tests/test_candidate_api.py`
- Create: `tests/test_preference_api.py`
- Create: `tests/test_promotion.py`
- Modify: `app/main.py`

- [ ] **Step 1: Write failing candidate feedback API test**

```python
# tests/test_candidate_api.py
def test_run_candidate_feedback_materializes_and_records_event(api_client, seeded_run_candidate):
    response = api_client.post(
        f"/api/run-candidates/{seeded_run_candidate}/feedback",
        json={"rating": "like", "reason": "warm close-up", "corrected_tags": ["warm_lighting"]},
    )
    assert response.status_code == 200
    assert response.json()["candidate_id"]
    assert response.json()["event_id"]
```

- [ ] **Step 2: Write failing promotion safety tests**

```python
# tests/test_promotion.py
import sqlite3

import pytest


def count_rows(path, table: str) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_promote_requires_confirmation(promotion_service, candidate_id):
    with pytest.raises(ValueError, match="confirmation required"):
        promotion_service.promote(candidate_id, confirm=False)


def test_like_does_not_change_media_count(api_client, library_db, seeded_run_candidate):
    before = count_rows(library_db, "media")
    api_client.post(f"/api/run-candidates/{seeded_run_candidate}/feedback", json={"rating": "like"})
    assert count_rows(library_db, "media") == before


def test_failed_activation_retries_same_media_id(promotion_service, candidate_id, monkeypatch):
    monkeypatch.setattr(promotion_service.index_store, "activate", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("activation failed")))
    with pytest.raises(RuntimeError, match="activation failed"):
        promotion_service.promote(candidate_id, confirm=True)
    planned = promotion_service.get_attempt(candidate_id)["media_id"]
    monkeypatch.undo()
    assert promotion_service.promote(candidate_id, confirm=True) == planned
```

- [ ] **Step 3: Run and verify failures**

Run: `uv run pytest tests/test_candidate_api.py tests/test_preference_api.py tests/test_promotion.py -v`

Expected: FAIL with missing routes/services.

- [ ] **Step 4: Implement candidate and Profile routers**

Add these routes with Pydantic request/response models:

```text
GET  /api/candidates
GET  /api/candidates/next
GET  /api/candidates/{candidate_id}
POST /api/candidates/{candidate_id}/feedback
POST /api/candidates/{candidate_id}/promote
POST /api/candidates/rerank
POST /api/run-candidates/{run_candidate_id}/feedback
POST /api/run-candidates/{run_candidate_id}/promote
POST /api/preference/rebuild
GET  /api/preference/builds
GET  /api/preference/profiles
GET  /api/preference/profiles/{profile_id}
```

`/api/candidates/rerank` requires `limit` or `source_video_sha256`; reject an unbounded request with HTTP 422.

- [ ] **Step 5: Implement promotion as a checkpointed service**

```python
def promote(self, candidate_id: str, confirm: bool) -> str:
    if not confirm:
        raise ValueError("confirmation required")
    candidate = self.candidates.get(candidate_id)
    if candidate["status"] == "promoted":
        return candidate["promoted_media_id"]
    attempt = self._claim_or_resume(candidate)
    try:
        if attempt.state == "claimed":
            self._validate_quality_and_artifact(candidate)
            self._reject_duplicate(candidate)
            attempt = self._checkpoint(attempt, "validated")
        if attempt.state == "validated":
            vector = self.embedding.compute_candidate_embedding(candidate)
            prepared = self.index_builder.prepare_add(
                base_version=attempt.base_index_version,
                vector=vector,
                media_id=attempt.media_id,
            )
            attempt = self._checkpoint(attempt, "index_prepared", prepared_index_version=prepared)
        if attempt.state == "index_prepared":
            self._insert_media_frames_annotations_and_refs(candidate, attempt.media_id)
            attempt = self._checkpoint(attempt, "media_written")
        if attempt.state == "media_written":
            try:
                self.index_store.activate(
                    attempt.prepared_index_version,
                    expected_current=attempt.base_index_version,
                )
            except RuntimeError as exc:
                if "current index changed" not in str(exc):
                    raise
                attempt = self._reprepare_from_current(attempt, candidate)
                self.index_store.activate(
                    attempt.prepared_index_version,
                    expected_current=attempt.base_index_version,
                )
            attempt = self._checkpoint(attempt, "index_activated")
        if attempt.state == "index_activated":
            self._complete_candidate_and_attempt(candidate_id, attempt)
        return attempt.media_id
    except Exception as exc:
        self._record_attempt_error(attempt.attempt_id, exc)
        raise
```

`_claim_or_resume()` runs in `BEGIN IMMEDIATE`, sets `active_slot=1`, and rejects a second active promotion. Each checkpoint updates `heartbeat_at`. A stale active attempt can be resumed by the same candidate or marked failed by the operational CLI after 10 minutes. `_insert_media_frames_annotations_and_refs()` is idempotent on the planned `media_id`; ordinary media list queries exclude media IDs whose promotion attempt is not `completed`. `_complete_candidate_and_attempt()` sets candidate status, `promoted_media_id`, attempt state `completed`, and `active_slot=NULL` in one transaction.

The prepared index is immutable but inactive. If preparation fails, current is unchanged and no media row exists. If activation fails, the inserted media remains hidden as pending and retry reuses it. If final bookkeeping fails after activation, current references an existing media row and retry only completes bookkeeping. Add fault-injection tests at every checkpoint.

- [ ] **Step 6: Add guarded operational CLI commands**

`scripts/pipeline.py` must implement:

```text
runs create --video <path>
candidates rerank --limit <n> --apply
candidates promote <candidate_id> --confirm
```

Without `--apply` or `--confirm`, write commands exit non-zero after printing a dry-run summary.

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_candidate_api.py tests/test_preference_api.py tests/test_promotion.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add app/routers/candidates.py app/routers/preference.py app/services/promotion.py app/main.py scripts/pipeline.py tests/test_candidate_api.py tests/test_preference_api.py tests/test_promotion.py
git commit -m "feat: expose candidate feedback profiles and promotion"
```

### Task 19: Build the Versioned Preference Map

**Files:**
- Create: `app/services/preference_map.py`
- Create: `app/routers/preference_map.py`
- Create: `tests/test_preference_map.py`
- Modify: `app/services/preference_schema.py`
- Modify: `pyproject.toml`
- Modify: `app/main.py`

- [ ] **Step 1: Add map dependencies**

Run:

```powershell
uv add umap-learn scikit-learn
```

Expected: `pyproject.toml` and `uv.lock` update successfully.

- [ ] **Step 2: Write failing deterministic-version and filter tests**

```python
# tests/test_preference_map.py
def test_same_inputs_produce_same_map_version(map_service, seeded_vectors):
    first = map_service.build(index_version="idx-1", candidate_watermark="w1")
    second = map_service.build(index_version="idx-1", candidate_watermark="w1")
    assert first == second


def test_map_filters_entity_type(api_client, seeded_map):
    response = api_client.get("/api/preference-map", params={"entity_type": "candidate_gif"})
    assert response.status_code == 200
    assert {point["entity_type"] for point in response.json()["points"]} == {"candidate_gif"}


def test_map_schema_is_installed(library_db):
    conn = get_connection(library_db)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"preference_map_builds", "preference_map_points"} <= names
```

- [ ] **Step 3: Run and verify failure**

Run: `uv run pytest tests/test_preference_map.py -v`

Expected: FAIL because map service/routes do not exist.

- [ ] **Step 4: Implement deterministic map build and cache**

Append this DDL to `PREFERENCE_SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS preference_map_builds (
    map_version TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    index_version TEXT NOT NULL,
    candidate_watermark TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
    progress REAL NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 1),
    error_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS preference_map_points (
    map_version TEXT NOT NULL,
    index_version TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('media','candidate_gif')),
    entity_id TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    cluster_id INTEGER,
    emotion TEXT,
    scene_type TEXT,
    rating TEXT,
    PRIMARY KEY(map_version,entity_type,entity_id),
    FOREIGN KEY(map_version) REFERENCES preference_map_builds(map_version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_preference_map_emotion
  ON preference_map_points(map_version,entity_type,emotion);
CREATE INDEX IF NOT EXISTS idx_preference_map_cluster
  ON preference_map_points(map_version,entity_type,cluster_id);
```

```python
class PreferenceMapService:
    def build(self, *, index_version: str, candidate_watermark: str) -> str:
        vectors, entities = self._load_compatible_vectors(index_version, candidate_watermark)
        config = {"n_neighbors": 15, "min_dist": 0.1, "metric": "cosine", "random_state": 42, "clusters": 20}
        if len(vectors) == 0:
            raise ValueError("no compatible vectors available for map build")
        version_input = json.dumps({
            "index": index_version,
            "candidate": candidate_watermark,
            "embedding_model": self.embedding_model,
            "embedding_dim": len(vectors[0]),
            "config": config,
        }, sort_keys=True)
        map_version = hashlib.sha256(version_input.encode()).hexdigest()[:16]
        if self._exists(map_version):
            return map_version
        if len(vectors) < 3:
            coordinates = np.array([[float(index), 0.0] for index in range(len(vectors))])
        else:
            umap_config = {key: value for key, value in config.items() if key != "clusters"}
            umap_config["n_neighbors"] = min(config["n_neighbors"], len(vectors) - 1)
            coordinates = umap.UMAP(**umap_config).fit_transform(vectors)
        cluster_count = max(1, min(config["clusters"], len(vectors)))
        clusters = KMeans(n_clusters=cluster_count, random_state=42, n_init=10).fit_predict(vectors)
        self._replace_map_version(map_version, index_version, entities, coordinates, clusters)
        return map_version
```

Do not run UMAP in the request thread. `POST /api/preference-map/rebuild` creates a CPU job; `GET /api/preference-map/jobs/{job_id}` reports progress. Limit API point payload to identifiers, coordinates, color fields, and preview URLs.

- [ ] **Step 5: Implement map and statistics routes**

Add:

```text
GET  /api/preference-map?entity_type=media|candidate_gif|all
GET  /api/preference-map/stats
GET  /api/preference-map/{entity_id}/neighbors
POST /api/preference-map/rebuild
GET  /api/preference-map/jobs/{job_id}
```

Filters: emotion, scene type, rating, source film, cluster, and free-text tag query. Statistics must apply the same filter object as points.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_preference_map.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add pyproject.toml uv.lock app/services/preference_schema.py app/services/preference_map.py app/routers/preference_map.py app/main.py tests/test_preference_map.py
git commit -m "feat: add versioned preference map API"
```

---

## Phase 5: React Workbench

### Task 20: Scaffold the React Workbench and Typed API Client

**Files:**
- Create: `web/` Vite React TypeScript application
- Create: `web/src/api/types.ts`
- Create: `web/src/api/client.ts`
- Create: `web/src/layout/WorkbenchLayout.tsx`
- Create: `web/src/app/App.tsx`
- Create: `web/src/styles.css`
- Create: `web/src/test/setup.ts`

- [ ] **Step 1: Create the Vite project and install selected dependencies**

Run:

```powershell
npm create vite@latest web -- --template react-ts
Set-Location web
npm install
npm install @tanstack/react-query react-router-dom echarts echarts-for-react lucide-react
npm install -D vitest @testing-library/react @testing-library/jest-dom @testing-library/user-event jsdom @playwright/test
Set-Location ..
```

Expected: `web/package-lock.json` exists.

- [ ] **Step 2: Write a failing shell navigation test**

```tsx
// web/src/app/App.test.tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "./App";

it("renders the operational workspaces", () => {
  render(<MemoryRouter><App /></MemoryRouter>);
  expect(screen.getByRole("link", { name: "Test Runs" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Preference Map" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Quality Status" })).toBeInTheDocument();
});
```

- [ ] **Step 3: Configure Vitest and verify failure**

Add `test` and `test:run` scripts and jsdom setup. Run: `npm --prefix web run test:run`.

Expected: FAIL because `App` does not expose the required navigation.

- [ ] **Step 4: Define shared API types from backend contracts**

```ts
// web/src/api/types.ts
export type RunStatus = "queued" | "running" | "cancel_requested" | "cancelled" | "completed" | "failed" | "interrupted";
export interface RunSummary { run_id: string; status: RunStatus; source_video_path: string; progress: number; index_version: string; preference_profile_version: string | null; }
export interface ScoreBreakdown { base_rag_similarity: number; profile_score: number | null; raw_score: number; final_score: number; penalty_multiplier: number; active_weights: Record<string, number>; inactive_reasons: Record<string, string>; matched_profiles: MatchedProfile[]; }
export interface MatchedProfile { profile_id: string; scenario_key: string; confidence: number; similarity: number; contribution: number; }
export interface MapPoint { entity_type: "media" | "candidate_gif"; entity_id: string; x: number; y: number; cluster_id: number; emotion: string | null; rating: string | null; preview_url: string; }
```

- [ ] **Step 5: Implement a fetch client and SSE subscription**

```ts
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  return response.json() as Promise<T>;
}

export function subscribeToRun(runId: string, onEvent: (event: MessageEvent) => void): () => void {
  const source = new EventSource(`/api/runs/${runId}/events`);
  const eventNames = [
    "run.started", "step.started", "step.progress", "frame.completed",
    "candidate.completed", "run.completed", "run.failed", "run.cancelled",
  ];
  eventNames.forEach((name) => source.addEventListener(name, onEvent as EventListener));
  return () => source.close();
}
```

- [ ] **Step 6: Implement the restrained operational shell**

Use a fixed top navigation, full-width work areas, 8px-or-less radii, lucide icons, and a shared right inspector region. Do not add a marketing landing page, hero, decorative gradients, or nested cards.

- [ ] **Step 7: Run tests and build**

Run:

```powershell
npm --prefix web run test:run
npm --prefix web run build
```

Expected: PASS and a production bundle under `web/dist`.

- [ ] **Step 8: Commit**

```powershell
git add web
git commit -m "feat: scaffold typed RAG workbench frontend"
```

### Task 21: Implement the Test Run Workspace and Live Progress

**Files:**
- Create: `web/src/runs/RunList.tsx`
- Create: `web/src/runs/RunForm.tsx`
- Create: `web/src/runs/RunWorkspace.tsx`
- Create: `web/src/runs/RunTimeline.tsx`
- Create: `web/src/runs/RetrievalEvidence.tsx`
- Create: `web/src/inspector/MediaInspector.tsx`
- Create tests beside each component

- [ ] **Step 1: Write failing workflow tests**

```tsx
it("creates a baseline run with current script defaults", async () => {
  render(<RunForm />);
  expect(screen.getByLabelText("Sample interval")).toHaveValue(20);
  expect(screen.getByLabelText("Preference Memory")).not.toBeChecked();
  await user.click(screen.getByRole("button", { name: "Create run" }));
  expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining("/api/runs"), expect.objectContaining({ method: "POST" }));
});

it("selecting a frame loads its retrieval evidence", async () => {
  render(<RunWorkspace runId="run-1" />);
  await user.click(await screen.findByRole("button", { name: /00:00:20/ }));
  expect(await screen.findByText("Top-K evidence")).toBeVisible();
});
```

- [ ] **Step 2: Run and verify failure**

Run: `npm --prefix web run test:run -- RunForm RunWorkspace`

Expected: FAIL because components do not exist.

- [ ] **Step 3: Implement run list and validated form**

The form exposes only server-approved parameters. When memory is enabled, query current compatible Profile and show its version read-only. Disable submission while required video path or Profile is invalid.

```tsx
const mutation = useMutation({
  mutationFn: (request: CreateRunRequest) => api<RunSummary>("/runs", { method: "POST", body: JSON.stringify(request) }),
  onSuccess: (run) => navigate(`/runs/${run.run_id}`),
});
```

- [ ] **Step 4: Implement stable three-column dimensions**

```css
.run-workspace {
  display: grid;
  grid-template-columns: minmax(180px, 240px) minmax(480px, 1fr) minmax(260px, 340px);
  min-height: calc(100vh - 52px);
}
.timeline-track { min-height: 28px; position: relative; }
.icon-button { width: 32px; height: 32px; display: inline-grid; place-items: center; }
```

At mobile widths, switch to tabs for list/content/inspector; do not overlay controls on the video.

- [ ] **Step 5: Implement SSE-driven invalidation**

Subscribe only while status is active. On `step.progress`, update the query cache; on `frame.completed`, invalidate frames; on terminal events, close SSE and invalidate the full run.

- [ ] **Step 6: Implement evidence and score inspection**

`RetrievalEvidence` shows Top-K thumbnail, media ID, similarity, emotion, film, and evidence snapshot. `MediaInspector` shows ScoreBreakdown active/inactive components, matched Profile confidence, penalty multiplier, and artifact links.

- [ ] **Step 7: Run tests and build**

Run:

```powershell
npm --prefix web run test:run
npm --prefix web run build
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add web/src/runs web/src/inspector web/src/styles.css
git commit -m "feat: add live test run workspace"
```

### Task 22: Implement Run Comparison

**Files:**
- Create: `web/src/runs/RunComparison.tsx`
- Create: `web/src/runs/RunComparison.test.tsx`
- Modify: `web/src/app/App.tsx`

- [ ] **Step 1: Write a failing rank-delta test**

```tsx
it("shows candidate rank and score deltas", async () => {
  render(<RunComparison left="baseline" right="memory" />);
  expect(await screen.findByText("#3 → #1")).toBeVisible();
  expect(screen.getByText(/profile \+0.09/)).toBeVisible();
});
```

- [ ] **Step 2: Run and verify failure**

Run: `npm --prefix web run test:run -- RunComparison`

Expected: FAIL because comparison component does not exist.

- [ ] **Step 3: Implement comparison queries and same-source guard**

```tsx
const { data } = useQuery({
  queryKey: ["run-comparison", left, right],
  queryFn: () => api<RunComparisonReport>(`/run-comparisons?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`),
});
```

For different videos, render only summary metrics. For identical source SHA256, render aligned frame markers, Top-K Jaccard, added/removed candidates, rank delta, base delta, Profile contribution, and penalty delta.

- [ ] **Step 4: Add comparison route and baseline selector**

Route: `/runs/compare?left=<id>&right=<id>`. Restrict selectable baseline to completed runs; mark differing index/Profile versions prominently rather than hiding them.

- [ ] **Step 5: Run tests and build**

Run: `npm --prefix web run test:run && npm --prefix web run build`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add web/src/runs/RunComparison.tsx web/src/runs/RunComparison.test.tsx web/src/app/App.tsx
git commit -m "feat: visualize baseline and preference run differences"
```

### Task 23: Implement Preference Map, Review Queue, and Feedback Loop

**Files:**
- Create: `web/src/map/PreferenceMap.tsx`
- Create: `web/src/map/MapStats.tsx`
- Create: `web/src/candidates/ReviewQueue.tsx`
- Create component tests
- Modify: `web/src/inspector/MediaInspector.tsx`

- [ ] **Step 1: Write failing map-filter and feedback tests**

```tsx
it("filters the map to long-term candidates", async () => {
  render(<PreferenceMap />);
  await user.click(screen.getByRole("button", { name: "Candidates" }));
  expect(mockFetch).toHaveBeenLastCalledWith(expect.stringContaining("entity_type=candidate_gif"), expect.anything());
});

it("records dislike without promoting", async () => {
  render(<ReviewQueue />);
  await user.click(await screen.findByRole("button", { name: "Dislike" }));
  expect(mockFeedback).toHaveBeenCalledWith(expect.objectContaining({ rating: "dislike" }));
  expect(mockPromote).not.toHaveBeenCalled();
});
```

- [ ] **Step 2: Run and verify failure**

Run: `npm --prefix web run test:run -- PreferenceMap ReviewQueue`

Expected: FAIL because map/review components do not exist.

- [ ] **Step 3: Implement Canvas map with bounded thumbnails**

Use ECharts scatter with `large: true` for points. Load preview images only for hovered/selected entities and at most 100 visible high-zoom entities. Keep point dimensions stable while images load.

```tsx
const option = {
  animation: false,
  xAxis: { show: false },
  yAxis: { show: false },
  series: [{ type: "scatter", large: points.length > 2000, data: points.map(p => [p.x, p.y, p.entity_id]), symbolSize: 7 }],
};
```

- [ ] **Step 4: Implement shared filters and reverse-linked statistics**

One `MapFilters` object drives both `/preference-map` and `/preference-map/stats`. Clicking a chart segment updates filters; brush selection updates a selected entity set and recomputes displayed statistics without mutating server data.

- [ ] **Step 5: Implement Review Queue modes and Profile rebuild state**

Tabs: `Map` and `Review Queue`. Queue strategies: highest score, uncertain, random. Feedback immediately advances to the next candidate and shows event ID. Profile rebuild is a separate button with build progress; feedback never silently rebuilds.

- [ ] **Step 6: Add explicit promote confirmation**

Use a modal showing quality status, duplicate check state, destination metadata, and the exact statement that promotion writes main media and a new FAISS version. The confirm button sends `{ "confirm": true }`; closing the modal sends nothing.

- [ ] **Step 7: Run tests and build**

Run:

```powershell
npm --prefix web run test:run
npm --prefix web run build
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add web/src/map web/src/candidates web/src/inspector web/src/app/App.tsx
git commit -m "feat: add preference map and candidate feedback loop"
```

---

## Phase 6: WSL2 Deployment and Operational Hardening

### Task 24: Package API, Worker, and Web with Docker Compose

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.yml`
- Create: `docker/nginx.conf`
- Create: `web/Dockerfile`
- Create: `tests/test_docker_config.py`
- Modify: `configs/models.yaml`

- [ ] **Step 1: Write a failing Compose contract test**

```python
# tests/test_docker_config.py
from pathlib import Path

import yaml


def test_compose_separates_api_worker_and_web_and_mounts_media_read_only():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"api", "worker", "web"} <= set(services)
    assert services["api"]["image"] == services["worker"]["image"]
    assert services["api"]["environment"]["GIFAGENT_LIBRARY_DB"] == "/app/data/library.db"
    assert "host.docker.internal:host-gateway" in services["api"]["extra_hosts"]
    media_mount = next(value for value in services["api"]["volumes"] if value["target"] == "/media")
    assert media_mount["read_only"] is True


def test_nginx_disables_buffering_for_sse():
    config = Path("docker/nginx.conf").read_text(encoding="utf-8")
    assert "proxy_buffering off" in config
    assert "proxy_read_timeout 1h" in config
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_docker_config.py -v`

Expected: FAIL because the deployment files do not exist.

- [ ] **Step 3: Create the backend image**

```dockerfile
# Dockerfile
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY scripts ./scripts
COPY configs ./configs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`.dockerignore` must exclude `.git`, `.venv`, `.superpowers`, `data`, `backups`, `web/node_modules`, `web/dist`, caches, logs, and local environment files. Do not bake any database, FAISS index, video, or secret into the image.

- [ ] **Step 4: Create the frontend image and SSE-safe reverse proxy**

```dockerfile
# web/Dockerfile
FROM node:22-bookworm-slim AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /web/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
```

The Web build context is the repository root, allowing the final stage to copy `docker/nginx.conf` without escaping the context.

```nginx
# docker/nginx.conf
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;

    location /api/runs/ {
        proxy_pass http://api:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
    }

    location /api/ {
        proxy_pass http://api:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location / {
        try_files $uri /index.html;
    }
}
```

- [ ] **Step 5: Define the production Compose topology**

```yaml
# docker-compose.yml
name: gifagent
services:
  api:
    image: gifagent-backend:local
    build:
      context: .
      dockerfile: Dockerfile
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    environment: &backend-environment
      GIFAGENT_LIBRARY_DB: /app/data/library.db
      GIFAGENT_RUN_DB: /app/data/runs/runs.db
      GIFAGENT_MEDIA_ROOT: /media
      GIFAGENT_FAISS_DIR: /app/data/faiss
      GIFAGENT_OLLAMA_BASE_URL: http://host.docker.internal:11434
    extra_hosts: &host-gateway
      - "host.docker.internal:host-gateway"
    volumes: &backend-volumes
      - type: bind
        source: ./data
        target: /app/data
      - type: bind
        source: ${GIFAGENT_MEDIA_ROOT:?set GIFAGENT_MEDIA_ROOT to the WSL media directory}
        target: /media
        read_only: true
      - type: bind
        source: ./configs
        target: /app/configs
        read_only: true
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/api/status"]
      interval: 10s
      timeout: 3s
      retries: 12
    restart: unless-stopped

  worker:
    image: gifagent-backend:local
    command: ["python", "-m", "scripts.run_worker"]
    environment: *backend-environment
    extra_hosts: *host-gateway
    volumes: *backend-volumes
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped

  web:
    image: gifagent-web:local
    build:
      context: .
      dockerfile: web/Dockerfile
    ports:
      - "${GIFAGENT_WEB_PORT:-63058}:80"
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped
```

Map `GIFAGENT_OLLAMA_BASE_URL` into the existing VLM, LLM, and embedding configuration resolution. Environment variables override YAML, while YAML remains the local non-container default.

- [ ] **Step 6: Verify WSL2-to-host Ollama connectivity**

Run from WSL2 before starting Compose:

```bash
curl -fsS http://localhost:11434/api/tags
export GIFAGENT_MEDIA_ROOT=/mnt/d/path/to/videos
docker compose config --quiet
docker compose build
docker compose up -d
docker compose exec api curl -fsS http://host.docker.internal:11434/api/tags
docker compose ps
```

Expected: both `/api/tags` calls return JSON; `api`, `worker`, and `web` are running; opening `http://localhost:63058` serves the workbench. If Ollama only binds loopback and the container call fails, configure Ollama on the WSL host with `OLLAMA_HOST=0.0.0.0:11434` and restrict external access with the host firewall.

- [ ] **Step 7: Run tests**

Run:

```powershell
uv run pytest tests/test_docker_config.py -v
docker compose config --quiet
```

Expected: PASS and valid normalized Compose configuration.

- [ ] **Step 8: Commit**

```powershell
git add Dockerfile .dockerignore docker-compose.yml docker/nginx.conf web/Dockerfile configs/models.yaml tests/test_docker_config.py
git commit -m "build: package workbench for WSL2 Docker Compose"
```

### Task 25: Add Deterministic Closed-Loop and Browser E2E Tests

**Files:**
- Create: `scripts/seed_e2e_workbench.py`
- Create: `tests/test_e2e_closed_loop.py`
- Create: `docker-compose.e2e.yml`
- Create: `web/playwright.config.ts`
- Create: `web/tests/workbench.spec.ts`
- Modify: `web/package.json`
- Modify: `.gitignore`

- [ ] **Step 1: Write a failing backend closed-loop test**

```python
# tests/test_e2e_closed_loop.py
def test_feedback_rebuild_new_run_preserves_old_run(closed_loop_harness):
    baseline = closed_loop_harness.run(memory=False)
    old_scores = closed_loop_harness.candidate_scores(baseline)
    target = closed_loop_harness.materialize_top_candidate(baseline)
    event_id = closed_loop_harness.feedback(target, "like")
    profile_version = closed_loop_harness.rebuild_profile()
    memory_run = closed_loop_harness.run(memory=True, profile_version=profile_version)

    assert event_id.startswith("pref_")
    assert closed_loop_harness.get_run(baseline)["preference_profile_version"] is None
    assert closed_loop_harness.candidate_scores(baseline) == old_scores
    assert closed_loop_harness.get_run(memory_run)["preference_profile_version"] == profile_version
    assert closed_loop_harness.media_count() == closed_loop_harness.initial_media_count
```

The fixture uses temporary databases, `FakeRunInference`, a deterministic fixed index, and a fake media backend. It must not require Docker, ffmpeg, Ollama, a GPU, or production data.

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_e2e_closed_loop.py -v`

Expected: FAIL until all service boundaries are wired together.

- [ ] **Step 3: Implement an idempotent E2E seed command**

`scripts/seed_e2e_workbench.py` accepts `--root`, creates a 25-second synthetic MP4 with ffmpeg, initializes temporary library/run databases, publishes a three-dimensional FAISS version, inserts enough labeled vectors for global and one scenario Profile, and prints a JSON object containing paths and versions. Re-running with the same root must produce the same logical entities and must not duplicate media, events, or Profile versions.

The script must refuse any root that resolves to the configured production `data` directory. Add `data/e2e/` to `.gitignore`.

- [ ] **Step 4: Configure Playwright against the real Web/API/worker stack**

```ts
// web/playwright.config.ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:63058",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 } } },
  ],
});
```

Add scripts `e2e` and `e2e:install` to `web/package.json`. The E2E Compose environment sets `GIFAGENT_INFERENCE_BACKEND=fake` and points all state to `data/e2e`; production Compose never sets this flag.

```yaml
# docker-compose.e2e.yml
services:
  api:
    environment:
      GIFAGENT_LIBRARY_DB: /app/data/e2e/library.db
      GIFAGENT_RUN_DB: /app/data/e2e/runs.db
      GIFAGENT_FAISS_DIR: /app/data/e2e/faiss
      GIFAGENT_MEDIA_ROOT: /media
      GIFAGENT_INFERENCE_BACKEND: fake
    volumes:
      - ./data/e2e:/app/data/e2e
      - ./data/e2e/media:/media:ro
  worker:
    environment:
      GIFAGENT_LIBRARY_DB: /app/data/e2e/library.db
      GIFAGENT_RUN_DB: /app/data/e2e/runs.db
      GIFAGENT_FAISS_DIR: /app/data/e2e/faiss
      GIFAGENT_MEDIA_ROOT: /media
      GIFAGENT_INFERENCE_BACKEND: fake
    volumes:
      - ./data/e2e:/app/data/e2e
      - ./data/e2e/media:/media:ro
  web:
    ports:
      - "63059:80"
```

- [ ] **Step 5: Implement the browser workflow**

```ts
// web/tests/workbench.spec.ts
import { expect, test } from "@playwright/test";

test("run, compare, review, rebuild, and preserve history", async ({ page }) => {
  await page.goto("/runs");
  await page.getByRole("button", { name: "New run" }).click();
  await page.getByLabel("Video path").fill("/media/e2e.mp4");
  await page.getByRole("button", { name: "Create run" }).click();
  await expect(page.getByText("Completed")).toBeVisible({ timeout: 45_000 });
  const baselineUrl = page.url();

  await page.getByRole("link", { name: "Review Queue" }).click();
  await page.getByRole("button", { name: "Like" }).click();
  await expect(page.getByText(/pref_/)).toBeVisible();
  await page.getByRole("button", { name: "Rebuild Profile" }).click();
  await expect(page.getByText(/Profile .* completed/)).toBeVisible();

  await page.getByRole("link", { name: "Test Runs" }).click();
  await page.getByRole("button", { name: "New run" }).click();
  await page.getByLabel("Video path").fill("/media/e2e.mp4");
  await page.getByLabel("Preference Memory").check();
  await page.getByRole("button", { name: "Create run" }).click();
  await expect(page.getByText("Completed")).toBeVisible({ timeout: 45_000 });
  await page.getByRole("button", { name: "Compare" }).click();
  await expect(page.getByText("Score contribution")).toBeVisible();

  await page.goto(baselineUrl);
  await expect(page.getByText("Preference Memory: Off")).toBeVisible();
});
```

Use stable accessible labels instead of CSS selectors. Add a second test that filters the Preference Map, opens the shared inspector, verifies no text overlap at both viewports, and confirms that closing the Promote dialog performs no write.

- [ ] **Step 6: Run backend and browser E2E**

Run:

```powershell
uv run pytest tests/test_e2e_closed_loop.py -v
uv run python scripts/seed_e2e_workbench.py --root data/e2e
docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --build
$env:PLAYWRIGHT_BASE_URL='http://localhost:63059'
npm --prefix web run e2e:install
npm --prefix web run e2e
Remove-Item Env:PLAYWRIGHT_BASE_URL
docker compose -f docker-compose.yml -f docker-compose.e2e.yml down
```

Expected: backend test PASS; Playwright PASS for desktop and mobile; retained artifacts exist only on failure.

- [ ] **Step 7: Commit**

```powershell
git add .gitignore docker-compose.e2e.yml scripts/seed_e2e_workbench.py tests/test_e2e_closed_loop.py web/playwright.config.ts web/tests/workbench.spec.ts web/package.json web/package-lock.json
git commit -m "test: cover the observable preference feedback loop end to end"
```

### Task 26: Import Legacy Test Results Without Re-running Models

**Files:**
- Create: `scripts/import_run_json.py`
- Create: `tests/test_import_run_json.py`
- Modify: `app/runs/repository.py`

- [ ] **Step 1: Write failing idempotency and partial-evidence tests**

```python
# tests/test_import_run_json.py
def test_import_legacy_result_is_idempotent(tmp_path, legacy_result_json):
    run_db = tmp_path / "runs.db"
    first = import_legacy_result(legacy_result_json, run_db=run_db)
    second = import_legacy_result(legacy_result_json, run_db=run_db)
    repo = RunRepository(run_db)
    assert first == second
    assert len(repo.list_runs()) == 1
    assert len(repo.list_candidates(first)) == 2


def test_import_marks_missing_retrieval_evidence_as_partial(tmp_path, legacy_result_json):
    run_id = import_legacy_result(legacy_result_json, run_db=tmp_path / "runs.db")
    run = RunRepository(tmp_path / "runs.db").get_run(run_id)
    snapshot = json.loads(run["model_snapshot_json"])
    assert snapshot["imported_legacy"] is True
    assert snapshot["evidence_completeness"] == "partial"
```

The fixture mirrors the actual `adaptive_test_result.json` shape: top-level run parameters plus `top_clips[]` containing rank, timestamp, start/end seconds, worthiness, duration, caption, emotion, notes, and reason.

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_import_run_json.py -v`

Expected: FAIL because the importer does not exist.

- [ ] **Step 3: Implement a deterministic, dry-run-first importer**

```python
class LegacyClip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = Field(ge=1)
    timestamp: float = Field(ge=0)
    start_ts: float = Field(ge=0)
    end_ts: float = Field(ge=0)
    gif_worthiness: float = Field(ge=0, le=1)
    duration: float = Field(gt=0)
    frame_count: int = Field(ge=1)
    merged: bool
    caption: str
    emotional_core: str
    aesthetic_notes: list[str]
    reason: str


class LegacyRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video: str
    sample_interval: int
    total_samples: int
    scored_kept: int
    worthiness_distribution: dict[str, int]
    synthesis: dict
    merge_gap: int
    refine_radius: int
    refine_interval: int
    output_ratio: float
    max_output: int
    embed_dedup_threshold: float
    total_clips: int
    deduped_clips: int
    clusters_after_dedup: int
    output_count: int
    multi_frame_clips: int
    top_clips: list[LegacyClip]

    def parameters(self) -> RunParameters:
        return RunParameters(
            sample_interval=self.sample_interval,
            refine_interval=self.refine_interval,
            refine_radius=self.refine_radius,
            merge_gap=self.merge_gap,
            output_ratio=self.output_ratio,
            max_output=self.max_output,
            embedding_dedup_threshold=self.embed_dedup_threshold,
        )


def legacy_run_id(source: Path, payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256((str(source.resolve()) + "\n" + canonical).encode()).hexdigest()
    return f"legacy_{digest[:16]}"


def import_legacy_result(source: Path, *, run_db: Path, apply: bool = True) -> str:
    payload = LegacyRunResult.model_validate_json(source.read_text(encoding="utf-8"))
    run_id = legacy_run_id(source, payload.model_dump())
    if not apply:
        return run_id
    repo = RunRepository(run_db)
    if repo.find_run(run_id) is not None:
        return run_id
    with repo.transaction() as tx:
        tx.insert_imported_run(
            run_id=run_id,
            source_video_path=payload.video,
            source_video_sha256="legacy-unknown",
            status="completed",
            index_version="legacy-unversioned",
            parameters=payload.parameters(),
            model_snapshot={"imported_legacy": True, "evidence_completeness": "partial"},
        )
        for clip in payload.top_clips:
            tx.insert_legacy_candidate(run_id, clip)
        tx.append_event(run_id, "run.imported", {"source": str(source), "candidate_count": len(payload.top_clips)})
    return run_id
```

Each clip gets one synthetic representative frame at `timestamp`; `final_score` and `base_rag_score` equal `gif_worthiness`; Preference fields remain absent; no retrieval hits are invented. Run `validate_frame_analysis()` and preserve detected placeholder problems in `quality_errors_json` rather than repairing or discarding historical output.

- [ ] **Step 4: Add the guarded CLI**

```text
python -m scripts.import_run_json <json-path> --run-db <path>
python -m scripts.import_run_json <json-path> --run-db <path> --apply
```

Without `--apply`, print run ID, candidate count, validation errors, and the statement `NO WRITES PERFORMED`; exit successfully. Refuse import while a row with the same deterministic run ID has conflicting source metadata.

- [ ] **Step 5: Test against a copy of the current result shape**

Run:

```powershell
uv run pytest tests/test_import_run_json.py -v
uv run python -m scripts.import_run_json data/adaptive_test_result.json --run-db data/runs/runs.db
```

Expected: tests PASS; the second command prints a dry-run report and performs no write.

- [ ] **Step 6: Commit**

```powershell
git add app/runs/repository.py scripts/import_run_json.py tests/test_import_run_json.py
git commit -m "feat: import legacy test runs as partial evidence"
```

### Task 27: Add Holdout Evaluation, Data Invariants, and Performance Gates

**Files:**
- Create: `app/services/preference_evaluation.py`
- Create: `scripts/evaluate_preference.py`
- Create: `scripts/benchmark_workbench.py`
- Create: `tests/test_preference_evaluation.py`
- Create: `tests/test_acceptance_invariants.py`
- Create: `tests/test_workbench_benchmark.py`

- [ ] **Step 1: Write failing metric and leakage tests**

```python
# tests/test_preference_evaluation.py
def test_holdout_metrics_use_only_unseen_judgments():
    judgments = [
        Judgment("a", "like"), Judgment("b", "like"),
        Judgment("c", "dislike"), Judgment("d", "neutral"),
    ]
    report = evaluate_pair(
        baseline_ranking=["c", "d", "a", "b"],
        memory_ranking=["a", "b", "d", "c"],
        judgments=judgments,
        profile_source_ids={"training-only"},
        k=2,
    )
    assert report.like_at_k_baseline == 0
    assert report.like_at_k_memory == 1
    assert report.dislike_at_k_baseline == 0.5
    assert report.dislike_at_k_memory == 0


def test_profile_source_overlap_rejects_evaluation():
    with pytest.raises(ValueError, match="holdout leakage"):
        evaluate_pair(["a"], ["a"], [Judgment("a", "like")], {"a"}, k=1)
```

Define `Like@K` as recall of all holdout likes appearing in the first K positions. Define `Dislike@K` as disliked items divided by K. Use gains `like=3`, `neutral=1`, `dislike=0` for NDCG. Report candidate coverage and do not compute a pass/fail decision when fewer than 30 holdout judgments are present.

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/test_preference_evaluation.py -v`

Expected: FAIL because evaluation functions do not exist.

- [ ] **Step 3: Implement pair validation and the default-enable gate**

The evaluator must reject pairs unless source SHA256, parameters, model snapshot, and index version match. The Profile version may differ by definition. It must verify that no holdout target ID appears in `source_event_watermark_json` or the Profile build's effective source set.

```python
@dataclass(frozen=True)
class GateDecision:
    eligible: bool
    passed: bool
    reasons: tuple[str, ...]


def decide_default_enable(reports: list[EvaluationReport]) -> GateDecision:
    holdout_count = sum(report.holdout_count for report in reports)
    video_count = len({report.source_video_sha256 for report in reports})
    like_baseline = weighted_mean(reports, "like_at_k_baseline")
    like_memory = weighted_mean(reports, "like_at_k_memory")
    dislike_baseline = weighted_mean(reports, "dislike_at_k_baseline")
    dislike_memory = weighted_mean(reports, "dislike_at_k_memory")
    reasons = []
    if video_count < 3:
        reasons.append("requires at least 3 videos")
    if holdout_count < 30:
        reasons.append("requires at least 30 holdout judgments")
    if like_baseline > 0 and like_memory < like_baseline * 1.05:
        reasons.append("Like@20 improvement is below 5 percent")
    if dislike_memory > dislike_baseline + 0.01:
        reasons.append("Dislike@20 increased by more than 1 percentage point")
    eligible = video_count >= 3 and holdout_count >= 30
    return GateDecision(eligible=eligible, passed=eligible and not reasons, reasons=tuple(reasons))
```

When baseline Like@20 is zero, require an absolute memory Like@20 increase of at least `0.05`; add that branch and a unit test. The evaluator writes JSON and Markdown reports but never edits configuration.

- [ ] **Step 4: Implement the evaluation CLI**

```text
python -m scripts.evaluate_preference \
  --baseline-run <id> --memory-run <id> \
  --judgments <holdout.jsonl> --k 20 \
  --output data/runs/evaluations/<name>.json
```

The JSONL schema is `{ "run_candidate_id": string, "rating": "like"|"neutral"|"dislike" }`. The command prints metrics, leakage validation, and gate reasons. A failed gate exits code 2; insufficient data exits code 3; both leave `preference_memory.enabled=false`.

- [ ] **Step 5: Encode database acceptance invariants**

`tests/test_acceptance_invariants.py` runs the design document's integrity SQL against seeded completed runs and checks:

```text
no retrieval hit without a frame
no completed candidate without final score or rank
one index version per run
no candidate vector without a candidate
current Profile always references a completed build
no duplicate materialized run candidate
promoted candidate always has promoted_media_id
memory-enabled run always has preference_profile_version
ordinary feedback leaves media count and current FAISS version unchanged
```

Use one parametrized test per query and assert the violating row count is zero.

- [ ] **Step 6: Add local performance probes**

`scripts/benchmark_workbench.py` seeds 8,000 map points and reports p50/p95 for map API serialization, selected-frame Top-K retrieval, and event append-to-read latency. `tests/test_workbench_benchmark.py` marks these tests `performance` and skips unless `GIFAGENT_RUN_PERFORMANCE=1`.

Acceptance on the reference WSL2 machine:

```text
8,000-point map first interactive < 2 seconds (Playwright performance entry)
SSE commit-to-visible p95 < 1 second
cached selected-frame Top-K visible p95 < 300 ms
DOM does not contain more than 100 GIF/img previews in long lists or map mode
```

- [ ] **Step 7: Run evaluation and invariant tests**

Run:

```powershell
uv run pytest tests/test_preference_evaluation.py tests/test_acceptance_invariants.py -v
$env:GIFAGENT_RUN_PERFORMANCE='1'
uv run pytest tests/test_workbench_benchmark.py -v
Remove-Item Env:GIFAGENT_RUN_PERFORMANCE
```

Expected: metric and invariant tests PASS; performance measurements meet thresholds on the designated reference machine. Do not change the global default when the gate is ineligible or failed.

- [ ] **Step 8: Commit**

```powershell
git add app/services/preference_evaluation.py scripts/evaluate_preference.py scripts/benchmark_workbench.py tests/test_preference_evaluation.py tests/test_acceptance_invariants.py tests/test_workbench_benchmark.py
git commit -m "test: enforce preference holdout and workbench acceptance gates"
```

### Task 28: Publish the Operator Runbook and Complete Release Verification

**Files:**
- Create: `docs/runbook-rag-workbench.md`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-06-18-rag-observability-workbench-design.md` only if implementation decisions changed

- [ ] **Step 1: Write the runbook**

Document these exact operating procedures:

```text
preflight, writer shutdown, SQLite backup, and restore
WSL2 environment variables and Docker Compose startup/shutdown
Ollama host reachability and model verification
baseline run creation and replay
manual per-run Preference Memory enablement
feedback, Profile rebuild, holdout evaluation, and rollback to prior current Profile
candidate rerank and explicit promotion
legacy JSON dry-run/import
stale worker recovery and interrupted run retry
database integrity queries and immutable index verification
log/artifact locations and safe cleanup boundaries
```

State prominently that like/dislike does not write `media` or publish a FAISS version, while Promote does both after confirmation.

- [ ] **Step 2: Update the README entry points**

Add concise links to the design, this implementation plan, the operator runbook, local API command, worker command, Web development command, and WSL2 Compose command. Keep model installation and media path prerequisites explicit.

- [ ] **Step 3: Run the complete static and automated verification**

Run in separate commands so a failure is not hidden:

```powershell
uv run pytest -q
uv run python -m compileall app scripts
npm --prefix web run test:run
npm --prefix web run build
docker compose config --quiet
git diff --check
git status --short
```

Expected: all tests and builds PASS; `git diff --check` emits no output. Review `git status --short` and stage only files from this implementation; never stage live databases, WAL/SHM files, logs, generated artifacts, `.superpowers/`, or unrelated user files.

- [ ] **Step 4: Run the deployment smoke test in WSL2**

```bash
export GIFAGENT_MEDIA_ROOT=/mnt/d/path/to/videos
docker compose up -d --build
curl -fsS http://localhost:63058/api/status
docker compose exec api curl -fsS http://host.docker.internal:11434/api/tags
docker compose logs --tail=100 api worker web
```

Expected: status and Ollama calls succeed; logs contain no migration, SQLite lock, missing index version, or SSE proxy errors.

- [ ] **Step 5: Execute the manual acceptance matrix**

1. Create a baseline run and record its run ID, parameters, model snapshot, and index version.
2. Replay a completed frame and confirm Top-K evidence remains available after editing current media metadata.
3. Create a memory run with a fixed Profile and compare score components to baseline.
4. Like, neutralize, and dislike the same candidate; confirm three events exist and only the latest is effective.
5. Rebuild a Profile; confirm old runs retain the old version and scores.
6. Confirm ordinary feedback leaves `media` row count and FAISS `current.json` unchanged.
7. Cancel an active run; confirm it reaches `cancelled` between items and preserves completed evidence.
8. Restart the worker during a run; confirm stale work becomes `interrupted` and retry creates a child run.
9. Promote a quality-valid, non-duplicate candidate with confirmation; confirm one media row and one immutable index version are added.
10. Exercise desktop and mobile layouts; confirm controls, text, video, timeline, and inspector do not overlap.

- [ ] **Step 6: Keep the default disabled unless the holdout gate passes**

Do not bundle a default-on configuration change into the feature commits. If and only if Task 27 produces an eligible passing report and the operator approves, make a separate reviewed commit changing `preference_memory.enabled` to `true`; retain `auto_rebuild_enabled=false` for the first release.

- [ ] **Step 7: Commit documentation**

```powershell
git add README.md docs/runbook-rag-workbench.md
git commit -m "docs: add RAG workbench operator runbook"
```

---

## Final Definition of Done

- [ ] G0-G6 phase gates have recorded evidence.
- [ ] Existing RAG behavior remains identical when Preference Memory is disabled.
- [ ] Every run fixes its model, index, parameters, and optional Profile versions at creation.
- [ ] Historical frames, retrieval evidence, candidates, and score explanations are replayable and immutable.
- [ ] Feedback is append-only, latest-effective, and does not automatically pollute `media` or FAISS.
- [ ] Profile rebuilds and map builds are immutable, deterministic, and atomically published.
- [ ] Candidate promotion is explicit, quality checked, duplicate checked, retryable, and versioned.
- [ ] Web run, compare, map, queue, inspector, and responsive workflows pass automated tests.
- [ ] API, worker, and Web run under Docker Compose on WSL2 and reach host Ollama.
- [ ] Backend, frontend, E2E, data invariant, and deployment verification pass.
- [ ] `preference_memory.enabled` remains false unless the independent holdout gate is eligible, passing, and explicitly approved.
