# GIF Action Completeness Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add transition-safe action-boundary detection so GIFs preferentially contain a complete action, remain naturally loopable when possible, split long actions at natural stages, and never exceed 20 seconds.

**Architecture:** Build one cached low-resolution temporal-evidence layer shared by the existing transition guard and a new action-boundary service. A shared candidate materializer runs transition protection first, then CV action analysis, optional one-shot VLM verification, duration/split/loop policy, and segment rescoring before either direct or staged dedup/ranking. Uncertain action boundaries fall back to the current 40/60 window clamped inside the transition-safe segment.

**Tech Stack:** Python 3.14, OpenCV, NumPy, Pillow, httpx/Ollama-compatible VLM, PyYAML, Gradio, pytest, FFmpeg/FFprobe.

## Global Constraints

- Action narrative completeness is higher priority than loop quality.
- Preferred GIF duration is 4–12 seconds; complete 2–4 second actions remain valid.
- `adaptive.max_duration` defaults to 20 seconds and is a hard invariant at every FFmpeg boundary.
- The analysis window defaults to 30 seconds and is never itself an export duration.
- Confirmed hard cuts, dissolves, and fades are absolute boundaries; action expansion cannot cross them.
- Coherent slow pan, upward motion, and light zoom remain eligible and cannot be treated as subject-action boundaries solely from raw pixel motion.
- Long actions exceeding 20 seconds split only at transition boundaries, stable pauses, residual-motion valleys, or VLM-confirmed semantic stages.
- If CV and the one permitted VLM check cannot verify a boundary, use the existing 40/60 window clamped to the transition-safe segment.
- Never synthesize reverse playback or ping-pong loops.
- A failed action check affects one candidate only; it cannot fail the video job.
- Every split child is rescored when it has no existing scored frame and then participates normally in dedup, ranking, and `max_output`.
- VLM verification is capped at one call per eligible candidate and at 25% of action candidates in real-sample acceptance.
- Runtime behavior comes only from the frozen task config and algorithm version; no ambient environment variable selects action behavior.
- Pose recognition, object tracking, and deep action-recognition models remain roadmap items and are not first-version dependencies.
- Existing GIFs, task history, database records, review labels, and result files must never be deleted by enabling, disabling, or rolling back this feature.
- Preserve unrelated dirty worktree files. Stage only the source, tests, configs, and documentation named by each task.

---

## File and Responsibility Map

### New production files

- `app/services/temporal_evidence.py` — cached low-resolution frame decoding, pair metrics, affine compensation, and residual maps shared by transition and action analysis.
- `app/services/action_boundary.py` — action configuration, motion-curve analysis, boundary candidates, completeness score, duration/split policy, and loop scoring.
- `app/services/action_vlm.py` — contact-sheet construction, constrained prompt, strict response parsing, and candidate-index selection.
- `app/services/action_candidates.py` — pure fan-out from action decisions into candidate dictionaries with best-frame selection and metadata.
- `app/services/action_pipeline.py` — shared direct/staged orchestration with callbacks for frame scoring and VLM generation.
- `scripts/validate_action_completeness.py` — labeled-sample evaluator for boundary error, duration, split, fallback, transition, VLM-call, and output-count metrics.

### New test files

- `tests/test_temporal_evidence.py`
- `tests/action_media_fixtures.py`
- `tests/test_action_boundary.py`
- `tests/test_action_candidates.py`
- `tests/test_action_vlm.py`
- `tests/test_action_pipeline.py`
- `tests/test_adaptive_direct_action.py`
- `tests/test_action_validation.py`
- `docs/reports/action-completeness-validation-2026-07-29.md` (created only after the real-media run)

### Existing files modified by the feature

- `app/services/transition_guard.py` — consume shared temporal evidence without changing public transition behavior.
- `scripts/test_video_adaptive.py` — config extraction, direct integration, staged integration, v2 manifests, metrics, and final export invariants.
- `app/task_engine/artifacts.py` — accept and validate rank/gif manifest schema v2 while retaining v1.
- `configs/models.yaml`
- `configs/models.adult_candidate.yaml`
- `app/ui/tabs/settings.py`
- `tests/test_transition_guard.py`
- `tests/test_adaptive_config.py`
- `tests/test_config_help_annotations.py`
- `tests/test_tasks_api.py`
- `tests/test_gif_windows.py`
- `tests/test_adaptive_direct_transition.py`
- `tests/task_engine/test_manifest_validation.py`
- `tests/task_engine/test_full_production_stage_chain.py`
- `scripts/import_adaptive_candidates.py`
- `app/routers/candidates.py`
- `app/ui/tabs/review.py`
- `tests/test_candidates_api.py`
- `tests/test_candidate_review_layout.py`
- `README.md`
- `Agent.md`

---

### Task 1: Extract Cached Temporal Evidence from the Transition Guard

**Files:**
- Create: `app/services/temporal_evidence.py`
- Create: `tests/test_temporal_evidence.py`
- Modify: `app/services/transition_guard.py`
- Modify: `tests/test_transition_guard.py`

**Interfaces:**
- Produces: `TemporalScanConfig(fps: float, width: int, motion_compensation: bool)`.
- Produces: `TemporalFrame(sample_index: int, timestamp_s: float, gray: np.ndarray, hsv: np.ndarray)`.
- Produces: `TemporalPairEvidence` with raw change metrics, affine metrics, and `residual_map`.
- Produces: `TemporalEvidence(start_s, end_s, fps, width, frames, pairs)` with `slice()` and `resample()` methods.
- Produces: `TemporalEvidenceCache.scan(video_path, start_s, end_s, config) -> TemporalEvidence`.
- Changes: `guard_candidate_window(video_path, start_s, end_s, anchor_ts_s, config_values=None, *, temporal_evidence: TemporalEvidence | None = None)`; existing positional calls remain valid.

- [ ] **Step 1: Write failing cache and compatibility tests**

Define `_write_cache_video(path, *, hard_cut)` in `tests/test_temporal_evidence.py` using `cv2.VideoWriter`, 8 FPS, and 48 deterministic 160×90 feature-rich frames. With `hard_cut=False`, move one rectangle across a fixed grid; with `hard_cut=True`, use one grid/color palette for frames 0–23 and a distinct grid/color palette for frames 24–47.

```python
def test_overlapping_scans_decode_only_missing_samples(tmp_path):
    action_video = _write_cache_video(tmp_path / "motion.mp4", hard_cut=False)
    cache = TemporalEvidenceCache()
    cfg = TemporalScanConfig(fps=8.0, width=160, motion_compensation=True)

    first = cache.scan(action_video, 0.0, 4.0, cfg)
    decoded_after_first = cache.decoded_frame_count
    second = cache.scan(action_video, 2.0, 6.0, cfg)

    assert first.frames
    assert second.frames
    assert cache.decoded_frame_count < decoded_after_first * 2
    assert len({frame.sample_index for frame in second.frames}) == len(second.frames)


def test_precomputed_evidence_matches_direct_transition_scan(tmp_path):
    hard_cut_video = _write_cache_video(tmp_path / "hard-cut.mp4", hard_cut=True)
    cache = TemporalEvidenceCache()
    scan_cfg = TemporalScanConfig(fps=8.0, width=320, motion_compensation=True)
    evidence = cache.scan(hard_cut_video, 0.0, 6.0, scan_cfg)

    direct = guard_candidate_window(hard_cut_video, 0.0, 6.0, 1.0, BASE_CFG)
    shared = guard_candidate_window(
        hard_cut_video,
        0.0,
        6.0,
        1.0,
        BASE_CFG,
        temporal_evidence=evidence,
    )

    assert shared.transition_action == direct.transition_action
    assert shared.hard_cut_count == direct.hard_cut_count
    assert shared.segments == direct.segments
```

Also cover `TemporalEvidence.slice()` retaining only in-range frames/pairs,
`resample(4.0)` selecting deterministic sample indexes from an 8 FPS scan, one
successful retry after a simulated first decode failure, and strict failure
after two attempts on unreadable media.

- [ ] **Step 2: Run the new tests and confirm the missing module/interface failure**

Run:

```powershell
uv run pytest -q tests/test_temporal_evidence.py tests/test_transition_guard.py
```

Expected: collection fails because `app.services.temporal_evidence` and the keyword argument do not exist.

- [ ] **Step 3: Implement the shared evidence types and cache**

Use these public shapes:

```python
@dataclass(frozen=True)
class TemporalScanConfig:
    fps: float = 8.0
    width: int = 320
    motion_compensation: bool = True


@dataclass(frozen=True)
class TemporalFrame:
    sample_index: int
    timestamp_s: float
    gray: np.ndarray
    hsv: np.ndarray


@dataclass(frozen=True)
class TemporalPairEvidence:
    timestamp_s: float
    previous_gray: np.ndarray
    gray: np.ndarray
    histogram_distance: float
    edge_distance: float
    luma_change: float
    compensated_residual: float
    inlier_ratio: float
    translate_x: float
    translate_y: float
    scale: float
    residual_map: np.ndarray


@dataclass(frozen=True)
class TemporalEvidence:
    start_s: float
    end_s: float
    fps: float
    width: int
    frames: tuple[TemporalFrame, ...]
    pairs: tuple[TemporalPairEvidence, ...]

    def slice(self, start_s: float, end_s: float) -> "TemporalEvidence":
        selected_frames = tuple(
            frame for frame in self.frames
            if start_s - 1e-6 <= frame.timestamp_s <= end_s + 1e-6
        )
        selected_pairs = tuple(
            pair for pair in self.pairs
            if start_s - 1e-6 <= pair.timestamp_s <= end_s + 1e-6
        )
        return TemporalEvidence(start_s, end_s, self.fps, self.width, selected_frames, selected_pairs)
```

`TemporalEvidenceCache` must identify a video by resolved path, file size, and
`st_mtime_ns`, then key decoded frames by that identity, FPS, width, and integer
`sample_index = round(timestamp_s * fps)`. The derived pair-evidence key also
includes `motion_compensation`; replacing a file at the same path cannot reuse
stale evidence. `scan()` calculates the required inclusive index range, groups
missing indexes into contiguous ranges, and decodes each missing range with
`cv2.VideoCapture`. Retry an open/read failure once after releasing the first
capture; raise the typed media error after the second failure. Then compute pair
evidence from cached adjacent frames.

For each affine-compensated pair:

```python
warped = cv2.warpAffine(
    previous.gray,
    transform,
    (current.gray.shape[1], current.gray.shape[0]),
    borderMode=cv2.BORDER_REFLECT,
)
residual_map = cv2.absdiff(warped, current.gray)
compensated_residual = float(np.mean(residual_map) / 255.0)
```

If affine estimation fails, use an identity transform, `inlier_ratio=0.0`, and the unwarped absolute difference. Record `decoded_frame_count` only when a new sampled frame enters the cache.

- [ ] **Step 4: Refactor transition detection to consume the shared pairs**

Replace `_PairMetric`, `_scan_pairs()`, and duplicated affine computation with conversion from `TemporalPairEvidence` to `BoundaryEvidence`. When no evidence is supplied, construct `TemporalScanConfig` from `TransitionGuardConfig` and perform a local cache scan. Reject supplied evidence that does not cover the requested window or contains fewer than two pairs.

Preserve all current hard-cut, dissolve, flash, anchor-margin, and coherent-camera-motion decisions.

- [ ] **Step 5: Run focused transition/evidence tests**

Run:

```powershell
uv run pytest -q tests/test_temporal_evidence.py tests/test_transition_guard.py tests/test_transition_candidates.py
```

Expected: all pass, including byte-for-byte-equivalent serialized transition result fields.

- [ ] **Step 6: Commit Task 1**

```powershell
git add app/services/temporal_evidence.py app/services/transition_guard.py tests/test_temporal_evidence.py tests/test_transition_guard.py
git commit -m "refactor: share temporal motion evidence"
```

---

### Task 2: Detect CV Action Motion and Boundary Candidates

**Files:**
- Create: `app/services/action_boundary.py`
- Create: `tests/action_media_fixtures.py`
- Create: `tests/test_action_boundary.py`

**Interfaces:**
- Consumes: `TemporalEvidence` and `TemporalPairEvidence` from Task 1.
- Produces from `tests/action_media_fixtures.py`: deterministic writers plus `scan_video(video_path, start_s, end_s, fps=8.0, width=320)`.
- Produces: `ActionBoundaryConfig.from_mapping(values, strict=False)`.
- Produces: `ActionBoundaryCandidate(start_s, peak_s, end_s, confidence, start_settle, end_settle, peak_inclusion, boundary_quiet)`.
- Produces: `ActionMotionAnalysis(motion_type, candidates, residual_curve, active_runs, stable_valleys, confidence, analysis_error)`, where `residual_curve` is a timestamp/value tuple sequence, `active_runs` is a start/end tuple sequence, and `stable_valleys` is a timestamp sequence.
- Produces: `analyze_action_motion(evidence, safe_start_s, safe_end_s, anchor_ts_s, config_values) -> ActionMotionAnalysis`.

- [ ] **Step 1: Create deterministic action-video fixtures and failing behavior tests**

The fixture module must generate feature-rich backgrounds plus deterministic moving shapes. Add these tests:

```python
def test_static_then_move_then_settle_finds_complete_action(tmp_path):
    video = write_start_move_settle_video(tmp_path / "complete.mp4")
    evidence = scan_video(video, 0.0, 8.0)

    result = analyze_action_motion(evidence, 0.0, 8.0, 4.0, BASE_ACTION_CFG)
    best = result.candidates[0]

    assert result.motion_type == "subject_action"
    assert best.start_s == pytest.approx(2.0, abs=0.75)
    assert best.end_s == pytest.approx(6.0, abs=0.75)
    assert best.start_settle > 0.0
    assert best.end_settle > 0.0


def test_slow_global_pan_is_ambient_camera_motion(tmp_path):
    video = write_pan_with_static_subject(tmp_path / "pan.mp4")
    evidence = scan_video(video, 0.0, 8.0)

    result = analyze_action_motion(evidence, 0.0, 8.0, 4.0, BASE_ACTION_CFG)

    assert result.motion_type == "ambient_camera_motion"
    assert result.candidates == ()
```

Also add named tests for: subject action during camera pan; pure slow vertical
upward camera motion; gentle global zoom; a turn and a two-lobe wave; action
active at the left analysis edge; action active at the right edge; a
0.375-second pause that remains one run; and a 1.25-second pause that becomes a
stable valley.

The fixture helper is concrete and shared by later tests:

```python
def scan_video(
    video_path: Path,
    start_s: float,
    end_s: float,
    fps: float = 8.0,
    width: int = 320,
) -> TemporalEvidence:
    return TemporalEvidenceCache().scan(
        video_path,
        start_s,
        end_s,
        TemporalScanConfig(fps=fps, width=width, motion_compensation=True),
    )
```

- [ ] **Step 2: Run tests and confirm import failure**

Run:

```powershell
uv run pytest -q tests/test_action_boundary.py
```

Expected: collection fails because `app.services.action_boundary` does not exist.

- [ ] **Step 3: Implement configuration and immutable result types**

`ActionBoundaryConfig` uses these defaults:

```python
@dataclass(frozen=True)
class ActionBoundaryConfig:
    enabled: bool = True
    vlm_verify_enabled: bool = True
    analysis_version: int = 1
    analysis_window_s: float = 30.0
    preferred_min_duration_s: float = 4.0
    preferred_max_duration_s: float = 12.0
    min_duration_s: float = 2.0
    max_duration_s: float = 20.0
    scan_fps: float = 4.0
    boundary_confidence_threshold: float = 0.65
    loop_adjust_s: float = 0.75
    vlm_min_worthiness: float = 0.60
    fallback_mode: str = "fixed_window"
```

Define the analysis type exactly:

```python
@dataclass(frozen=True)
class ActionMotionAnalysis:
    motion_type: str
    candidates: tuple[ActionBoundaryCandidate, ...]
    residual_curve: tuple[tuple[float, float], ...]
    active_runs: tuple[tuple[float, float], ...]
    stable_valleys: tuple[float, ...]
    confidence: float
    analysis_error: str | None = None
```

`from_mapping(values, strict=True)` raises `ValueError` for an unsupported
`analysis_version`, non-finite values, invalid score ranges, `preferred_min >
preferred_max`, `preferred_max > max_duration`, `max_duration >
analysis_window_s`, `min_duration < 2`, non-positive FPS, or fallback modes
other than `fixed_window`. First-version execution accepts only
`analysis_version=1`. Non-strict parsing replaces malformed optional values
with defaults but still normalizes booleans and finite numeric types.

