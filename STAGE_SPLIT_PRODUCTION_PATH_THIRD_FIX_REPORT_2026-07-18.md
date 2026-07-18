# Stage Split Production Path Third Fix Report (2026-07-18)

## Summary

All 7 issues (3 P0 + 4 P1) from `STAGE_SPLIT_PRODUCTION_PATH_THIRD_REVIEW_IMPLEMENTATION_2026-07-18.md` have been addressed.

**Test results**: 870 passed, 2 skipped (baseline: 848 passed, 2 skipped). Net +22 new tests.
**Pre-existing failure**: `test_stages_created_in_order` in `test_e2e.py` was already failing before these changes (only 3 of 8 stages created, VLM stage blocks further advancement). Not caused by this fix.

---

## P0-1: Legacy v4 DB (migration table only has version 4) fails to reopen

### Root cause
`_migrate_task_schema()` checked only `task_migrations` records. When a v4 DB only had migration 4 recorded (no 3), re-running applied v3 DDL which created the old `uq_artifact_stage_kind_clip` UNIQUE index, causing "UNIQUE constraint failed" on multi-frame artifacts.

### Files changed
- `app/task_engine/schema.py:110-226`: Replaced `_migrate_task_schema()` with dual-evidence logic
  - `_detect_schema_state(conn)` (line 110): Uses `PRAGMA index_list` and `PRAGMA table_info` to detect actual schema on disk
  - Migration v3 block (line 181): Records v3 as applied when v4 schema detected; applies v3 DDL only for fresh DBs
  - Migration v4 block (line 226): Re-detects schema state after v3; applies v4 when v3 columns exist but v4 index missing
  - Each migration in explicit `BEGIN IMMEDIATE` transaction with rollback on error

### New/modified tests (`tests/task_engine/test_schema_v4_reopen.py`)
- `test_v4_db_survives_reopen_with_multi_frame_artifacts` (existing, now passes with new logic)
- `test_v4_db_old_index_dropped_new_index_exists` (existing)
- `test_v4_db_integrity_passes` (existing)
- `test_v4_db_migrations_table_has_max_one_per_version` (existing)
- **NEW** `test_v3_db_migration_3_only_applies_v4_on_reopen`: v3-only DB upgrades to v4 on reopen
- **NEW** `test_no_migrations_but_v3_columns_exist_upgrades_safely`: empty migration table + v3 columns safe upgrade
- **NEW** `test_triple_reopen_idempotent`: three consecutive opens produce no errors, one record per version
- **NEW** `test_foreign_key_check_passes_after_migration`: PRAGMA foreign_key_check returns no violations

---

## P0-2: Real Worker materialize input incomplete

### Root cause
`STAGE_INPUT_KINDS["materialize"]` only had `gif_file`, not `gif_clip_manifest`. Worker used generic resolver instead of video-scoped aggregator. `_gif_clip_terminal_statuses` was passed via config hack instead of the input envelope.

### Files changed
- `app/task_engine/artifacts.py`:
  - `STAGE_INPUT_KINDS["materialize"]` (line 200): Changed to `("gif_file", "gif_clip_manifest")`
  - `_INPUT_PRODUCER` (line 203): Added `"gif_clip_manifest": "gif_clip"`
  - **NEW** `resolve_materialize_inputs(conn, video_id)` (line 385): Dedicated resolver aggregating ALL succeeded gif_clip stages
    - Returns `gif_file` and `gif_clip_manifest` tuples
    - Validates each clip: both artifact kinds exist, stage_id/clip_id match, SHA-256 consistent
    - Duplicate detection for both artifact kinds
    - Manifest-GIF SHA cross-validation
  - **NEW** `build_materialize_input_envelope()` (line 471): Builds versioned input envelope with `schema_version`, `stage`, `artifacts`, and `stage_statuses`
- `app/task_engine/worker.py`:
  - `_build_context()` (line 290): Materialize stage uses `resolve_materialize_inputs()` + `build_materialize_input_envelope()` instead of generic resolver
  - Removed `_gif_clip_terminal_statuses` config injection hack
- `app/task_engine/adaptive_adapter.py`:
  - `run()` (line 191): For materialize stage, writes the versioned envelope to `input_manifest.json`; for other stages, uses flat kind->artifacts mapping
  - Injects `_stage_id` and `_clip_id` into config for P1-3 artifact_id computation
- `scripts/test_video_adaptive.py`:
  - `_stage_materialize()` (line 2073): Reads from both flat format and versioned envelope (`inputs["artifacts"]`); falls back to legacy `_gif_clip_terminal_statuses`

### New/modified tests (`tests/task_engine/test_stage_inputs.py`, `tests/task_engine/test_production_e2e.py`)
- **NEW** `test_vlm_stage_input_kinds_includes_sample_frames`: STAGE_INPUT_KINDS includes both kinds
- **NEW** `test_resolve_vlm_inputs_returns_both_kinds`: Resolver returns sample_manifest + sample_frames
- **NEW** `test_resolve_materialize_inputs_returns_both_kinds`: Materialize resolver returns gif_file + gif_clip_manifest
- **NEW** `test_build_materialize_input_envelope`: Envelope has correct structure with stage_statuses
- **NEW** `test_worker_driven_materialize_no_manual_input`: Worker uses `_build_context()` to resolve inputs, no manual input_manifest.json

