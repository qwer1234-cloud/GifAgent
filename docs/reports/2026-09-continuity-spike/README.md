# Continuity Spike (Milestone 0A) — Operator Contract

Date: 2026-09-05
Status: harness construction complete; real-media feasibility measurement NOT run
Spec: `docs/superpowers/specs/2026-09-04-person-semantic-continuity-body-curve-design.md`
Plan: `docs/superpowers/plans/2026-09-05-continuity-milestone-0a-implementation.md`

## 1. What milestone 0A is — and is not

Milestone 0A delivers the **read-only feasibility harness** for the person
semantic-continuity / body-curve design: contracts, a deterministic fake
backend, statistical aggregation, read-only telemetry, and an immutable CLI
runner.

**0A is not the feasibility result.** The real measurement (50 real
source-video candidates plus 200 stratified historical GIFs with a real
detector/pose/tracker/amplitude/face backend) is milestone **0B** and remains
a separate go/no-go execution. No report produced by this tool enables
production `enforce`, signs a qualification manifest, or changes any GIF
business state. `real_media_spike=NOT_RUN`,
`historical_gif_yield_spike=NOT_RUN`, `amplitude_model_accuracy=NOT_MEASURED`,
`face_identity_gain=NOT_MEASURED`,
`facial_advisory_marginal_capture=NOT_MEASURED`,
`production_enforce_qualification=NOT_CREATED`.

The CLI never calls GLM or any remote model. GLM-5.3-Flash inside the Harness
may later review a frozen report and review bundles manually; that is an
explicit human action, and the CLI has no GLM integration.

## 2. Running the harness

All commands use the repository venv:

```powershell
# Validate a manifest only: no output directory, no backend, no telemetry.
.\.venv\Scripts\python.exe -m scripts.continuity_spike `
  --manifest path\to\manifest.json `
  --output-dir path\to\runs `
  --backend fake `
  --dry-run

# Normal run (immutable run directory; telemetry off by default).
.\.venv\Scripts\python.exe -m scripts.continuity_spike `
  --manifest path\to\manifest.json `
  --output-dir path\to\runs `
  --backend fake `
  --telemetry off
```

CLI surface:

| Flag | Meaning |
| --- | --- |
| `--manifest PATH` | required; strict-validated spike input manifest |
| `--output-dir PATH` | required caller-owned directory; a fresh `run-<uuid>` child is claimed per run |
| `--backend SPEC` | `fake` (default) or `module:function` naming a zero-argument factory |
| `--telemetry MODE` | `off` (default) \| `ollama` \| `gpu` \| `all` |
| `--ollama-base-url URL` | default `http://127.0.0.1:11434`; restricted to http(s), no query/fragment, redirects disabled |
| `--dry-run` | validation and stdout JSON only; zero filesystem/service side effects |

`--dry-run` does not create the output directory, does not load a backend,
does not capture telemetry, and prints one sorted finite JSON object with
population/label/scenario/truncation/duration summaries. Resolution and
subject-size strata stay `NOT_MEASURED` in dry-run because they come from
backend evidence, not filename assumptions.

## 3. Manifest input contract

Input items must explicitly declare their media kind and freeze every
identity field; the loader rejects unknown keys, booleans in numeric fields,
non-finite numbers, duplicate IDs/tags, non-snake-case tags, relative paths,
missing files, and hashes that are not 64 lowercase hex characters. JSON
errors always name the field path (for example `candidates[0].window_end_s`).

- `media_kind` must be `source_video` or `historical_gif`.
- `source_path` must be absolute and must exist; `source_sha256` is verified
  immediately before and after each per-source batch and on every dry-run.
- `source_video` items must satisfy
  `0 <= confirmed_shot_start_s <= context_start_s <= window_start_s <
  window_end_s <= context_end_s <= confirmed_shot_end_s <= media_duration_s`,
  so the backend's default two-second context can never cross an already
  confirmed shot boundary.
