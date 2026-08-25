# Pipeline Throughput and GIF Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run and a commit containing only the files listed for that task.

**Goal:** Cut single-video end-to-end wall time to 40% or less of the current baseline while removing the deterministic GIF encoding defects (non-divisible frame rate, default palette allocation, dither crawl) and making VLM scoring reproducible — without changing the eight-stage task graph, the per-clip `gif_clip` retry contract, or any historical data.

**Architecture:** Every behavior change is a new frozen-snapshot config key whose default reproduces today's behavior. Work lands in four independently shippable phases: (A) measurement plus zero-risk encode/determinism fixes, (B) model-lifecycle repair plus the two-tier scoring prompt and parallel frame extraction, (C) intra-stage VLM concurrency plus stage-class worker concurrency, (D) sub-second boundary snapping, score calibration, and a Quality Lab model A/B. Frame extraction is consolidated into one shared service; palette flags are consolidated into one shared builder so Direct and Staged always emit identical FFmpeg commands.

**Tech Stack:** Python 3.11+, `concurrent.futures.ThreadPoolExecutor`, httpx, FFmpeg/ffprobe, OpenCV + NumPy (already declared), Ollama, SQLite, pytest, YAML/Gradio configuration UI.

**Design:** `docs/superpowers/specs/2026-08-23-pipeline-throughput-and-gif-quality-design.md`

## Global Constraints

- Do not change the stage graph: `discover -> sample -> vlm -> refine -> synthesize -> rank_dedup -> gif_clip -> materialize`.
- Do not change the `gif_clip` per-clip fan-out contract. Each clip must remain independently retryable.
- Do not add a new heavyweight dependency.
- Every new config key must default to current behavior and must flow through `extract_config()` in `scripts/test_video_adaptive.py` into the frozen job snapshot. No ambient environment variable may select behavior.
- Direct and Staged paths must produce identical export windows and identical FFmpeg command shapes for the same frozen config.
- Performance-only keys (`*_workers`, `num_predict`, `keep_alive`, `free_vram_before_load`) must NOT enter `quality_moe_config_hash` or `action_config_hash`. Output-affecting keys (`gif_*`, `score_schema_mode`, `vlm_seed`, `single_frame_max_duration_s`, `boundary_snap_*`, `score_calibration_*`) MUST enter the relevant hash.
- Strict `gif_worthiness` validation is untouchable: finite, in `[0, 1]`, bool rejected, never a 0.5 fallback, three retries.
- Existing user changes and historical GIF/database/checkpoint/label data must not be deleted or overwritten. All tests use `tmp_path` and temporary SQLite.
- `adaptive.clear_output_dir` defaults to `true`, so re-running a video wipes its output folder. Benchmark runs must use copies or new directories, never in-place reruns of historical videos.
- `cpu_stage_workers` must stay at `1` until every concurrency-safety test in Task 12 passes.
- `CONFIG_FIELD_KEYS` / `CONFIG_FIELD_HELP` have their canonical definition in `app/ui/tabs/settings.py`; `app/ui/candidate_review.py` re-exports them, but `app/ui/legacy_candidate_review.py` carries an independent duplicate copy. Adding a Settings field means updating both definitions, supplying Chinese help text, and bumping the hard-coded count in `tests/test_config_help_annotations.py` (currently `assert len(CONFIG_FIELD_KEYS) == 28`).
- `configs/models.adult_candidate.yaml` has **already drifted** from `configs/models.yaml` (it still carries `worthiness_threshold: 0.42`, `refine_threshold: 0.55`, `merge_score_threshold: 0.50`, `vlm_temperature: 0.50`, `gif_fps: 24`, `max_duration: 20`, and no `max_refine_frames`), even though `README.md` and `Agent.md` describe it as a mirror. Do not assume parity. When this plan says to set a value "in both YAML files", set it explicitly in each; do not copy one over the other.
- Target hardware: 16GB VRAM, VLM ~12GB.



## File Map

**Create**

- `app/services/frame_extract.py`: single `extract_frames()` entry point with bounded thread pool, deterministic ordering, per-timestamp error attribution.
- `app/services/gif_encode.py`: `build_palette_filters()` — whitelisted `palettegen` / `paletteuse` argument construction shared by Direct and Staged.
- `app/services/stage_timing.py`: lightweight timing collector serialized into manifest `timings`.
- `app/services/boundary_snap.py`: sub-second start/end snapping over `TemporalEvidenceCache`.
- `app/services/score_calibration.py`: load, validate provenance, and apply a frozen calibrator.
- `scripts/fit_score_calibration.py`: fit a calibrator from `preference_events` using `app/quality_lab/calibration.py`.
- `tests/test_frame_extract.py`
- `tests/test_gif_encode.py`
- `tests/test_stage_timing.py`
- `tests/test_two_tier_scoring.py`
- `tests/test_boundary_snap.py`
- `tests/test_score_calibration.py`
- `tests/task_engine/test_stage_class_concurrency.py`

**Modify**

