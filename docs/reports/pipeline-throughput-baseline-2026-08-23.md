# Pipeline throughput and GIF quality — implementation status (2026-08-23)

This note records what landed in Phases A–D and what was **not** measured
on live media. It does **not** invent wall-time or token numbers.

## What shipped (code)

| Phase | Tasks | Behavior |
|-------|-------|----------|
| A | 1–5 | Stage timings, GIF fps/palette, single-frame duration cap, seeded VLM |
| B | 7–9 | Ollama keep-alive, shared parallel `extract_frames`, two-tier scoring |
| C | 11–12 | `vlm_score_workers` inside vlm/refine; GPU-class + CPU-class workers |
| D | 14–15 | Opt-in `boundary_snap_*` and `score_calibration_*` (both **off**) |

New keys default to the previous behavior. Retry never rewrites
`task_jobs.config_json`, so historical jobs keep their frozen snapshot.
Existing GIFs, `library.db`, and `task_state.db` rows are not rewritten by
these changes.

## Switches that stay off until a real A/B

`configs/models.yaml` and `configs/models.adult_candidate.yaml` keep:

- `boundary_snap_enabled: false`
- `score_calibration_enabled: false`
- `score_calibration_path: ""`

Enable snap only after a blind A/B on 12–24 videos wins. Enable
calibration only after `scripts/fit_score_calibration.py` has ≥200 labeled
samples and holdout NDCG does not regress.

## Deferred live work (this session)

These plan steps need a resident VLM, real videos, and/or a packaged EXE.
They were **not** run here:

- Phase A/B/C benchmark columns (end-to-end wall time, `vlm_output_tokens`, extract time)
- Task 9 live `legacy` vs `two_tier` score-histogram compare
- Task 11 `vlm_score_workers` = 1/2/3 p50 sweep (`OLLAMA_NUM_PARALLEL` must match)
- Task 16 Quality Lab `freeze_manifest` + `BlindReviewService` pair judging
- Task 13 packaged EXE rebuild (`scripts/rebuild_exe.sh`) and HTTP 8000/7861 smoke

Do not copy `vlm_score_workers: 2` or `cpu_stage_workers: 3` onto other
hardware without measuring. If 12GB of weights plus KV cache spills on
16GB VRAM, p50 rises and workers must drop back to 1.

`adaptive.clear_output_dir` defaults to `true`. Benchmark reruns must use
copies or new export directories, never in-place overwrites of historical
outputs.

## How to run the deferred A/B later

1. Copy 12–24 videos (duration / resolution / pace buckets) into a new tree.
2. `app.quality_lab.manifests.freeze_manifest` + `assign_splits`; fingerprints
   from `app.services.video_fingerprint.compute_fingerprint()`.
3. Register the pre-change snapshot and the Phase A–C snapshot as two
   `experiment_configs`; run the tune split; judge via `BlindReviewService`
   before revealing labels.
4. Only then flip `boundary_snap_enabled` or `score_calibration_enabled`.
5. Optional ceiling: A/B the current IQ2_M 35B against a 7B-class uncensored
   vision model at Q5_K_M / Q6 (about 6–8GB), judged blind, not by scores.

## Historical data check

Confirm `data/*.db` mtimes and sizes after any local test run. Tests in this
work used `tmp_path` / temporary SQLite only.