- Every `source_video` item carries exactly one of
  `subtle_motion`/`moderate_motion`/`large_motion` and exactly one of
  `face_visible`/`face_occluded`/`face_not_visible`. These are human
  sampling-bucket labels, not model conclusions.
- Every `historical_gif` item must set shot/context/window bounds to `0` and
  `media_duration_s`, and must use `human_label=UNLABELED`. A `feedback_label`
  of `unknown` is valid for contract testing, but only `favorite`/`like`/
  `dislike`/`neutral` values count toward the real 0B population.
- `data_origin` must be `real` or `synthetic`; `human_label` is
  `ACCEPTABLE`, `INVALID`, or `UNLABELED`.

The fifteen supported source-video scenario tags are exactly
`REQUIRED_SOURCE_VIDEO_SCENARIOS`:
`single_subject`, `co_primary_subjects`, `partial_body`, `headless`,
`close_contact`, `occlusion`, `fast_motion`, `subtle_motion`,
`moderate_motion`, `large_motion`, `face_visible`, `face_occluded`,
`face_not_visible`, `low_light`, `media_edge`. A real go/no-go report
requires every bucket to be populated.

## 4. Evidence semantics frozen by 0A

**Action amplitude.** Amplitude is torso-normalized and camera-compensated.
The noise floor must come from an independent reference — a confidently
static context interval, non-subject background residual, or both — never
from the candidate amplitude distribution itself. Low amplitude alone is not
a failure: the backend reports `subtle`/`moderate`/`large` classes plus phase
completeness. Background compensation requires at least 0.30 background
coverage and 0.50 affine inlier ratio; below either value the amplitude is
`UNAVAILABLE` and the candidate is reviewed. 0A validates this contract, not
model accuracy.

**Face evidence.** Face appearance embedding is additional identity evidence
inside the single `appearance_identity` independence group. Facial
landmarks/expression dynamics are advisory: `NOT_DETECTED`,
`DETECTED_UNUSABLE`, expression `ABSTAIN`, and expression `UNAVAILABLE` never
block an otherwise valid candidate, and a single facial anomaly cannot
produce `HARD_REJECT`. Absent or unusable faces are not penalized. No
psychological-state, personality, consent, or other sensitive-attribute
inference exists anywhere in this harness.

**Evidence independence.** `HARD_REJECT` requires a confirmed
`scene_boundary`, or at least two distinct groups among `scene_boundary`,
`pose_geometry`, `appearance_identity`, `photometric_motion`,
`semantic_vlm`. Multiple signals inside one group count once. The report
flags any same-group hard reject as invalid.

**Batch isolation.** One source batch maps to exactly one
`backend.evaluate_source(...)` call, so a future GPU backend swaps model
families once per video rather than once per candidate. Source SHA-256 is
verified before and after each batch; any mismatch replaces the batch's
results with `source_hash_mismatch` errors. A batch exception produces
`backend_error` results and the run continues with the next source.

**Historical GIFs.** Historical output reports curve-feature availability
and missing reasons, measured per-item cost, effective
favorite/like/dislike training rows, resolution/duration/feedback/subject-
size strata, and `projected_full_inventory_gpu_hours` for the ~16,894-item
inventory. Historical candidates always use amplitude `NOT_APPLICABLE` and
GIF-only dispositions; they can never upgrade to `AUTO_PASS`.

## 5. Runtime artifacts

Every normal run claims a fresh `run-<uuid>` directory; nothing is ever
overwritten (`_atomic_json` refuses to replace an existing artifact, and all
payloads are written with `allow_nan=False` so no `NaN`/`Infinity` can leak
into JSON).

| Artifact | Contents |
| --- | --- |
| `manifest.snapshot.json` | normalized candidates with resolved absolute paths, backend id/synthetic flag, Python/platform identity, invocation argv (no environment variables or secrets) |
| `results/<candidate_id>.json` | one normalized, policy-applied, contract-validated probe result per candidate |
| `runtime.json` | telemetry snapshots (`kind=telemetry`) and measured per-batch entries (`kind=source_batch_timing` with `source_key`, `media_kind`, `candidate_count`, `wall_clock_ms`) |
| `report.json` | the aggregate `continuity-spike-report-v1` report |