- `scripts/test_video_adaptive.py`: `extract_config()`, `_score_vlm_frame()`, `get_score_prompt()`, `wait_model()`, `stop_model()`, six frame-extraction call sites, both GIF export call sites, `_stage_vlm`, `_stage_refine`, `_stage_rank_dedup`, `_stage_gif_clip`, `run_pipeline`. `_stage_synthesize` is deliberately **not** modified — see Task 9 Step 4.
- `app/services/gif_windows.py`: `single_frame_max_duration_s` parameter.
- `app/quality_moe/repair.py`: keep `build_ffmpeg_filter` prefix-only; palette args move to `gif_encode.py`.
- `app/task_engine/repository.py`: optional `stage_names` filter on `claim_stage()`.
- `app/task_engine/worker.py`: stage-class awareness.
- `scripts/task_worker.py`: GPU/CPU worker thread counts.
- `app/ui/launcher.py`: start configured worker threads.
- `configs/models.yaml`, `configs/models.adult_candidate.yaml`: new keys.
- `app/ui/tabs/settings.py`: expose the user-facing subset.
- `build_exe.spec`: hidden imports for new service modules if not collected.
- `tests/test_adaptive_config.py`, `tests/test_gif_windows.py`, `tests/test_batch_logging.py`, `tests/test_config_help_annotations.py`, `tests/task_engine/test_vlm_stage_runtime.py`, `tests/task_engine/test_lease_isolation.py`, `tests/task_engine/test_packaged_stage_imports.py`.
- `README.md`, `Agent.md`: document new keys, tuning guidance, and the benchmark procedure.

**Create at the end (only after real runs succeed)**

- `docs/reports/pipeline-throughput-baseline-2026-08-23.md`

---



## Phase A — Measurement and Zero-Risk Corrections



### Task 1: Stage and loop timing instrumentation

**Files:**

- Create: `app/services/stage_timing.py`, `tests/test_stage_timing.py`
- Modify: `scripts/test_video_adaptive.py`

**Interfaces:**

- `StageTimings.span(name)` context manager; `record(name, ms)`; `observe_vlm(eval_count, total_ms)`.
- `StageTimings.to_dict()` → `{"totals_ms": {...}, "p50_ms": {...}, "counts": {...}, "vlm_output_tokens": int}`.

- [ ] **Step 1: Write failing tests**

Cover: totals accumulate across spans, p50 is computed from recorded samples, `to_dict()` is JSON-serializable and key-ordered (so manifests stay byte-reproducible), a zero-sample metric is omitted rather than emitting `null`, and the collector never raises when a span body raises (it must record then re-raise).

- [ ] **Step 2: Implement** `stage_timing.py`

No new dependency. Use `time.perf_counter()`. Keep it allocation-cheap: it will wrap ~900 VLM calls per video.

- [ ] **Step 3: Wire into the pipeline**

Instrument, in both Direct and Staged paths:

- frame extraction loops (`extract_ms`),
- `_score_vlm_frame` call sites (`vlm_ms`, `vlm_calls`),
- `wait_model` / `stop_model` (`model_wait_ms`),
- GIF export attempts (`gif_export_ms`),
- whole-stage wall time in `run_stage_mode()`.

Capture `eval_count` from the Ollama response body in `_score_vlm_frame` and feed it to `observe_vlm`. This is the metric that directly quantifies discarded tokens.

Serialize into each stage manifest under a new `timings` key and into the final `result_*.json`. `timings` must be additive: schema validators must accept manifests without it.

- [ ] **Step 4: Run focused tests**

```powershell
uv run pytest -q tests/test_stage_timing.py tests/task_engine/test_manifest_validation.py tests/test_adaptive_config.py
```

- [ ] **Step 5: Commit**

```powershell
git add -- app/services/stage_timing.py tests/test_stage_timing.py scripts/test_video_adaptive.py
git commit -m "Add stage and VLM timing instrumentation to adaptive pipeline"
```

---



### Task 2: Capture the pre-change baseline

**Files:**

- Create: `docs/reports/pipeline-throughput-baseline-2026-08-23.md`

**Interfaces:** none (measurement only).

- [ ] **Step 1: Freeze a 3-video benchmark set**

Copy three videos of differing duration/pace into a fresh directory outside `data/`. Record each video's fingerprint via `app.services.video_fingerprint.compute_fingerprint()`. Do not reuse a directory that already contains exports.

- [ ] **Step 2: Run the current pipeline unchanged**

```powershell
uv run python scripts/test_video_batch.py --dir "<benchmark_dir>" --extensions ".mp4,.mkv,.ts"
```

- [ ] **Step 3: Record the baseline table**

For each video: total wall time, per-stage wall time, `vlm_calls`, `vlm_output_tokens`, extraction time, GIF export time, exported GIF count, total GIF bytes, mean GIF bytes. Note the exact git commit and the frozen config hash.

- [ ] **Step 4: Verify historical data is untouched**

```powershell
Get-Item data/*.db | Select-Object Name, Length, LastWriteTime
```

- [ ] **Step 5: Commit the report**

```powershell
git add -- docs/reports/pipeline-throughput-baseline-2026-08-23.md
git commit -m "Record pre-change adaptive pipeline throughput baseline"
```

---



### Task 3: GIF encode correctness

**Files:**

- Create: `app/services/gif_encode.py`, `tests/test_gif_encode.py`
- Modify: `scripts/test_video_adaptive.py`, `app/quality_moe/repair.py`, `configs/models.yaml`, `configs/models.adult_candidate.yaml`, `tests/test_batch_logging.py`, `tests/test_adaptive_config.py`

**Interfaces:**

- `build_palette_filters(*, stats_mode: str, dither: str, diff_mode: str) -> tuple[str, str]` returning the `palettegen=...` and `paletteuse=...` fragments.
- Unknown values raise `ValueError`. No configuration string is ever interpolated into a filtergraph unvalidated.

- [ ] **Step 1: Write failing tests**

```python
def test_defaults_reproduce_current_commands():
    gen, use = build_palette_filters(
        stats_mode="full", dither="sierra2_4a", diff_mode="none"
    )
    assert gen == "palettegen"
    assert use == "paletteuse"

def test_diff_mode_and_stats_mode_are_emitted():
    gen, use = build_palette_filters(
        stats_mode="diff", dither="sierra2_4a", diff_mode="rectangle"
    )
    assert gen == "palettegen=stats_mode=diff"
    assert use == "paletteuse=dither=sierra2_4a:diff_mode=rectangle"

def test_unknown_value_is_rejected():
    with pytest.raises(ValueError):
        build_palette_filters(
            stats_mode="full", dither="; rm -rf /", diff_mode="none"
        )
```

