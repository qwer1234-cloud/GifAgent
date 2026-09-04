# Continuity Milestone 0A Implementation Plan

> **For GLM-5.3 Harness:** Execute this plan task-by-task and track every checkbox (`- [ ]`). If an equivalent plan-execution skill is available, use it, but no optional skill or external model access is a prerequisite.

**Goal:** Build a read-only, model-neutral continuity feasibility harness, including action-amplitude, face-identity/advisory-dynamics, historical-GIF yield, and evidence-independence contracts, while freezing the current pipeline ordering.

**Architecture:** Keep all spike code under `scripts/` and all tests under `tests/continuity_spike/`; production pipeline code, database schema, UI, and GIF business state remain unchanged. The harness validates an explicit manifest containing source-video windows and historical GIF samples, calls a pluggable experimental backend, records normalized amplitude with independent noise references, face identity/advisory dynamics, evidence-independence groups, feature-extraction yield, and read-only runtime telemetry, then writes an immutable run directory. A deterministic fake backend and generated media test the harness, but synthetic runs are always statistically ineligible.

**Tech Stack:** Python 3.11+, dataclasses and `typing.Protocol`, JSON, `argparse`, `httpx`, `subprocess`, OpenCV test fixtures, pytest, mypy.

**Spec:** `docs/superpowers/specs/2026-09-04-person-semantic-continuity-body-curve-design.md`

## GLM-5.3 Harness Execution Contract

- Execute Tasks 1–6 in order immediately; this plan has no wait, scheduling, API-key, or model-quota prerequisite.
- Work from repository root `C:\Users\sunhao\Desktop\code\GifAgent` and read the complete spec before editing.
- Use `.\.venv\Scripts\python.exe` for every Python command and first verify it reports Python 3.11 or newer. Do not use the shell's bare `python`, which may resolve to an incompatible interpreter; if the repository environment is absent, stop before changing dependencies and report that exact prerequisite.
- Preserve every pre-existing modified or untracked file not named in the current task. Stage only the exact task file list and inspect `git diff --cached --name-only` before each commit.
- Follow every red/green test step. Do not claim a task complete from code inspection or from an earlier test run.
- Never call GLM from GifAgent code. GLM-5.3 is the Harness implementation agent for this plan, not a product provider.
- Do not run live Ollama, `nvidia-smi`, or media inference from tests. Every external boundary is injected or monkeypatched.
- If a planned signature conflicts with verified current source, stop that task, record the exact file/line evidence, and revise only the plan/spec instead of improvising a production architecture.
- After each task, report changed files, focused test output, and commit hash. After Task 6, report the full verification evidence required by the handoff.

## Global Constraints

- This plan implements only milestone 0A harness code. Running the real 50-video-candidate plus 200-historical-GIF measurement is milestone 0B and remains a separate go/no-go execution after the experimental backend and explicit media manifest are supplied.
- Do not add or alter production SQLite tables, `candidate_gifs`, task-engine stages, review UI, ranking, dedup behavior, Quality MoE policy, or GIF encoding.
- Do not add Torch, ONNXRuntime, detector, pose, segmentation, ReID, or GLM dependencies.
- Milestone 0A defines action-amplitude, face identity, facial-landmark/expression advisory, and historical feature-yield fields but does not run a real pose, face, expression, or curve-feature model.
- Small action amplitude is not a reject condition. The backend must compare normalized motion against an adaptive noise floor and report phase completeness separately.
- The amplitude noise floor must come from a confidently static context interval, non-subject background residual, or both; it must never be estimated from the candidate amplitude distribution itself.
- Background camera compensation requires at least 0.30 background coverage and 0.50 affine inlier ratio. Below either value, amplitude is `UNAVAILABLE` and the candidate is reviewed.
- Face appearance embedding may add evidence to `appearance_identity`; facial landmarks and expression dynamics are advisory and never alter continuity-v1 disposition by themselves.
- `NOT_DETECTED`, `DETECTED_UNUSABLE`, expression `ABSTAIN`, and expression `UNAVAILABLE` do not block an otherwise valid candidate.
- A single amplitude, face-identity, facial-landmark, or expression anomaly cannot produce `HARD_REJECT`.
- `HARD_REJECT` requires a confirmed `scene_boundary` or at least two distinct groups from `scene_boundary`, `pose_geometry`, `appearance_identity`, `photometric_motion`, and `semantic_vlm`; multiple signals inside one group count once.
- GLM-5.3-Flash is not called by this code; later Harness review is an explicit human action.
- Source media are opened read-only and SHA-256 is verified immediately before and after each per-source batch; every result in the batch records the same verified source identity.
- `--dry-run` performs validation and prints JSON to stdout; it must not create an output directory, load a backend, call Ollama, or invoke `nvidia-smi`.
- Runtime telemetry is read-only. It may query Ollama `/api/ps` and `nvidia-smi`; it must never stop, load, or unload a model.
- A fake backend or any synthetic candidate forces `go_no_go.state=INSUFFICIENT_REAL_DATA` regardless of measured values.
- A real go/no-go report requires at least 50 real source-video candidates with non-`UNLABELED` human labels, at least 200 real historical GIFs with non-`unknown` feedback labels, and all source-video scenario buckets; even a passing report requires human review and never signs an `enforce` qualification manifest.
- Report thresholds are candidate p50 at most 10,000 ms, candidate p95 at most 30,000 ms, grouped source-video p95 at most 1,200,000 ms, peak VRAM at most 14,336 MiB, `AUTO_PASS` coverage at least 0.40, and `REVIEW_REQUIRED` rate at most 0.60.
- All output is written below a caller-owned output directory into a newly claimed `run-<uuid>` directory; no existing output file is overwritten.
- Use PowerShell-compatible commands and stage only files listed by each task because the worktree contains unrelated user changes.

