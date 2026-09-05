# Curve Preference Qualification Report — 2026-09

Status: harness complete; production replay pending operator execution.
Plan: `docs/superpowers/plans/2026-09-05-curve-preference-baseline-replay-qualification.md`
Spec: 人物语义连续性与身体曲线设计 §5.8
Target model: `curve-pref-v1-20260905120400` (status DRAFT)

## 1. What this harness does

For one DRAFT curve preference model the qualification CLI
(`scripts/qualify_curve_preference_model.py`) does the following, in order:

1. Opens the production database (`dist/GifAgentUI/data/library.db`)
   **read-only** and takes a consistent SQLite online backup into a fresh,
   uniquely named run directory (`qualification/{qualification_id}/work.db`),
   verified with `PRAGMA integrity_check` and a SHA-256 of the snapshot.
2. Verifies the model is DRAFT and that its frozen train/holdout manifests
   re-hash to the stored values (manifest SHA-256 **and** source-set
   SHA-256), and that train/holdout source sets are disjoint.
3. Builds a **source-isolated Preference Profile** inside the work snapshot,
   excluding every holdout source video, and freezes the profile inputs and
   outputs as `profile_snapshot.json` (+ SHA-256).
4. Validates every holdout manifest row against the snapshot: candidate
   identity, serialized curve features (SHA-256), effective label event
   identity (id/time/rating), and GIF file hash. Post-manifest corrections
   are reported as `effective_event_changed` and block qualification.
5. Completes baseline evidence **inside the work snapshot only**: rows
   missing a VLM summary are enriched with the existing local Ollama
   enrichment primitives (same contact-sheet prompt path as new candidates;
   never a default/median filler), and missing text vectors are generated
   through the production `upsert_candidate_vector` path
   (`nomic-embed-text:latest/768`). Any enrichment failure blocks
   qualification (`base_score_coverage_incomplete`).
6. Scores every fully covered candidate three ways through one shared pure
   function (`score_preference_export_candidate`), which mirrors the
   production `_rank_clips_with_preference` blend:
   - `baseline`: production algorithm, curve off;
   - `nominal`: curve weight 0.10, confidence 1.0;
   - `effective`: curve weight 0.10 × derived confidence, where
     `confidence = min(data_confidence, clamp(nominal_delta_lcb95/0.05, 0, 1))`.
7. Computes paired source-grouped metrics (strict within-source pairwise
   accuracy with ties reported separately, mean per-source NDCG@20 with the
   service's GAIN_MAP, paired source-group bootstrap LCB95 with fixed seed
   20260905 and 2000 reps) and applies the fail-closed decision.
8. Writes the immutable bundle (`run_state.json`, `input_manifest.json`,
   `profile_snapshot.json`, `evidence.jsonl`, `baseline_scores.jsonl`,
   `nominal_scores.jsonl`, `effective_scores.jsonl`, `metrics.json`,
   `report.md`, `work.db`) and finally re-opens the production DB read-only
   to confirm the source logical identity did not change during the run.

## 2. Gates (all must pass; any failure ⇒ NOT_QUALIFIED)

- `model_not_draft` / `model_manifest_hash_mismatch` — model lifecycle and
  frozen-input integrity;
- `source_overlap` — train/holdout source disjointness;
- `holdout_too_small` (< 30) / `missing_class` — population validity;
- `candidate_identity_mismatch` / `effective_event_changed` /
  `artifact_missing` / `artifact_hash_mismatch` — per-row evidence binding;
- `base_score_coverage_incomplete` — 100% baseline coverage required
  (no synthetic comparability);
- `no_within_source_pairs` / `non_finite_metric` — metric validity;
- `nominal_pairwise_lcb_not_positive` — nominal curve delta must beat the
  baseline with 95% confidence (paired source bootstrap);
- `effective_pairwise_lcb_not_positive` — the confidence-scaled variant must
  still beat the baseline;
- `effective_ndcg_regression` — no NDCG@20 regression allowed;
- `diagnostic_limit` — `--limit` rehearsals can never emit QUALIFIED.

A `NOT_QUALIFIED` run still preserves the full evidence bundle for audit.
`QUALIFIED` does **not** publish anything: promotion is a separate, explicit
command (`scripts/promote_curve_preference_model.py`) that re-validates the
bundle hashes against the live production model row.

## 3. Operator quick reference