Also assert that a non-divisible `gif_fps` is flagged: add `is_divisible_gif_fps(fps) -> bool` and test that `24` is False while `25`, `20`, `10`, `50` are True.

- [ ] **Step 2: Implement** `gif_encode.py`

Whitelists: `stats_mode ∈ {full, diff, single}`, `dither ∈ {none, bayer, floyd_steinberg, sierra2_4a}`, `diff_mode ∈ {none, rectangle}`. Emit the bare `palettegen` / `paletteuse` token when every value is the FFmpeg default, so default-config command arrays stay byte-identical to today.

- [ ] **Step 3: Use it at both export sites**

Replace the hardcoded `f"{ffmpeg_filter},palettegen"` and `f"{ffmpeg_filter}[x];[x][1:v]paletteuse"` in the Direct exporter and in `_stage_gif_clip`. Leave `build_ffmpeg_filter` in `repair.py` responsible only for the `fps` / repair / `scale` prefix.

- [ ] **Step 4: Update config**

Set `adaptive.gif_fps: 25` and add `gif_palette_stats_mode: diff`, `gif_dither: sierra2_4a`, `gif_diff_mode: rectangle` in both YAML files. `extract_config()` defaults must remain `full` / `sierra2_4a` / `none`.

When `gif_fps` is not divisible into 100, log a single warning naming the nearest divisible values. Do not raise — historical snapshots carry `24` and must still run.

- [ ] **Step 5: Run focused tests**

```powershell
uv run pytest -q tests/test_gif_encode.py tests/test_batch_logging.py tests/test_adaptive_config.py tests/quality_moe/test_repair.py
```

- [ ] **Step 6: Commit**

```powershell
git add -- app/services/gif_encode.py tests/test_gif_encode.py scripts/test_video_adaptive.py app/quality_moe/repair.py configs/models.yaml configs/models.adult_candidate.yaml tests/test_batch_logging.py tests/test_adaptive_config.py
git commit -m "Fix GIF frame-rate rounding and expose palette generation flags"
```

---



### Task 4: Evidence-bounded export duration

**Files:**

- Modify: `app/services/gif_windows.py`, `scripts/test_video_adaptive.py`, `configs/models.yaml`, `configs/models.adult_candidate.yaml`, `app/ui/tabs/settings.py`, `tests/test_gif_windows.py`, `tests/test_config_help_annotations.py`, `tests/test_adaptive_config.py`

**Interfaces:**

- `build_export_window(clip, *, total_duration_s, min_duration_s, max_duration_s, single_frame_max_duration_s=None)`.
- `None` falls back to `max_duration_s`, preserving every existing caller.

- [ ] **Step 1: Write failing tests**

```python
def test_single_frame_uses_its_own_cap():
    clip = {"frame_count": 1, "gif_worthiness": 1.0, "best_frame_ts": 60.0}
    window = build_export_window(
        clip, total_duration_s=600.0, min_duration_s=2.0,
        max_duration_s=20.0, single_frame_max_duration_s=5.0,
    )
    assert window.duration_s == pytest.approx(5.0)

def test_multi_frame_ignores_single_frame_cap():
    clip = {"frame_count": 4, "start_ts": 30.0, "end_ts": 42.0,
            "best_frame_ts": 36.0}
    window = build_export_window(
        clip, total_duration_s=600.0, min_duration_s=2.0,
        max_duration_s=20.0, single_frame_max_duration_s=5.0,
    )
    assert window.duration_s == pytest.approx(15.0)

def test_omitting_the_cap_preserves_legacy_behavior():
    clip = {"frame_count": 1, "gif_worthiness": 1.0, "best_frame_ts": 60.0}
    window = build_export_window(
        clip, total_duration_s=600.0, min_duration_s=2.0, max_duration_s=20.0
    )
    assert window.duration_s == pytest.approx(20.0)
```

- [ ] **Step 2: Implement**

Apply the cap only inside the `frame_count > 1` else-branch. The 40%/60% anchor bias, total-duration clamping, and boundary clamping stay unchanged.

- [ ] **Step 3: Thread the config through**

`extract_config()` reads `adaptive.single_frame_max_duration_s`, defaulting to the resolved `max_duration`. Pass it at both `build_export_window` call sites (Direct exporter and `_stage_gif_clip`). Note that `min_duration` / `max_duration` reach `extract_config()` via `freeze_action_config(adaptive)` in `app/services/action_config.py`, so the new key must be resolved after that merge.

- [ ] **Step 4: Update config and Settings tab**

Set `max_duration: 8` and `single_frame_max_duration_s: 5` in both YAML files. Add `adaptive.single_frame_max_duration_s` to `CONFIG_FIELD_KEYS`, `CONFIG_FIELD_HELP`, optionally `CONFIG_FIELD_LABELS`, and the save/load round trip in `app/ui/tabs/settings.py`.

- [ ] **Step 5: Run focused tests**

```powershell
uv run pytest -q tests/test_gif_windows.py tests/test_config_help_annotations.py tests/test_adaptive_config.py tests/test_adaptive_direct_transition.py tests/test_adaptive_direct_action.py
```

- [ ] **Step 6: Commit**

```powershell
git add -- app/services/gif_windows.py scripts/test_video_adaptive.py configs/models.yaml configs/models.adult_candidate.yaml app/ui/tabs/settings.py tests/test_gif_windows.py tests/test_config_help_annotations.py tests/test_adaptive_config.py
git commit -m "Bound single-frame export duration by its own evidence cap"
```

---



### Task 5: Deterministic VLM scoring

