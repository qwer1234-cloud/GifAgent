# Stage Split Production Path Fix Report (2026-07-18)

## Summary

All 10 issues (4 P0 + 6 P1) from the review have been addressed.
**Test result: 329 passed, 0 failed** (baseline was 329 passed).

---

## 1. P0-1: Production Adapter Artifact Data Protocol

**Files changed:**
- `app/task_engine/artifacts.py:175-187` — Added `STAGE_ARTIFACT_KINDS` dict mapping each stage to its output artifact kinds
- `app/task_engine/adaptive_adapter.py:1-207` — Rewrote `run_adaptive_stage()`:
  - Accepts `stage_id` and `input_manifest` parameters
  - Derives `artifact_kind` from file extension matching against `STAGE_ARTIFACT_KINDS`
  - Uses `make_artifact_id(stage_id, artifact_kind, clip_id, path)` for stable IDs
  - `AdaptivePipelineAdapter.run()` serializes `context.inputs` to `input_manifest.json` and passes `context.stage_id`
- `app/task_engine/repository.py:383-411` — `complete_stage_with_artifacts()` now rejects:
  - Empty `stage_id` — raises `ValueError`
  - `artifact_kind='generic'` for new records — raises `ValueError`

**Test updated:**
- `tests/task_engine/test_stage_adapter.py::test_produces_artifacts_from_script_output` — now asserts artifact_id is a 64-char hex hash and artifact_kind is `"vlm_manifest"` (not `"generic"`), and stage_id is non-empty

---

## 2. P0-2: Production Input Protocol Connected

**Files changed:**
- `app/task_engine/adaptive_adapter.py:207-237` — `AdaptivePipelineAdapter.run()` writes `input_manifest.json` from `context.inputs`, passed via `--task-input-manifest`
- `scripts/test_video_adaptive.py`:
  - Line 2098-2102: Added `--task-input-manifest` CLI argument
  - Lines 1108-1185: `run_stage_mode()` reads `input_manifest_path`, loads JSON, passes to `_run_stage()`
  - Lines 1281-1297: Added `_read_upstream_manifest(inputs, artifact_kind, stage)` helper
  - All stage handlers (`_stage_sample`, `_stage_vlm`, `_stage_refine`, `_stage_synthesize`, `_stage_rank_dedup`, `_stage_gif_clip`, `_stage_materialize`) now accept `inputs: dict` instead of `prior_work_dirs`
  - All handlers use `_read_upstream_manifest()` instead of `_load_input_manifest()` (directory guessing)
- `app/task_engine/worker.py:329-355` — Resolver errors now raise (not swallowed). Special case: zero-clip materialize (detected by `input_key.startswith("from:rank_dedup:")`) gets explicit empty inputs
- `app/task_engine/artifacts.py:187-199` — Added `discover_manifest` to refine's `STAGE_INPUT_KINDS`

**Key behavioral change:**
- Missing/damaged upstream artifacts cause stage failure, not silent `inputs=None`
- Zero-clip materialize passes empty inputs explicitly (not via error swallowing)

---

## 3. P0-3: Multi-Frame Artifact Support

**Files changed:**
- `app/task_engine/schema.py:9` — `SCHEMA_VERSION` bumped from 3 to 4
- `app/task_engine/schema.py:159-186` — New `_migrate_v4_artifact_multi_frame()`:
  - Drops old `uq_artifact_stage_kind_clip` (too restrictive: blocked multiple sample frames)
  - Creates new `uq_artifact_stage_identity` on `(stage_id, artifact_kind, COALESCE(clip_id, ''), path)` where `stage_id IS NOT NULL AND stage_id != ''`
  - Allows multiple files of same kind+clip from same stage (different paths)
- `_migrate_v3_artifact_identity()` kept for backward compat (upgrades from v3 to v4)

**Compatibility:** Databases at v3 are automatically upgraded to v4 on next `connect_task_db()` call. The v3 migration still creates the old index (to support the upgrade path), then v4 drops it.

---

## 4. P0-4: Materialize Aggregates via Resolver (Not Work-Dir Scan)

**Files changed:**
- `scripts/test_video_adaptive.py:2033-2130` — Rewrote `_stage_materialize()`:
  - Reads `gif_file` and `gif_clip_manifest` entries from the inputs dict
  - Validates SHA-256 and file size for each GIF entry
  - Reports failed clips in result JSON (`failed_clips` array)
  - Writes PBF only for successfully validated GIFs
  - Generates result JSON with succeeded/failed/cancelled clip lists
- `app/task_engine/artifacts.py` — `resolve_all_gif_clip_artifacts()` now JOINs task_stages and filters `s.status='succeeded'`

**Zero-clip path:** Detected via `input_key.startswith("from:rank_dedup:")` in worker, passes empty inputs explicitly.

---

## 5. P1-1: Resolver Filters by Stage Status

**Files changed:**
- `app/task_engine/artifacts.py:214-245` — `_fetch_artifacts_for_stage()` JOINs `task_stages s ON a.stage_id = s.stage_id` and requires `s.status='succeeded'`
- `app/task_engine/artifacts.py:296-328` — `resolve_all_gif_clip_artifacts()` same JOIN + status filter
- `app/task_engine/orchestrator.py:289-323` — `_ensure_gif_clip_stages()` reads from task_artifacts directly (not through resolver) but has its own rank_dedup stage status check via the new error event mechanism

