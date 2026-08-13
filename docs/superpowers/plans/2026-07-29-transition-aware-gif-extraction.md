# Transition-Aware GIF Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run and a commit containing only the files listed for that task.

**Goal:** Prevent final GIFs from crossing hard cuts or obvious dissolves while preserving coherent slow camera motion, including a real run against the specified *Modern Love Story* video.

**Architecture:** Add a media-only `transition_guard` service that scans each final candidate window with low-resolution frame features and global affine-motion compensation. Add a shared export-window/candidate-splitting layer, then call it before deduplication and clip-ID creation in both direct and task-engine paths. Preserve the current stage graph and expose only the three safety-critical settings in the Config UI.

**Tech Stack:** Python 3.11+, OpenCV (already declared), NumPy, Pillow, FFmpeg/ffprobe, existing VLM client, pytest, YAML/Gradio configuration UI.

## Global Constraints

- Do not add PySceneDetect, deep optical-flow models, or another heavyweight dependency.
- Keep the existing task graph: `discover -> sample -> vlm -> refine -> synthesize -> rank_dedup -> gif_clip -> materialize`.
- A confirmed hard cut, dissolve, fade, or crossfade must split the candidate; an ambiguous low-confidence change may only be penalized.
- A coherent slow pan/tilt/zoom must remain eligible for export.
- Trimmed/split segments shorter than 2.0 seconds are dropped.
- Boundary safety margin defaults to 0.25 seconds.
- All split segments share the existing global `max_output` cap.
- Final GIF duration must not exceed `adaptive.max_duration` in either direct or staged mode.
- Configuration is read from the frozen task snapshot; no ambient environment variable may select transition behavior.
- Existing user changes and historical GIF/database data must not be deleted or overwritten.
- The target validation video is `C:\Users\sunhao\Desktop\ToWatch\现代爱情故事.1991.BD1080p.国英双语中字.mp4`.

## File Map

- Create `app/services/transition_guard.py`: frame scanning, motion compensation, boundary classification, serializable result types.
- Create `app/services/gif_windows.py`: one export-window calculation shared by direct and staged paths.
- Create `app/services/transition_candidates.py`: pure transformation from guard segments plus scored frames to clean candidate records.
- Create `tests/test_transition_guard.py`: synthetic video fixtures and media-level guard tests.
- Create `tests/test_gif_windows.py`: duration, centering, clamping, and minimum-length tests.
- Create `tests/test_transition_candidates.py`: trim/split/drop and local-best-frame tests.
- Create `tests/test_adaptive_direct_transition.py`: direct-pipeline guard integration tests.
- Modify `app/services/clip_merge.py`: optional boundary-aware merge break.
- Modify `tests/test_clip_merge.py`: shot-boundary and boundary metadata cases.
- Modify `scripts/test_video_adaptive.py`: config extraction, shared window use, direct integration, staged integration, and result metrics.
- Modify `configs/models.yaml` and `configs/models.adult_candidate.yaml`: transition defaults.
- Modify `app/ui/tabs/settings.py` and `tests/test_config_help_annotations.py`: three user-facing fields and Chinese help.
- Modify `tests/test_adaptive_config.py`: config defaults and strict duration behavior.
- Modify `tests/task_engine/test_full_production_stage_chain.py` and/or focused task-engine tests: staged rank/dedup and GIF fan-out behavior.
- Modify `build_exe.spec` if the new script-only service imports are not collected by the existing `collect_submodules("app")` rule.
- Modify `README.md` and `Agent.md`: document transition-aware extraction, settings, and validation command.
- Create a validation report under `docs/reports/` only after the real target run succeeds.

---

### Task 1: Add failing media behavior tests and deterministic video fixtures

**Files:**
- Create: `tests/test_transition_guard.py`
- Test fixtures: generated in the test module under `tmp_path`; no repository media files.