Report sections: `population`, `scenario_coverage`,
`evidence_independence`, `motion_amplitude`, `face_identity`,
`facial_advisory`, `historical_curve_features`, `performance`,
`position_quintiles` (candidate position in fifths of the source video,
including budget-exhaustion counts), `vlm`, `threshold_checks`, and
`go_no_go`.

Thresholds (source-video candidates only): candidate p50 ≤ 10,000 ms,
candidate p95 ≤ 30,000 ms, grouped source-video p95 ≤ 1,200,000 ms (measured
from harness `source_batch_timing` entries, not from correlated component
sums), peak VRAM ≤ 14,336 MiB, `AUTO_PASS` ≥ 0.40 on human-`ACCEPTABLE`
candidates, `REVIEW_REQUIRED` ≤ 0.60 on labeled candidates. A missing
measurement (for example no GPU telemetry) fails the check fail-closed.

Go/no-go states:

- `INSUFFICIENT_REAL_DATA` — synthetic backend, any synthetic candidate,
  fewer than 50 real source-video candidates, fewer than 200 real historical
  GIFs, any `UNLABELED` source-video label, any `unknown` historical
  feedback, a missing scenario bucket, an empty historical
  resolution/duration/subject-size bucket, a missing favorite/like/dislike
  label, or candidate/result mismatch. The fake backend forces this state
  regardless of measured values.
- `NO_GO` — complete real population but at least one threshold failure.
- `READY_FOR_HUMAN_GO_NO_GO_REVIEW` — population and every threshold pass.
  Even this state requires human review and never signs an enforce
  qualification manifest.

## 6. Telemetry boundaries

Telemetry is observational. It may query Ollama's process list
(`GET <base-url>/api/ps`, five-second timeout) and read GPU memory counters
via `nvidia-smi --query-gpu=...`. It never stops, loads, unloads, or pulls
any model, and telemetry failures are recorded as structured errors
(`ollama_unavailable`, `ollama_malformed`, `nvidia_smi_unavailable`,
`nvidia_smi_malformed`) without failing the source batch. The
operator-configured endpoint is restricted to http(s) origins whose resolved
addresses are loopback or private-network only (link-local metadata,
reserved, multicast, and public addresses are rejected), and redirects are
disabled.

## 7. Backend plugin contract

`--backend fake` returns `DeterministicFakeBackend` (`backend_id =
"deterministic-fake-v1"`, `synthetic = True`). Any other spec must be
`module:function` naming a zero-argument factory returning an object with a
non-empty string `backend_id`, a boolean `synthetic` flag, and a callable
`evaluate_source(candidates) -> tuple[CandidateProbeResult, ...]` whose
results align one-to-one with the input tuple. Real experimental backends
arrive in milestone 0B; anything a real backend produces must still pass
`validate_probe_result`, and invalid output is converted to
`backend_contract_invalid` errors instead of being trusted.

## 8. 0A limitations

- No detector, pose, segmentation, ReID, face, expression, or curve-feature
  model is included, loaded, or measured; no Torch/ONNXRuntime dependency is
  added.
- The fake backend's favorable values exist only to exercise the report
  contract; its `synthetic=True` flag keeps every such run statistically
  ineligible.
- Production pipeline code, SQLite schema, `candidate_gifs`, task-engine
  stages, ranking/dedup behavior, Quality MoE policy, and GIF encoding are
  unchanged by 0A. The pipeline-ordering freeze is a characterization test
  in `tests/test_adaptive_direct_transition.py`.
- The next decision is milestone 0B: build an explicit 50-video-candidate /
  200-historical-GIF manifest and supply a real experimental
  detector/pose/tracker/amplitude/face backend, then re-run this CLI and
  review the frozen report by hand.
