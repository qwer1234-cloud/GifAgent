# MoE Aesthetic Quality Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a report-first, content-neutral MoE quality gate that records explainable expert evidence, verifies bounded non-generative repairs, obtains a unified Ollama visual judgment, and applies approved repair recipes during Direct and Staged GIF export.

**Architecture:** A new `app.quality_moe` package owns immutable evidence models, frozen config, deterministic experts, repair search, the Ollama contact-sheet judge, and orchestration. Existing transition/action checks remain authoritative hard gates. Both pipeline modes call the same evaluator after deduplication and persist the assessment inside existing immutable result/manifests; GIF export translates only validated recipes into FFmpeg filters, so original media remains read-only and no new database or external daemon is introduced.

**Tech Stack:** Python 3.11, dataclasses, NumPy, OpenCV, Pillow, httpx, FFmpeg/FFprobe, PyYAML, pytest.

## Global Constraints

- Default mode is `report_only: true`; soft quality decisions cannot remove candidates until calibration is explicitly enabled.
- Soft automatic rejection requires `min_judge_confidence: 0.80` and `min_independent_negative_families: 2` in the frozen config.
- `ABSTAINED`, `UNAVAILABLE`, and `INVALID` evidence never becomes a zero score or a negative vote.
- Adult and non-adult content use the same prompts, dimensions, thresholds, and decision policy; content refusal produces `ABSTAIN`.
- Repairs use original pixels only, use one stable recipe for the complete clip, generate at most `12` proxy variants, require gain `>= 0.15` and confidence `>= 0.80`, and never overwrite source media.
- Geometry is bounded to crop area `>= 70%`, zoom `<= 1.25x`, rotation `<= 2°`, and perspective corner movement `<= 2%`; v1 automatic search enables only transformations whose safety can be verified from sampled pixels.
- Generative outpainting, new viewpoints, missing-content synthesis, and action generation are excluded.
- Deterministic Task Engine state remains authoritative; assessments are append-only content in immutable task artifacts and direct-mode result JSON.
- Expert inference must be bundled in-process or use the configured Ollama service; no second user-managed service is introduced.
- The supplied movie is read-only input; smoke artifacts go under ignored `build/quality_moe_smoke/` and production `data/*.db`, exports, labels, checkpoints, and writable configs are not mutated.

---

### Task 1: Evidence Models, Frozen Configuration, and Policy Guard

**Files:**
- Create: `app/quality_moe/__init__.py`
- Create: `app/quality_moe/models.py`
- Create: `app/quality_moe/config.py`
- Create: `app/quality_moe/policy.py`
- Create: `tests/quality_moe/__init__.py`
- Create: `tests/quality_moe/test_models_config_policy.py`

**Interfaces:**
- Consumes: full job configuration mapping and existing per-clip transition/action fields.
- Produces: `QualityMoeConfig.from_mapping(mapping)`, `ExpertEvidence`, `RepairRecipe`, `QualityAssessment`, `hard_gate_reasons(clip)`, and `enforce_decision(...)`.

- [ ] **Step 1: Write failing model and configuration tests**

```python
def test_non_available_evidence_has_no_numeric_vote():
    evidence = ExpertEvidence(
        candidate_id="c1", evaluation_version="quality-moe-v1",
        expert_id="judge", expert_version="v1", signal_family="semantic_video_critic",
        status=EvidenceStatus.ABSTAINED,
    )
    assert evidence.available_scores() == {}

def test_config_rejects_unsafe_repair_limits():
    with pytest.raises(ValueError, match="max_proxy_variants"):
        QualityMoeConfig.from_mapping({"quality_moe": {"repairability": {"max_proxy_variants": 13}}})
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/quality_moe/test_models_config_policy.py`

Expected: collection fails because `app.quality_moe` does not exist.

- [ ] **Step 3: Implement immutable JSON-safe domain models and strict config parsing**

```python
class EvidenceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    ABSTAINED = "ABSTAINED"
    INVALID = "INVALID"

class QualityDecision(str, Enum):
    KEEP_AS_IS = "KEEP_AS_IS"
    KEEP_FOR_REPAIR = "KEEP_FOR_REPAIR"
    REVIEW = "REVIEW"
    REJECT = "REJECT"
    ABSTAIN = "ABSTAIN"

@dataclass(frozen=True)
class ExpertEvidence:
    candidate_id: str
    evaluation_version: str
    expert_id: str
    expert_version: str
    signal_family: str
    status: EvidenceStatus
    scores: Mapping[str, float] = field(default_factory=dict)
    findings: tuple[Mapping[str, object], ...] = ()
    summary: str = ""
    input_hash: str = ""
    config_hash: str = ""
    prompt_hash: str | None = None
    latency_ms: int = 0
```