## File Structure

- Modify `tests/test_adaptive_direct_transition.py`: characterize the current snap → dedup → identity → Quality MoE → rank order shared by direct mode.
- Create `scripts/continuity_spike_contracts.py`: immutable manifest/result types including amplitude/face evidence, JSON parsing, validation, source identity checks, and backend protocol.
- Create `scripts/continuity_spike_backends.py`: deterministic fake backend and explicit `module:function` backend loader.
- Create `scripts/continuity_spike_metrics.py`: percentile, scenario coverage, amplitude/face aggregation, thresholds, and go/no-go state.
- Create `scripts/continuity_spike_runtime.py`: read-only Ollama and NVIDIA telemetry parsers.
- Create `scripts/continuity_spike.py`: CLI orchestration, dry-run, immutable run directories, per-source batch isolation, and report writing.
- Create `tests/continuity_spike/test_contracts.py`: manifest and hash contract tests.
- Create `tests/continuity_spike/test_backends.py`: fake and plugin-loader tests.
- Create `tests/continuity_spike/test_metrics.py`: statistical aggregation and eligibility tests.
- Create `tests/continuity_spike/test_runtime.py`: HTTP/subprocess telemetry tests with no live services.
- Create `tests/continuity_spike/test_cli.py`: end-to-end CLI tests over generated videos.
- Create `docs/reports/2026-09-continuity-spike/README.md`: operator contract, artifact definitions, and explicit 0A limitations.

---

### Task 1: Freeze the Current Pipeline Ordering Contract

**Files:**
- Modify: `tests/test_adaptive_direct_transition.py`

**Interfaces:**
- Consumes: `_run_direct_pipeline_fixture`, `app.pipeline.stages.rank_dedup._stage_rank_dedup`, and its existing imported collaborators.
- Produces: a characterization test proving the current ordering without changing production code.

- [ ] **Step 1: Run the existing direct-transition test file as a baseline**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_adaptive_direct_transition.py -q
```

Expected: exit code 0. Record the count because unrelated worktree changes may otherwise be confused with this task.

- [ ] **Step 2: Add a characterization test that wraps the real collaborators**

Append this test, keeping wrappers around the real implementations so the fixture retains existing behavior:

```python
def test_direct_rank_dedup_preserves_snap_dedup_identity_quality_rank_order(
    tmp_path, monkeypatch,
):
    events: list[str] = []

    def track(name, function):
        def wrapped(*args, **kwargs):
            events.append(name)
            return function(*args, **kwargs)
        return wrapped

    for name in (
        "_apply_boundary_snaps",
        "embedding_dedup_clips",
        "temporal_dedup_clips",
        "_assign_candidate_identities",
        "_evaluate_quality_pipeline_candidates",
        "_rank_pipeline_clips",
    ):
        monkeypatch.setattr(
            rank_dedup_stage,
            name,
            track(name, getattr(rank_dedup_stage, name)),
        )

    _run_direct_pipeline_fixture(
        tmp_path,
        monkeypatch,
        cfg_overrides={"temporal_dedup_enabled": True},
    )

    expected = [
        "_apply_boundary_snaps",
        "embedding_dedup_clips",
        "temporal_dedup_clips",
        "_assign_candidate_identities",
        "_evaluate_quality_pipeline_candidates",
        "_rank_pipeline_clips",
    ]
    assert [name for name in events if name in expected] == expected
```

- [ ] **Step 3: Run the characterization test**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_adaptive_direct_transition.py::test_direct_rank_dedup_preserves_snap_dedup_identity_quality_rank_order -v
```

Expected: PASS. This is intentionally a characterization test; it should pass against current code and guard future insertion work.

- [ ] **Step 4: Run the complete direct-transition test file**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_adaptive_direct_transition.py -q
```

Expected: the baseline count plus one test, all passing.

- [ ] **Step 5: Commit the isolated test**

```powershell
git add tests/test_adaptive_direct_transition.py
git commit -m "test: freeze rank dedup stage ordering"
```

### Task 2: Define the Experimental Manifest, Result, and Backend Contracts

**Files:**
- Create: `scripts/continuity_spike_contracts.py`
- Create: `scripts/continuity_spike_backends.py`
- Create: `tests/continuity_spike/test_contracts.py`
- Create: `tests/continuity_spike/test_backends.py`

**Interfaces:**
- Consumes: standard-library `dataclasses`, `hashlib`, `importlib`, `json`, `pathlib`, and `typing.Protocol`.
- Produces: `SpikeCandidate`, `SpikeManifest`, `CandidateProbeResult`, `ContinuitySpikeBackend`, `load_manifest(path)`, `verify_source_identity(candidate)`, `validate_probe_result(candidate, result)`, `make_error_result`, `load_backend(spec)`, and `DeterministicFakeBackend`.

- [ ] **Step 1: Write failing contract tests**

Create tests that construct a real temporary file and use its actual SHA-256:

```python
def test_load_manifest_freezes_explicit_source_window(tmp_path):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"synthetic-video")
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": "continuity-spike-input-v1",
        "candidates": [{
            "candidate_id": "synthetic-001",
            "media_kind": "source_video",
            "source_path": str(video.resolve()),
            "source_sha256": digest,
            "media_duration_s": 8.0,
            "confirmed_shot_start_s": 0.5,
            "context_start_s": 0.5,
            "window_start_s": 1.0,
            "window_end_s": 3.5,
            "context_end_s": 5.5,
            "confirmed_shot_end_s": 6.0,
            "scenario_tags": ["single_subject", "subtle_motion", "face_visible", "media_edge"],
            "data_origin": "synthetic",
            "human_label": "UNLABELED",
            "feedback_label": "unknown",
        }],
    }), encoding="utf-8")

    manifest = load_manifest(manifest_path)

    assert manifest.schema_version == "continuity-spike-input-v1"
    assert manifest.candidates[0].duration_s == 2.5
    assert verify_source_identity(manifest.candidates[0]) == digest