**Files:**

- Modify: `scripts/test_video_adaptive.py`, `configs/models.yaml`, `configs/models.adult_candidate.yaml`, `tests/test_adaptive_config.py`, `tests/task_engine/test_vlm_stage_runtime.py`

**Interfaces:**

- `extract_config()` gains `vlm_seed` (default `None`).
- The scoring options dict includes `seed` only when configured, so default snapshots produce byte-identical request bodies to today.

- [ ] **Step 1: Write failing tests**

Assert that with `vlm_seed` unset the request JSON contains no `seed` key; with `vlm_seed: 7` the options contain `{"seed": 7}`; and that `vlm_seed` participates in the output-affecting config hash while `vlm_score_workers` does not.

- [ ] **Step 2: Implement**

Build `VLM_OPTIONS` from the frozen config, conditionally adding `seed`.

- [ ] **Step 3: Update config**

Set `vlm_temperature: 0.0`, `vlm_top_p: 1.0`, `vlm_top_k: 1`, `vlm_seed: 20260823` in both YAML files. `extract_config()` defaults stay at the current `0.65 / 0.95 / 60 / None`.

- [ ] **Step 4: Verify reproducibility on real media**

Score the same 20 frames twice from one benchmark video and assert every `gif_worthiness` is bit-identical. Record the result in the baseline report; do not commit a test that requires a live model.

- [ ] **Step 5: Run focused tests**

```powershell
uv run pytest -q tests/test_adaptive_config.py tests/task_engine/test_vlm_stage_runtime.py
```

- [ ] **Step 6: Commit**

```powershell
git add -- scripts/test_video_adaptive.py configs/models.yaml configs/models.adult_candidate.yaml tests/test_adaptive_config.py tests/task_engine/test_vlm_stage_runtime.py
git commit -m "Make VLM scoring deterministic via seeded greedy decoding"
```

---



### Task 6: Phase A regression gate

- [ ] **Step 1: Full gate**

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py -s
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

- [ ] **Step 2: Benchmark rerun and comparison**

Re-run the 3-video benchmark on fresh copies. Append a Phase A column to the baseline report: GIF frame delays must now be a single integer centisecond value, mean GIF bytes should drop, and scoring must be reproducible.

- [ ] **Step 3: Confirm historical data unchanged, then commit the report update**

---



## Phase B — Lifecycle Repair, Parallel Extraction, Two-Tier Prompt



### Task 7: Ollama lifecycle repair

**Files:**

- Modify: `scripts/test_video_adaptive.py`, `configs/models.yaml`, `configs/models.adult_candidate.yaml`, `tests/task_engine/test_vlm_stage_runtime.py`

**Interfaces:**

- `extract_config()` gains `vlm_keep_alive` (default `"30m"`).
- New `vlm.free_vram_before_load` (default `true` = current behavior).

- [ ] **Step 1: Write failing tests**

Assert the `wait_model` probe request carries `{"num_predict": 1}`; `_score_vlm_frame` sends `keep_alive`; `stop_model` returns immediately without sleeping when `/api/ps` reports the model is not loaded; and `_stage_vlm` skips the `nomic-embed-text` unload when `free_vram_before_load` is false. Use a fake transport — no live model.

- [ ] **Step 2: Implement**

Three independent changes:

1. Add `"options": {"num_predict": 1}` to the `wait_model` probe POST so an already-loaded model does not generate a full reply to `"ping"`.
2. Add `"keep_alive"` to the `_score_vlm_frame` request body.
3. Query `/api/ps` first in `stop_model` and short-circuit; gate the `_stage_vlm` unload sequence on `free_vram_before_load`.

Preserve every existing timeout, retry count, and failure classification.

- [ ] **Step 3: Update config**

Set `vlm.free_vram_before_load: false` (16GB has headroom) and `adaptive.vlm_keep_alive: "30m"` in both YAML files.

- [ ] **Step 4: Run focused tests**

```powershell
uv run pytest -q tests/task_engine/test_vlm_stage_runtime.py tests/test_ollama_runtime.py tests/task_engine/test_stage_adapter.py
```

- [ ] **Step 5: Commit**

```powershell
git add -- scripts/test_video_adaptive.py configs/models.yaml configs/models.adult_candidate.yaml tests/task_engine/test_vlm_stage_runtime.py
git commit -m "Remove wasted model probe generation and keep the VLM resident between stages"
```

---



### Task 8: Shared parallel frame extraction

**Files:**

- Create: `app/services/frame_extract.py`, `tests/test_frame_extract.py`
- Modify: `scripts/test_video_adaptive.py`, `configs/models.yaml`, `configs/models.adult_candidate.yaml`, `app/ui/tabs/settings.py`, `tests/test_config_help_annotations.py`, `tests/test_adaptive_config.py`, `build_exe.spec`

**Interfaces:**

```python
@dataclass(frozen=True)
class FrameExtractResult:
    timestamp_s: float
    path: str
    ok: bool
    returncode: int | None
    error: str

def extract_frames(
    video_path: str,
    timestamps: Sequence[float],
    out_dir: str,
    *,
    width: int = 640,
    jpeg_quality: int = 3,
    workers: int = 1,
    timeout_s: float = 15.0,
    runner: Callable = subprocess.run,
) -> list[FrameExtractResult]: ...
```

- [ ] **Step 1: Write failing tests**

Cover: results are ordered by input timestamp regardless of completion order; `workers=1` issues calls in ascending timestamp order; a non-zero return code yields `ok=False` with the code preserved; a timeout yields `ok=False` with an attributable error; the emitted command for `workers=1` matches the current array shape plus `-an -sn -q:v`; and `workers > len(timestamps)` does not over-subscribe.

Use an injected `runner` so no real FFmpeg is required.