---

## P0-3: Formal output overwrites historical files

### Root cause
`os.replace()` in `_stage_materialize()` directly replaced destination without checking existing file SHA-256.

### Files changed
- `scripts/test_video_adaptive.py` `_stage_materialize()` (line 2073):
  - Before publishing, checks if destination exists:
    - Not exists: normal publish
    - Exists + same SHA-256: idempotent reuse (skip write, print message)
    - Exists + different SHA-256: DON'T overwrite; adds failure entry with `overwrite_prevented` reason and suggested conflict name
  - Temp files: `.filename.stage_id_hash.uuid.tmp` format (unique per stage)
  - Temp files on same volume as formal export for atomic rename
  - Cleanup: temp files removed on failure; historical files never deleted
  - result JSON, PBF, materialize manifest generated AFTER all GIFs published

### New/modified tests (`tests/task_engine/test_production_e2e.py`)
- **NEW** `TestProductionP03OverwriteProtection.test_same_sha_idempotent_reuse`: Same name + same SHA = no write
- **NEW** `TestProductionP03OverwriteProtection.test_different_sha_no_overwrite`: Same name + different SHA = historical file unchanged

---

## P1-1: Deep merge config before normalization

### Root cause
Router used `{**full_config, **body.config_json}` shallow merge, replacing entire `adaptive` dict instead of merging keys. `normalize_task_config()` excluded `_experiment`, `config_hash`, `task_work_dir`, `export_base_dir`.

### Files changed
- `app/routers/tasks.py` (line 158): Uses `deep_merge(full_config, body.config_json or {})` before adding `video_paths` and `_task` metadata
  - Added `from app.quality_lab.config_builder import deep_merge`
- `app/quality_lab/config_builder.py` `normalize_task_config()` (line 74):
  - Added explicit preservation of `_experiment`, `config_hash`, `task_work_dir`, `export_base_dir` keys
  - These keys now survive the normalization cycle regardless of whether they come from `config_snapshot` or top-level

### New/modified tests (`tests/task_engine/test_control_config_snapshot.py`)
- **NEW** `test_partial_adaptive_override_preserves_other_fields`: Partial adaptive dict override preserves non-overridden fields
- **NEW** `TestNormalizeTaskConfigPreservesMetadata.test_preserves_experiment_metadata`: All metadata keys survive
- **NEW** `TestNormalizeTaskConfigPreservesMetadata.test_preserves_metadata_through_config_snapshot`: Metadata survives via snapshot path
- **NEW** `TestNormalizeTaskConfigPreservesMetadata.test_config_hash_computed_from_merged_config`: Hash from final merged config, not pre-merge

---

## P1-2: Wire Manifest Validator into stage handlers

### Root cause
`_read_upstream_manifest()` did basic checks but never called the shared `validate_manifest_json()`.

### Files changed
- `scripts/test_video_adaptive.py` `_read_upstream_manifest()` (line 1297):
  - Reads raw bytes from file
  - Calls `validate_manifest_json(raw_bytes, artifact_kind, expected_stage, expected_clip_id)`
  - Maps artifact_kind to expected producer stage name
  - For gif_clip_manifest, additionally cross-checks SHA against gif_file in inputs
  - All validation errors raise ValueError (converted to structured StageError by worker)

### New tests (`tests/task_engine/test_manifest_validation.py`)
- `test_valid_discover_manifest`, `test_valid_sample_manifest`, `test_missing_required_field`, `test_wrong_stage_name`, `test_wrong_clip_id`, `test_empty_json`, `test_invalid_json`, `test_unknown_artifact_kind`, `test_wrong_encoding`, `test_rank_dedup_clip_count_mismatch`, `test_rank_dedup_duplicate_clip_ids`, `test_rank_dedup_empty_clip_id`
- `test_read_upstream_manifest_valid`, `test_read_upstream_manifest_missing_field_raises`, `test_read_upstream_manifest_wrong_stage_raises`, `test_read_upstream_manifest_empty_file_raises`

---

## P1-3: Restore explicit sample_frames dependency for VLM

### Root cause
VLM input dependency was `("sample_manifest",)` without sample_frames. Sample manifest used bare paths as sole reference.

### Files changed
- `app/task_engine/artifacts.py`:
  - `STAGE_INPUT_KINDS["vlm"]` (line 193): Changed to `("sample_manifest", "sample_frames")`