**Interfaces:**
- Consumes the public `guard_candidate_window()` interface defined in Task 2.
- Produces deterministic behavioral expectations for the guard implementation.

- [ ] **Step 1: Write synthetic-video helpers and failing tests**

Use OpenCV `VideoWriter` so tests do not depend on a shell-specific FFmpeg filter graph. The helper must write 8 fps, 320x180 BGR frames and return the path. Cover these cases:

```python
BASE_CFG = {
    "transition_guard_enabled": True,
    "transition_scan_fps": 8,
    "transition_scan_width": 320,
    "transition_boundary_margin_s": 0.25,
    "transition_min_duration_s": 2.0,
    "transition_motion_compensation": True,
    "transition_hard_threshold": 0.65,
    "transition_soft_threshold": 0.40,
    "transition_soft_run_frames": 3,
}

def test_hard_cut_splits_window(tmp_path):
    video = write_hard_cut_video(tmp_path / "hard_cut.mp4")
    result = guard_candidate_window(video, 0.0, 6.0, 1.0, BASE_CFG)
    assert result.transition_action == "split"
    assert result.hard_cut_count >= 1
    assert len(result.segments) == 2
    assert all(s.end_s - s.start_s >= 2.0 for s in result.segments)

def test_slow_upward_motion_is_kept(tmp_path):
    video = write_affine_pan_video(tmp_path / "slow_pan.mp4", dy=-2.0)
    result = guard_candidate_window(video, 0.0, 4.0, 2.0, BASE_CFG)
    assert result.transition_action in {"keep", "trim"}
    assert result.hard_cut_count == 0
    assert result.motion_type == "coherent_camera_motion"

def test_crossfade_splits_without_using_single_frame_score(tmp_path):
    video = write_crossfade_video(tmp_path / "crossfade.mp4")
    result = guard_candidate_window(video, 0.0, 6.0, 1.0, BASE_CFG)
    assert result.soft_transition_count >= 1
    assert result.transition_action == "split"

def test_single_flash_is_not_a_cut(tmp_path):
    video = write_flash_video(tmp_path / "flash.mp4")
    result = guard_candidate_window(video, 0.0, 4.0, 2.0, BASE_CFG)
    assert result.hard_cut_count == 0
    assert result.transition_action in {"keep", "trim"}

def test_local_subject_motion_is_not_a_cut(tmp_path):
    video = write_moving_subject_video(tmp_path / "subject_motion.mp4")
    result = guard_candidate_window(video, 0.0, 4.0, 2.0, BASE_CFG)
    assert result.hard_cut_count == 0
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```powershell
uv run pytest -q tests/test_transition_guard.py
```

Expected: collection or assertion failures because `app.services.transition_guard` does not yet exist.

- [ ] **Step 3: Commit the failing fixtures/tests**

```powershell
git add -- tests/test_transition_guard.py
git commit -m "test: define transition guard media cases"
```

### Task 2: Implement the media-only transition guard

**Files:**
- Create: `app/services/transition_guard.py`
- Test: `tests/test_transition_guard.py`

**Interfaces:**
- Produces `TransitionGuardConfig`, `BoundaryEvidence`, `GuardSegment`, `TransitionGuardResult`, and `guard_candidate_window()`.
- `TransitionGuardResult.to_dict()` is consumed by manifests and `adaptive_test_result.json`.

- [ ] **Step 1: Define immutable result types and config normalization**

Implement dataclasses with stable string fields rather than leaking OpenCV objects:

```python
@dataclass(frozen=True)
class TransitionGuardConfig:
    enabled: bool = True
    scan_fps: float = 8.0
    scan_width: int = 320
    boundary_margin_s: float = 0.25
    min_duration_s: float = 2.0
    motion_compensation: bool = True
    hard_threshold: float = 0.65
    soft_threshold: float = 0.40
    soft_run_frames: int = 3
    rescore_split_segments: bool = True

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> "TransitionGuardConfig":
        """Coerce and clamp the transition_* values listed above."""