- [ ] **Step 2: Implement**

`ThreadPoolExecutor(max_workers=max(1, min(workers, len(timestamps))))`. Keep `-ss` before `-i` (unchanged seek semantics — this task must not alter which frame is produced). Sort results by timestamp before returning.

- [ ] **Step 3: Migrate all six call sites**

Direct coarse, Direct refine, Direct action rescore, `_stage_sample`, `_stage_refine`, `_stage_rank_dedup` action rescore. Preserve each site's existing post-conditions: the `> 500` byte check, the `min_brightness` grayscale filter, and the `refine_extraction_failed` counter.

- [ ] **Step 4: Update config and Settings tab**

Add `adaptive.frame_extract_workers` (default `1`), set to `6` in both YAML files. Expose it on the Settings tab with Chinese help text.

- [ ] **Step 5: Update packaging**

Verify `collect_submodules("app")` picks up `app.services.frame_extract`; if not, add it to `build_exe.spec` hidden imports and to `tests/task_engine/test_packaged_stage_imports.py`.

- [ ] **Step 6: Run focused tests**

```powershell
uv run pytest -q tests/test_frame_extract.py tests/test_adaptive_config.py tests/test_config_help_annotations.py tests/task_engine/test_packaged_stage_imports.py tests/task_engine/test_stage_pipeline.py
```

- [ ] **Step 7: Commit**

```powershell
git add -- app/services/frame_extract.py tests/test_frame_extract.py scripts/test_video_adaptive.py configs/models.yaml configs/models.adult_candidate.yaml app/ui/tabs/settings.py tests/test_config_help_annotations.py tests/test_adaptive_config.py tests/task_engine/test_packaged_stage_imports.py build_exe.spec
git commit -m "Consolidate frame extraction into one bounded parallel service"
```

---



### Task 9: Two-tier scoring prompt

**Files:**

- Create: `tests/test_two_tier_scoring.py`
- Modify: `scripts/test_video_adaptive.py`, `configs/models.yaml`, `configs/models.adult_candidate.yaml`, `app/ui/tabs/settings.py`, `tests/test_config_help_annotations.py`, `tests/test_adaptive_config.py`, `tests/task_engine/test_full_production_stage_chain.py`

**Interfaces:**

- `get_score_prompt(mode, *, schema="full")` where `schema ∈ {"score", "full"}`.
- `_score_vlm_frame(..., schema="full")`.
- New `SCORE_PROMPT_FAST` and `SCORE_PROMPT_ADULT_FAST` retaining the full scoring rubric text but requesting only `{"gif_worthiness": 0.0}` (plus `"sex_act": 0.0` in adult mode).

- [ ] **Step 1: Write failing tests**

```python
def test_score_schema_requests_only_numeric_fields():
    prompt = get_score_prompt("adult", schema="score")
    assert '"gif_worthiness"' in prompt and '"sex_act"' in prompt
    assert '"caption"' not in prompt and '"aesthetic_notes"' not in prompt
    # The rubric drives the score distribution and must survive.
    assert "0.8-1.0" in prompt

def test_score_schema_skips_caption_quality_gate(fake_transport):
    parsed, error = _score_vlm_frame(..., schema="score")
    assert error is None
    assert parsed["gif_worthiness"] == pytest.approx(0.71)
    assert parsed.get("caption", "") == ""

def test_score_schema_still_rejects_invalid_worthiness(fake_transport):
    parsed, error = _score_vlm_frame(..., schema="score")  # returns "AVERAGE"
    assert parsed is None and "invalid gif_worthiness" in error

def test_caption_backfill_is_non_fatal(fake_failing_transport):
    clips = backfill_clip_captions(clips, ...)
    assert all("caption" in c["best_frame"] for c in clips)
    assert clips[0]["best_frame"]["caption"] == ""

def test_backfill_respects_budget():
    clips = backfill_clip_captions(make_clips(300), ..., max_frames=150)
    assert scored_call_count == 150
```

Add two Staged-path tests: `two_tier` and `legacy` produce the same clip time intervals on the same synthetic scored set; and the provisional merge inside `_stage_refine` selects exactly the same `best_frame` set that `_stage_synthesize` later derives from the written manifest.

- [ ] **Step 2: Implement the fast prompts and schema switch**

In `score` mode, skip the `parse_vlm_response` caption quality gate but keep the strict `gif_worthiness` validation, `sex_act_score()` extraction, the three-attempt retry loop, and identical error strings. Apply `vlm_num_predict_score` / `vlm_num_predict_caption` when configured.

- [ ] **Step 3: Implement** `backfill_clip_captions()`

Runs after merge, over each clip's `best_frame`, in descending score order, bounded by `caption_backfill_max_frames`. Merges `caption`, `emotional_core`, `aesthetic_notes`, and `reason` back into the frame dict. Any failure leaves the field empty and records a counter — it must never raise.

- [ ] **Step 4: Wire both paths**

Direct: immediately after `merge_scored_frames_into_clips`, before action/transition handling.

Staged: at the **tail of** `_stage_refine`, not in `_stage_synthesize`. This placement is forced by artifact lineage. `STAGE_INPUT_KINDS["synthesize"]` is `("refine_manifest",)`; `STAGE_ARTIFACT_KINDS["refine"]` is `("refine_manifest",)`, so refine's extracted JPEGs are never registered as artifacts; and `_stage_synthesize(work_dir, cfg, inputs)` takes no `config_data`, so it cannot reach the VLM runtime. Backfilling there would require an unvalidated cross-work-dir file read, breaking the "stages only read validated upstream artifacts" invariant enforced by `tests/task_engine/test_production_artifact_contract.py`.

`_stage_refine` already holds `config_data`, already calls `wait_model`, and already owns the frame files locally.