`QualityMoeConfig.from_mapping` must copy values, validate finite ranges, canonicalize and hash the resolved mapping, and expose `to_dict()` without reading environment variables.

- [ ] **Step 4: Add policy tests for hard-gate precedence and precise soft rejection**

```python
@pytest.mark.parametrize("field,value", [
    ("transition_action", "drop"),
    ("action_completeness_score", 0.2),
])
def test_hard_gate_cannot_be_overridden_by_keep(field, value):
    clip = {field: value}
    result = enforce_decision(
        proposed=QualityDecision.KEEP_AS_IS,
        confidence=0.99,
        negative_signal_families=(),
        hard_reasons=hard_gate_reasons(clip),
        repair=None,
        config=QualityMoeConfig.defaults(),
    )
    assert result.decision is QualityDecision.REJECT

def test_soft_reject_requires_two_independent_families():
    result = enforce_decision(
        proposed=QualityDecision.REJECT, confidence=0.95,
        negative_signal_families=("nr_vqa",), hard_reasons=(), repair=None,
        config=QualityMoeConfig.defaults(),
    )
    assert result.decision is QualityDecision.REVIEW
```

- [ ] **Step 5: Implement `hard_gate_reasons` and `enforce_decision`**

The guard must make hard failure `REJECT`, demote unprotected soft rejection to `REVIEW`, require an existing validated recipe for `KEEP_FOR_REPAIR`, and map judge refusal to `ABSTAIN` without adding a negative family.

- [ ] **Step 6: Run tests and commit Task 1**

Run: `uv run pytest -q tests/quality_moe/test_models_config_policy.py`

Expected: all tests pass.

Commit: `feat: add quality MoE evidence and policy contracts`

---

### Task 2: Deterministic Sampling and Complementary Experts

**Files:**
- Create: `app/quality_moe/sampling.py`
- Create: `app/quality_moe/experts.py`
- Create: `tests/quality_moe/test_sampling_experts.py`

**Interfaces:**
- Consumes: `video_path`, exact `start_ts`/`end_ts`, `candidate_id`, and existing clip evidence.
- Produces: `SampledClip`, `sample_clip_frames(...)`, `TechnicalAestheticExpert.evaluate(...)`, `TemporalExpert.evaluate(...)`, and `CinematicExpert.evaluate(...)`.

- [ ] **Step 1: Write synthetic-frame expert tests**

```python
def test_underexposed_clip_reports_repairable_exposure_issue():
    frames = tuple(np.full((90, 160, 3), 12, dtype=np.uint8) for _ in range(6))
    evidence = TechnicalAestheticExpert().evaluate(sampled_clip(frames))
    assert evidence.status is EvidenceStatus.AVAILABLE
    assert evidence.scores["technical_integrity"] < 0.5
    assert any(f["code"] == "underexposed_subject" for f in evidence.findings)

def test_single_flash_frame_is_temporal_negative_signal():
    frames = [np.full((90, 160, 3), 80, np.uint8) for _ in range(6)]
    frames[3] = np.full((90, 160, 3), 250, np.uint8)
    evidence = TemporalExpert().evaluate(sampled_clip(tuple(frames)))
    assert evidence.scores["temporal_coherence"] < 0.7
    assert evidence.signal_family == "deterministic_temporal"
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest -q tests/quality_moe/test_sampling_experts.py`

Expected: imports fail for missing sampling and expert modules.

- [ ] **Step 3: Implement bounded, timestamped sampling**

`sample_clip_frames` uses OpenCV random access at 6–8 evenly spaced timestamps, scales longest side to at most 640, verifies every timestamp lies inside the exact candidate interval, and returns `UNAVAILABLE`-compatible diagnostics instead of scanning neighboring files.

- [ ] **Step 4: Implement three independent expert outputs**

Use hand-bounded metrics:

```python
exposure_score = 1.0 - min(1.0, abs(median_luma - 0.5) / 0.5)
sharpness_score = 1.0 - math.exp(-laplacian_variance / 250.0)
clipping_penalty = min(1.0, shadow_clip + highlight_clip)
loop_score = 1.0 - normalized_frame_difference(first, last)
```