- [ ] **Step 4: Implement the residual-motion curve**

For action FPS, use `evidence.resample(config.scan_fps)`. Define per-pair residual energy and changed-area ratio:

```python
residual_energy = float(np.mean(pair.residual_map) / 255.0)
pixel_floor = max(6.0, float(np.percentile(pair.residual_map, 75)))
changed_ratio = float(np.mean(pair.residual_map >= pixel_floor))
motion_value = 0.65 * residual_energy + 0.35 * changed_ratio
```

Smooth with a three-sample median filter. Compute robust thresholds:

```python
baseline = float(np.median(curve))
mad = float(np.median(np.abs(curve - baseline)))
active_threshold = baseline + max(2.5 * mad, 0.015)
stable_threshold = baseline + max(1.25 * mad, 0.0075)
```

Close inactive gaps up to `round(0.5 * fps)` samples. Stable valleys require at least `round(1.0 * fps)` consecutive samples at or below `stable_threshold`.

Classify `ambient_camera_motion` only when at least 70% of pairs have `inlier_ratio >= 0.45`, coherent non-trivial affine displacement, and median residual energy below `active_threshold`.

- [ ] **Step 5: Generate at most three ranked boundary candidates**

Choose the active run containing the anchor; if none contains it, consider only a run whose nearest endpoint is within one second of the anchor. Expand the run to the nearest stable samples before and after it. Produce up to three combinations using the closest and next-closest stable boundaries.

Calculate candidate confidence as:

```python
confidence = (
    0.30 * start_settle
    + 0.35 * end_settle
    + 0.20 * peak_inclusion
    + 0.15 * boundary_quiet
)
```

All components are clamped to `[0, 1]`. If motion remains active at an analysis edge, set the matching settle component to `0.0` and cap confidence at `0.60`.

- [ ] **Step 6: Run action-boundary and transition regressions**

Run:

```powershell
uv run pytest -q tests/test_action_boundary.py tests/test_temporal_evidence.py tests/test_transition_guard.py
```

Expected: all pass; slow pan remains preserved in both transition and action classifications.

- [ ] **Step 7: Commit Task 2**

```powershell
git add app/services/action_boundary.py tests/action_media_fixtures.py tests/test_action_boundary.py
git commit -m "feat: detect action motion boundaries"
```

---

### Task 3: Finalize Duration, Split, Loop, and Fallback Policy

**Files:**
- Modify: `app/services/action_boundary.py`
- Create: `app/services/action_candidates.py`
- Create: `tests/test_action_candidates.py`
- Modify: `tests/test_action_boundary.py`

**Interfaces:**
- Consumes: `ActionMotionAnalysis`, `TemporalEvidence`, and `ActionBoundaryConfig`.
- Produces: `ActionSegment(start_s, end_s, peak_s, reason, needs_rescore)`.
- Produces: `ActionBoundaryResult` with final segments and all serializable action metadata.
- Produces: `finalize_action_analysis(analysis, evidence, safe_start_s, safe_end_s, anchor_ts_s, selected_candidate_index, config) -> ActionBoundaryResult`.
- Produces: `build_action_clips(clip, action_result, scored_frames, min_duration_s) -> list[dict]`.

- [ ] **Step 1: Write failing duration, split, loop, and fan-out tests**

```python
def test_complete_three_second_action_is_not_padded_past_safe_context():
    candidate = ActionBoundaryCandidate(
        start_s=2.0,
        peak_s=3.0,
        end_s=5.0,
        confidence=0.9,
        start_settle=1.0,
        end_settle=1.0,
        peak_inclusion=1.0,
        boundary_quiet=1.0,
    )
    analysis = ActionMotionAnalysis(
        motion_type="subject_action",
        candidates=(candidate,),
        residual_curve=((2.0, 0.0), (3.0, 0.2), (5.0, 0.0)),
        active_runs=((2.0, 5.0),),
        stable_valleys=(),
        confidence=0.9,
    )
    result = finalize_action_analysis(
        analysis,
        make_flat_evidence(0.0, 8.0),
        0.0,
        8.0,
        3.0,
        0,
        ACTION_CFG,
    )

    segment = result.segments[0]
    assert 2.0 <= segment.end_s - segment.start_s < 4.0
    assert segment.start_s <= 2.0
    assert segment.end_s >= 5.0


def test_twenty_five_second_action_splits_at_stable_valley():
    candidate = ActionBoundaryCandidate(
        start_s=0.0,
        peak_s=8.0,
        end_s=25.0,
        confidence=0.9,
        start_settle=0.8,
        end_settle=0.9,
        peak_inclusion=1.0,
        boundary_quiet=0.8,
    )
    analysis = ActionMotionAnalysis(
        motion_type="subject_action",
        candidates=(candidate,),
        residual_curve=((0.0, 0.1), (8.0, 0.3), (13.0, 0.0), (20.0, 0.3), (25.0, 0.0)),
        active_runs=((0.0, 12.5), (13.5, 25.0)),
        stable_valleys=(13.0,),
        confidence=0.9,
    )
    result = finalize_action_analysis(
        analysis,
        make_flat_evidence(0.0, 30.0),
        0.0,
        30.0,
        8.0,
        0,
        ACTION_CFG,
    )

    assert len(result.segments) == 2
    assert all(segment.end_s - segment.start_s <= 20.0 for segment in result.segments)
    assert result.action_split_reason == "stable_motion_valley"
```

Define `make_flat_evidence(start_s, end_s)` in `tests/test_action_candidates.py` with 4 FPS timestamped gray/HSV frames, identity affine metrics, and zero residual maps. Add tests proving: 4–12 seconds stays one segment; a 12–20 second complete action stays whole; no reliable split returns `fallback_fixed`; pre-roll is 0.4 seconds and post-roll is 0.6 seconds when safe; loop adjustment never moves inside the detected core; all fields pass `json.dumps(result.to_dict(), allow_nan=False)`; and each fan-out child selects the highest-scored in-range frame.

- [ ] **Step 2: Run tests and confirm missing finalization interfaces**

Run:

```powershell
uv run pytest -q tests/test_action_boundary.py tests/test_action_candidates.py
```

Expected: failures name `finalize_action_analysis`, `ActionBoundaryResult`, and `build_action_clips`.

- [ ] **Step 3: Implement final action result and completeness score**

Use these serializable fields:

```python
@dataclass(frozen=True)
class ActionBoundaryResult:
    action_boundary_mode: str
    safe_start_s: float
    safe_end_s: float
    anchor_ts_s: float
    boundary_candidates: tuple[ActionBoundaryCandidate, ...]
    segments: tuple[ActionSegment, ...]
    action_start_ts: float | None
    action_peak_ts: float | None
    action_end_ts: float | None
    action_completeness_score: float | None
    action_boundary_confidence: float
    loop_quality_score: float | None
    action_split_reason: str | None
    action_vlm_verified: bool
    action_fallback_reason: str | None
    action_analysis_version: int = 1
    diagnostics: dict[str, float | int | str | None] = field(default_factory=dict)
    analysis_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

Import `asdict`, `field` from `dataclasses`. Construct results only through the
validated finalizer so every non-null float, including values nested in
`boundary_candidates` and `diagnostics`, is finite before `to_dict()` is
called.

Completeness uses:

```python
score = (
    0.25 * candidate.start_settle
    + 0.30 * candidate.end_settle
    + 0.20 * candidate.peak_inclusion
    + 0.15 * candidate.boundary_quiet
    + 0.10 * semantic_confirmation
)
```

Use `semantic_confirmation=0.5` when VLM was not called. Use `None` for `ambient_camera_motion` and `fallback_fixed`.

- [ ] **Step 4: Implement exact duration and split policy**

Apply 0.4 seconds before and 0.6 seconds after the detected core, clamped to the safe segment. Do not force a complete 2–4 second action to four seconds. Keep complete 12–20 second actions whole.

For a window over 20 seconds, split at the highest-ranked stable valleys so every child is at most 20 seconds and at least `min_duration_s`. Prefer valleys nearest a balanced partition, then higher stability, then earlier timestamp for deterministic ties. If no valid split exists, return one fixed-window fallback centered with the shared 40/60 bias and `action_fallback_reason="long_action_split_fallback"`.

- [ ] **Step 5: Implement loop scoring without trimming the core**

Evaluate candidate endpoint pairs only inside the pre/post padding. Use:

```python
loop_score = (
    0.40 * structural_similarity
    + 0.25 * color_similarity
    + 0.20 * subject_position_similarity
    + 0.15 * motion_direction_continuity
)
```

Search at action FPS within `loop_adjust_s` of each padded endpoint. A proposed start must remain at or before the detected action start; a proposed end must remain at or after the detected action end. Reject combinations outside the safe segment or above `max_duration_s`.

- [ ] **Step 6: Implement pure candidate fan-out**

`build_action_clips()` copies transition metadata; writes
`action_start_ts/action_peak_ts/action_end_ts`, completeness, confidence, loop,
split, VLM, fallback, version, and JSON-safe diagnostics; sets
`guarded_export_window=True`; and assigns `needs_rescore=True` only when a
segment contains no scored frame. It filters any child outside
`[min_duration_s, max_duration_s]`. It may only move loop endpoints through the
buffer search above and contains no reverse or ping-pong export mode.

- [ ] **Step 7: Run focused policy tests**

Run:

```powershell
uv run pytest -q tests/test_action_boundary.py tests/test_action_candidates.py tests/test_gif_windows.py
```

Expected: all pass and existing guarded transition windows remain immutable.

- [ ] **Step 8: Commit Task 3**

```powershell
git add app/services/action_boundary.py app/services/action_candidates.py tests/test_action_boundary.py tests/test_action_candidates.py
git commit -m "feat: finalize complete action segments"
```

---

### Task 4: Add Constrained VLM Sequence Verification

**Files:**
- Create: `app/services/action_vlm.py`
- Create: `tests/test_action_vlm.py`

**Interfaces:**
- Consumes: `TemporalEvidence`, `ActionBoundaryCandidate`, and the existing JSON guard.
- Produces: `ActionVlmDecision(selected_candidate_index, action_label, first_phase, anchor_phase, last_phase, complete, confidence, reason)`.
- Produces: `build_action_contact_sheet(evidence, candidates, min_frames=6, max_frames=8) -> bytes`.
- Produces: `build_action_verification_prompt(candidates, frame_labels) -> str`.
- Produces: `parse_action_vlm_decision(raw_text, candidate_count) -> ActionVlmDecision | None`.
- Produces: `verify_action_candidates(evidence, candidates, generator) -> ActionVlmDecision | None`, where `generator(image_bytes, prompt) -> str`.

- [ ] **Step 1: Write strict parser and one-call tests**

```python
def test_parser_accepts_only_a_candidate_index():
    raw = json.dumps({
        "selected_candidate_index": 1,
        "action_label": "stands up",
        "first_phase": "preparation",
        "anchor_phase": "ongoing",
        "last_phase": "complete",
        "complete": True,
        "confidence": 0.82,
        "reason": "motion starts after rest and settles at the end",
    })

    decision = parse_action_vlm_decision(raw, candidate_count=2)

    assert decision is not None
    assert decision.selected_candidate_index == 1
    assert decision.complete is True


def test_verifier_calls_generator_once(evidence, candidates):
    calls = []

    def generator(image_bytes: bytes, prompt: str) -> str:
        calls.append((image_bytes, prompt))
        return json.dumps({
            "selected_candidate_index": 0,
            "action_label": "stands up",
            "first_phase": "preparation",
            "anchor_phase": "ongoing",
            "last_phase": "complete",
            "complete": True,
            "confidence": 0.82,
            "reason": "the selected range includes the full movement",
        })

    decision = verify_action_candidates(evidence, candidates, generator)

    assert decision is not None
    assert len(calls) == 1
    assert b"\xff\xd8" in calls[0][0]
```

Reject negative/out-of-range indexes, free-form timestamps, unknown phases,
booleans used as numeric confidence, non-finite confidence, missing fields,
malformed JSON, fewer than six contact-sheet frames when the evidence contains
at least six samples, and more than eight contact-sheet frames.

- [ ] **Step 2: Run tests and confirm missing module failure**

Run:

```powershell
uv run pytest -q tests/test_action_vlm.py
```

Expected: collection fails because `app.services.action_vlm` does not exist.

- [ ] **Step 3: Implement contact-sheet construction**

Select the first frame, anchor-nearest frame, last frame, and evenly spaced
interior frames, deduplicated by sample index. Use six frames when at least six
samples exist and at most eight; for shorter evidence, use every available
frame. Render each frame with a visible index and relative timestamp. Encode one
JPEG using Pillow and return its bytes.

- [ ] **Step 4: Implement the constrained prompt and parser**

The prompt must list candidate index plus start/peak/end frame indexes and require exactly these JSON keys:

```json
{
  "selected_candidate_index": 0,
  "action_label": "stands up",
  "first_phase": "preparation",
  "anchor_phase": "ongoing",
  "last_phase": "complete",
  "complete": true,
  "confidence": 0.82,
  "reason": "the selected range includes preparation, movement, and settling"
}
```

Allowed phases are `preparation`, `ongoing`, `recovery`, `complete`, and `unknown`. Parse through `parse_json_response()`, enforce strict field types, clamp no values silently, and return `None` on any invalid response.

When the accepted VLM candidate and the top CV candidate share the same action
peak and their start/end values differ by no more than one second each, the
finalizer uses their wider envelope if it remains transition-safe and no longer
than 20 seconds. Larger disagreement uses the VLM-selected candidate only when
its confidence meets the configured threshold; otherwise it follows the
transition-clamped fixed-window fallback.

- [ ] **Step 5: Run VLM unit tests**

Run:

```powershell
uv run pytest -q tests/test_action_vlm.py
```

Expected: all pass without a network connection.

- [ ] **Step 6: Commit Task 4**

```powershell
git add app/services/action_vlm.py tests/test_action_vlm.py
git commit -m "feat: verify action boundaries with constrained VLM"
```

---

### Task 5: Build the Shared Transition-to-Action Candidate Materializer

**Files:**
- Create: `app/services/action_pipeline.py`
- Create: `tests/test_action_pipeline.py`

**Interfaces:**
- Consumes: transition guard, temporal cache, CV action analysis, VLM verifier, action finalization, and action candidate fan-out.
- Produces: `ActionMaterialization(clips, transition_metrics, action_metrics)`.
- Produces: `materialize_action_candidates(*, video_path, clip, scored_frames, total_duration_s, config, evidence_cache, frame_scorer, sequence_generator) -> ActionMaterialization`.
- Callback: `frame_scorer(timestamp_s: float, label: str) -> dict | None`.
- Callback: `sequence_generator(image_bytes: bytes, prompt: str) -> str`.

- [ ] **Step 1: Write failing orchestration-order and fallback tests**

```python
class FixedEvidenceCache:
    def __init__(self, evidence):
        self.evidence = evidence

    def scan(self, video_path, start_s, end_s, config):
        return self.evidence.slice(start_s, end_s)


def test_materializer_runs_transition_before_action(monkeypatch):
    events = []
    evidence = make_flat_evidence(0.0, 30.0)
    clip = {
        "start_ts": 10.0,
        "end_ts": 16.0,
        "best_frame_ts": 13.0,
        "frame_count": 1,
        "gif_worthiness": 0.9,
    }
    scored_frames = [{
        "timestamp": 13.0,
        "path": "frame-13.jpg",
        "gif_worthiness": 0.9,
    }]

    def fake_guard(video_path, start_s, end_s, anchor_ts_s, config, temporal_evidence=None):
        events.append("transition")
        segment = GuardSegment(10.0, 16.0, "clean")
        return TransitionGuardResult(
            transition_action="keep",
            segments=(segment,),
            boundaries=(),
            hard_cut_count=0,
            soft_transition_count=0,
            motion_type="static_or_local_motion",
            transition_risk=0.0,
            guard_reason="clean",
            anchor_segment=segment,
        )

    def fake_action(evidence, safe_start_s, safe_end_s, anchor_ts_s, config):
        events.append("action")
        candidate = ActionBoundaryCandidate(
            start_s=11.0,
            peak_s=13.0,
            end_s=15.0,
            confidence=0.9,
            start_settle=1.0,
            end_settle=1.0,
            peak_inclusion=1.0,
            boundary_quiet=1.0,
        )
        return ActionMotionAnalysis(
            motion_type="subject_action",
            candidates=(candidate,),
            residual_curve=((11.0, 0.0), (13.0, 0.2), (15.0, 0.0)),
            active_runs=((11.0, 15.0),),
            stable_valleys=(),
            confidence=0.9,
        )

    monkeypatch.setattr(action_pipeline, "guard_candidate_window", fake_guard)
    monkeypatch.setattr(action_pipeline, "analyze_action_motion", fake_action)

    result = materialize_action_candidates(
        video_path="source.mp4",
        clip=clip,
        scored_frames=scored_frames,
        total_duration_s=60.0,
        config=ACTION_PIPELINE_CFG,
        evidence_cache=FixedEvidenceCache(evidence),
        frame_scorer=lambda timestamp_s, label: {
            "timestamp": timestamp_s,
            "path": f"{label}.jpg",
            "gif_worthiness": 0.8,
        },
        sequence_generator=lambda image_bytes, prompt: json.dumps({
            "selected_candidate_index": 0,
            "action_label": "movement",
            "first_phase": "preparation",
            "anchor_phase": "ongoing",
            "last_phase": "complete",
            "complete": True,
            "confidence": 0.8,
            "reason": "complete",
        }),
    )

    assert events == ["transition", "action"]
    assert result.clips
    assert all(candidate["guarded_export_window"] for candidate in result.clips)