Implementation: before writing the refine manifest, run `merge_scored_frames_into_clips` over the `scored_frames` list about to be serialized, using the same frozen merge keys `_stage_synthesize` uses (`merge_gap`, `merge_score_threshold`, `max_merge_span_s`, `merge_peak_threshold`). Because that function is pure, the groups are identical to the ones synthesize will compute. Backfill only each group's `best_frame` and write the caption back into the manifest's `frames` entries.

`_stage_synthesize` then needs **no change at all** — it already reads `"caption": sf.get("caption", "")` when building `clips_data`.

Record `caption_backfill_attempted` / `_succeeded` / `_failed` in the refine manifest.

- [ ] **Step 5: Update config and Settings tab**

Add `score_schema_mode` (default `legacy`), `caption_backfill_max_frames` (150), `vlm_num_predict_score` (default `null`), `vlm_num_predict_caption` (default `null`). Set `score_schema_mode: two_tier`, `vlm_num_predict_score: 48`, `vlm_num_predict_caption: 320` in both YAML files. Expose `score_schema_mode` as a Settings dropdown with Chinese help.

- [ ] **Step 6: Run focused tests**

```powershell
uv run pytest -q tests/test_two_tier_scoring.py tests/test_adaptive_config.py tests/test_config_help_annotations.py tests/test_clip_dedup.py tests/task_engine/test_full_production_stage_chain.py
```

- [ ] **Step 7: Validate the score distribution on real media**

Score one benchmark video under both `legacy` and `two_tier`. Compare `gif_worthiness` histograms. If the distribution shifts enough to move candidate counts by more than 15%, tune the fast prompt rubric wording before proceeding — do not compensate by moving thresholds.

- [ ] **Step 8: Commit**

```powershell
git add -- tests/test_two_tier_scoring.py scripts/test_video_adaptive.py configs/models.yaml configs/models.adult_candidate.yaml app/ui/tabs/settings.py tests/test_config_help_annotations.py tests/test_adaptive_config.py tests/task_engine/test_full_production_stage_chain.py
git commit -m "Add two-tier scoring so coarse frames emit scores instead of discarded prose"
```

---



### Task 10: Phase B regression gate

- [ ] **Step 1: Full gate** (same five commands as Task 6)
- [ ] **Step 2: Benchmark rerun**

Append a Phase B column. Expected: `vlm_output_tokens` at or below 25% of baseline, extraction time at or below 35% of baseline, `vlm` + `refine` wall time at or below 30% of baseline. Clip time intervals must match Phase A.

- [ ] **Step 3: Confirm historical data unchanged, then commit the report update**

---



## Phase C — Concurrency



### Task 11: Intra-stage VLM scoring concurrency

**Files:**

- Modify: `scripts/test_video_adaptive.py`, `configs/models.yaml`, `configs/models.adult_candidate.yaml`, `app/ui/tabs/settings.py`, `tests/test_config_help_annotations.py`, `tests/test_adaptive_config.py`, `tests/task_engine/test_vlm_stage_runtime.py`

- [ ] **Step 1: Write failing tests**

Assert scored results are ordered by timestamp regardless of completion order; `workers=1` preserves today's sequential call order; per-frame failures are attributed to the right timestamp; `attempted_count` / `parsed_count` / `failed_count` are unchanged versus serial for the same inputs; and cancellation/exception in one frame does not lose other results.

- [ ] **Step 2: Implement**

Wrap the `_stage_vlm` and `_stage_refine` scoring loops (and their Direct counterparts) in `ThreadPoolExecutor(max_workers=vlm_score_workers)`. Sort by timestamp before writing the manifest so manifests stay byte-reproducible. Progress logging must remain monotonic (log on completion count, not on submission).

- [ ] **Step 3: Update config**

Add `adaptive.vlm_score_workers` (default `1`), set to `2` in both YAML files. Expose on the Settings tab with Chinese help that names the VRAM trade-off. Document `OLLAMA_NUM_PARALLEL` in `README.md`.

- [ ] **Step 4: Measure before raising the value**

Run one benchmark video at `vlm_score_workers` = 1, 2, 3. Record `vlm_ms_p50` and total wall time. Keep the best value that does not regress p50 — if 12GB of weights plus KV cache spills out of 16GB, p50 will rise and the setting must stay at the lower value.

- [ ] **Step 5: Run focused tests**

```powershell
uv run pytest -q tests/task_engine/test_vlm_stage_runtime.py tests/test_adaptive_config.py tests/test_config_help_annotations.py tests/task_engine/test_full_production_stage_chain.py
```

- [ ] **Step 6: Commit**

```powershell
git add -- scripts/test_video_adaptive.py configs/models.yaml configs/models.adult_candidate.yaml app/ui/tabs/settings.py tests/test_config_help_annotations.py tests/test_adaptive_config.py tests/task_engine/test_vlm_stage_runtime.py
git commit -m "Allow bounded concurrent VLM scoring within vlm and refine stages"
```

---



### Task 12: Stage-class worker concurrency

**Files:**

- Create: `tests/task_engine/test_stage_class_concurrency.py`
- Modify: `app/task_engine/repository.py`, `app/task_engine/worker.py`, `scripts/task_worker.py`, `app/ui/launcher.py`, `configs/models.yaml`, `tests/task_engine/test_lease_isolation.py`, `tests/task_engine/test_repository.py`

**Interfaces:**

- `TaskRepository.claim_stage(..., stage_names: Sequence[str] | None = None)`. `None` preserves today's unfiltered FIFO claim.
- `GPU_STAGES = ("vlm", "refine", "rank_dedup")`; `CPU_STAGES = ("discover", "sample", "synthesize", "gif_clip", "materialize")`.

