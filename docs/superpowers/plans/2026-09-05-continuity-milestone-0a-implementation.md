# Continuity Milestone 0A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only, model-neutral continuity feasibility harness, including action-amplitude and conditional facial-expression contracts, and freeze the current pipeline ordering before real source videos and a candidate vision backend are available.

**Architecture:** Keep all spike code under `scripts/` and all tests under `tests/continuity_spike/`; production pipeline code, database schema, UI, and GIF business state remain unchanged. The harness validates an explicit manifest of source-video windows, calls a pluggable experimental backend, records normalized action-amplitude and conditionally activated facial-expression evidence, samples Ollama/GPU state without changing it, and writes an immutable run directory containing per-candidate summaries plus a go/no-go report. A deterministic fake backend and generated videos test the harness, but synthetic runs are always marked statistically ineligible.

**Tech Stack:** Python 3.11, dataclasses and `typing.Protocol`, JSON, `argparse`, `httpx`, `subprocess`, OpenCV test fixtures, pytest, mypy.

**Spec:** `docs/superpowers/specs/2026-09-04-person-semantic-continuity-body-curve-design.md`

## Global Constraints

- This plan implements only milestone 0A; it does not run the approximately 50-candidate real-media spike because the workspace currently contains no source videos.
- Do not add or alter production SQLite tables, `candidate_gifs`, task-engine stages, review UI, ranking, dedup behavior, Quality MoE policy, or GIF encoding.
- Do not add Torch, ONNXRuntime, detector, pose, segmentation, ReID, or GLM dependencies.
- Milestone 0A defines action-amplitude and facial-expression result fields but does not run a real pose, face, or expression model.
- Small action amplitude is not a reject condition. The backend must compare normalized motion against an adaptive noise floor and report phase completeness separately.
- Facial expression is conditionally required only when a usable face is reported: at least three sampled frames, face short side at least 64 px, mean landmark confidence at least 0.70, and usable coverage at least 0.60.
- `NOT_DETECTED` and `DETECTED_UNUSABLE` faces do not block an otherwise valid candidate; `USABLE` face evidence with expression `ABSTAIN`, `UNAVAILABLE`, or `REVIEW` maps to candidate review rather than hard rejection.
- A single amplitude or facial-expression anomaly cannot produce `HARD_REJECT`.
- GLM-5.3-Flash is not called by this code; later Harness review is an explicit human action.
- Source videos are opened read-only and SHA-256 is verified before and after every evaluated candidate.
- `--dry-run` performs validation and prints JSON to stdout; it must not create an output directory, load a backend, call Ollama, or invoke `nvidia-smi`.
- Runtime telemetry is read-only. It may query Ollama `/api/ps` and `nvidia-smi`; it must never stop, load, or unload a model.
- A fake backend or any synthetic candidate forces `go_no_go.state=INSUFFICIENT_REAL_DATA` regardless of measured values.
- A real go/no-go report requires at least 50 real candidates and all scenario buckets declared in the design, including subtle/large motion and face visibility conditions; even a passing report requires human review and never signs an `enforce` qualification manifest.
- Report thresholds are candidate p50 at most 10,000 ms, candidate p95 at most 30,000 ms, grouped source-video p95 at most 1,200,000 ms, peak VRAM at most 14,336 MiB, `AUTO_PASS` coverage at least 0.40, and `REVIEW_REQUIRED` rate at most 0.60.
- All output is written below a caller-owned output directory into a newly claimed `run-<uuid>` directory; no existing output file is overwritten.
- Use PowerShell-compatible commands and stage only files listed by each task because the worktree contains unrelated user changes.

## File Structure