```

Also add exact negative cases for duplicate `candidate_id`, a non-absolute source path, malformed SHA-256, `end <= start`, empty tags, unsupported `media_kind`/`data_origin`/human/feedback labels, missing source, and a changed source hash.

- [ ] **Step 2: Run contract tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_contracts.py -v
```

Expected: collection fails because `scripts.continuity_spike_contracts` does not exist.

- [ ] **Step 3: Implement immutable contract types and strict validation**

Use these exact public shapes:

```python
DataOrigin = Literal["real", "synthetic"]
MediaKind = Literal["source_video", "historical_gif"]
HumanLabel = Literal["ACCEPTABLE", "INVALID", "UNLABELED"]
FeedbackLabel = Literal["favorite", "like", "dislike", "neutral", "unknown"]
Disposition = Literal[
    "AUTO_PASS",
    "REVIEW_REQUIRED",
    "HARD_REJECT",
    "GIF_ONLY_PASS",
    "GIF_ONLY_REVIEW",
    "GIF_ONLY_REJECT",
    "ERROR",
]
VlmStatus = Literal["COMPLETED", "ABSTAIN", "SKIPPED", "ERROR"]
AmplitudeStatus = Literal["PASS", "REVIEW", "UNAVAILABLE", "NOT_APPLICABLE"]
AmplitudeClass = Literal["unobservable", "subtle", "moderate", "large"]
NoiseReferenceSource = Literal["static_context", "background_residual", "combined", "unavailable"]
CameraCompensationStatus = Literal["AVAILABLE", "LOW_CONFIDENCE", "NOT_APPLICABLE"]
FacePresenceStatus = Literal["NOT_DETECTED", "DETECTED_UNUSABLE", "USABLE"]
IdentityEvidenceStatus = Literal["NOT_APPLICABLE", "CONSISTENT", "CONFLICT", "UNAVAILABLE"]
FacialDynamicsStatus = Literal["NOT_APPLICABLE", "AVAILABLE", "TEMPORAL_ALERT", "ABSTAIN", "UNAVAILABLE"]
CurveFeatureStatus = Literal["AVAILABLE", "UNAVAILABLE", "NOT_RUN"]
IndependenceGroup = Literal[
    "scene_boundary",
    "pose_geometry",
    "appearance_identity",
    "photometric_motion",
    "semantic_vlm",
]

@dataclass(frozen=True)
class SpikeCandidate:
    candidate_id: str
    media_kind: MediaKind
    source_path: Path
    source_sha256: str
    media_duration_s: float
    confirmed_shot_start_s: float
    context_start_s: float
    window_start_s: float
    window_end_s: float
    context_end_s: float
    confirmed_shot_end_s: float
    scenario_tags: tuple[str, ...]
    data_origin: DataOrigin
    human_label: HumanLabel
    feedback_label: FeedbackLabel

    @property
    def duration_s(self) -> float:
        return self.window_end_s - self.window_start_s

@dataclass(frozen=True)
class SpikeManifest:
    schema_version: str
    candidates: tuple[SpikeCandidate, ...]

@dataclass(frozen=True)
class CandidateProbeResult:
    candidate_id: str
    source_sha256: str
    backend_id: str
    disposition: Disposition
    total_frame_count: int
    detected_frame_count: int
    pose_valid_frame_count: int
    max_evidence_gap_s: float | None
    subject_set_ambiguous: bool
    tracking_loss_count: int
    amplitude_status: AmplitudeStatus
    amplitude_class: AmplitudeClass
    normalized_amplitude_peak: float | None
    normalized_amplitude_noise_floor: float | None
    amplitude_noise_reference: NoiseReferenceSource
    background_coverage: float | None
    affine_inlier_ratio: float | None
    camera_compensation_status: CameraCompensationStatus
    amplitude_phase_complete: bool | None
    body_identity_status: IdentityEvidenceStatus
    face_presence_status: FacePresenceStatus
    face_identity_status: IdentityEvidenceStatus
    face_visible_frame_count: int
    face_usable_frame_count: int
    face_usable_coverage: float | None
    min_face_short_side_px: float | None
    mean_face_landmark_confidence: float | None
    facial_landmark_status: FacialDynamicsStatus
    facial_expression_status: FacialDynamicsStatus
    curve_feature_status: CurveFeatureStatus
    curve_feature_missing_reason: str | None
    source_media_width_px: int | None
    source_media_height_px: int | None
    primary_subject_area_ratio: float | None
    face_decode_width_px: int | None
    face_evidence_wall_clock_ms: float | None
    curve_feature_wall_clock_ms: float | None
    confirming_independence_groups: tuple[IndependenceGroup, ...]
    vlm_status: VlmStatus
    wall_clock_ms: float
    reason_codes: tuple[str, ...]

class ContinuitySpikeBackend(Protocol):
    backend_id: str
    synthetic: bool

    def evaluate_source(
        self,
        candidates: tuple[SpikeCandidate, ...],
    ) -> tuple[CandidateProbeResult, ...]:
        raise NotImplementedError
```

