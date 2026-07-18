# Stage Split Production Path Second Fix Report (2026-07-18)

## Test Results Summary

```
compileall: clean (all modified files)
tests/task_engine + tests/quality_lab: 357 passed, 0 failed (including all Phase 0, 4, 6 E2E tests)
full test suite (excluding pre-existing gradio/|None| errors): 847 passed, 2 skipped, 0 failed
```

Baseline was 820 passed, 2 skipped. After Phase 0-3: 845 passed, 2 skipped, 2 E2E failures. After Phase 4-6: all 3 E2E tests pass (+3 new slow tests), using real ffprobe/ffmpeg with valid minimal test videos.

---

## Phase 0: RED Tests (all GREEN after fixes)

### New test files created:

1. **`tests/task_engine/test_schema_v4_reopen.py`** (4 tests)
   - `test_v4_db_survives_reopen_with_multi_frame_artifacts` — creates v4 DB with 2 sample_frames, closes, reopens, verifies both survive
   - `test_v4_db_old_index_dropped_new_index_exists` — verifies uq_artifact_stage_kind_clip is gone, uq_artifact_stage_identity exists
   - `test_v4_db_integrity_passes` — PRAGMA integrity_check returns ok
   - `test_v4_db_migrations_table_has_max_one_per_version` — no duplicate migration entries

2. **`tests/task_engine/test_production_artifact_contract.py`** (7 tests)
   - `test_discover_only_registers_manifest` — only discover_manifest is registered, not config/input
   - `test_sample_only_registers_manifest_and_frames` — only sample_manifest + sample_frames
   - `test_gif_clip_only_registers_gif_and_manifest` — exactly 1 gif_file + 1 gif_clip_manifest
   - `test_materialize_only_registers_result_and_manifest` — result + materialize_manifest + optional pbf_file
   - `test_missing_artifact_kind_is_rejected` — ValueError when no artifact_kind
   - `test_unknown_artifact_kind_is_rejected` — ValueError for unknown kind
   - `test_wrong_stage_artifact_kind_is_rejected` — ValueError when kind belongs to wrong stage

3. **`tests/task_engine/test_control_config_snapshot.py`** (6 tests)
   - Config builder produces top-level `adaptive`, `preference_memory`, `vlm`, `models`
   - No `config_snapshot` nesting in new format
   - Deep merge preserves nested fields, None deletes keys
   - Historical config_snapshot wrapper parseable

4. **`tests/task_engine/test_materialize_production.py`** (5 tests)
   - 2 successful clips → resolve_all_gif_clip_artifacts returns both
   - All succeed → video succeeds
   - Partial success → video needs_attention
   - All fail → video needs_attention
   - Zero-clip → materialize created directly, succeeds

5. **`tests/task_engine/test_lease_isolation.py`** (3 tests)
   - Per-stage lease Event does not leak between calls
   - Threading.Lock exists for thread safety
   - heartbeat_seconds < lease_seconds validation

6. **`tests/task_engine/test_production_e2e.py`** (2 tests, marked @pytest.mark.slow)
   - E2E discover via real AdaptivePipelineAdapter with mocked VLM/LLM
   - Worker initializes job and runs discover to completion
   - **Currently failing** — requires Phase 6 fake ffprobe/ffmpeg PATH infrastructure

---

## Phase 1: Schema Migration Version-Awareness

**File:** `app/task_engine/schema.py`

**Root Cause:** `_migrate_task_schema()` ran v3 and v4 migrations unconditionally on every `connect_task_db()` call. On reopen of a v4 database (which already has multi-frame artifacts), v3 tried to recreate `uq_artifact_stage_kind_clip` which violates the UNIQUE constraint because multiple sample_frames exist with same (stage_id, kind, NULL clip).

**Fix:**
1. Read applied versions from `task_migrations` table before running any migration
2. Each migration (v3, v4) runs only if its version is not already in the applied set
3. Migration version is recorded alongside the DDL in the same flow
4. Compatible with: fresh DB, old DB without migrations table, v3 DB, v4 DB with multi-frame artifacts

**Key code change:** `_migrate_task_schema()` now:
```python
applied = {r["version"] for r in conn.execute("SELECT version FROM task_migrations").fetchall()}
if 3 not in applied:
    _migrate_v3_artifact_identity(conn)
    conn.execute("INSERT OR IGNORE INTO task_migrations ...", (3, ...))
if 4 not in applied:
    _migrate_v4_artifact_multi_frame(conn)
    conn.execute("INSERT OR IGNORE INTO task_migrations ...", (4, ...))
```

---

## Phase 2: Explicit Artifact Output Protocol