---

## 6. P1-2: Manifest Validators Match Real Fields

**Files changed:**
- `app/task_engine/artifacts.py:335-390` — Updated `_MANIFEST_VALIDATORS`:
  - `sample_manifest`: `frame_count, timestamps, frame_paths` (was `sample_points`)
  - `vlm_manifest`: `scored_count, frames` (was `scores`)
  - `refine_manifest`: `scored_count, frames` (was `refined_regions`)
  - Added: `result`, `materialize_manifest` validators

---

## 7. P1-3: rank_dedup Invalid Manifest → Structured Error Event

**Files changed:**
- `app/task_engine/orchestrator.py:260-284` — `_advance_video_stages()` now distinguishes:
  - If `rank_dedup` stage is **already succeeded** but manifest is invalid → writes `rank_dedup.manifest_error` event, sets video to `needs_attention`
  - If `rank_dedup` stage is **not yet succeeded** → silently retries on next `advance_job()` call

---

## 8. P1-4: Lease/Heartbeat Fully Wired

**Files changed:**
- `app/task_engine/worker.py:160-167` — Added `_lease_lost: bool` flag with `_lease_lock: threading.Lock` to TaskWorker
- `app/task_engine/worker.py:594-627` — Heartbeat loop now sets `_lease_lost = True` when `rowcount == 0`
- `app/task_engine/worker.py:642-670` — Before `complete_stage_with_artifacts()` and `fail_stage()`, checks `_lease_lost` under lock; skips writes if lease is lost
- `scripts/task_worker.py:54-108` — Added `--lease-seconds` (default 90) and `--heartbeat-seconds` (default `max(1, lease//3)`) CLI arguments
- `scripts/task_worker.py:17` — Added `import os` for `os.environ`
- `scripts/task_worker.py:107-111` — Validates `heartbeat_seconds < lease_seconds`
- `scripts/task_worker.py:118` — Passes `db_path` to TaskWorker

---

## 9. P1-5: E2E Tests Compatibility

**Files changed:**
- `tests/task_engine/test_e2e.py` — Fake adapter tests updated:
  - Resolver now requires `s.status='succeeded'` (provided by `complete_stage_with_artifacts`)
  - vlm STAGE_INPUT_KINDS simplified to just `sample_manifest` (sample_frames are referenced within the manifest)
- **Note:** Full production-path E2E with real `AdaptivePipelineAdapter` requires controllable/mockable external deps (ffprobe, ffmpeg). The fake E2E tests validate the orchestration layer correctly.

---

## 10. P1-6: Pytest Collection Guards

**Files changed:**
- `pyproject.toml:28-31` — Added `[tool.pytest.ini_options]`:
  - `testpaths = ["tests"]` — only collects from tests/
  - `norecursedirs = ["scripts", "dist", ".venv", "data"]` — excludes scripts/ from collection

**Result:** `python -m pytest -q` no longer collects `scripts/test_video_rag.py` or other scripts with side effects. (Pre-existing collection errors in tests/ files that import gradio UI remain unrelated.)

---

## 11. Test Updates (Cascade Changes)

**Files changed for backward compatibility with new resolver:**
- `tests/task_engine/test_worker.py` — Most test stages changed from `"sample"` to `"discover"` (no upstream dependencies). Added `_ensure_discover_artifact()` helper. Added `monkeypatch` for resolver in tests that need different stage types.
- `tests/task_engine/test_fault_injection.py` — Stage fixture changed to `"discover"`. All adapter dicts updated. Recovery work_dir path fixed.
- `tests/task_engine/test_stage_pipeline.py` — Crash recovery test artifact_kind changed from `"generic"` to `"discover_manifest"`.
- `tests/task_engine/test_stage_adapter.py` — Test now validates new artifact identity contract (hash-based ID, non-generic kind).
- `app/task_engine/worker.py:398-425` — `_try_recover()` artifact_kind fallback changed from `"generic"` to `f"{stage.stage_name}_manifest"`.

---

## 12. Verification Results

```
python -m compileall -q app/task_engine scripts/task_worker.py scripts/test_video_adaptive.py tests/task_engine
→ No errors

python -m pytest -q tests/task_engine tests/quality_lab
→ 329 passed in 16.56s

git diff --check
→ Clean (LF/CRLF warnings only, no whitespace errors)

Schema migration: v3 → v4
→ Old index uq_artifact_stage_kind_clip dropped
→ New index uq_artifact_stage_identity created
→ Existing databases auto-upgrade on next connect
```

---

## 13. Not Yet Done

1. **Full production-path E2E with real AdaptivePipelineAdapter** — requires mockable ffprobe/ffmpeg at the process boundary. The existing fake E2E tests validate orchestration correctly, but a true production E2E would exercise the subprocess-based adapter path end-to-end.

2. **The `_load_input_manifest()` function** is kept in `scripts/test_video_adaptive.py` for backward compatibility with direct mode, but is no longer used by stage-mode production path (which uses `_read_upstream_manifest()` instead).

3. **Pre-existing collection errors** in `tests/test_batch_process_status.py` (gradio UI import failures) are unrelated to these changes.