Implement the public functions with these exact signatures: `load_manifest(path: Path) -> SpikeManifest`, `verify_source_identity(candidate: SpikeCandidate) -> str`, `validate_probe_result(candidate: SpikeCandidate, result: CandidateProbeResult) -> None`, and `make_error_result(candidate: SpikeCandidate, backend_id: str, reason_code: str, wall_clock_ms: float) -> CandidateProbeResult`. The error factory preserves `candidate.source_sha256`, sets disposition `ERROR`, amplitude `UNAVAILABLE`, camera compensation `NOT_APPLICABLE`, body/face identity and facial dynamics `NOT_APPLICABLE`, curve features `NOT_RUN`, VLM `ERROR`, zero frame counts/groups, and only the supplied stable reason code.

Manifest validation must reject booleans where a number is expected, non-finite numbers, non-snake-case tags/reason codes, duplicate tags, duplicate IDs, relative paths, non-files, hashes other than 64 lowercase hex characters, and unknown top-level or candidate keys. Media duration must be positive. A source-video item must satisfy `0 <= confirmed_shot_start_s <= context_start_s <= window_start_s < window_end_s <= context_end_s <= confirmed_shot_end_s <= media_duration_s`; this makes it impossible for the backend's default two-second context to cross an already confirmed shot boundary. Every `source_video` candidate must contain exactly one of `subtle_motion`/`moderate_motion`/`large_motion` and exactly one of `face_visible`/`face_occluded`/`face_not_visible`; these are human sampling-bucket labels, not model conclusions. Every `historical_gif` item must set shot/context/window bounds to `0` and `media_duration_s` and use `human_label=UNLABELED`; its feedback may be `unknown` for contract testing, but only a non-`unknown` value counts toward the real 0B population. JSON errors must include the field path, such as `candidates[0].window_end_s`.

Result validation must enforce these cross-field rules:

- `candidate_id` and `source_sha256` must match the requested candidate;
- amplitude peak and noise floor are finite and non-negative when present;
- amplitude `PASS` requires a peak above the independent noise floor, a reference other than `unavailable`, `amplitude_phase_complete=true`, and camera compensation `AVAILABLE` when background compensation is used;
- for source video, background compensation is `LOW_CONFIDENCE` when coverage is below 0.30 or affine inlier ratio below 0.50, and then amplitude must be `UNAVAILABLE` with candidate disposition at least `REVIEW_REQUIRED`;
- body identity `CONFLICT` cannot have disposition `AUTO_PASS` and contributes only `appearance_identity`;
- `USABLE` face requires at least 3 usable frames, face short side at least 64 px, mean landmark confidence at least 0.70, and usable coverage at least 0.60;
- `NOT_DETECTED` or `DETECTED_UNUSABLE` requires face identity and both facial-dynamics statuses `NOT_APPLICABLE`;
- facial-landmark/expression `TEMPORAL_ALERT`, `ABSTAIN`, or `UNAVAILABLE` is permitted with `AUTO_PASS` because it is advisory;
- face identity `CONFLICT` cannot have disposition `AUTO_PASS`, but it contributes only `appearance_identity`; body ReID and face embedding conflicts remain correlated observations inside that same group;
- `HARD_REJECT` requires `scene_boundary` or at least two distinct confirming groups; group values must be unique, two reason codes from the same group still count once, and facial-landmark/expression advisory statuses never add a confirming group;
- `historical_gif` must use amplitude `NOT_APPLICABLE`; it may use face fields for extraction diagnostics, but its disposition must be `GIF_ONLY_PASS`, `GIF_ONLY_REVIEW`, `GIF_ONLY_REJECT`, or `ERROR`; source-video candidates cannot use GIF-only dispositions.
- media dimensions and component wall-clock values are positive when present; `primary_subject_area_ratio` is within `[0, 1]`; `curve_feature_status=AVAILABLE` requires dimensions, subject area ratio, and `curve_feature_wall_clock_ms`.

- [ ] **Step 4: Run contract tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: Write failing backend-loader tests**

Test the built-in fake, an in-memory `module:function` factory, a missing module, a non-callable attribute, and an object missing the protocol surface:

```python
def test_fake_backend_is_always_marked_synthetic(candidate):
    backend = load_backend("fake")
    result = backend.evaluate_source((candidate,))[0]
    assert backend.synthetic is True
    assert result.backend_id == "deterministic-fake-v1"
    assert result.candidate_id == candidate.candidate_id
    assert result.amplitude_class == "subtle"
    assert result.face_presence_status == "USABLE"
    assert result.face_identity_status == "CONSISTENT"
    assert result.facial_expression_status == "AVAILABLE"
```