`synthesize` stays CPU-class because Task 9 put caption backfill in `_stage_refine`; synthesize only runs a pure merge plus a cloud LLM call. `rank_dedup` is GPU-class because it calls embedding and lazily uses the VLM for action rescoring.

- [ ] **Step 1: Write the five concurrency-safety tests**

These are the gate for raising `cpu_stage_workers` above `1`:

```python
def test_two_workers_never_claim_the_same_stage(tmp_path): ...
def test_concurrent_advance_job_does_not_duplicate_gif_clip_stages(tmp_path): ...
def test_materialize_is_created_exactly_once_under_concurrency(tmp_path): ...
def test_heartbeat_connections_do_not_interfere(tmp_path): ...
def test_busy_timeout_absorbs_multi_worker_contention(tmp_path): ...
```

Drive real threads against a temporary SQLite database. The duplicate-stage test must exercise the `ensure_stage` idempotency key `f"from:rank_dedup:clip:{cid}"` from two threads simultaneously.

- [ ] **Step 2: Add the** `stage_names` **filter**

Extend the `claim_stage` SQL with an optional `AND stage_name IN (...)`. Keep `ORDER BY created_at ASC, stage_id ASC` and the `BEGIN IMMEDIATE` transaction exactly as they are.

- [ ] **Step 3: Add stage-class worker threads**

`scripts/task_worker.py` and `app/ui/launcher.py` start `gpu_stage_workers` threads filtered to `GPU_STAGES` and `cpu_stage_workers` threads filtered to `CPU_STAGES`. Each thread owns a distinct `lease_owner`. Shutdown must join every thread.

The `gif_clip` fan-out contract is untouched: parallelism comes from separate workers claiming distinct clip rows, and each clip stays independently retryable.

- [ ] **Step 4: Update config**

Add `task_engine.gpu_stage_workers` (default `1`) and `task_engine.cpu_stage_workers` (default `1`). Only raise `cpu_stage_workers` to `3` in YAML after Step 1's tests pass.

- [ ] **Step 5: Run focused tests**

```powershell
uv run pytest -q tests/task_engine/test_stage_class_concurrency.py tests/task_engine/test_lease_isolation.py tests/task_engine/test_repository.py tests/task_engine/test_worker.py tests/task_engine/test_fault_injection.py tests/task_engine/test_orchestrator.py
```

- [ ] **Step 6: Commit**

```powershell
git add -- tests/task_engine/test_stage_class_concurrency.py app/task_engine/repository.py app/task_engine/worker.py scripts/task_worker.py app/ui/launcher.py configs/models.yaml tests/task_engine/test_lease_isolation.py tests/task_engine/test_repository.py
git commit -m "Run one GPU-class and several CPU-class stage workers concurrently"
```

---



### Task 13: Phase C regression gate and packaged smoke test

- [ ] **Step 1: Full gate** (same five commands as Task 6)
- [ ] **Step 2: Benchmark rerun**

Append a Phase C column. Target: end-to-end wall time at or below 40% of baseline, `gif_clip` total at or below 45%.

- [ ] **Step 3: Rebuild and smoke-test the packaged EXE**

```bash
"C:\Program Files\Git\bin\bash.exe" scripts/rebuild_exe.sh
```

Stop the running packaged GUI and any WSL `sleep infinity` keeper rooted in `dist/GifAgentUI` first. Verify HTTP 200 on ports 8000 and 7861 with isolated runtime data, then push one real queued video at least through `discover` / `sample` — UI startup alone does not exercise the bundled stage-script import closure. Record the new EXE SHA-256.

- [ ] **Step 4: Confirm historical data unchanged, then commit the report update**

---



## Phase D — Quality Ceiling



### Task 14: Sub-second boundary snapping

**Files:**

- Create: `app/services/boundary_snap.py`, `tests/test_boundary_snap.py`
- Modify: `scripts/test_video_adaptive.py`, `configs/models.yaml`, `configs/models.adult_candidate.yaml`, `tests/test_adaptive_config.py`, `build_exe.spec`

**Interfaces:**

- `snap_window(video_path, start_s, end_s, *, radius_s, guard_result, config, cache) -> SnapResult` carrying the new bounds plus `snap_action ∈ {"snapped", "kept", "unavailable"}` and the reason.

- [ ] **Step 1: Write failing tests**

Using the synthetic-video helpers already in `tests/test_transition_guard.py` and `tests/action_media_fixtures.py`: a window starting mid-motion snaps to the nearby motion minimum; snapping never crosses a confirmed hard cut; snapping never enters `transition_boundary_margin_s`; a `guarded_export_window=True` clip is returned unchanged; a result that would fall below `transition_min_duration_s` is rejected in favor of the original window; and a decode failure returns `unavailable` with the original window rather than dropping the candidate.

- [ ] **Step 2: Implement**

Reuse `TemporalEvidenceCache` from `app/services/temporal_evidence.py` — it already batches an interval into a single FFmpeg decode at `transition_scan_fps`. Do not add a new decode path.

- [ ] **Step 3: Wire in after transition guard, before dedup**

In both Direct and `_stage_rank_dedup`. Record per-clip `snap_action` and aggregate counts in the manifest, mirroring how `transition_guard` reports.

- [ ] **Step 4: Update config**

Add `boundary_snap_enabled` (default `false`) and `boundary_snap_radius_s` (default `0.6`). Leave disabled in YAML until the Task 16 A/B passes.

- [ ] **Step 5: Run focused tests**

```powershell
uv run pytest -q tests/test_boundary_snap.py tests/test_transition_guard.py tests/test_temporal_evidence.py tests/test_adaptive_config.py tests/task_engine/test_packaged_stage_imports.py
```

- [ ] **Step 6: Commit**