```powershell
# Validation only: no snapshot, no VLM calls, no writes.
python scripts/qualify_curve_preference_model.py `
  --db dist/GifAgentUI/data/library.db `
  --model-version curve-pref-v1-20260905120400 `
  --work-dir data/curve_preference_qualification `
  --dry-run

# 30-candidate rehearsal (diagnostic; never QUALIFIED).
python scripts/qualify_curve_preference_model.py `
  --db dist/GifAgentUI/data/library.db `
  --model-version curve-pref-v1-20260905120400 `
  --work-dir data/curve_preference_qualification `
  --limit 30

# Full 2,510-row qualification replay.
python scripts/qualify_curve_preference_model.py `
  --db dist/GifAgentUI/data/library.db `
  --model-version curve-pref-v1-20260905120400 `
  --work-dir data/curve_preference_qualification
```

Local Ollama must be serving the enrichment model for rows that lack VLM
evidence; the dry run reports exactly how many such rows exist. The final
go/no-go conclusion is recorded in section 4 of this file after the full
replay has been executed and independently verified.

## 4. Final go/no-go conclusion

**`BLOCKED_EVIDENCE_INCOMPLETE`** (2026-09-05, preflight + rehearsal stage;
the full 2,510-row replay has not executed).

### Preflight evidence

- HEAD at execution: `851337f1`; worktree carries unrelated uncommitted
  user changes (not staged by any qualification commit).
- Production DB `dist/GifAgentUI/data/library.db`: `PRAGMA integrity_check`
  = ok; three curve model rows, all `DRAFT`; GUI process running (WAL mode,
  concurrent reads safe).
- Dry run (`--dry-run`): `holdout_rows=2510`, `holdout_sources=117`,
  `source_overlap=0`, `model_ok=true`, `required_enrichments=124` — matches
  the plan's frozen population exactly.
- Local Ollama (WSL) is serving on `*:11434` with **no models resident**;
  the VLM (`huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`) loads on
  demand and was verified reachable from the host at the discovered WSL
  address.

### Blocker (found by the 30-candidate rehearsal)

The 30-candidate rehearsal aborted before scoring: the **source-isolated
profile build gate** reports
`candidate_vector_count=9446 < effective_feedback_count=10530`.

Measurement of the gap (read-only, on the rehearsal snapshot):

- 10,530 effective training targets (latest scoring event per candidate,
  holdout sources excluded);
- 1,084 of them have **no clip vector** (`nomic-embed-text:latest/768`);
- all 1,084 also lack a usable VLM summary, so vector generation requires
  VLM enrichment first (GIF artifacts themselves are all present on disk).

The plan's Task 4 scoped enrichment to the 124 holdout rows only; the
train-side gap is a data reality the plan did not anticipate, and the
profile gate correctly refuses to build a profile whose training events are
not fully vector-covered. Fail-closed semantics worked as designed: the run
aborted, left `run_state.json` as INCOMPLETE, and no production table was
mutated.

### What this blocks

- baseline scoring requires the isolated profile, so no pairwise/NDCG
  metrics exist yet;
- the VLM JSON parse rate and per-row latency are therefore also unmeasured.

### Remediation (smallest next experiment)

1. In a fresh work snapshot, run the existing production enrichment path
   (`scripts/enrich_library_candidates.py` semantics) over the 1,084
   train-side candidates that lack VLM summaries, then
   `upsert_candidate_vector` for all of them (≈ 1,084 VLM calls, estimated
   4–10 GPU-hours on the local RTX 4070 Ti SUPER, plus ≈ 1,100 text
   embeddings), and the 124 holdout-row enrichments required by Task 4
   regardless.
2. Re-run the qualification CLI (rehearsal → full replay). All curve-side
   gates are unchanged and ready; the population gates should then pass
   (holdout 2,510 ≥ 30, classes present, zero source overlap).

### Verification already recorded

- Focused suites: 115 tests pass across `test_curve_preference_model.py`,
  `test_curve_preference_scoring.py`,
  `test_curve_preference_qualification.py`,
  `test_curve_preference_promotion.py`, `test_preference_reranker.py`.
- mypy: clean on `app/services/curve_preference.py` and
  `app/services/curve_preference_qualification.py`.
- Bundle determinism, resume-without-re-enrichment, refusal paths, and
  source non-mutation are covered by the Task 6/7 tests above.

The model remains DRAFT; no production reranking or config change has been
made.