- [ ] **Step 6: Run backend tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_backends.py -v
```

Expected: FAIL because the backend loader does not exist.

- [ ] **Step 7: Implement the backend loader and deterministic fake**

`load_backend("fake")` returns `DeterministicFakeBackend`. Any other spec must contain exactly one colon and load a zero-argument factory through `importlib.import_module`; validate the returned object has non-empty `backend_id`, boolean `synthetic`, and callable `evaluate_source`. `evaluate_source` receives only candidates sharing one source path/hash, enabling one model residency cycle per source video; result IDs must align one-to-one with the input tuple.

The fake returns the input source SHA-256, eight detected and pose-valid frames, a 0.125-second maximum gap, `AUTO_PASS`, `SKIPPED` VLM, zero tracking losses, and body identity `CONSISTENT`. It derives additional deterministic values from scenario tags solely to exercise the report contract:

- `subtle_motion`: amplitude class `subtle`, peak 0.15, noise floor 0.03;
- `moderate_motion`: amplitude class `moderate`, peak 0.45, noise floor 0.03;
- `large_motion`: amplitude class `large`, peak 0.90, noise floor 0.03;
- every video amplitude result uses `combined`, background coverage 0.60, affine inlier ratio 0.80, camera compensation `AVAILABLE`, and complete phase;
- `face_visible`: face `USABLE`, identity `CONSISTENT`, 6/8 usable frames, 96 px short side, 0.90 landmark confidence, facial landmarks/expression `AVAILABLE`;
- `face_occluded`: face `DETECTED_UNUSABLE`, 1/4 usable frames, 48 px short side, 0.45 confidence, identity/dynamics `NOT_APPLICABLE`;
- otherwise: face `NOT_DETECTED`, zero face frames, identity/dynamics `NOT_APPLICABLE`;
- every result reports deterministic finite media dimensions and component costs needed by its active evidence;
- `historical_gif`: disposition `GIF_ONLY_PASS`, amplitude `NOT_APPLICABLE`, camera compensation `NOT_APPLICABLE`, curve feature `AVAILABLE`, a 0.50 primary-subject area ratio, and a finite curve-feature cost; source-video candidates use curve feature `NOT_RUN`.

Its synthetic flag ensures these favorable values never qualify a run.

- [ ] **Step 8: Run both contract suites and strict type checking**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_contracts.py tests/continuity_spike/test_backends.py -q
.\.venv\Scripts\python.exe -m mypy scripts/continuity_spike_contracts.py scripts/continuity_spike_backends.py
```

Expected: both commands exit 0.

- [ ] **Step 9: Commit the contract boundary**

```powershell
git add scripts/continuity_spike_contracts.py scripts/continuity_spike_backends.py tests/continuity_spike/test_contracts.py tests/continuity_spike/test_backends.py
git commit -m "feat: add continuity spike contracts"
```

### Task 3: Aggregate Evidence and Produce a Non-Qualifying Go/No-Go Report

**Files:**
- Create: `scripts/continuity_spike_metrics.py`
- Create: `tests/continuity_spike/test_metrics.py`

**Interfaces:**
- Consumes: `SpikeManifest`, validated `CandidateProbeResult` values, and runtime snapshot mappings.
- Produces: `nearest_rank_percentile(values, percentile)` and `build_spike_report(manifest, results, runtime_snapshots, *, backend_synthetic)` with source-video, historical-yield, independence, amplitude, face-identity, and facial-advisory summaries.

- [ ] **Step 1: Write failing percentile and aggregation tests**

Cover exact boundary behavior:

```python
def test_nearest_rank_percentile_uses_observed_value():
    assert nearest_rank_percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0

def test_synthetic_backend_can_never_reach_go_no_go(candidate_manifest):
    report = build_spike_report(
        candidate_manifest,
        [passing_result(candidate_manifest.candidates[0])],
        [{"gpu": [{"memory_used_mib": 1000, "memory_total_mib": 16384}]}],
        backend_synthetic=True,
    )
    assert report["go_no_go"]["state"] == "INSUFFICIENT_REAL_DATA"
    assert "synthetic_backend" in report["go_no_go"]["reasons"]
```

Add cases for 50 labeled real source-video candidates plus 200 feedback-labeled historical GIFs, each threshold exceeded one at a time, mixed real/synthetic input, incomplete source-video human labels, unknown historical feedback, missing source-video scenario buckets, amplitude class/independent-noise aggregation, low-confidence camera compensation, face-identity contribution, advisory expression `ABSTAIN` that does not alter disposition, invalid same-group hard rejection, historical curve-feature yield/cost, VLM `ABSTAIN`, error rate, source-video position quintiles, and measured source-batch wall-clock p95.

- [ ] **Step 2: Run metrics tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_metrics.py -v
```

Expected: collection fails because `scripts.continuity_spike_metrics` does not exist.

- [ ] **Step 3: Implement deterministic aggregation**

Use these required scenario buckets:

```python
REQUIRED_SOURCE_VIDEO_SCENARIOS = frozenset({
    "single_subject",
    "co_primary_subjects",
    "partial_body",
    "headless",
    "close_contact",
    "occlusion",
    "fast_motion",
    "subtle_motion",
    "moderate_motion",
    "large_motion",
    "face_visible",
    "face_occluded",
    "face_not_visible",
    "low_light",
    "media_edge",
})