```

`from_mapping()` must coerce numeric values, clamp `scan_fps` and `scan_width` to positive values, reject NaN/inf, and enforce `min_duration_s >= 0.1` and `boundary_margin_s >= 0.0`.

- [ ] **Step 2: Implement low-resolution frame scanning**

Use `cv2.VideoCapture`, seek the requested window, sample at `scan_fps`, resize to `scan_width`, and convert to grayscale/HSV. Return a typed internal metric per adjacent pair containing histogram distance, edge distance, raw luma change, and timestamps. Release the capture in a `finally` block. A short or unreadable window returns a result with `transition_action="unverified"` and a non-empty `guard_error`.

- [ ] **Step 3: Implement global motion compensation**

For each suspicious pair, track `goodFeaturesToTrack()` points with `calcOpticalFlowPyrLK()`, estimate an affine transform using `estimateAffinePartial2D(points_prev, points_curr, method=RANSAC)`, warp the previous grayscale frame, and compute normalized residual. Record inlier ratio and transform deltas. If too few points are available, use residual=1.0 and inlier ratio=0.0 for classification; do not crash.

- [ ] **Step 4: Implement boundary classification and segment construction**

Use the normalized metrics to classify:

- `hard_cut` for a single large jump with poor alignment;
- `dissolve` or `fade` for `soft_run_frames` consecutive moderate changes that cannot share a stable affine model;
- `coherent_camera_motion` for stable affine motion with low compensated residual;
- `flash_or_exposure` for a transient luma spike with recoverable structure.

Confirmed hard/soft boundaries become split points. Apply `boundary_margin_s` on each side, clamp to the original window, and drop segments shorter than `min_duration_s`. A single clean segment containing `anchor_ts_s` yields `keep` or `trim`; multiple clean segments yield `split`.

- [ ] **Step 5: Run media tests and tune only against deterministic fixtures**

Run:

```powershell
uv run pytest -q tests/test_transition_guard.py
```

Expected: PASS for static, slow pan, hard cut, crossfade, flash, and local subject motion. If a threshold change is needed, change the named config default and add an assertion documenting the fixture it protects.

- [ ] **Step 6: Commit the service**

```powershell
git add -- app/services/transition_guard.py tests/test_transition_guard.py
git commit -m "feat: add motion-aware transition guard"
```

### Task 3: Centralize export-window calculation and add transition config extraction

**Files:**
- Create: `app/services/gif_windows.py`
- Create: `tests/test_gif_windows.py`
- Modify: `scripts/test_video_adaptive.py:460-516`
- Modify: `configs/models.yaml` and `configs/models.adult_candidate.yaml`
- Modify: `tests/test_adaptive_config.py`

**Interfaces:**
- Produces `ExportWindow` and `build_export_window()`.
- `extract_config()` returns all `transition_*` values using the YAML defaults.

- [ ] **Step 1: Write failing window/config tests**

```python
def test_single_frame_window_is_centered_and_capped():
    window = build_export_window(
        clip={"frame_count": 1, "best_frame_ts": 10.0, "gif_worthiness": 1.0},
        total_duration_s=30.0,
        min_duration_s=1.5,
        max_duration_s=5.0,
    )
    assert window.duration_s == 5.0
    assert window.start_s >= 0.0
    assert window.end_s <= 30.0

def test_multi_frame_window_never_exceeds_max_duration():
    window = build_export_window(
        clip={"frame_count": 12, "start_ts": 10.0, "end_ts": 40.0,
              "best_frame_ts": 20.0, "gif_worthiness": 0.8},
        total_duration_s=60.0,
        min_duration_s=2.0,
        max_duration_s=5.0,
    )
    assert window.duration_s <= 5.0

def test_config_extracts_transition_defaults():
    cfg = extract_config({"adaptive": {}})
    assert cfg["transition_guard_enabled"] is True
    assert cfg["transition_min_duration_s"] == 2.0
    assert cfg["transition_boundary_margin_s"] == 0.25