- Modify `tests/test_adaptive_direct_transition.py`: characterize the current snap → dedup → identity → Quality MoE → rank order shared by direct mode.
- Create `scripts/continuity_spike_contracts.py`: immutable manifest/result types including amplitude/face evidence, JSON parsing, validation, source identity checks, and backend protocol.
- Create `scripts/continuity_spike_backends.py`: deterministic fake backend and explicit `module:function` backend loader.
- Create `scripts/continuity_spike_metrics.py`: percentile, scenario coverage, amplitude/face aggregation, thresholds, and go/no-go state.
- Create `scripts/continuity_spike_runtime.py`: read-only Ollama and NVIDIA telemetry parsers.
- Create `scripts/continuity_spike.py`: CLI orchestration, dry-run, immutable run directories, per-candidate isolation, and report writing.
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
python -m pytest tests/test_adaptive_direct_transition.py -q
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
python -m pytest tests/test_adaptive_direct_transition.py::test_direct_rank_dedup_preserves_snap_dedup_identity_quality_rank_order -v
```

Expected: PASS. This is intentionally a characterization test; it should pass against current code and guard future insertion work.

- [ ] **Step 4: Run the complete direct-transition test file**

Run:

```powershell
python -m pytest tests/test_adaptive_direct_transition.py -q
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
            "source_path": str(video.resolve()),
            "source_sha256": digest,
            "window_start_s": 1.0,
            "window_end_s": 3.5,
            "scenario_tags": ["single_subject", "subtle_motion", "face_visible", "media_edge"],
            "data_origin": "synthetic",
        }],
    }), encoding="utf-8")

    manifest = load_manifest(manifest_path)

    assert manifest.schema_version == "continuity-spike-input-v1"
    assert manifest.candidates[0].duration_s == 2.5
    assert verify_source_identity(manifest.candidates[0]) == digest
```

Also add exact negative cases for duplicate `candidate_id`, a non-absolute source path, malformed SHA-256, `end <= start`, empty tags, unsupported `data_origin`, missing source, and a changed source hash.

- [ ] **Step 2: Run contract tests to verify they fail**

Run:

```powershell
python -m pytest tests/continuity_spike/test_contracts.py -v
```

Expected: collection fails because `scripts.continuity_spike_contracts` does not exist.

- [ ] **Step 3: Implement immutable contract types and strict validation**

Use these exact public shapes:

```python
DataOrigin = Literal["real", "synthetic"]
Disposition = Literal["AUTO_PASS", "REVIEW_REQUIRED", "HARD_REJECT", "ERROR"]
VlmStatus = Literal["COMPLETED", "ABSTAIN", "SKIPPED", "ERROR"]
AmplitudeStatus = Literal["PASS", "REVIEW", "UNAVAILABLE"]
AmplitudeClass = Literal["unobservable", "subtle", "moderate", "large"]
FacePresenceStatus = Literal["NOT_DETECTED", "DETECTED_UNUSABLE", "USABLE"]
ExpressionStatus = Literal["NOT_APPLICABLE", "PASS", "REVIEW", "ABSTAIN", "UNAVAILABLE"]

@dataclass(frozen=True)
class SpikeCandidate:
    candidate_id: str
    source_path: Path
    source_sha256: str
    window_start_s: float
    window_end_s: float
    scenario_tags: tuple[str, ...]
    data_origin: DataOrigin

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
    amplitude_phase_complete: bool | None
    face_presence_status: FacePresenceStatus
    face_visible_frame_count: int
    face_usable_frame_count: int
    face_usable_coverage: float | None
    min_face_short_side_px: float | None
    mean_face_landmark_confidence: float | None
    facial_expression_status: ExpressionStatus
    vlm_status: VlmStatus
    wall_clock_ms: float
    reason_codes: tuple[str, ...]

class ContinuitySpikeBackend(Protocol):
    backend_id: str
    synthetic: bool

    def evaluate(self, candidate: SpikeCandidate) -> CandidateProbeResult:
        raise NotImplementedError