def test_unverified_action_uses_transition_clamped_fixed_window(monkeypatch):
    evidence = make_flat_evidence(0.0, 30.0)
    segment = GuardSegment(10.0, 16.0, "clean")
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: TransitionGuardResult(
            transition_action="keep",
            segments=(segment,),
            boundaries=(),
            hard_cut_count=0,
            soft_transition_count=0,
            motion_type="static_or_local_motion",
            transition_risk=0.0,
            guard_reason="clean",
            anchor_segment=segment,
        ),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: ActionMotionAnalysis(
            motion_type="unknown",
            candidates=(),
            residual_curve=(),
            active_runs=(),
            stable_valleys=(),
            confidence=0.0,
            analysis_error="unverified",
        ),
    )
    result = materialize_action_candidates(
        video_path="source.mp4",
        clip={
            "start_ts": 10.0,
            "end_ts": 16.0,
            "best_frame_ts": 15.0,
            "frame_count": 1,
            "gif_worthiness": 0.9,
        },
        scored_frames=[{
            "timestamp": 15.0,
            "path": "frame-15.jpg",
            "gif_worthiness": 0.9,
        }],
        total_duration_s=60.0,
        config=ACTION_PIPELINE_CFG,
        evidence_cache=FixedEvidenceCache(evidence),
        frame_scorer=lambda timestamp_s, label: None,
        sequence_generator=lambda image_bytes, prompt: "",
    )

    assert result.clips[0]["action_boundary_mode"] == "fallback_fixed"
    assert 10.0 <= result.clips[0]["start_ts"]
    assert result.clips[0]["end_ts"] <= 16.0
```

Also verify: 30-second analysis uses 40/60 bias; split children rescore only when
needed; VLM is called at most once; VLM is skipped for
`ambient_camera_motion`; action-disabled mode preserves transition-only
behavior; and action metrics count `input`, `output`, `cv`, `extended`,
`trimmed`, `split`, `ambient_motion`, `vlm_checked`, `vlm_succeeded`,
`vlm_failed`, `fallback`, and `low_loop_quality`, plus finite `cv_ms`, `vlm_ms`,
and `total_ms` timings. Assert fallback reasons are grouped into a serializable
`fallback_reasons` mapping.

- [ ] **Step 2: Run tests and confirm missing orchestration module**

Run:

```powershell
uv run pytest -q tests/test_action_pipeline.py
```

Expected: collection fails because `app.services.action_pipeline` does not exist.

- [ ] **Step 3: Implement analysis-window and callback types**

Use:

```python
FrameScorer = Callable[[float, str], dict[str, Any] | None]
SequenceGenerator = Callable[[bytes, str], str]


@dataclass(frozen=True)
class ActionMaterialization:
    clips: tuple[dict[str, Any], ...]
    transition_metrics: dict[str, int]
    action_metrics: dict[str, int | float | dict[str, int]]
```

Build the analysis window as:

```python
duration = min(config.analysis_window_s, total_duration_s)
start_s = max(0.0, anchor_ts_s - duration * 0.4)
start_s = min(start_s, total_duration_s - duration)
end_s = start_s + duration
```

- [ ] **Step 4: Implement shared orchestration**

For one merged clip:

1. Scan the analysis window once through the per-video `TemporalEvidenceCache`.
2. Call `guard_candidate_window()` with `temporal_evidence=evidence`.
3. For each viable transition segment, call `analyze_action_motion()` on `evidence.slice(segment.start_s, segment.end_s)`.
4. If CV confidence is below threshold and worthiness is at least the VLM threshold, call `verify_action_candidates()` once.
5. Pass the selected index or `None` into `finalize_action_analysis()`.
6. Fan out with `build_action_clips()`.
7. Call `frame_scorer()` for children marked `needs_rescore`; discard only the child if scoring returns `None`.
8. Return immutable tuples plus counter dictionaries.

An exception in action analysis is converted to a transition-clamped fixed-window result and increments `fallback`; an unreadable source remains a task-level media error only when the existing transition guard also cannot verify any safe segment.

- [ ] **Step 5: Run all service-level action tests**

Run:

```powershell
uv run pytest -q tests/test_temporal_evidence.py tests/test_transition_guard.py tests/test_action_boundary.py tests/test_action_candidates.py tests/test_action_vlm.py tests/test_action_pipeline.py
```

Expected: all pass; no HTTP requests leave the tests.

- [ ] **Step 6: Commit Task 5**

```powershell
git add app/services/action_pipeline.py tests/test_action_pipeline.py
git commit -m "feat: materialize transition-safe action clips"
```

---

### Task 6: Freeze Validated Configuration and Expose Two UI Controls

**Files:**
- Modify: `configs/models.yaml`
- Modify: `configs/models.adult_candidate.yaml`
- Modify: `scripts/test_video_adaptive.py`
- Modify: `app/ui/tabs/settings.py`
- Modify: `tests/test_adaptive_config.py`
- Modify: `tests/test_config_help_annotations.py`
- Modify: `tests/test_tasks_api.py`

**Interfaces:**
- Consumes: `ActionBoundaryConfig.from_mapping(values, strict=True)`.
- Produces: flat action keys in `extract_config()`.
- Produces: two visible settings in the existing config load/save/reload order.

- [ ] **Step 1: Change config tests first**

Replace the old default-10 assertion and add:

```python
def test_adaptive_action_defaults_are_frozen():
    cfg = extract_config({"adaptive": {}})

    assert cfg["min_duration"] == 2.0
    assert cfg["max_duration"] == 20.0
    assert cfg["action_guard_enabled"] is True
    assert cfg["action_vlm_verify_enabled"] is True
    assert cfg["action_analysis_version"] == 1
    assert cfg["action_analysis_window_s"] == 30.0
    assert cfg["action_preferred_min_duration_s"] == 4.0
    assert cfg["action_preferred_max_duration_s"] == 12.0
    assert cfg["action_scan_fps"] == 4.0
    assert cfg["action_boundary_confidence_threshold"] == 0.65
    assert cfg["action_loop_adjust_s"] == 0.75
    assert cfg["action_vlm_min_worthiness"] == 0.60
    assert cfg["action_fallback_mode"] == "fixed_window"
```

Add invalid-relationship cases and assert `ValueError` includes the offending key. Update config help expectations from 25 to 27 keys and require Chinese help for both new checkboxes.

- [ ] **Step 2: Run config tests and confirm expected failures**

Run:

```powershell
uv run pytest -q tests/test_adaptive_config.py tests/test_config_help_annotations.py tests/test_tasks_api.py
```

Expected: failures show max duration 10, absent action keys, and the old UI field count.

- [ ] **Step 3: Update both YAML presets and extraction defaults**

Set:

```yaml
adaptive:
  min_duration: 2
  max_duration: 20
  action_guard_enabled: true
  action_vlm_verify_enabled: true
  action_analysis_version: 1
  action_analysis_window_s: 30
  action_preferred_min_duration_s: 4
  action_preferred_max_duration_s: 12
  action_scan_fps: 4
  action_boundary_confidence_threshold: 0.65
  action_loop_adjust_s: 0.75
  action_vlm_min_worthiness: 0.60
  action_fallback_mode: fixed_window
