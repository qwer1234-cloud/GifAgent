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

`PENDING` — the full production replay has not been executed yet. This
section will be updated with the actual run numbers (coverage, fallbacks,
pairwise delta LCB95 for nominal and effective variants, NDCG delta,
effective confidence) and one of:

- `GO_FOR_EXPLICIT_PROMOTION` — with the exact promotion command for the
  user to run;
- `NO_GO_RETRAIN_OR_REDESIGN` — with stable reason codes and the smallest
  next experiment;
- `BLOCKED_EVIDENCE_INCOMPLETE` — with the blocking candidate IDs/reasons.

Even a `GO` stops short of promotion: the promote command is executed by the
user, on demand, after reviewing the frozen report.