THRESHOLDS = {
    "candidate_p50_ms_max": 10_000.0,
    "candidate_p95_ms_max": 30_000.0,
    "source_video_p95_ms_max": 1_200_000.0,
    "peak_vram_mib_max": 14_336.0,
    "auto_pass_rate_min": 0.40,
    "review_required_rate_max": 0.60,
}
```

Performance and disposition thresholds use source-video candidates only. `auto_pass_rate` uses `human_label=ACCEPTABLE` as its denominator; `review_required_rate` uses all labeled source-video candidates. Historical GIF results and facial-advisory alerts are excluded from both rates. `evidence_independence` reports confirming group sets and rejects any `HARD_REJECT` that lacks `scene_boundary` and has fewer than two distinct groups.

`build_spike_report` must return schema `continuity-spike-report-v1` with `population`, `scenario_coverage`, `evidence_independence`, `motion_amplitude`, `face_identity`, `facial_advisory`, `historical_curve_features`, `performance`, `position_quintiles`, `vlm`, `threshold_checks`, and `go_no_go`.

`motion_amplitude` contains class counts, peak/noise-floor p50/p95, noise-reference source counts, background coverage/affine-inlier p50/p95, low-confidence compensation rate, phase-incomplete rate, and amplitude-triggered review rate. `face_identity` contains face presence, usable rate, body/face identity-status counts, and `marginal_true_catch_count`: human-`INVALID` candidates where body identity is not `CONFLICT`, face identity is `CONFLICT`, and no group other than `appearance_identity` confirms a failure. `facial_advisory` contains face decode-width buckets, component cost, landmark/expression statuses, and marginal-alert candidates; advisory statuses never enter `threshold_checks`, disposition rates, or hard-reject confirmation counts. `historical_curve_features` contains availability and missing reasons by exact buckets: resolution (`max(width,height) <= 256`, `257..480`, `>480`), duration (`<2 s`, `2..5 s`, `>5 s`), feedback, and primary-subject area ratio (`<0.25`, `0.25..0.60`, `>0.60`); it also reports effective favorite/like/dislike rows with available features, p50/p95/mean cost, and `projected_full_inventory_gpu_hours = mean_wall_clock_ms * 16894 / 3_600_000`. `performance` uses backend per-candidate timings for candidate p50/p95 and harness-measured `source_batch_timing` runtime entries for source-video p95; it also reports the sum of candidate component timings separately, without treating that correlated sum as video wall-clock. `position_quintiles` uses window midpoint divided by `media_duration_s` and reports disposition/budget exhaustion by 20% bucket. The final state is:

- `INSUFFICIENT_REAL_DATA` when the backend is synthetic, any candidate is synthetic, fewer than 50 real source-video candidates exist, fewer than 200 real historical GIFs exist, any source-video human label is `UNLABELED`, any historical feedback label is `unknown`, a required source-video scenario bucket is missing, any defined historical resolution/duration/subject-size bucket is empty, favorite/like/dislike are not all represented, or candidate IDs/results do not match one-to-one;
- `NO_GO` when the real population is complete but at least one threshold fails;
- `READY_FOR_HUMAN_GO_NO_GO_REVIEW` when population and every threshold pass.

It must never output `QUALIFIED`, `ENFORCE`, or a qualification manifest.

- [ ] **Step 4: Run metrics tests and type checking**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_metrics.py -q
.\.venv\Scripts\python.exe -m mypy scripts/continuity_spike_metrics.py
```

Expected: both commands exit 0.

- [ ] **Step 5: Commit metrics separately**

```powershell
git add scripts/continuity_spike_metrics.py tests/continuity_spike/test_metrics.py
git commit -m "feat: report continuity spike feasibility metrics"
```

### Task 4: Add Read-Only Ollama and GPU Telemetry

**Files:**
- Create: `scripts/continuity_spike_runtime.py`
- Create: `tests/continuity_spike/test_runtime.py`

**Interfaces:**
- Consumes: an injected HTTP GET callable and subprocess runner.
- Produces: `query_ollama_ps`, `query_nvidia_smi`, and `capture_runtime_snapshot(phase, source_key, telemetry_mode, *, get, run)`; all network and process callables are injected in tests.

- [ ] **Step 1: Write failing telemetry parser tests**

Use fake responses and runners; no test may contact the user's actual Ollama or GPU:

```python
def test_query_ollama_ps_reports_actual_resident_models():
    models = query_ollama_ps(
        "http://127.0.0.1:11434",
        get=lambda url, timeout: FakeResponse({
            "models": [{"name": "qwen3-vl:8b", "size_vram": 9_000_000_000}],
        }),
    )
    assert models == [{"name": "qwen3-vl:8b", "size_vram_bytes": 9_000_000_000}]

def test_query_nvidia_smi_is_read_only():
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="0, NVIDIA GPU, 1024, 16384\n", stderr="")
    result = query_nvidia_smi(run=fake_run)
    assert calls == [[
        "nvidia-smi", "--query-gpu=index,name,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]]
    assert result[0]["memory_used_mib"] == 1024.0
```

Add unavailable HTTP, malformed `/api/ps`, absent `nvidia-smi`, non-zero process exit, malformed CSV, and telemetry mode filtering.

- [ ] **Step 2: Run telemetry tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_runtime.py -v
```

Expected: collection fails because `scripts.continuity_spike_runtime` does not exist.

- [ ] **Step 3: Implement read-only telemetry with structured failures**

`query_ollama_ps` issues only `GET <base_url>/api/ps` with a five-second timeout. `query_nvidia_smi` issues only the exact query command in the test. `capture_runtime_snapshot` records `kind=telemetry`, UTC time, monotonic nanoseconds, phase, source key, requested mode, available payloads, and structured errors such as `ollama_unavailable` or `nvidia_smi_unavailable`. Telemetry failure must not fail a source batch. Task 5 adds separate `kind=source_batch_timing` entries to the same runtime artifact; telemetry and measured timings must not be conflated.

- [ ] **Step 4: Add a source-level guard against lifecycle commands**

The test reads `scripts/continuity_spike_runtime.py` and asserts it contains none of these command fragments: `ollama stop`, `ollama run`, `ollama pull`, `/api/generate`, `/api/chat`, or `/api/embed`. This is defense in depth in addition to injected-call tests.

- [ ] **Step 5: Run telemetry tests and type checking**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_runtime.py -q
.\.venv\Scripts\python.exe -m mypy scripts/continuity_spike_runtime.py
```

Expected: both commands exit 0.

- [ ] **Step 6: Commit telemetry separately**

```powershell
git add scripts/continuity_spike_runtime.py tests/continuity_spike/test_runtime.py
git commit -m "feat: capture read only continuity telemetry"
```

### Task 5: Build the Immutable Spike CLI