```

In `extract_config()`, parse these exact keys and call strict validation before
returning. Do not read action behavior from environment variables. Serialize
the normalized action subset in sorted-key compact JSON and expose its SHA-256
as `action_config_hash`; the same values must produce the same hash in direct
and staged execution.

- [ ] **Step 4: Add two settings checkboxes with Chinese help**

Add `adaptive.action_guard_enabled` and `adaptive.action_vlm_verify_enabled` immediately after the existing transition controls. Update:

- `CONFIG_FIELD_KEYS` and `CONFIG_FIELD_HELP`;
- checkbox tooltip selectors in `CONFIG_TOOLTIP_JS`;
- `load_config()` adaptive field count and order;
- `save_config()` parameters and YAML writes;
- `all_inputs` save/reload order.

Before writing YAML, validate the combined adaptive mapping. On invalid values return a Chinese error message and leave the file unchanged.

- [ ] **Step 5: Verify task config freezing**

Extend `tests/test_tasks_api.py` so a created job snapshot contains all action
keys and its config hash changes when an action threshold changes. Assert the
snapshot's `action_config_hash` matches a direct `extract_config()` call over
the same mapping. The persisted snapshot, not the process environment, must be
the value consumed by stages.

- [ ] **Step 6: Run config/UI/task tests**

Run:

```powershell
uv run pytest -q tests/test_adaptive_config.py tests/test_config_help_annotations.py tests/test_tasks_api.py tests/task_engine/test_control_config_snapshot.py
```

Expected: all pass.

- [ ] **Step 7: Commit Task 6**

```powershell
git add configs/models.yaml configs/models.adult_candidate.yaml scripts/test_video_adaptive.py app/ui/tabs/settings.py tests/test_adaptive_config.py tests/test_config_help_annotations.py tests/test_tasks_api.py
git commit -m "feat: configure action completeness guard"
```

---

### Task 7: Integrate the Shared Action Materializer into Direct Mode

**Files:**
- Modify: `scripts/test_video_adaptive.py`
- Create: `tests/test_adaptive_direct_action.py`
- Modify: `tests/test_adaptive_direct_transition.py`
- Modify: `tests/test_gif_windows.py`

**Interfaces:**
- Consumes: `materialize_action_candidates()` from Task 5 and frozen VLM runtime config.
- Produces: direct result `action_guard` metrics and per-clip action metadata.

- [ ] **Step 1: Write a failing direct pipeline ordering test**

```python
def test_direct_action_split_happens_before_dedup(tmp_path, monkeypatch):
    materialized = ActionMaterialization(
        clips=(
            {
                "start_ts": 2.0,
                "end_ts": 7.0,
                "best_frame_ts": 5.0,
                "best_frame_path": "frame-5.jpg",
                "frame_count": 1,
                "gif_worthiness": 0.90,
                "caption": "first action stage",
                "emotional_core": "energy",
                "guarded_export_window": True,
                "action_boundary_mode": "cv",
                "action_completeness_score": 0.88,
                "action_boundary_confidence": 0.84,
                "loop_quality_score": 0.64,
                "action_split_index": 1,
                "action_split_count": 2,
                "action_split_reason": "stable_motion_valley",
                "action_vlm_verified": False,
                "action_fallback_reason": None,
                "action_analysis_version": 1,
            },
            {
                "start_ts": 8.0,
                "end_ts": 14.0,
                "best_frame_ts": 11.0,
                "best_frame_path": "frame-11.jpg",
                "frame_count": 1,
                "gif_worthiness": 0.89,
                "caption": "second action stage",
                "emotional_core": "energy",
                "guarded_export_window": True,
                "action_boundary_mode": "cv",
                "action_completeness_score": 0.86,
                "action_boundary_confidence": 0.82,
                "loop_quality_score": 0.61,
                "action_split_index": 2,
                "action_split_count": 2,
                "action_split_reason": "stable_motion_valley",
                "action_vlm_verified": False,
                "action_fallback_reason": None,
                "action_analysis_version": 1,
            },
        ),
        transition_metrics={
            "input": 1,
            "split": 0,
            "trim": 0,
            "drop": 0,
            "unverified": 0,
            "hard_cut": 0,
            "soft_transition": 0,
            "motion": 1,
        },
        action_metrics={
            "input": 1,
            "output": 2,
            "cv": 1,
            "extended": 1,
            "trimmed": 0,
            "split": 1,
            "ambient_motion": 0,
            "vlm_checked": 0,
            "vlm_succeeded": 0,
            "vlm_failed": 0,
            "fallback": 0,
            "low_loop_quality": 0,
            "cv_ms": 4.0,
            "vlm_ms": 0.0,
            "total_ms": 5.0,
            "fallback_reasons": {},
        },
    )
    monkeypatch.setattr(
        test_video_adaptive,
        "materialize_action_candidates",
        lambda **kwargs: materialized,
    )

    result = _run_direct_pipeline_fixture(
        tmp_path,
        monkeypatch,
        max_output=2,
    )

    assert result["dedup_input_clips"] == 2
    assert len(result["top_clips"]) == 2
    assert result["action_guard"]["split"] == 1
    assert all(clip["guarded_export_window"] for clip in result["top_clips"])
```

Define `_run_direct_pipeline_fixture()` in
`tests/test_adaptive_direct_transition.py` by extracting the deterministic setup
already present in
`test_direct_pipeline_fans_guarded_segments_out_before_dedup()`: fake
`ffprobe` returns `16.0`, JPEG extraction writes the same 32×32 source image,
model lifecycle and sleeps are no-ops, frame VLM scoring returns worthiness
`0.9`, GIF export writes `GIF89a`, embeddings are deterministic, and the
ranker records its input. The helper accepts `max_output`, applies it to
`_cfg()`, calls `run_pipeline()`, and returns the result. Import this helper in
`tests/test_adaptive_direct_action.py`; keep the original transition test
calling it too, so the fixture refactor itself is regression-covered.

Add tests proving the configured VLM endpoint/model serves sequence verification, only one sequence call occurs per candidate, fallback windows remain inside transition segments, and final direct FFmpeg durations never exceed 20 seconds.

- [ ] **Step 2: Run direct tests and confirm the action phase is absent**

Run:

```powershell
uv run pytest -q tests/test_adaptive_direct_action.py tests/test_adaptive_direct_transition.py tests/test_gif_windows.py
```

Expected: new action assertions fail while existing transition tests remain green.

- [ ] **Step 3: Replace direct phase 2.65 with shared materialization**

Create one `TemporalEvidenceCache` per `run_pipeline()` call. For every merged clip, call `materialize_action_candidates()` before embedding or temporal dedup.

The direct `frame_scorer` callback:

1. extracts a 640px representative JPEG at the requested timestamp;
2. calls `_score_vlm_frame()` with `vlm_runtime.model` and `vlm_runtime.base_url`;
3. returns the parsed frame payload or `None`.

The sequence callback posts the contact-sheet bytes to the same configured endpoint/model with the action prompt and existing VLM options. It returns the raw response text; parsing remains in `action_vlm.py`.

- [ ] **Step 4: Preserve exact final windows and emit action metadata**

Every action-materialized candidate uses its exact `start_ts/end_ts` in FFmpeg. Extend `gif_export_results`, `top_clips`, and the result root with:

- `action_guard` counters;
- boundary mode;
- completeness, confidence, and loop scores;
- split index/count/reason;
- VLM verification and fallback reason;
- action analysis version.
- `action_config_hash`, action input/output counts, and finite CV/VLM/total timings.

Maintain the existing transition fields and PotPlayer bookmark times.

- [ ] **Step 5: Run direct, transition, and export tests**

Run:

```powershell
uv run pytest -q tests/test_adaptive_direct_action.py tests/test_adaptive_direct_transition.py tests/test_action_pipeline.py tests/test_gif_windows.py tests/test_transition_guard.py
```

Expected: all pass.

- [ ] **Step 6: Commit Task 7**

```powershell
git add scripts/test_video_adaptive.py tests/test_adaptive_direct_action.py tests/test_adaptive_direct_transition.py tests/test_gif_windows.py
git commit -m "feat: guard direct GIF action completeness"
```

---

### Task 8: Integrate Staged Mode and Version Action Manifests

**Files:**
- Modify: `scripts/test_video_adaptive.py`
- Modify: `app/task_engine/artifacts.py`
- Modify: `tests/task_engine/test_manifest_validation.py`
- Modify: `tests/task_engine/test_full_production_stage_chain.py`
- Modify: `tests/test_gif_windows.py`

**Interfaces:**
- Consumes: the same action materializer and callbacks used by direct mode.
- Produces: `rank_dedup_manifest` schema v2 with root `action_guard`.
- Produces: `gif_clip_manifest` schema v2 with final action fields.
- Retains: schema v1 readers for historical artifacts.

- [ ] **Step 1: Write failing staged and manifest-v2 tests**

Add:

```python
def test_rank_manifest_v2_requires_action_metadata():
    manifest = {
        "schema_version": 2,
        "stage": "rank_dedup",
        "clip_count": 1,
        "clips": [{
            "clip_id": "clip-1",
            "start_ts": 2.0,
            "end_ts": 8.0,
        }],
        "action_guard": {},
    }

    with pytest.raises(ValueError, match="action_boundary_mode"):
        validate_manifest_json(
            json.dumps(manifest).encode("utf-8"),
            "rank_dedup_manifest",
        )