**Files modified:**
- `app/task_engine/adaptive_adapter.py` — removed extension-based kind guessing, enforces explicit artifact_kind
- `scripts/test_video_adaptive.py` — removed directory scanning, each stage handler returns explicit `_artifacts`
- `app/task_engine/artifacts.py` — added `pbf_file` to `STAGE_ARTIFACT_KINDS`
- `app/task_engine/repository.py` — added `ref.stage_id == stage_id` validation

**Changes:**
1. `run_adaptive_stage()` now:
   - Requires `artifact_kind` explicitly from script output (rejects missing/generic)
   - Validates `artifact_kind` is in `STAGE_ARTIFACT_KINDS` for the current stage
   - Rejects control files (config_snapshot.json, input_manifest.json, stage.log, result_*.json)
2. `run_stage_mode()` directory scanning removed — reads from `output["_artifacts"]`
3. Each stage handler in `test_video_adaptive.py` returns explicit `_artifacts` list
4. `complete_stage_with_artifacts()` validates `ref.stage_id == stage_id`
5. `pbf_file` added to materialize whitelist

**Adapter validation contract:**
- Missing artifact_kind → ValueError("no explicit artifact_kind")
- Unknown artifact_kind → ValueError("cannot produce artifact_kind='...'")
- Wrong-stage artifact_kind → ValueError("cannot produce artifact_kind='...'")

---

## Phase 3: Unified Task Config Snapshot

**Files modified:**
- `app/quality_lab/config_builder.py` — added `normalize_task_config()` function
- `app/routers/tasks.py` — API uses normalize_task_config for new tasks
- `app/task_engine/worker.py` — `_build_context()` uses normalize_task_config
- `scripts/test_video_adaptive.py` — stage mode uses normalize_task_config

**New task format (top-level):**
```json
{
  "adaptive": {}, "preference_memory": {}, "vlm": {}, "models": {},
  "video_paths": [], "_task": {"limit": 0, "extensions": ""}
}
```

**`normalize_task_config()` logic:**
1. If `config_snapshot` exists (historical format), use it as base
2. Deep-merge top-level business keys over the snapshot
3. Extract `_task` metadata
4. Preserve `task_work_dir` and other non-metadata keys
5. Returns unified top-level business config

**Historical compatibility:** Tasks created with old `config_snapshot` wrapper still parse correctly — `normalize_task_config` extracts the snapshot as base and merges any top-level overrides.

---

## Phase 5: Lease Isolation (Per-Stage Events)

**File modified:** `app/task_engine/worker.py`

**Change:** The deprecated `self._lease_lost` instance attribute is replaced by per-stage `threading.Event` objects created inside each `_run_stage()` call. The heartbeat thread and main thread share this local Event, not a persisted attribute.

- Heartbeat thread sets `lease_lost.set()` instead of `self._lease_lost = True`
- Main thread checks `lease_lost.is_set()` instead of `self._lease_lost`
- Each `_run_stage()` invocation creates a fresh `lease_lost` Event — no cross-stage leakage

---

## Remaining Not Yet Fixed

### Manifest Validator Integration
The `validate_manifest_json()` function exists in `artifacts.py` but is not yet called from `_read_upstream_manifest()` in `test_video_adaptive.py` in all cases. Currently only called in `_ensure_gif_clip_stages()`.

---

## Phase 4: Complete Materialize Publishing (COMPLETED)

**Files modified:**
- `scripts/test_video_adaptive.py` — `_stage_materialize()` rewritten
- `app/task_engine/artifacts.py` — added `get_gif_clip_terminal_statuses()`
- `app/task_engine/worker.py` — inject terminal statuses into materialize config

**Changes:**

1. **Materialize copies GIFs to formal export directory** — reads `export_base_dir` from config (or falls back to `data/exports/adaptive_test`), creates `<export_base>/<video_name>/`, and copies each verified GIF there.

2. **Atomic copy with SHA-256 verification** — `shutil.copy2` to `<name>.tmp`, verify SHA-256 of copy, `os.replace` to final name. Failed copies are reported in the result.

3. **Result JSON structure** — includes `succeeded` (with formal_path, sha256, start_ts, end_ts, gif_name), `failed` (verification failures + publish failures), `cancelled` (from terminal statuses), and `gif_clip_terminal_statuses` (all terminal gif_clip stage statuses).

4. **PBF references formal-export GIFs** — PBF file is written to the formal export directory with bookmarks referencing formal GIF names.

5. **Status rules** — already handled by `_aggregate_video_status` in the orchestrator: all-success -> succeeded, partial -> needs_attention, all-fail -> needs_attention, zero-clip -> succeeded.