The technical expert emits `nr_vqa`; temporal emits `deterministic_temporal`; cinematic emits `cinematic_classifier`. Film-style attributes such as low-key lighting, handheld motion, or deliberate blur are descriptive unless a separate measurable failure exists.

- [ ] **Step 5: Add tests for content neutrality, bounded scores, sampling failures, and deterministic repeatability**

Tests compare identical pixels with different semantic labels and require identical scores; corrupt/nonexistent media returns a typed unavailable result; all scores are finite in `[0, 1]`.

- [ ] **Step 6: Run tests and commit Task 2**

Run: `uv run pytest -q tests/quality_moe/test_sampling_experts.py`

Expected: all tests pass.

Commit: `feat: add deterministic quality experts`

---

### Task 3: Verified Non-Generative Repair Search and FFmpeg Filters

**Files:**
- Create: `app/quality_moe/repair.py`
- Create: `tests/quality_moe/test_repair.py`

**Interfaces:**
- Consumes: `SampledClip`, `QualityMoeConfig`, and the deterministic expert scorer.
- Produces: `search_repairs(...) -> RepairSearchResult`, `apply_recipe_to_frame(...)`, and `build_ffmpeg_filter(recipe, fps, max_width)`.

- [ ] **Step 1: Write repair bound and stable-clip tests**

```python
def test_search_never_exceeds_twelve_variants_and_uses_one_recipe_per_clip():
    result = search_repairs(dark_sample(), QualityMoeConfig.defaults())
    assert len(result.evaluated_recipes) <= 12
    assert result.best_recipe is not None
    transformed = [apply_recipe_to_frame(frame, result.best_recipe) for frame in dark_sample().frames]
    assert len({result.best_recipe.recipe_id for _ in transformed}) == 1

def test_invalid_crop_is_rejected():
    with pytest.raises(ValueError, match="crop area"):
        RepairRecipe(recipe_id="bad", crop=(0.0, 0.0, 0.5, 0.5)).validate()
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest -q tests/quality_moe/test_repair.py`

Expected: missing repair module.

- [ ] **Step 3: Implement finite photometric recipe enumeration and pixel transforms**

Enumerate only the approved EV, gamma, contrast, shadow/highlight, and white-balance values. Apply one recipe to every sampled frame with clipped float arithmetic, never invent pixels, and save only original/best contact sheets to the provided work directory.

- [ ] **Step 4: Implement verified repair selection**

Re-run technical and cinematic experts on every actual transformed proxy. A recipe is validated only when `quality_gain >= 0.15`, confidence `>= 0.80`, temporal coherence does not regress, and clipping/sharpness safety checks pass. Emit `repair_delta` evidence from measured original/best scores.

- [ ] **Step 5: Implement FFmpeg filter generation with exact bounds**

```python
def build_ffmpeg_filter(recipe: RepairRecipe | None, *, fps: int, max_width: int) -> str:
    filters = [f"fps={fps}"]
    if recipe is not None:
        filters.extend(recipe_to_safe_filters(recipe))
    filters.append(f"scale={max_width}:-1:flags=lanczos")
    return ",".join(filters)
```

Both palette generation and GIF encoding must consume the identical returned prefix. Reject unknown recipe fields rather than passing arbitrary strings to FFmpeg.

- [ ] **Step 6: Run tests and commit Task 3**

Run: `uv run pytest -q tests/quality_moe/test_repair.py`

Expected: all tests pass.

Commit: `feat: add bounded quality repair search`

---

### Task 4: Unified Ollama Contact-Sheet Judge

**Files:**
- Create: `app/quality_moe/judge.py`
- Create: `tests/quality_moe/test_judge.py`

**Interfaces:**
- Consumes: original/best-proxy contact sheets, all evidence statuses, allowed recipe IDs, frozen judge config, and an injected HTTP transport.
- Produces: `OllamaQualityJudge.judge(...) -> JudgeResult` with strict decision/schema fields and prompt hash.

- [ ] **Step 1: Write tests for valid JSON, refusal, malformed output, and recipe hallucination**