```

- [ ] **Step 2: Run the tests to verify failure**

```powershell
uv run pytest -q tests/test_gif_windows.py tests/test_adaptive_config.py
```

Expected: missing module/keys or failing duration assertions.

- [ ] **Step 3: Implement `build_export_window()`**

Use one strict rule for both paths: multi-frame duration is bounded by `max_duration_s`; single-frame duration interpolates between min/max using worthiness, then clamps to video duration. Center around the best frame with the existing 40%/60% before/after bias, and return an immutable `ExportWindow(start_s, end_s, duration_s)`.

- [ ] **Step 4: Add YAML defaults and `extract_config()` keys**

Add the exact keys from the approved spec to both model presets. Do not read `GIFAGENT_*` environment variables for these values. Keep `transition_guard_enabled=true`, minimum 2.0, margin 0.25, scan 8 fps/320 px, motion compensation enabled, thresholds 0.65/0.40, soft run 3, and split rescore enabled.

- [ ] **Step 5: Run focused tests and commit**

```powershell
uv run pytest -q tests/test_gif_windows.py tests/test_adaptive_config.py
git add -- app/services/gif_windows.py tests/test_gif_windows.py scripts/test_video_adaptive.py configs/models.yaml configs/models.adult_candidate.yaml tests/test_adaptive_config.py
git commit -m "feat: centralize adaptive export windows and guard config"
```

### Task 4: Add boundary-aware merge and pure candidate splitting

**Files:**
- Modify: `app/services/clip_merge.py`
- Modify: `tests/test_clip_merge.py`
- Create: `app/services/transition_candidates.py`
- Create: `tests/test_transition_candidates.py`

**Interfaces:**
- `merge_scored_frames_into_clips(frames, *, merge_gap, merge_score_threshold, max_merge_span_s=24.0, peak_threshold=None, shot_boundaries=None)` must refuse to merge across any supplied boundary.
- `build_guarded_clips(clip, guard_result, scored_frames, min_duration_s)` returns clean candidate dictionaries without performing VLM calls.

- [ ] **Step 1: Add failing merge/split tests**

```python
def test_boundary_breaks_high_score_merge():
    clips = merge_scored_frames_into_clips(
        [_f(0, 0.8), _f(5, 0.8), _f(10, 0.8)],
        merge_gap=15,
        merge_score_threshold=0.5,
        max_merge_span_s=24,
        shot_boundaries=[7.0],
    )
    assert [c["frame_count"] for c in clips] == [2, 1]

def test_guarded_split_chooses_best_frame_per_segment():
    result = fake_guard_result_with_segments((0.25, 2.0), (2.5, 5.0), anchor=1.0)
    clean = build_guarded_clips(
        clip={"start_ts": 0.0, "end_ts": 5.0, "best_frame_ts": 1.0,
              "frame_count": 3, "gif_worthiness": 0.7},
        guard_result=result,
        scored_frames=[_f(1.0, 0.7), _f(4.0, 0.9)],
        min_duration_s=2.0,
    )
    assert len(clean) == 2
    assert clean[1]["best_frame_ts"] == 4.0