- `scripts/test_video_adaptive.py`:
  - `_hash_artifact_id()` (line 1295): Stable artifact_id computation using canonical_hash, accepts stage_id parameter
  - `_stage_sample()` (line 1520): Accepts `config_data` for stage_id, stores `frame_entries` with artifact_id + timestamp in sample manifest
  - `_stage_vlm()` (line 1590): Accepts `config_data`, cross-references sample_manifest frame_entries with sample_frames resolver entries by artifact_id; validates file existence, path consistency, SHA-256; raises on missing frame, SHA error, duplicate artifact_id, unknown frame reference
  - `_run_stage()` dispatcher: Passes `config_data` to sample and vlm stages
- `app/task_engine/adaptive_adapter.py`:
  - `run()` (line 191): Injects `_stage_id` and `_clip_id` into config for artifact_id computation

### New/modified tests (`tests/task_engine/test_stage_inputs.py`)
- `test_vlm_stage_input_kinds_includes_sample_frames`
- `test_resolve_vlm_inputs_returns_both_kinds`
- `test_vlm_fails_when_sample_frames_missing`

---

## P1-4: Real full-production E2E tests

### New tests (`tests/task_engine/test_production_e2e.py`)
- **NEW** `TestProductionP14FullE2E.test_worker_driven_materialize_no_manual_input`: Worker uses `_build_context()` to resolve materialize inputs, verifies formal GIF publication and PBF generation
- **NEW** `TestProductionP14FullE2E.test_zero_clip_materialize_succeeds`: Zero gif_clip stages produces completed materialize
- **NEW** `TestProductionP14FullE2E.test_single_gif_clip_failure_only_that_clip_retried`: One succeeded + one needs_attention clip, verify terminal statuses distinguish them
- **NEW** `TestProductionP03OverwriteProtection.test_same_sha_idempotent_reuse` and `test_different_sha_no_overwrite`

---

## Schema migration version + compatibility

| Scenario | Detection | Behavior |
|---|---|---|
| Fresh DB (no migrations) | No columns, no indexes | Apply v3 then v4 in order |
| v3 only (migration 3 recorded) | v3 index exists, v4 missing | Apply v4, record both |
| v4 only (migration 4 recorded, v4 on disk) | v4 index exists | Record v3 as applied, skip DDL |
| v3 on disk, no migration records | v3 columns exist | Record v3, apply v4 |
| Contradiction (no v3 columns, v4 required) | stage_id/artifact_kind missing | Raise RuntimeError |

---

## Verification outputs

```
python -m compileall -q app scripts tests
  -> clean (no output)

python -m pytest -q tests/task_engine tests/quality_lab --ignore=tests/task_engine/test_e2e.py
  -> 379 passed

python -m pytest -q --ignore=tests/task_engine/test_e2e.py
  -> 870 passed, 2 skipped

git diff --check -- app/task_engine/ app/quality_lab/ app/routers/tasks.py scripts/test_video_adaptive.py
  -> clean (no whitespace errors)
```

---

## Remaining not-yet-fixed issues

1. `tests/task_engine/test_e2e.py::TestFullChainE2E::test_stages_created_in_order`: Pre-existing failure (confirmed via `git stash` + run). Only discover/sample/vlm stages are created (3 of 8). VLM stage blocks further advancement without real LLM. Not caused by this fix.

2. `@pytest.mark.slow` warnings: 8 custom marks not registered in pyproject.toml. Cosmetic only.

---

## Files changed (summary)

| File | Change | Issue(s) |
|---|---|---|
| `app/task_engine/schema.py` | Dual-evidence schema migration | P0-1 |
| `app/task_engine/artifacts.py` | resolve_materialize_inputs, envelope builder, STAGE_INPUT_KINDS updates | P0-2, P1-3 |
| `app/task_engine/worker.py` | Materialize uses dedicated resolver, remove _gif_clip_terminal_statuses hack | P0-2 |
| `app/task_engine/adaptive_adapter.py` | Materialize envelope writing, _stage_id injection | P0-2, P1-3 |
| `app/routers/tasks.py` | deep_merge before normalization | P1-1 |
| `app/quality_lab/config_builder.py` | Preserve _experiment/config_hash/task_work_dir/export_base_dir | P1-1 |
| `scripts/test_video_adaptive.py` | Overwrite protection, envelope format reading, manifest validation wiring, sample_frames cross-reference, _stage_id for artifact_id | P0-3, P0-2, P1-2, P1-3 |
| `tests/task_engine/test_schema_v4_reopen.py` | +4 tests: v3-only, no-migrations, triple-reopen, FK check | P0-1 |
| `tests/task_engine/test_manifest_validation.py` | NEW: 15 tests for validator + wiring | P1-2 |
| `tests/task_engine/test_stage_inputs.py` | NEW: 5 tests for VLM/materialize input resolution | P1-3 |
| `tests/task_engine/test_control_config_snapshot.py` | +3 tests: partial override, metadata preservation, hash computation | P1-1 |
| `tests/task_engine/test_production_e2e.py` | +5 tests: worker materialize, overwrite protection, zero-clip, partial failure | P0-3, P0-2, P1-4 |
| `tests/task_engine/test_stage_adapter.py` | Updated 2 tests: _stage_id in config | P1-3 |