```powershell
git add -- app/services/boundary_snap.py tests/test_boundary_snap.py scripts/test_video_adaptive.py configs/models.yaml configs/models.adult_candidate.yaml tests/test_adaptive_config.py build_exe.spec
git commit -m "Add opt-in sub-second export boundary snapping"
```

---



### Task 15: Score calibration

**Files:**

- Create: `app/services/score_calibration.py`, `scripts/fit_score_calibration.py`, `tests/test_score_calibration.py`
- Modify: `scripts/test_video_adaptive.py`, `configs/models.yaml`, `tests/test_adaptive_config.py`, `build_exe.spec`

**Interfaces:**

- `load_calibrator(path, *, model_id, prompt_mode) -> Calibrator | None` — returns `None` and logs when provenance does not match the frozen snapshot.
- `Calibrator.apply(score: float) -> float`.

- [ ] **Step 1: Write failing tests**

A monotone calibrator maps scores monotonically; a provenance mismatch on `model_id` or `prompt_mode` refuses to load; a missing or malformed file degrades to `None` without raising; both raw and calibrated scores are recorded; and thresholding uses the calibrated value while the manifest preserves the raw value.

- [ ] **Step 2: Implement the fitting script**

Read effective like/dislike events from `preference_events`, join to the raw `gif_worthiness` of each candidate, then call `calibration_curve()` and `fit_monotonic_calibrator()` from `app/quality_lab/calibration.py`. Emit JSON with `model_id`, `prompt_mode`, `sample_count`, `created_at`, thresholds, and values. Refuse to write below 200 labeled samples.

The script is read-only against `library.db`.

- [ ] **Step 3: Apply at scoring time**

Apply before threshold comparison. Keep the raw score in the manifest under `gif_worthiness_raw`.

- [ ] **Step 4: Update config**

Add `score_calibration_enabled` (default `false`) and `score_calibration_path` (default `""`). Leave disabled until a calibrator exists.

- [ ] **Step 5: Run focused tests**

```powershell
uv run pytest -q tests/test_score_calibration.py tests/quality_lab/test_calibration.py tests/test_adaptive_config.py tests/task_engine/test_packaged_stage_imports.py
```

- [ ] **Step 6: Commit**

```powershell
git add -- app/services/score_calibration.py scripts/fit_score_calibration.py tests/test_score_calibration.py scripts/test_video_adaptive.py configs/models.yaml tests/test_adaptive_config.py build_exe.spec
git commit -m "Apply frozen isotonic score calibration before worthiness thresholds"
```

---



### Task 16: Blind A/B validation and model experiment

**Files:**

- Modify: `docs/reports/pipeline-throughput-baseline-2026-08-23.md`

- [ ] **Step 1: Freeze a benchmark manifest**

Use `app.quality_lab.manifests.freeze_manifest` with `assign_splits` over at least 12 videos (24 preferred), covering different duration, resolution, and pace buckets. Fingerprints come from `app.services.video_fingerprint.compute_fingerprint()`.

- [ ] **Step 2: Run the configuration comparison**

Register the pre-change config and the Phase A–C config as two `experiment_configs`, run both over the tune split, and create a blind A/B session via `BlindReviewService`. Judge every pair before revealing.

- [ ] **Step 3: Decide on the deferred switches**

Enable `boundary_snap_enabled` only if it wins its own blind A/B. Enable `score_calibration_enabled` only once a calibrator with 200+ samples exists and holdout NDCG does not regress.

- [ ] **Step 4: Run the model experiment (optional, highest ceiling)**

A/B the current `IQ2_M` 35B against a 7B-class uncensored vision model at Q5_K_M or Q6 (roughly 6–8GB, leaving room for higher `vlm_score_workers`). Two-bit quantization degrades numeric discrimination most, which is a plausible cause of the flat score distribution recorded as gotcha #6 in `Agent.md`. Judge via the same blind A/B, not by inspecting scores.

- [ ] **Step 5: Record results and commit the report**

---



### Task 17: Documentation and release

**Files:**

- Modify: `README.md`, `Agent.md`

- [ ] **Step 1: Document the new keys**

Update the production-defaults table in `README.md` and the `Key Parameters` block in `Agent.md`. State plainly that every new key defaults to prior behavior and that Retry never rewrites `config_json`, so historical jobs keep their old behavior.

Also correct a pre-existing documentation error found while writing this plan: both files describe `configs/models.adult_candidate.yaml` as mirroring `configs/models.yaml`, but it has drifted (`worthiness_threshold: 0.42` vs `0.62`, `refine_threshold: 0.55` vs `0.70`, `merge_score_threshold: 0.50` vs `0.58`, `vlm_temperature: 0.50` vs `0.25`, and no `max_refine_frames`). Describe it as a preset, not a mirror, and list the divergences.

- [ ] **Step 2: Document the tuning and benchmark procedure**

`vlm_score_workers` and `cpu_stage_workers` are hardware-dependent and must be measured, not copied. Warn that `clear_output_dir: true` means benchmark runs must use copies.

- [ ] **Step 3: Document the GIF frame-rate constraint**

Explain that GIF delays are integer centiseconds, so `gif_fps` should divide 100 (25, 20, 10, 50) and that 24 produces uneven frame delays.

- [ ] **Step 4: Final release gate**

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py -s
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
Get-Item data/*.db | Select-Object Name, Length, LastWriteTime
```

- [ ] **Step 5: Commit**

```powershell
git add -- README.md Agent.md
git commit -m "Document throughput and GIF quality configuration"
```

---



## Rollback

Every behavior change is a config key. Restoring `configs/models.yaml` and `configs/models.adult_candidate.yaml` to the defaults listed in the design document returns the pipeline to current behavior without a code revert, without reprocessing anything, and without deleting any historical GIF, task record, label, or preference data.