```python
def test_refusal_becomes_abstain_without_negative_vote(fake_transport):
    fake_transport.respond({"response": "I cannot assess explicit content."})
    result = judge(fake_transport).judge(request())
    assert result.decision is QualityDecision.ABSTAIN
    assert result.evidence.status is EvidenceStatus.ABSTAINED
    assert result.negative_signal_families == ()

def test_unknown_recipe_id_is_invalid(fake_transport):
    fake_transport.respond({"response": json.dumps(valid_payload(selected_recipe_id="invented"))})
    result = judge(fake_transport).judge(request(allowed_recipe_ids=("repair-1",)))
    assert result.evidence.status is EvidenceStatus.INVALID
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest -q tests/quality_moe/test_judge.py`

Expected: missing judge module.

- [ ] **Step 3: Implement content-neutral prompt and contact-sheet request**

The prompt explicitly prohibits topic/identity/adult-content scoring, supplies the six dimensions and decision enum, marks unavailable experts, and requires one JSON object. Send original and repaired sheets through Ollama `/api/generate`, temperature `0`, with the configured model and timeout.

- [ ] **Step 4: Implement strict parsing and one structural retry**

Use `app.services.json_guard.parse_json_response`, validate finite `[0,1]` dimensions, independent-family names, reason codes, and selected recipe membership. One malformed response receives a correction prompt using the same images; a second failure returns `INVALID` and `ABSTAIN`.

- [ ] **Step 5: Run tests and commit Task 4**

Run: `uv run pytest -q tests/quality_moe/test_judge.py`

Expected: all tests pass without network access.

Commit: `feat: add unified Ollama quality judge`

---

### Task 5: Candidate Evaluator, Provenance, and Report-Only Routing

**Files:**
- Create: `app/quality_moe/evaluator.py`
- Create: `tests/quality_moe/test_evaluator.py`

**Interfaces:**
- Consumes: exact source video and clip mapping, frozen config, work directory, optional judge/experts for dependency injection.
- Produces: `evaluate_candidate(...) -> QualityAssessment` and `evaluate_candidates(...) -> QualityBatchResult`.

- [ ] **Step 1: Write orchestration tests**

Cover parallel evidence collection order independence, judge-unavailable degradation, hard-gate short-circuit, report-only retention, active filtering, repair-before-reject routing, and stable provenance hashes.

```python
def test_report_only_keeps_rejected_candidate_but_records_recommendation(tmp_path):
    batch = evaluate_candidates(..., config=config(report_only=True), judge=rejecting_judge())
    assert len(batch.effective_clips) == 1
    assert batch.assessments[0].decision is QualityDecision.REJECT
    assert batch.effective_clips[0]["quality_assessment"]["decision"] == "REJECT"
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest -q tests/quality_moe/test_evaluator.py`

Expected: missing evaluator module.

- [ ] **Step 3: Implement deterministic orchestration**

Sample once, run three low-cost experts, route low/gray/conflicting candidates through repair search, call the judge with original and best proxy, then enforce policy. Sort serialized evidence by `(signal_family, expert_id)` so hashes and manifests are repeatable.

- [ ] **Step 4: Implement append-only provenance fields**

Assessment JSON includes input file SHA-256, candidate boundaries, frame timestamps, evaluation/config/model/prompt hashes, evidence statuses, recipe, latency, recommendation, and effective routing. It does not include secrets or raw API keys.

- [ ] **Step 5: Run tests and commit Task 5**

Run: `uv run pytest -q tests/quality_moe/test_evaluator.py`

Expected: all tests pass.

Commit: `feat: orchestrate MoE quality assessment`

---

### Task 6: Direct/Staged Pipeline and Immutable Manifest Integration

**Files:**
- Modify: `scripts/test_video_adaptive.py`
- Modify: `app/task_engine/artifacts.py`
- Modify: `configs/models.yaml`
- Modify: `build_exe.spec`
- Modify: `tests/test_adaptive_config.py`
- Modify: `tests/task_engine/test_manifest_validation.py`
- Modify: `tests/task_engine/test_production_artifact_contract.py`
- Modify: `tests/task_engine/test_packaged_stage_imports.py`
- Create: `tests/quality_moe/test_pipeline_integration.py`

**Interfaces:**
- Consumes: `evaluate_candidates` and `build_ffmpeg_filter` from Tasks 3 and 5.
- Produces: Direct result `quality_moe` summary, staged rank manifest `quality_moe` section plus per-clip `quality_assessment`, and repaired GIF exports tied to a validated `selected_recipe_id`.

- [ ] **Step 1: Write failing config and staged-manifest tests**