```

Implement the public functions with these exact signatures: `load_manifest(path: Path) -> SpikeManifest`, `verify_source_identity(candidate: SpikeCandidate) -> str`, `validate_probe_result(candidate: SpikeCandidate, result: CandidateProbeResult) -> None`, and `make_error_result(candidate: SpikeCandidate, backend_id: str, reason_code: str, wall_clock_ms: float) -> CandidateProbeResult`. The error factory sets disposition `ERROR`, amplitude `UNAVAILABLE`/`unobservable`, face `NOT_DETECTED` with expression `NOT_APPLICABLE`, VLM `ERROR`, zero frame counts, and only the supplied stable reason code.

Manifest validation must reject booleans where a number is expected, non-finite numbers, non-snake-case tags/reason codes, duplicate tags, duplicate IDs, relative paths, non-files, hashes other than 64 lowercase hex characters, and unknown top-level or candidate keys. Every candidate must contain exactly one of `subtle_motion`/`moderate_motion`/`large_motion` and exactly one of `face_visible`/`face_occluded`/`face_not_visible`; these are human sampling-bucket labels, not model conclusions. JSON errors must include the field path, such as `candidates[0].window_end_s`.

Result validation must enforce these cross-field rules:

- `candidate_id` must match the requested candidate;
- amplitude peak and noise floor are finite and non-negative when present;
- amplitude `PASS` requires a peak above the adaptive noise floor and `amplitude_phase_complete=true`;
- `USABLE` face requires at least 3 usable frames, face short side at least 64 px, mean landmark confidence at least 0.70, usable coverage at least 0.60, and a facial-expression status other than `NOT_APPLICABLE`;
- `NOT_DETECTED` or `DETECTED_UNUSABLE` requires facial-expression status `NOT_APPLICABLE`;
- a `USABLE` face with `ABSTAIN`, `UNAVAILABLE`, or `REVIEW` cannot have disposition `AUTO_PASS`;
- a single reason from the amplitude or facial-expression families cannot have disposition `HARD_REJECT`.

- [ ] **Step 4: Run contract tests and verify they pass**

Run:

```powershell
python -m pytest tests/continuity_spike/test_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: Write failing backend-loader tests**

Test the built-in fake, an in-memory `module:function` factory, a missing module, a non-callable attribute, and an object missing the protocol surface:

```python
def test_fake_backend_is_always_marked_synthetic(candidate):
    backend = load_backend("fake")
    result = backend.evaluate(candidate)
    assert backend.synthetic is True
    assert result.backend_id == "deterministic-fake-v1"
    assert result.candidate_id == candidate.candidate_id
    assert result.amplitude_class == "subtle"
    assert result.face_presence_status == "USABLE"
    assert result.facial_expression_status == "PASS"
```

- [ ] **Step 6: Run backend tests to verify they fail**

Run:

```powershell
python -m pytest tests/continuity_spike/test_backends.py -v
```

Expected: FAIL because the backend loader does not exist.

- [ ] **Step 7: Implement the backend loader and deterministic fake**

`load_backend("fake")` returns `DeterministicFakeBackend`. Any other spec must contain exactly one colon and load a zero-argument factory through `importlib.import_module`; validate the returned object has non-empty `backend_id`, boolean `synthetic`, and callable `evaluate`.

The fake returns eight detected and pose-valid frames, a 0.125-second maximum gap, `AUTO_PASS`, `SKIPPED` VLM, and zero tracking losses. It derives additional deterministic values from scenario tags solely to exercise the report contract:

- `subtle_motion`: amplitude class `subtle`, peak 0.15, noise floor 0.03;
- `moderate_motion`: amplitude class `moderate`, peak 0.45, noise floor 0.03;
- `large_motion`: amplitude class `large`, peak 0.90, noise floor 0.03;
- `face_visible`: face `USABLE`, 6/8 usable frames, 96 px short side, 0.90 landmark confidence, expression `PASS`;
- `face_occluded`: face `DETECTED_UNUSABLE`, 1/4 usable frames, 48 px short side, 0.45 confidence, expression `NOT_APPLICABLE`;
- otherwise: face `NOT_DETECTED`, zero face frames, expression `NOT_APPLICABLE`.

Its synthetic flag ensures these favorable values never qualify a run.

- [ ] **Step 8: Run both contract suites and strict type checking**

Run:

```powershell
python -m pytest tests/continuity_spike/test_contracts.py tests/continuity_spike/test_backends.py -q
python -m mypy scripts/continuity_spike_contracts.py scripts/continuity_spike_backends.py
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
- Produces: `nearest_rank_percentile(values, percentile)` and `build_spike_report(manifest, results, runtime_snapshots, *, backend_synthetic)` with amplitude and conditional-face summaries.

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
    assert report["go_no_go"]["reasons"] == ["synthetic_backend", "real_candidate_count_below_50"]
```

Add cases for 50 real passing candidates, 50 real candidates exceeding each threshold one at a time, mixed real/synthetic input, missing scenario buckets, amplitude class/noise-floor aggregation, conditional face activation, expression `ABSTAIN`, VLM `ABSTAIN`, error rate, and per-source summed wall-clock p95.

- [ ] **Step 2: Run metrics tests to verify they fail**

Run:

```powershell
python -m pytest tests/continuity_spike/test_metrics.py -v
```

Expected: collection fails because `scripts.continuity_spike_metrics` does not exist.

- [ ] **Step 3: Implement deterministic aggregation**

Use these required scenario buckets:

```python
REQUIRED_SCENARIOS = frozenset({
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

`build_spike_report` must return schema `continuity-spike-report-v1` with `population`, `scenario_coverage`, `evidence`, `motion_amplitude`, `facial_expression`, `performance`, `vlm`, `threshold_checks`, and `go_no_go`.

`motion_amplitude` contains class counts, peak/noise-floor p50/p95, unobservable rate, phase-incomplete rate, and amplitude-triggered review rate. `facial_expression` contains face presence counts, usable-face activation rate, usable coverage p50/p95, expression status counts, and expression-triggered review rate; expression denominators use only `USABLE` face results so absent faces do not look like failures. The final state is:

- `INSUFFICIENT_REAL_DATA` when the backend is synthetic, any candidate is synthetic, fewer than 50 real candidates exist, a required scenario bucket is missing, or candidate IDs/results do not match one-to-one;
- `NO_GO` when the real population is complete but at least one threshold fails;
- `READY_FOR_HUMAN_GO_NO_GO_REVIEW` when population and every threshold pass.

It must never output `QUALIFIED`, `ENFORCE`, or a qualification manifest.

- [ ] **Step 4: Run metrics tests and type checking**

Run:

```powershell
python -m pytest tests/continuity_spike/test_metrics.py -q
python -m mypy scripts/continuity_spike_metrics.py
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
- Produces: `query_ollama_ps`, `query_nvidia_smi`, and `capture_runtime_snapshot`; all network and process callables are injected in tests.

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
python -m pytest tests/continuity_spike/test_runtime.py -v
```

Expected: collection fails because `scripts.continuity_spike_runtime` does not exist.

- [ ] **Step 3: Implement read-only telemetry with structured failures**

`query_ollama_ps` issues only `GET <base_url>/api/ps` with a five-second timeout. `query_nvidia_smi` issues only the exact query command in the test. `capture_runtime_snapshot` records UTC time, monotonic nanoseconds, phase, candidate ID, requested mode, available payloads, and structured errors such as `ollama_unavailable` or `nvidia_smi_unavailable`. Telemetry failure must not fail a candidate evaluation.

- [ ] **Step 4: Add a source-level guard against lifecycle commands**

The test reads `scripts/continuity_spike_runtime.py` and asserts it contains none of these command fragments: `ollama stop`, `ollama run`, `ollama pull`, `/api/generate`, `/api/chat`, or `/api/embed`. This is defense in depth in addition to injected-call tests.

- [ ] **Step 5: Run telemetry tests and type checking**

Run:

```powershell
python -m pytest tests/continuity_spike/test_runtime.py -q
python -m mypy scripts/continuity_spike_runtime.py
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

Also test two successive runs create different directories, unknown CLI values fail before writes, source mutation during backend evaluation produces an `ERROR` result with `source_hash_mismatch`, one backend exception does not abort later candidates, a usable face with expression `ABSTAIN` is normalized to `REVIEW_REQUIRED`, and artifact JSON contains no NaN/Infinity.

- [ ] **Step 2: Run CLI tests to verify they fail**

Run:

```powershell
python -m pytest tests/continuity_spike/test_cli.py -v
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

`run()` first parses and validates the manifest and verifies every source hash. If `--dry-run` is present, print a sorted, finite JSON object containing `valid`, schema version, candidate count, real/synthetic counts, scenario counts, and source hashes, then return without resolving the backend or creating directories.

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

- [ ] **Step 5: Implement per-candidate isolation and immutable artifacts**

For each candidate:

1. verify source SHA-256;
2. capture `before_candidate` telemetry;
3. measure `time.perf_counter_ns()` around `backend.evaluate(candidate)`;
4. use `dataclasses.replace` to replace the backend-reported wall clock with the harness measurement;
5. use `dataclasses.replace` to apply the harness policy: amplitude `REVIEW`/`UNAVAILABLE`, or a usable-face expression `ABSTAIN`/`UNAVAILABLE`/`REVIEW`, upgrades an otherwise `AUTO_PASS` candidate to `REVIEW_REQUIRED`; neither family can create `HARD_REJECT` alone;
6. call `validate_probe_result(candidate, result)` and convert remaining invalid backend output to `ERROR` with `backend_contract_invalid`;
7. verify source SHA-256 again;
8. convert backend exceptions or hash changes through `make_error_result` with a stable reason code;
9. capture `after_candidate` telemetry;
10. write one result JSON and continue.

After all candidates, write runtime snapshots and the aggregate report. Snapshot the normalized input manifest, resolved absolute paths, backend ID/synthetic flag, Python/platform identity, and invocation arguments without environment variables or secrets.

- [ ] **Step 6: Run CLI tests, all spike tests, and type checking**

Run:

```powershell
python -m pytest tests/continuity_spike/test_cli.py -q
python -m pytest tests/continuity_spike -q
python -m mypy scripts/continuity_spike.py scripts/continuity_spike_contracts.py scripts/continuity_spike_backends.py scripts/continuity_spike_metrics.py scripts/continuity_spike_runtime.py
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