```

- [ ] **Step 2: Run focused tests to verify failure**

```powershell
uv run pytest -q tests/test_clip_merge.py tests/test_transition_candidates.py
```

- [ ] **Step 3: Implement boundary-aware merge**

Add a helper that tests whether any boundary lies in `(prev_timestamp, frame_timestamp]`. Flush the current group before merging across that boundary. Preserve existing behavior when `shot_boundaries` is omitted.

- [ ] **Step 4: Implement pure candidate transformation**

For each `GuardSegment`, select the highest `gif_worthiness` scored frame whose timestamp lies inside the segment. Copy the original clip metadata, replace `start_ts/end_ts/best_frame_ts/best_frame_path/frame_count`, and add `transition_action`, `transition_risk`, `motion_type`, and `guard_reason`. Segments without a scored frame get `needs_rescore=true`; the caller decides whether to invoke VLM.

- [ ] **Step 5: Run tests and commit**

```powershell
uv run pytest -q tests/test_clip_merge.py tests/test_transition_candidates.py
git add -- app/services/clip_merge.py tests/test_clip_merge.py app/services/transition_candidates.py tests/test_transition_candidates.py
git commit -m "feat: split candidates at confirmed shot boundaries"
```

### Task 5: Integrate the guard into direct extraction

**Files:**
- Modify: `scripts/test_video_adaptive.py:582-1000,1139-1368`
- Modify: `tests/test_transition_candidates.py` or create `tests/test_adaptive_direct_transition.py`

**Interfaces:**
- Direct `run_pipeline()` calls `build_export_window()`, `guard_candidate_window()`, and `build_guarded_clips()` before embedding/temporal deduplication.
- Direct output includes transition counters and per-clip guard metadata.

- [ ] **Step 1: Add a failing direct integration test with monkeypatched guard**

Patch `scripts.test_video_adaptive.guard_candidate_window` to return two clean segments for one merged clip and assert that `run_pipeline()` sends both segments to the ranker while preserving the total output cap. Patch FFmpeg/VLM/embedding boundaries so the test is deterministic and does not require Ollama.

- [ ] **Step 2: Run the focused test to verify failure**

```powershell
uv run pytest -q tests/test_adaptive_direct_transition.py
```

- [ ] **Step 3: Apply the shared export window before guard evaluation**

Replace the direct path's duplicated duration math around the export loop with `build_export_window()`. For each merged clip, pass the real window and best timestamp to `guard_candidate_window()`.

- [ ] **Step 4: Materialize split segments and rescore only missing segments**

Use `build_guarded_clips()` for segments with existing scored frames. For `needs_rescore=true`, extract one midpoint frame and call `_score_vlm_frame()` through the existing provider/config path. A failed supplemental score drops only that segment. Do not call LLM synthesis to decide transition safety.

- [ ] **Step 5: Run guard before embedding/temporal dedup and preserve metrics**

Update `dedup_input_clips`, `embedding_deduped_clips`, `deduped_clips`, `top_clips`, and output counts after clean segments are materialized. Add a `transition_guard` object to the top-level result with input, split, trim, drop, unverified, hard-cut, soft-transition, and motion counters.

- [ ] **Step 6: Run focused tests and commit**

```powershell
uv run pytest -q tests/test_adaptive_direct_transition.py tests/test_transition_guard.py tests/test_gif_windows.py
git add -- scripts/test_video_adaptive.py tests/test_adaptive_direct_transition.py
git commit -m "feat: guard direct adaptive GIF candidates"
```

### Task 6: Integrate the guard into staged rank/dedup and GIF export

**Files:**
- Modify: `scripts/test_video_adaptive.py:1823-1855,2371-2605,2613-2723`
- Modify: `tests/task_engine/test_full_production_stage_chain.py` or a focused new staged test

**Interfaces:**
- `_run_stage()` passes `video_path` and frozen `config_data` to rank/dedup.
- `_stage_synthesize()` carries the refined scored-frame entries needed for segment-local best-frame selection.
- `_stage_rank_dedup(video_path, export_dir, work_dir, cfg, inputs, config_data)` cleans clips before embedding/temporal dedup and stable clip IDs.

- [ ] **Step 1: Add failing staged tests**

Build a deterministic short MP4 with two shots and a slow-pan fixture. Assert the rank manifest contains no cross-boundary clip, the slow-pan clip remains, and every `gif_clip` manifest has `end_ts-start_ts <= max_duration`.

- [ ] **Step 2: Run the focused staged test to verify failure**

```powershell
uv run pytest -q tests/task_engine/test_full_production_stage_chain.py -k "transition or max_duration"
```

Expected: the new assertions fail because staged rank/dedup currently has no video path/guard and `gif_clip` only enforces minimum duration.

- [ ] **Step 3: Thread source video/config into rank/dedup**

Modify the dispatcher call at `_run_stage()` and the function signature so rank/dedup can use the frozen config and source video. Keep artifact kinds and stage order unchanged.

- [ ] **Step 4: Preserve refined frame evidence in the synth manifest**

Add a versioned `scored_frames` list or equivalent frame references to the synth manifest so rank/dedup can choose a local best frame after splitting. Keep existing `clips`, summary, tags, and output keys backward-compatible.

- [ ] **Step 5: Guard before dedup and clip ID assignment**

Run window calculation, guard, pure split, and supplemental scoring before embedding/temporal dedup. Apply `output_ratio` and `max_output` only after clean segments are in the candidate list, then assign IDs using cleaned timestamps.

- [ ] **Step 6: Make `gif_clip` a strict export step**

Use the cleaned rank manifest window, enforce both minimum and maximum duration, and fail only for real FFmpeg export errors. A guard decision must not first appear in `gif_clip`.

- [ ] **Step 7: Run staged tests and commit**

```powershell
uv run pytest -q tests/task_engine/test_full_production_stage_chain.py -k "transition or max_duration"
git add -- scripts/test_video_adaptive.py tests/task_engine/test_full_production_stage_chain.py
git commit -m "feat: guard staged GIF clips before fan-out"
```

### Task 7: Add config UI, provenance fields, packaging coverage, and documentation

**Files:**
- Modify: `app/ui/tabs/settings.py:24-80,215-400`
- Modify: `tests/test_config_help_annotations.py`
- Modify: `build_exe.spec` if the import check requires explicit service modules
- Modify: `README.md` and `Agent.md`

**Interfaces:**
- Config UI exposes `adaptive.transition_guard_enabled`, `adaptive.transition_min_duration_s`, and `adaptive.transition_boundary_margin_s` with Chinese help.
- Advanced scan/motion thresholds remain in YAML and task snapshots.

- [ ] **Step 1: Add failing UI/config tests**

Update the test expectation only in the test branch first:

```python
assert len(CONFIG_FIELD_KEYS) == 25
for key in (
    "adaptive.transition_guard_enabled",
    "adaptive.transition_min_duration_s",
    "adaptive.transition_boundary_margin_s",
):
    assert key in CONFIG_FIELD_KEYS
    assert any("\u4e00" <= ch <= "\u9fff" for ch in CONFIG_FIELD_HELP[key])