Assert that `extract_config` resolves the frozen `quality_moe` mapping, staged rank manifests accept valid evidence but reject unknown decisions, and zero-clip manifests still carry a quality summary with zero counts.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest -q tests/test_adaptive_config.py tests/task_engine/test_manifest_validation.py tests/quality_moe/test_pipeline_integration.py`

Expected: missing quality configuration/manifest fields.

- [ ] **Step 3: Integrate the evaluator after dedup in both paths**

Direct mode evaluates deduped clips before preference ranking/export. Staged `rank_dedup` assigns stable IDs, evaluates the planned candidates, writes evidence into the immutable `rank_dedup_manifest`, and in active mode fans out only `effective_clips`. Default report-only mode preserves current counts and ordering.

- [ ] **Step 4: Apply only validated repair recipes to GIF export**

Replace duplicated filter strings with `build_ffmpeg_filter`. Use the same recipe filter for palette and GIF commands. Copy `quality_decision`, current/recoverable quality, selected recipe, evidence/config hashes, and parent source identity into `gif_clip_manifest` and materialized result JSON.

- [ ] **Step 5: Add production artifact and packaging coverage**

`validate_manifest_json` validates decision enums, confidence, available-score ranges, recipe bounds, and report-only routing. Add every new `app.quality_moe` module to packaged import coverage so frozen stage execution imports successfully.

- [ ] **Step 6: Run targeted integration gates and commit Task 6**

Run:

```powershell
uv run pytest -q tests/quality_moe tests/test_adaptive_config.py tests/task_engine/test_manifest_validation.py tests/task_engine/test_production_artifact_contract.py tests/task_engine/test_packaged_stage_imports.py
```

Expected: all tests pass.

Commit: `feat: integrate quality MoE into GIF pipeline`

---

### Task 7: Read-Only Movie Smoke Runner, Documentation, and Release Verification

**Files:**
- Create: `scripts/evaluate_quality_moe.py`
- Create: `tests/quality_moe/test_cli.py`
- Modify: `Agent.md`
- Modify: `docs/superpowers/specs/2026-08-09-moe-aesthetic-quality-and-repairability-design.md`

**Interfaces:**
- Consumes: one explicit video path, optional start/duration, frozen YAML config, and output directory.
- Produces: a JSON assessment, original/best contact sheets, and exit status without scanning directories or mutating production data.

- [ ] **Step 1: Write CLI isolation tests**

Run the CLI entry function against a temporary synthetic video and assert it evaluates only the exact file, rejects output paths equal to the source, emits JSON, and leaves source SHA-256 unchanged.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest -q tests/quality_moe/test_cli.py`

Expected: CLI module missing.

- [ ] **Step 3: Implement the explicit-file smoke CLI**

Arguments are `--video`, `--start`, `--duration`, `--config`, `--output-dir`, and `--skip-judge`. The CLI defaults to a 12-second bounded clip, creates the output directory only after source validation, and writes `quality_assessment.json` atomically.

- [ ] **Step 4: Document operational behavior and design implementation status**

Document report-only default, Ollama judge degradation, repair artifact location, activation/calibration gate, and non-generative bounds. Update the design status to implemented only after all verification commands pass.

- [ ] **Step 5: Run the supplied movie smoke test read-only**

Run:

```powershell
$video = 'C:\Users\sunhao\Desktop\ToWatch\现代爱情故事.1991.BD1080p.国英双语中字.mp4'
$before = (Get-FileHash -Algorithm SHA256 -LiteralPath $video).Hash
uv run python scripts/evaluate_quality_moe.py --video $video --start 1800 --duration 12 --output-dir build/quality_moe_smoke/modern-love-1991
$after = (Get-FileHash -Algorithm SHA256 -LiteralPath $video).Hash
if ($before -ne $after) { throw 'Source video hash changed' }
```

If the movie is shorter than 1812 seconds, the CLI clamps start to the centered 12-second interval and records the resolved interval.

- [ ] **Step 6: Run fresh release gates**

```powershell
uv run python -m compileall -q app scripts tests
uv run pytest -q tests/task_engine/test_full_production_stage_chain.py -s
uv run pytest -q tests/task_engine tests/quality_lab
uv run pytest -q
git diff --check
```

Also compare `Get-FileHash data/*.db` before and after the gate; no production database hash may change.

- [ ] **Step 7: Review requirements, staged scope, and commit Task 7**

Re-read every design section against the implementation, inspect `git diff --stat` and `git status --short`, and commit only source, tests, config, plan, and documentation.

Commit: `docs: finish quality MoE rollout`
