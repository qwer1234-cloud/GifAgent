# Stage Split Plan Implementation Report (2026-07-18)

## 1. Summary

Implemented all phases A-H of `STAGE_SPLIT_RECHECK_IMPLEMENTATION_PLAN_2026-07-18.md`.
- **Before**: 808 passed, 2 skipped
- **After**: 820 passed (+12 new e2e tests), 2 skipped
- **Targeted tests** (task_engine + quality_lab): 329 passed
- **All tests compile and pass**; `git diff --check` reports only pre-existing CRLF warnings

## 2. Phase A: Artifact data protocol

### Modified files
- `app/task_engine/schema.py` — SCHEMA_VERSION 2→3; added `_migrate_v3_artifact_identity()` to add `stage_id TEXT` and `artifact_kind TEXT NOT NULL DEFAULT 'generic'` columns, plus unique index `uq_artifact_stage_kind_clip` and lookup index `idx_task_artifacts_lookup`
- `app/task_engine/models.py` — `ArtifactRef` gained optional `stage_id: str = ""` and `artifact_kind: str = "generic"` fields
- `app/task_engine/artifacts.py` — complete rewrite: added `make_artifact_id()`, `insert_artifact_dedup()`, `insert_artifacts_batch()`, `ArtifactCollisionError`, `resolve_stage_inputs()`, `resolve_all_gif_clip_artifacts()`, `validate_manifest_json()`, `validate_artifact_strict()`, `STAGE_INPUT_KINDS` dependency table

### Key design decisions
- Artifact identity uses `canonical_hash({stage_id, artifact_kind, clip_id, normalized_path})`
- Dedup insertion validates all fields match; raises `ArtifactCollisionError` on mismatch
- Resolver returns upstream artifacts from `task_artifacts` table (database as single source of truth)
- Non-clip-specific artifacts (e.g. `rank_dedup_manifest`) are NOT filtered by clip_id during resolution
- No FOREIGN KEY constraint via ALTER TABLE (SQLite limitation); application-level validation instead

## 3. Phase B: Atomic stage completion

### Modified files
- `app/task_engine/repository.py` — added `complete_stage_with_artifacts(stage_id, worker_id, output_key, artifacts)` method with single `BEGIN IMMEDIATE` transaction covering: lease check, artifact ownership validation, upsert all artifacts, stage update, event write
- `app/task_engine/worker.py` — removed per-artifact `conn.commit()` from `_insert_artifacts()`; it now only validates files (no DB writes); `_run_stage()` calls `complete_stage_with_artifacts()` for atomic persistence; `_try_recover()` uses the same method; `_save_result()` accepts `stage` parameter for crash recovery metadata

### Key design decisions
- SHA-256/size computed outside the transaction (by `_insert_artifacts` as validation-only)
- All artifact inserts + stage update happen in one `BEGIN IMMEDIATE` → single commit
- Recovery path uses same `complete_stage_with_artifacts()` — no second insert path

## 4. Phase C: Rewire real stage input chain

### Modified files
- `app/task_engine/stages.py` — `StageContext` gained `stage_id: str = ""` and `inputs: dict[str, tuple[ArtifactRef, ...]] | None = None` fields
- `app/task_engine/worker.py` — `_build_context()` calls `resolve_stage_inputs()` from artifacts module; removed `prev_stage_work_dir` injection entirely; config dict remains immutable

### Key design decisions
- Downstream stages read from `StageContext.inputs`, not from work_dir guessing
- Input resolution failure (missing upstream artifacts) sets `inputs=None`; adapter decides how to handle it
- Database is the only source of truth for dependency relationships

## 5. Phase D: Fix fan-out, zero-clip, and materialize

### Modified files
- `app/task_engine/orchestrator.py` — `_ensure_gif_clip_stages()` reads `rank_dedup_manifest` from `task_artifacts` table (not from filesystem); validates manifest via `validate_manifest_json()`; zero-clip creates materialize directly with proper `input_key`; N clips create N gif_clip stages each with unique clip_id

## 6. Phase E: Fix lease and heartbeat

### Modified files
- `app/task_engine/worker.py` — `TaskWorker.__init__()` accepts `lease_seconds: int = 90`, `heartbeat_seconds: int | None = None`, `db_path: str | None = None`; heartbeat thread uses `self._lease_seconds` and `self._heartbeat_seconds` (defaults to `max(1, lease_seconds // 3)`); db_path resolved from constructor or `PRAGMA database_list`; `claim_stage()` called with `lease_seconds=self._lease_seconds`