```

- [ ] **Step 2: Run the test to verify failure**

```powershell
uv run pytest -q tests/test_config_help_annotations.py
```

- [ ] **Step 3: Add the three controls and wire load/save/reload**

Insert the fields after `adaptive.max_duration`, update `load_config()`, `save_config()`, `_reload()`, the ordered Gradio input list, and the Chinese help map. Preserve all existing component ordering and values.

- [ ] **Step 4: Verify frozen config/provenance and package imports**

Run the task-config tests. Use a read-only import check for the new services. If PyInstaller does not collect the script-only imports, add explicit `app.services.transition_guard`, `app.services.gif_windows`, and `app.services.transition_candidates` hidden imports to `build_exe.spec` and run the existing package analysis smoke command.

- [ ] **Step 5: Document behavior and rollback**

Add a concise section to `README.md` and `Agent.md` covering the guard settings, slow-camera-motion allowance, split/drop rules, result metrics, and the real validation command. State that disabling the guard affects only new runs and does not delete historical data.

- [ ] **Step 6: Run UI/config tests and commit**

```powershell
uv run pytest -q tests/test_config_help_annotations.py tests/test_adaptive_config.py tests/test_tasks_api.py
git add -- app/ui/tabs/settings.py tests/test_config_help_annotations.py build_exe.spec README.md Agent.md
git commit -m "feat: expose transition guard settings and docs"
```

### Task 8: Run the full regression suite and verify artifacts

**Files:**
- Modify only files proven necessary by failing tests from Tasks 1-7.
- Create: `docs/reports/modern-love-story-transition-guard-validation-2026-07-29.md` after the real run.

- [ ] **Step 1: Run focused regression tests**

```powershell
uv run pytest -q tests/test_transition_guard.py tests/test_gif_windows.py tests/test_transition_candidates.py tests/test_clip_merge.py tests/test_adaptive_config.py tests/test_config_help_annotations.py
```

Expected: PASS.

- [ ] **Step 2: Run task-engine production tests**

```powershell
uv run pytest -q tests/task_engine/test_full_production_stage_chain.py tests/task_engine/test_production_e2e.py
```

Expected: PASS or existing environment-gated skips only; any new failure must be fixed before the real video run.

- [ ] **Step 3: Preserve the user-modified result file before the target run**

The direct script writes `data/adaptive_test_result.json`. Before invoking it, copy that file to a uniquely named system temp path, and restore it in a `finally` block after capturing the target result. Do not delete or overwrite the user's existing file without restoration.

- [ ] **Step 4: Verify the target source and run the full direct pipeline**

```powershell
$targetVideo = 'C:\Users\sunhao\Desktop\ToWatch\现代爱情故事.1991.BD1080p.国英双语中字.mp4'
$targetExport = 'data\exports\transition_guard_validation'
if(-not (Test-Path -LiteralPath $targetVideo)){ throw "Target video not found: $targetVideo" }
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 -- $targetVideo
uv run python -u scripts/test_video_adaptive.py --video $targetVideo --export-dir $targetExport
```

Run with the approved frozen config and keep the command, duration, model endpoint status, elapsed time, and output directory in the report. If the VLM endpoint is unavailable, record that as an environment failure and do not claim the visual acceptance passed.

- [ ] **Step 5: Audit transition metrics and final windows**

Parse the target result JSON and manifests. Assert:

```text
every final GIF duration <= adaptive.max_duration
every exported segment duration >= 2.0 seconds
every confirmed boundary is outside every final GIF interval
hard-cut and split/drop counters are present
slow-pan fixture and target slow-motion candidates are not systematically removed
```

Generate a small contact sheet or thumbnail list for manual inspection of the highest-risk hard-cut outputs and the highest-scoring slow-motion outputs. Use the app's image viewer only for those selected samples.

- [ ] **Step 6: Write the validation report**

The report must include the exact source path, command, config hash, guard algorithm version, before/after candidate counts, hard/soft transition counts, trim/split/drop/unverified counts, final GIF paths, and a short manual review table marking hard-cut mitigation and slow-motion retention.

- [ ] **Step 7: Run the complete non-slow suite**

```powershell
uv run pytest -q
```

Expected: all existing tests plus new tests pass, with only previously documented dependency warnings/skips.

- [ ] **Step 8: Commit the validation report and final source changes**

```powershell
git add -- docs/reports/modern-love-story-transition-guard-validation-2026-07-29.md
git commit -m "test: validate transition-aware GIF extraction on film"
```

## Plan Self-Review Checklist

- Spec coverage: media detection, motion allowance, trim/split/drop, minimum 2 seconds, shared direct/staged integration, strict max duration, config snapshot, UI help, error handling, metrics, synthetic tests, and target-film validation each have an explicit task.
- Type consistency: `guard_candidate_window()` returns `TransitionGuardResult`; `build_guarded_clips()` consumes its `segments`; both direct and staged use `build_export_window()` before guard evaluation.
- Boundary ordering: guard runs before embedding/temporal dedup, clip ID generation, and GIF fan-out.
- Data safety: only intended files are staged per task; the existing `data/adaptive_test_result.json` is backed up and restored around the direct validation run.
- No placeholder steps: every task has concrete files, commands, expected outcomes, and commit scope.