**Files:**
- Create: `scripts/continuity_spike.py`
- Create: `tests/continuity_spike/test_cli.py`

**Interfaces:**
- Consumes: `load_manifest`, `verify_source_identity`, `validate_probe_result`, `load_backend`, `capture_runtime_snapshot`, and `build_spike_report`.
- Produces: `CliRunResult`, `run(argv)`, and `main()`; normal runs write `manifest.snapshot.json`, `results/<candidate_id>.json`, `runtime.json`, and `report.json` under a unique run directory.

- [ ] **Step 1: Write failing dry-run and normal-run CLI tests**

Generate a valid MP4 with `tests.action_media_fixtures.write_start_move_settle_video`, hash it, and create a synthetic manifest inside `tmp_path`. Required assertions:

```python
def test_dry_run_validates_without_side_effects(tmp_path, monkeypatch, capsys):
    manifest_path, output_dir, source = write_synthetic_manifest(tmp_path)
    monkeypatch.setattr(spike_cli, "load_backend", lambda spec: pytest.fail("backend loaded"))
    monkeypatch.setattr(spike_cli, "capture_runtime_snapshot", lambda **kwargs: pytest.fail("telemetry called"))

    result = spike_cli.run([
        "--manifest", str(manifest_path),
        "--output-dir", str(output_dir),
        "--backend", "fake",
        "--dry-run",
    ])

    assert result.dry_run is True
    assert not output_dir.exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == result.source_hashes[0]
    assert json.loads(capsys.readouterr().out)["valid"] is True

def test_fake_run_is_immutable_and_statistically_ineligible(tmp_path):
    manifest_path, output_dir, source = write_synthetic_manifest(tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = spike_cli.run([
        "--manifest", str(manifest_path),
        "--output-dir", str(output_dir),
        "--backend", "fake",
        "--telemetry", "off",
    ])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert result.run_dir.parent == output_dir.resolve()
    report = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["go_no_go"]["state"] == "INSUFFICIENT_REAL_DATA"
```

Also test two successive runs create different directories, unknown CLI values fail before writes, candidates are grouped by source before one `evaluate_source` call, source mutation during a source batch produces `ERROR` results with `source_hash_mismatch`, one source-batch exception does not abort later sources, amplitude `UNAVAILABLE` is normalized to `REVIEW_REQUIRED`, body or face identity `CONFLICT` is normalized to `REVIEW_REQUIRED`, advisory expression `ABSTAIN` leaves an otherwise valid disposition unchanged, same-group `HARD_REJECT` output becomes `backend_contract_invalid`, runtime JSON contains the measured source-batch timing, and artifact JSON contains no NaN/Infinity.

- [ ] **Step 2: Run CLI tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_cli.py -v
```

Expected: collection fails because `scripts.continuity_spike` does not exist.

- [ ] **Step 3: Implement argument parsing and dry-run before any writer/backend construction**

Use this exact CLI surface:

```text
--manifest PATH             required
--output-dir PATH           required
--backend SPEC              default: fake
--telemetry MODE            off | ollama | gpu | all; default: off
--ollama-base-url URL        default: http://127.0.0.1:11434
--dry-run                    validation and stdout JSON only
```

`run()` first parses and validates the manifest and verifies every distinct source hash. If `--dry-run` is present, print a sorted, finite JSON object containing `valid`, schema version, source-video/historical-GIF counts, real/synthetic counts, human/feedback-label counts, scenario counts, shot/context truncation counts, duration buckets, and source hashes, then return without resolving the backend or creating directories. Resolution and subject-size strata remain `NOT_MEASURED` in dry-run because they come from backend evidence rather than filename assumptions.

- [ ] **Step 4: Implement unique run claiming and atomic JSON output**

Define the immutable `CliRunResult` exactly as follows:

```python
@dataclass(frozen=True)
class CliRunResult:
    dry_run: bool
    run_dir: Path | None
    report_path: Path | None
    source_hashes: tuple[str, ...]
```

Use these exact function signatures: `_claim_run_dir(parent: Path) -> Path`, `_atomic_json(path: Path, payload: Mapping[str, object]) -> None`, `run(argv: Sequence[str] | None = None) -> CliRunResult`, and `main() -> int`.

Use `mkdir(exist_ok=False)` for the unique child. `_atomic_json` writes UTF-8 to a sibling temporary file created with `tempfile.NamedTemporaryFile(delete=False, dir=path.parent)`, flushes and `os.fsync()`s it, and promotes it with `os.replace()` only when the destination does not already exist; if the destination exists, fail the run rather than overwrite it.

- [ ] **Step 5: Implement per-source batch isolation and immutable artifacts**

Group candidates by `(source_path, source_sha256, media_kind)` while preserving first-source order. For each source group:

1. verify source SHA-256;
2. capture `before_source` telemetry;
3. measure `time.perf_counter_ns()` around one `backend.evaluate_source(group)` call and append a runtime entry shaped as `{"kind": "source_batch_timing", "source_key": <sha256>, "media_kind": <kind>, "candidate_count": <count>, "wall_clock_ms": <finite non-negative float>}` separately from backend component/candidate timings;
4. verify result IDs align one-to-one with group candidate IDs;
5. use `dataclasses.replace` to apply only hard policy consequences: amplitude `REVIEW`/`UNAVAILABLE` or body/face identity `CONFLICT` upgrades an otherwise `AUTO_PASS` source-video candidate to `REVIEW_REQUIRED`;
6. leave facial-landmark/expression `TEMPORAL_ALERT`, `ABSTAIN`, or `UNAVAILABLE` as advisory fields without changing disposition;
7. call `validate_probe_result(candidate, result)` for every result and convert invalid output through `make_error_result(..., reason_code="backend_contract_invalid", ...)`;
8. verify source SHA-256 again; if it changed, replace every group result with `source_hash_mismatch` errors;
9. convert a batch exception to one error result per group candidate, then continue with the next source;
10. capture `after_source` telemetry and write one result JSON per candidate.

After all candidates, write runtime snapshots and the aggregate report. Snapshot the normalized input manifest, resolved absolute paths, backend ID/synthetic flag, Python/platform identity, and invocation arguments without environment variables or secrets.

- [ ] **Step 6: Run CLI tests, all spike tests, and type checking**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike/test_cli.py -q
.\.venv\Scripts\python.exe -m pytest tests/continuity_spike -q
.\.venv\Scripts\python.exe -m mypy scripts/continuity_spike.py scripts/continuity_spike_contracts.py scripts/continuity_spike_backends.py scripts/continuity_spike_metrics.py scripts/continuity_spike_runtime.py
```