### Key design decisions
- Independent `lease_seconds` and `heartbeat_seconds` — not tied to `RetryPolicy.max_delay_seconds`
- Heartbeat creates own SQLite connection inside the thread
- Heartbeat checks `lease_owner`/`status` match; stops if ownership lost

## 7. Phase F: Fix Quality Lab config

### Modified files
- `app/quality_lab/config_builder.py` — new file with `build_task_config()` and `deep_merge()` helper
- `app/quality_lab/runner.py` — uses `build_task_config()` for per-item config construction; merges all experiment config fields (not just `adaptive`)
- `tests/quality_lab/test_runner.py` — `FakeTaskClient` uses scope keys (directory + sorted video_paths hash) instead of directory-only dedup; test `test_submit_reuses_job_id_for_same_scope` updated for correct behavior

### Key design decisions
- Deep merge: nested dicts merged recursively; lists/scalars replaced entirely
- Config hash computed from business config only (excludes `_experiment` metadata)
- FakeTaskClient differentiates by video scope, not just directory

## 8. Phase G: Fix status aggregation and scoped job dedup

### Modified files
- `app/task_engine/orchestrator.py` — `_aggregate_video_status()` enforces explicit priority order: needs_attention/failed > cancelled > running/leased/retry_wait/pending > succeeded; partial GIF success (some succeeded, some cancelled) = needs_attention; materialize succeeded + gif_clip cancelled = needs_attention; zero-clip → succeeded
- `app/task_engine/orchestrator.py` — `advance_job()` processes cancel/retry commands BEFORE checking terminal state (fixes retry on needs_attention jobs); `_retry_job` and `_cancel_job` call `_ensure_no_open_txn` before `BEGIN IMMEDIATE`

### Key design decisions
- Cancelled gif_clip stages prevent video from reaching "succeeded"
- Only all-required-stages-succeeded or zero-clip results in "succeeded"
- Retry commands override terminal job state

## 9. Phase H: Real end-to-end tests

### New file
- `tests/task_engine/test_e2e.py` — 12 end-to-end tests using real TaskWorker with controllable fake adapters

### Test list
| Test | Verifies |
|---|---|
| `TestFullChainE2E::test_stages_created_in_order` | All 8 stage names appear in correct order |
| `TestFullChainE2E::test_artifact_counts` | Artifact counts and kinds per stage match expected values |
| `TestFullChainE2E::test_artifacts_sha256_verifiable` | Every artifact SHA-256 matches actual file content |
| `TestFullChainE2E::test_gif_clip_fan_out_count` | 3 clips → exactly 3 gif_clip stages with correct clip_ids |
| `TestRetryPreservesClips::test_failed_clip_retry_leaves_successful_unchanged` | Failed clip retried; successful clip SHA/mtime unchanged |
| `TestZeroClip::test_zero_clip_no_gif_clip_stages` | 0 clips → no gif_clip stages, materialize exists and succeeds |
| `TestConcurrentDedup::test_two_workers_no_duplicate_stages` | Two workers with separate connections don't duplicate stages |
| `TestArtifactIdentityDedup::test_idempotent_insert` | Same artifact idempotent insert |
| `TestArtifactIdentityDedup::test_collision_different_sha` | Same ID, wrong SHA → ArtifactCollisionError |
| `TestArtifactIdentityDedup::test_resolver_finds_upstream_artifacts` | Resolver finds discover_manifest from sample stage |
| `TestArtifactIdentityDedup::test_video_status_aggregation` | After full chain, video and job status = succeeded |
| `TestPartialFailureStatus::test_one_clip_failed_video_needs_attention` | One gif_clip failure → video needs_attention |

## 10. Final verification output

```
compileall -q app scripts tests: COMPILE OK (no errors)
pytest tests/task_engine tests/quality_lab: 329 passed
pytest tests -q: 820 passed, 2 skipped, 4 warnings
git diff --check: OK (3 pre-existing CRLF warnings only)
```

## 11. Schema migration version

- **New version**: `SCHEMA_VERSION = 3`
- **Migration**: Incremental (`ALTER TABLE ADD COLUMN`) — no data loss, no table drop/recreate
- **Compatibility**: Existing data preserved with `stage_id=NULL`, `artifact_kind='generic'` defaults
- **New indexes**: `uq_artifact_stage_kind_clip` (partial, WHERE stage_id IS NOT NULL) and `idx_task_artifacts_lookup`

## 12. Remaining issues

None. All planned changes are implemented and verified. No data/ files or dist/ touched. No commits made.
