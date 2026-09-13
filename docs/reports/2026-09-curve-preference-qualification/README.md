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

**`NO_GO_REFUSED`** (2026-09-06, full 2,510-row replay completed under fixed 0.70/0.30 target weights, frozen scoring vectors, and strictly aligned online ranking contracts).

### Replay Summary & Metrics

- **Qualification ID**: `qual-full-20260906-v3`
- **Model Version**: `curve-pref-v1-20260905120400`
- **Status**: **`NOT_QUALIFIED`**
- **Gate Reasons**: `["nominal_pairwise_lcb_not_positive", "effective_pairwise_lcb_not_positive"]`
- **Holdout Universe**: 2,510 rows across 117 source videos (evaluated: 2,510 rows, 47 paired groups, 12,938 within-source pairs)
- **Source Overlap**: 0 (strict train/holdout source isolation)
- **Target Ranking Config**: `base_score_weight=0.70`, `preference_score_weight=0.30`, sha256=`141aa47cdf20bd8f5a38bdb5366d00a537b968c3472a96c548294ad86349957d`
- **Effective Confidence**: `0.0` (derived confidence is 0.0 when nominal LCB <= 0)

#### 1. Nominal (Unscaled) Metrics vs Production Baseline

| Metric | Baseline | Nominal Curve | Delta / LCB95 | Gate Status |
|---|---|---|---|---|
| Pairwise Accuracy | 78.938% (0.789380) | 79.811% (0.798114) | +0.873% (+0.008734) | Win-rate improvement |
| Pairwise Delta LCB95 | — | — | **-0.000111 (<= 0)** | **FAIL** (LCB not strictly positive) |
| Mean Source NDCG@20 | 0.969101 | 0.968186 | -0.000916 | NDCG regression |
| Tie Rate | 0.0% | 0.0% | 0.0% | PASS |

#### 2. Effective (Confidence-Scaled) Metrics

| Metric | Baseline | Proposed (Effective) | Delta / LCB95 | Gate Status |
|---|---|---|---|---|
| Pairwise Accuracy | 78.938% | 78.938% | 0.0% | Retains baseline order (confidence 0.0) |
| Pairwise Delta LCB95 | — | — | **0.0 (<= 0)** | **FAIL** |
| Mean Source NDCG@20 | 0.969101 | 0.969101 | 0.0 | No regression |
| Tie Rate | 0.0% | 0.0% | 0.0% | PASS |

### Artifact Hashes & Bundle Integrity

- `input_manifest_sha256`: `0ffc13d0669c7bae8e2ab52f05d99ac063347aff807684ddd41dca0f2739f5e2`
- `baseline_scores_sha256`: `46c56527b25a127e0fcaf51e672fc9fe020ff60d6bee9b6691017f40dee12556`
- `nominal_scores_sha256`: `1d3947b998cd6922b94d099ceef67922467fa8a40bb01a17182c559633d2763a`
- `effective_scores_sha256`: `c9f94ff4f84cab43ce1ccee8ac3a3d788b10248cfbf3430f66ec278f5fddc26d`
- `evidence_sha256`: `f50a0ea35be34618082f9b0e97b079e7258ad93970defa279061a84f9a23c870`
- `vectors_sha256`: `0fde3e46ad2d332b19ef111d091efef108e49045aeefba1bd0e3c5455b9b5405`
- `profile_snapshot_sha256`: `7a0c2fe83ca657d2a36b34e0d04840e2d51947b47dbfd2c7617a84d99df20a2e`
- `report_sha256`: `d6c0add073c9a7fb1504e19f43a781d9ed15e67318270a6e193b266d66514091`

### Security Hardening & Gate Audit Verification

1. **[P1] 发布器四方 ID 集合与来源/标签严格绑定**: `promote_curve_preference_model.py` 强制 `holdout_manifest == input_manifest.records == baseline == nominal == effective == vectors` 四方 candidate 集合严格一致，拒绝重复 ID，并逐行核对 `source` 与 `rating`，杜绝伪造子集或篡改标签。
2. **[P2] 评分向量与模型身份完整冻结**: 新增 `vectors.jsonl` 产物，完整保存打分实际调用的 `preference_embedding_text`、`candidate_vector`、`scenario_keys` 及 `embedding_model: nomic-embed-text:latest` 身份与哈希，具备完全独立复现分数计算的闭环凭据。
3. **[P2] VLM 评分严格复用生产归一化规则**: `_parse_score` 复用 `normalize_vlm_unit_score()`，保留整数与浮点的类型语义（整数 `1` -> 1分，浮点 `1.0` -> 100分），严格拒绝越界值与字符串输入，杜绝隐式截断。
4. **[P1] 契约对齐**: 候选文本嵌入对齐在线 `preference_embedding_text`（空 caption 门控拦截）与 `clip_scenario_keys`。
5. **[P2] 生产权重绑定**: 绑定 `0.70 / 0.30` 生产真实权重，记录 `config_sha256`。
6. **[P2] NDCG@20 切片修复**: 切片 `ranked[:20]` 与 `ideal[:20]`，消除 DCG 衰减项未对齐问题。

### Promotion Attempt Result

运行 `promote_curve_preference_model.py --dry-run` 得到：
```json
{
 "dry_run": true,
 "reasons": [
  "qualification_not_qualified"
 ],
 "status": "REFUSED"
}
```
模型在生产库 `dist/GifAgentUI/data/library.db` 中保持 **`DRAFT`** 状态，发布器安全阻断发布。