# Inside test_rank_dedup_transition_guard_and_gif_max_duration(), after its
# existing rank stage and gif_clip calls:
assert rank["schema_version"] == 2
assert rank["action_guard"]["input"] == len(synth_manifest["clips"])
for path in gif_manifests:
    gif_manifest = json.loads(path.read_text(encoding="utf-8"))
    rank_clip = next(
        clip for clip in rank["clips"]
        if clip["clip_id"] == gif_manifest["clip_id"]
    )
    assert gif_manifest["schema_version"] == 2
    assert gif_manifest["start_ts"] == rank_clip["start_ts"]
    assert gif_manifest["end_ts"] == rank_clip["end_ts"]
    assert gif_manifest["action_boundary_mode"] == rank_clip["action_boundary_mode"]
```

Do not add a hidden pytest fixture: place these assertions in the existing
staged hard-cut test, whose concrete `synth_manifest`, `rank`, and
`gif_manifests` variables are shown immediately above in that file. Add the
action config keys from Task 6 to its existing `cfg` dictionary.

Also assert schema v1 rank/gif manifests still validate, schema v2 clip IDs
remain unique after action split, 2–20 second bounds are enforced, no action
child crosses the synthetic hard cut, and direct/staged execution of the same
fixture produces identical `(start_ts, end_ts, action_split_index,
action_split_count)` tuples before ranking.

- [ ] **Step 2: Run staged tests and confirm schema/action failures**

Run:

```powershell
uv run pytest -q tests/task_engine/test_manifest_validation.py tests/task_engine/test_full_production_stage_chain.py tests/test_gif_windows.py
```

Expected: v2 is unsupported and staged manifests lack action fields.

- [ ] **Step 3: Add per-kind schema-v2 validation**

In `_MANIFEST_VALIDATORS` set:

```python
"rank_dedup_manifest": {
    "versions": [1, 2],
    "required_fields": ["schema_version", "stage", "clips", "clip_count"],
},
"gif_clip_manifest": {
    "versions": [1, 2],
    "required_fields": ["schema_version", "stage", "clip_id", "gif_path"],
},
```

For schema v2:

- require root `action_guard` in rank manifests, including
  `action_config_hash`, `action_analysis_version`, action input/output counts,
  and finite CV/VLM/total timings;
- require every rank clip to contain `action_boundary_mode`, `action_boundary_confidence`, `action_vlm_verified`, `action_analysis_version`, `guarded_export_window`, `start_ts`, and `end_ts`;
- require gif manifests to contain the same action fields plus final `start_ts/end_ts`;
- validate finite numeric or `null` scores;
- require `2.0 <= end_ts - start_ts <= 20.0`;
- continue validating v1 exactly as before.

- [ ] **Step 4: Replace staged rank/dedup transition loop with shared action materialization**

Instantiate one evidence cache per `_stage_rank_dedup()` call. Use the frozen `config_data` VLM endpoint/model in both frame and sequence callbacks. Materialize action clips before embedding, temporal dedup, stable clip IDs, and `max_output`.

Write schema v2 even for zero clips, with zeroed `transition_guard` and `action_guard` counters.

- [ ] **Step 5: Preserve action fields through `gif_clip`**

When `guarded_export_window=True`, `_stage_gif_clip()` uses exact bounds and validates the 2–20 second invariant. Legacy unmarked v1 clips continue through `build_export_window()`.

The v2 gif manifest copies action metadata from the rank clip and records actual FFmpeg bounds, duration, size, SHA-256, and status.

- [ ] **Step 6: Run staged production regressions**

Run:

```powershell
uv run pytest -q tests/task_engine/test_manifest_validation.py tests/task_engine/test_full_production_stage_chain.py tests/task_engine/test_production_artifact_contract.py tests/test_gif_windows.py
```

Expected: all pass; v1 compatibility and v2 strictness are both covered.

- [ ] **Step 7: Commit Task 8**

```powershell
git add scripts/test_video_adaptive.py app/task_engine/artifacts.py tests/task_engine/test_manifest_validation.py tests/task_engine/test_full_production_stage_chain.py tests/test_gif_windows.py
git commit -m "feat: guard staged GIF action completeness"
```

---

### Task 9: Surface Action Evidence in Candidate Review

**Files:**
- Modify: `scripts/import_adaptive_candidates.py`
- Modify: `app/routers/candidates.py`
- Modify: `app/ui/tabs/review.py`
- Modify: `tests/test_candidates_api.py`
- Modify: `tests/test_candidate_review_layout.py`

**Interfaces:**
- Stores: action summary inside existing `candidate_gifs.vlm_summary_json`; no database migration.
- Produces: candidate API `action_summary`.
- Produces: compact Review label text.

- [ ] **Step 1: Write failing import/API/UI tests**

```python
def test_adaptive_import_preserves_action_summary():
    payload = _build_run_candidate(
        {
            "rank": 1,
            "start_ts": 4.0,
            "end_ts": 10.0,
            "action_boundary_mode": "hybrid_vlm",
            "action_completeness_score": 0.86,
            "action_boundary_confidence": 0.78,
            "loop_quality_score": 0.62,
            "action_split_index": 1,
            "action_split_count": 2,
            "action_vlm_verified": True,
        },
        "source.mp4",
        "a" * 64,
    )

    assert payload["vlm_summary"]["action"]["completeness"] == 0.86
    assert payload["vlm_summary"]["action"]["split"] == "1/2"


def test_review_label_contains_compact_action_summary():
    label = candidate_gallery_label({
        "candidate_id": "cand-1",
        "status": "candidate",
        "start_sec": 4.0,
        "end_sec": 10.0,
        "action_summary": {
            "mode": "hybrid_vlm",
            "completeness": 0.86,
            "confidence": 0.78,
            "loop_quality": 0.62,
            "split": "1/2",
            "vlm_verified": True,
        },
    })

    assert "动作完整 0.86" in label
    assert "循环 0.62" in label
    assert "拆分 1/2" in label