6. **Materialize resolver returns terminal statuses** — `get_gif_clip_terminal_statuses()` queries all terminal gif_clip stages (succeeded/failed/cancelled/needs_attention) and returns their clip_id and status. The worker injects this into the config as `_gif_clip_terminal_statuses` when building context for materialize.

7. **export_base_dir from config** — `_stage_materialize` reads `config_data["export_base_dir"]` (not from `cfg` since `extract_config` only pulls `adaptive`/`preference_memory`).

---

## Phase 6: Production Path E2E (COMPLETED)

**File:** `tests/task_engine/test_production_e2e.py`

**Changes:**

1. **Uses real ffprobe/ffmpeg** — instead of fake scripts (which can't override `.exe` on Windows due to `CreateProcess` search order), the tests create minimal valid MP4 files with the real ffmpeg and probe them with the real ffprobe. This is a better E2E test.

2. **3 E2E tests now passing**:
   - `test_e2e_discover_with_real_adapter` — creates a 2-second MP4 via ffmpeg, runs the discover stage via `run_adaptive_stage`, verifies the discover manifest has correct duration.
   - `test_worker_discovers_and_computes` — creates a video, initializes a job, runs the worker (`run_once` claims and executes discover), verifies stage status is `succeeded`.
   - `test_gif_clip_materialize_chain` — creates a fake GIF artifact, sets up materialize inputs, runs materialize via `run_adaptive_stage`, verifies formal export directory has GIF, PBF, and result JSON with proper structure.

3. **Test isolation** — all tests use `tmp_path` for files, DB, export dir. `GIFAGENT_CONFIG` env var points to a temp YAML to isolate the DB used by the subprocess.

4. **No control files in artifacts** — verified that materialize artifacts contain no `config_snapshot.json`, `input_manifest.json`, or `stage.log`.

5. **Pre-existing Python 3.9 compat fixes** — added `from __future__ import annotations` to `scripts/test_video_adaptive.py`, `app/services/json_guard.py`, `app/services/quality.py`, and changed `str | None` to `Optional[str]` in `app/services/schemas.py` to resolve runtime `TypeError` on Python 3.9.

---

## Files Changed

| File | Phase | Change Summary |
|------|-------|----------------|
| `app/task_engine/schema.py` | P1 | Version-aware migration, check `task_migrations` before running DDL |
| `app/task_engine/adaptive_adapter.py` | P2 | Require explicit artifact_kind, validate against stage whitelist, reject control files |
| `scripts/test_video_adaptive.py` | P2, P3 | Remove directory scanning, explicit `_artifacts` in each handler, `normalize_task_config` in stage mode |
| `app/task_engine/artifacts.py` | P2 | Add `pbf_file` to `STAGE_ARTIFACT_KINDS` |
| `app/task_engine/repository.py` | P2 | Add `ref.stage_id == stage_id` check in `complete_stage_with_artifacts` |
| `app/quality_lab/config_builder.py` | P3 | Add `normalize_task_config()` function |
| `app/routers/tasks.py` | P3 | Use `normalize_task_config` for new job config |
| `app/task_engine/worker.py` | P3, P5, P4 | Use `normalize_task_config` in `_build_context`, per-stage lease Events, inject terminal statuses for materialize |
| `tests/task_engine/test_stage_adapter.py` | P2 | Add `artifact_kind` to test fixture |
| `scripts/test_video_adaptive.py` | P2, P3, P4 | Remove directory scanning, explicit `_artifacts`, `normalize_task_config`, atomic copy + formal export in `_stage_materialize` |
| `app/task_engine/artifacts.py` | P2, P4 | Add `pbf_file` to `STAGE_ARTIFACT_KINDS`, add `get_gif_clip_terminal_statuses()` |
| `app/services/json_guard.py` | P6 | Add `from __future__ import annotations` (Python 3.9 compat) |
| `app/services/schemas.py` | P6 | Change `str \| None` -> `Optional[str]` (Python 3.9 + Pydantic compat) |
| `app/services/quality.py` | P6 | Add `from __future__ import annotations` (Python 3.9 compat) |
| `tests/task_engine/test_production_e2e.py` | P6 | Rewrite E2E tests to use real ffmpeg/ffprobe with valid test videos |

## New Test Files

| File | Tests | Status |
|------|-------|--------|
| `tests/task_engine/test_schema_v4_reopen.py` | 4 | PASS |
| `tests/task_engine/test_production_artifact_contract.py` | 7 | PASS |
| `tests/task_engine/test_control_config_snapshot.py` | 6 | PASS |
| `tests/task_engine/test_materialize_production.py` | 5 | PASS |
| `tests/task_engine/test_lease_isolation.py` | 3 | PASS |
| `tests/task_engine/test_production_e2e.py` | 3 | PASS (Phase 6 completed) |