- milestone 0A is harness construction, not the real 50-candidate feasibility result;
- the current repository snapshot had zero source videos when the plan was written;
- input candidates must reference absolute source-video paths and exact SHA-256 values;
- supported scenario tags are the fifteen `REQUIRED_SCENARIOS` values, including subtle/moderate/large motion and visible/occluded/not-visible face cases;
- action amplitude is torso-normalized and camera-compensated, low amplitude alone is not failure, and 0A only validates the contract rather than model accuracy;
- facial expression is conditionally activated by usable-face quality, absent/unusable faces are not penalized, and 0A makes no psychological-state inference;
- `--dry-run` has zero filesystem/service side effects;
- telemetry is observational and does not manage Ollama lifecycle;
- the fake backend and synthetic videos can validate contracts only;
- every run artifact and field is described, including `INSUFFICIENT_REAL_DATA`, `NO_GO`, and `READY_FOR_HUMAN_GO_NO_GO_REVIEW`;
- no report produced by this tool enables production `enforce`;
- GLM Harness can later review the frozen report and selected bundles manually, but the CLI has no GLM integration.

- [ ] **Step 2: Run the focused regression suite**

Run:

```powershell
python -m pytest tests/test_adaptive_direct_transition.py tests/continuity_spike -q
```

Expected: exit code 0.

- [ ] **Step 3: Run strict type checking for every new module**

Run:

```powershell
python -m mypy scripts/continuity_spike.py scripts/continuity_spike_contracts.py scripts/continuity_spike_backends.py scripts/continuity_spike_metrics.py scripts/continuity_spike_runtime.py
```

Expected: exit code 0 with no errors.

- [ ] **Step 4: Run repository non-slow regression tests**

Run:

```powershell
python -m pytest -m "not slow" -q
```

Expected: exit code 0. If failures also reproduce on the pre-task commit, record them as baseline failures and do not edit unrelated code; otherwise fix only regressions caused by files in this plan.

- [ ] **Step 5: Verify scope and artifact safety**

Run:

```powershell
git diff --check
git status --short
git diff --name-only HEAD~5..HEAD
rg -n "torch|onnxruntime|candidate_gifs|CREATE TABLE|ALTER TABLE|ollama stop|ollama run|/api/generate|/api/chat|/api/embed|glm" scripts/continuity_spike*.py
```

Expected: no whitespace errors; only planned files are attributable to these task commits; the final search prints no production dependency, database mutation, model lifecycle, inference endpoint, or GLM integration from the new scripts. References inside tests/documentation are acceptable only when they assert prohibition.

- [ ] **Step 6: Commit documentation and any in-scope verification repair**

```powershell
git add docs/reports/2026-09-continuity-spike/README.md
git commit -m "docs: add continuity spike operator contract"
```

- [ ] **Step 7: Produce the execution handoff**

Report exact focused/full test counts, mypy result, commit hashes, and the caller-owned directory used for smoke artifacts. State `real_media_spike=NOT_RUN`, `source_video_count=0`, `amplitude_model_accuracy=NOT_MEASURED`, `facial_expression_model_accuracy=NOT_MEASURED`, and `production_enforce_qualification=NOT_CREATED`. The next decision is whether to supply original videos and select an experimental detector/pose/tracker/amplitude/conditional-expression backend for milestone 0B.