```

Also verify malformed or legacy `{}` summaries produce the original label without errors.

- [ ] **Step 2: Run candidate review tests**

Run:

```powershell
uv run pytest -q tests/test_candidates_api.py tests/test_candidate_review_layout.py
```

Expected: failures show action metadata is not selected or rendered.

- [ ] **Step 3: Persist and expose action summary safely**

In `_build_run_candidate()`, place normalized finite fields under `vlm_summary["action"]`. In `_candidate_rows()`, select `vlm_summary_json`. Parse it in `_row_payload()` with a helper that returns `{}` for invalid JSON or non-object values.

Return:

```python
"action_summary": {
    "mode": "hybrid_vlm",
    "completeness": 0.86,
    "confidence": 0.78,
    "loop_quality": 0.62,
    "split": "1/2",
    "vlm_verified": True,
}
```

Only include keys with valid values.

- [ ] **Step 4: Render compact Review text**

Extract `candidate_gallery_label(candidate)` from `load_candidate_page()`. Keep the existing status, interval, and candidate ID, then append available action items:

```text
动作完整 0.86 | 循环 0.62 | 混合检测 | 拆分 1/2
```

Map `cv`, `hybrid_vlm`, `ambient_camera_motion`, and `fallback_fixed` to concise Chinese labels. Do not add a new mandatory review action or increase page size.

- [ ] **Step 5: Run API/UI and performance smoke tests**

Run:

```powershell
uv run pytest -q tests/test_candidates_api.py tests/test_candidate_review_layout.py tests/test_candidate_review_auto_advance.py tests/test_workbench_performance.py
```

Expected: all pass; pagination remains page-local and only the current page parses summary JSON.

- [ ] **Step 6: Commit Task 9**

```powershell
git add scripts/import_adaptive_candidates.py app/routers/candidates.py app/ui/tabs/review.py tests/test_candidates_api.py tests/test_candidate_review_layout.py
git commit -m "feat: show action completeness in review"
```

---

### Task 10: Add Validation Tooling, Documentation, and Release Evidence

**Files:**
- Create: `scripts/validate_action_completeness.py`
- Create: `tests/test_action_validation.py`
- Create after running validation: `docs/reports/action-completeness-validation-2026-07-29.md`
- Modify: `README.md`
- Modify: `Agent.md`

**Interfaces:**
- Consumes a local JSON manifest containing labeled real-video intervals.
- Produces a strict JSON metrics file and Markdown validation report.

- [ ] **Step 1: Write failing evaluator tests**

Use this input schema:

```json
{
  "schema_version": 1,
  "samples": [
    {
      "sample_id": "stand-up-001",
      "category": "body_action",
      "video_path": "C:/media/source.mp4",
      "analysis_start_s": 10.0,
      "analysis_end_s": 22.0,
      "anchor_ts_s": 16.0,
      "label_start_s": 12.0,
      "label_end_s": 19.0,
      "baseline_start_s": 13.0,
      "baseline_end_s": 18.0,
      "preferred_new": true,
      "confirmed_transition_s": []
    }
  ]
}
```

Add:

```python
def test_evaluator_computes_boundary_and_duration_metrics():
    samples = [
        {
            "sample_id": "stand-up-001",
            "category": "body_action",
            "video_path": "C:/media/body.mp4",
            "analysis_start_s": 0.0,
            "analysis_end_s": 10.0,
            "anchor_ts_s": 5.0,
            "label_start_s": 2.0,
            "label_end_s": 8.0,
            "baseline_start_s": 3.0,
            "baseline_end_s": 7.0,
            "preferred_new": True,
            "confirmed_transition_s": [],
        },
        {
            "sample_id": "object-open-001",
            "category": "object_action",
            "video_path": "C:/media/object.mp4",
            "analysis_start_s": 10.0,
            "analysis_end_s": 20.0,
            "anchor_ts_s": 15.0,
            "label_start_s": 12.0,
            "label_end_s": 18.0,
            "baseline_start_s": 13.0,
            "baseline_end_s": 17.0,
            "preferred_new": True,
            "confirmed_transition_s": [],
        },
    ]
    predictions = [
        {
            "sample_id": "stand-up-001",
            "start_s": 2.25,
            "end_s": 8.50,
            "action_boundary_mode": "hybrid_vlm",
            "action_complete": True,
            "action_split_count": 1,
            "crosses_confirmed_transition": False,
            "slow_pan_false_split": False,
            "fallback": False,
        },
        {
            "sample_id": "object-open-001",
            "start_s": 11.50,
            "end_s": 18.25,
            "action_boundary_mode": "cv",
            "action_complete": True,
            "action_split_count": 1,
            "crosses_confirmed_transition": False,
            "slow_pan_false_split": False,
            "fallback": False,
        },
    ]
    report = evaluate_samples(
        samples,
        predictions,
        vlm_call_count=1,
    )

    assert report["sample_count"] == 2
    assert report["complete_within_0_75s_rate"] == 1.0
    assert report["duration_in_2_20s_rate"] == 1.0
    assert report["vlm_call_ratio"] == 0.5
    json.dumps(report, allow_nan=False)
```

Test hard-transition intersection count, candidate output ratio, preferred win rate, slow-pan false split count, fallback rate, and invalid/non-finite manifest rejection.

- [ ] **Step 2: Run evaluator tests and confirm missing script API**

Run:

```powershell
uv run pytest -q tests/test_action_validation.py
```

Expected: import fails because the validation script does not exist.

- [ ] **Step 3: Implement evaluator and CLI**

Provide:

```powershell
uv run python scripts/validate_action_completeness.py `
  --manifest data/action_validation/manifest.json `
  --predictions data/action_validation/predictions.json `
  --output-json data/action_validation/metrics.json `
  --output-md docs/reports/action-completeness-validation-2026-07-29.md
```

The evaluator must calculate exact counts and rates for:

- start/end absolute error and the 0.75-second pass rate;
- complete-action manual acceptance;
- baseline-vs-new blind preference;
- duration in 2–20 seconds and 4–12 seconds;
- hard-transition intersection;
- slow-pan false split;
- split, VLM verification, fallback, and low-loop-quality counts;
- baseline and new candidate counts plus relative output change;
- VLM call ratio.

It must state unavailable manual fields as unavailable and never convert partial evidence into a pass.

- [ ] **Step 4: Update user and agent documentation**

In `README.md`, document:

- two visible action settings;
- 4–12 second preference and 20-second hard maximum;
- transition-first action flow;
- CV/VLM fallback behavior;
- action result counters and Review summary;
- validation commands;
- non-destructive disable/rollback.

Retain the already-added roadmap checklist for pose recognition, object tracking, and deep action-recognition models.

In `Agent.md`, document the shared service files, pipeline order, frozen config keys, manifest v2 compatibility, test commands, and the rule that guarded windows are immutable at final export.

- [ ] **Step 5: Run the focused feature suite**

Run:

```powershell
uv run pytest -q `
  tests/test_temporal_evidence.py `
  tests/test_transition_guard.py `
  tests/test_transition_candidates.py `
  tests/test_action_boundary.py `
  tests/test_action_candidates.py `
  tests/test_action_vlm.py `
  tests/test_action_pipeline.py `
  tests/test_adaptive_config.py `
  tests/test_config_help_annotations.py `
  tests/test_adaptive_direct_action.py `
  tests/test_adaptive_direct_transition.py `
  tests/test_gif_windows.py `
  tests/test_candidates_api.py `
  tests/test_candidate_review_layout.py `
  tests/task_engine/test_manifest_validation.py `
  tests/task_engine/test_full_production_stage_chain.py
```

Expected: all pass.

- [ ] **Step 6: Run complete regression and packaging/import checks**

Run:

```powershell
uv run python -m compileall app scripts
uv run pytest -q
uv run python -c "from app.services.action_boundary import analyze_action_motion; from app.services.action_pipeline import materialize_action_candidates; print('action-imports-ok')"
git diff --check
```

Expected: compile/import/diff checks pass and no new pytest failure class appears. The existing Windows GBK failures in `tests/test_version_manifest.py` may remain; record their exact current count and traceback separately instead of attributing them to action completeness.

- [ ] **Step 7: Run the 30-sample real-media acceptance**

Build a local manifest with at least:

- 10 obvious body-action samples;
- 6 subtle expression/interaction samples;
- 6 coherent camera-motion samples;
- 4 long-action samples;
- 4 multi-stage or transition-adjacent samples.

Run baseline and new behavior on identical source intervals and complete the blind preference field before generating the report. Acceptance requires:

- at least 85% visually complete action starts/ends;
- at least 70% blind preference for the new window;
- zero confirmed hard transitions inside final intervals;
- zero new false splits on the coherent-camera-motion set;
- 100% final durations in 2–20 seconds;
- candidate count no more than 5% below baseline;
- VLM calls no more than 25% of action candidates.

- [ ] **Step 8: Commit validation tooling, docs, and the evidence report**

```powershell
git add scripts/validate_action_completeness.py tests/test_action_validation.py README.md Agent.md docs/reports/action-completeness-validation-2026-07-29.md
git commit -m "docs: validate action completeness guard"
```

Do not stage local media, `data/action_validation/`, databases, lock files, or prior user result JSON.

---

## Plan Self-Review Checklist

- [x] Every design requirement maps to at least one task.
- [x] The transition guard always runs before action analysis.
- [x] Direct and staged paths consume the same action materializer.
- [x] The 30-second analysis range is distinct from the 20-second export maximum.
- [x] The fixed-window fallback remains transition-clamped and output-preserving.
- [x] Loop adjustment cannot cut the action core.
- [x] Long actions have deterministic split and fallback rules.
- [x] VLM returns a candidate index, never a free timestamp.
- [x] Manifest v1 remains readable while v2 is strict.
- [x] Action metadata reaches result JSON, staged artifacts, imports, API, and Review.
- [x] The two UI switches and Chinese help preserve load/save/reload ordering.
- [x] The deep-model roadmap remains outside first-version dependencies.
- [x] Every task has a failing test, passing test command, and explicit-file commit.