Expected: all commands exit 0 without contacting live services.

- [ ] **Step 7: Commit the CLI separately**

```powershell
git add scripts/continuity_spike.py tests/continuity_spike/test_cli.py
git commit -m "feat: add immutable continuity spike runner"
```

### Task 6: Document the 0A Operator Contract and Run Final Verification

**Files:**
- Create: `docs/reports/2026-09-continuity-spike/README.md`
- Modify only if verification finds a defect: files created in Tasks 1–5.

**Interfaces:**
- Consumes: the completed CLI and artifact schemas.
- Produces: an executable operator guide and final verification evidence; no real-media performance claim.

- [ ] **Step 1: Write the operator guide**

The guide must state all of the following explicitly:

- milestone 0A is harness construction, not the real 50-source-video-candidate plus 200-historical-GIF feasibility result;
- input items must explicitly identify `source_video` or `historical_gif`, use absolute paths and exact SHA-256 values, carry human/feedback labels required by their media kind, and freeze shot/context/window bounds so source-video context never crosses a confirmed shot boundary;
- supported source-video scenario tags are the fifteen `REQUIRED_SOURCE_VIDEO_SCENARIOS` values, including subtle/moderate/large motion and visible/occluded/not-visible face cases;
- action amplitude is torso-normalized and camera-compensated, uses independent static/background noise references, low amplitude alone is not failure, and 0A only validates the contract rather than model accuracy;
- face embedding is additional identity evidence within one independence group; facial landmarks/expression are advisory, absent/unusable faces are not penalized, and 0A makes no psychological-state inference;
- one source batch maps to one backend call so a future GPU backend can swap model families once per video rather than once per candidate;
- historical output reports curve-feature availability, missing reasons, measured cost, effective preference-training rows, resolution/duration/feedback/subject-size strata, and projected 16,894-item GPU hours;
- `--dry-run` has zero filesystem/service side effects;
- telemetry is observational and does not manage Ollama lifecycle;
- the fake backend and synthetic videos can validate contracts only;
- every run artifact and field is described, including `INSUFFICIENT_REAL_DATA`, `NO_GO`, and `READY_FOR_HUMAN_GO_NO_GO_REVIEW`;
- no report produced by this tool enables production `enforce`;
- GLM Harness can later review the frozen report and selected bundles manually, but the CLI has no GLM integration.

- [ ] **Step 2: Run the focused regression suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_adaptive_direct_transition.py tests/continuity_spike -q
```

Expected: exit code 0.

- [ ] **Step 3: Run strict type checking for every new module**

Run:

```powershell
.\.venv\Scripts\python.exe -m mypy scripts/continuity_spike.py scripts/continuity_spike_contracts.py scripts/continuity_spike_backends.py scripts/continuity_spike_metrics.py scripts/continuity_spike_runtime.py
```

Expected: exit code 0 with no errors.

- [ ] **Step 4: Run repository non-slow regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not slow" -q
```

Expected: exit code 0. If failures also reproduce on the pre-task commit, record them as baseline failures and do not edit unrelated code; otherwise fix only regressions caused by files in this plan.

- [ ] **Step 5: Verify scope and artifact safety**

Run:

```powershell
git diff --check
git status --short
git diff --name-only HEAD~6..HEAD
rg -n "torch|onnxruntime|candidate_gifs|CREATE TABLE|ALTER TABLE|ollama stop|ollama run|/api/generate|/api/chat|/api/embed|glm" scripts -g "continuity_spike*.py"
```

Expected: no whitespace errors; only planned files are attributable to these task commits; the final search prints no production dependency, database mutation, model lifecycle, inference endpoint, or GLM integration from the new scripts. References inside tests/documentation are acceptable only when they assert prohibition.

- [ ] **Step 6: Commit documentation and any in-scope verification repair**

```powershell
git add docs/reports/2026-09-continuity-spike/README.md
git commit -m "docs: add continuity spike operator contract"
```

- [ ] **Step 7: Produce the execution handoff**

Report exact focused/full test counts, mypy result, commit hashes, and the caller-owned directory used for smoke artifacts. State `real_media_spike=NOT_RUN`, `historical_gif_yield_spike=NOT_RUN`, `amplitude_model_accuracy=NOT_MEASURED`, `face_identity_gain=NOT_MEASURED`, `facial_advisory_marginal_capture=NOT_MEASURED`, and `production_enforce_qualification=NOT_CREATED`. The next decision is to execute milestone 0B with an explicit 50-video-candidate/200-historical-GIF manifest and an experimental detector/pose/tracker/amplitude/face backend.
