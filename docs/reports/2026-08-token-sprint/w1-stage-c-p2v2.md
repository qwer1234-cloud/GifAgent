# W1 Stage C — P2 scene metadata, first p2/v2 validation shard

- Date: 2026-08-29
- Executor: w1-stagec-executor (Stage C W1 data-track ramp, first validation shard)
- Branch: `sprint/w1-stagec` (worktree `.worktrees/sprint-w1-stagec`, off `master` @ `b31eb794`)
- Manifest basis: rev15 ruling — all Stage C P2 work uses `prompt_version p2/v2`; optional
  facets (`exposure`, `mood`) are restricted to source-verbatim mapping of the input caption.

## 1. Prompt p2/v2

- File: `scripts/token_sprint/prompts/p2/v2.md` (this branch; template immutable per
  `scripts/token_sprint/prompts.py` registry semantics — v1 left untouched).
- Delta vs v1: adds up to two OPTIONAL facets to the otherwise identical v1 schema —
  `exposure` (state of dress/nudity the caption states) and `mood` (emotional tone or
  expression the caption states) — under a locked source-verbatim constraint: the value
  (lowercased, `_` read back as space) must be a contiguous case-insensitive substring of
  the input caption; otherwise the key is omitted entirely (never null). Required facets
  `setting`/`act`/`finish` keep v1 semantics; refusal line and hard rules unchanged.
  Per-line provenance fields `prompt_version: "p2/v2"` and `prompt_sha256` were added to
  the output schema.
- SHA-256 (exact file bytes, as reported by `prompts.prompt_sha256("p2","v2")`):
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`

## 2. Input set (production DB, read-only)

- Source: `dist/GifAgentUI/data/library.db`, opened with SQLite URI `mode=ro` plus
  `PRAGMA query_only=ON`; no media bytes read; no writes performed.
- Enriched-row criterion: `vlm_summary_json` contains `enriched_by` (non-null producer,
  e.g. `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`).
- At extraction time the production `candidate_gifs` table held 13,458 rows of which
  235 were enriched; the first 100 by ascending rowid were taken (rowids 80-179).
- Input shard (data product, not in git): `data/token_sprint/shards/shard-w1stagec-001.input.jsonl`
  — 100 lines of `{rowid, candidate_id, caption, enriched_by}` (caption-only by design,
  see §5 note on the marker heuristic).

## 3. Output shard

- File: `data/token_sprint/shards/shard-001.p2.v2.b217afdf-8e4f-4c43-8fb6-02cc3e99b89f.jsonl`
- Lines: 100 (one per input row, input order; candidate_ids copied verbatim)
- SHA-256: `053040b13284ba2724c8bff03b046e2e11306067af43de223f7b331ce4932f42`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).

## 4. QC gates (Stage C per-shard standard)

### 4.1 validate_shard.py (run from the worktree, unmodified; p2/v2 supported as-is)

```json
{
  "coverage_rate": 1.0,
  "errors": [],
  "fidelity_rate_high_stratum": null,
  "n": 100,
  "pass_id": "p2",
  "prompt_sha256": "01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69",
  "prompt_version": "v2",
  "refuse_rate": 0.0,
  "schema_ok_rate": 1.0,
  "suspected_sanitized": [],
  "t0_gates": {
    "fidelity_rate_high_stratum_ge_90pct": true,
    "refuse_rate_le_15pct": true,
    "schema_ok_rate_ge_95pct": true
  },
  "vocab": null
}
```

### 4.2 Gate results

| Gate | Standard | Result | Verdict |
| --- | --- | --- | --- |
| Schema compliance | >= 98% | 100% by validator p2 check; 100% under a stricter manual check (per-line `prompt_version`/`prompt_sha256` exact, `candidate_id` present in input, optional facets str-or-absent never null, values snake_case or null) | PASS |
| Refusal rate | <= 15% | 0% (0 refusals; two 1win brand-graphic rows output all-null scenes instead of refusing — nothing was guessed) | PASS |
| Fidelity spot check | 20 rows, seed=20260829; optional facet values verbatim substrings of caption; no semantic errors | 20 rows sampled, 0 verbatim failures, 0 semantic errors (see 4.3) | PASS |

Optional facet emission over all 100 rows: 102 facets (70 `exposure`, 32 `mood`);
87/100 rows carry at least one optional facet. A mechanical verbatim check over ALL 102
emitted facet values (not only the sample) found 0 failures.

### 4.3 Fidelity sample table (random.Random(20260829), 20 of 100)

| Row | candidate_id | Facets emitted | Caption evidence (fragment) | Verbatim | Semantics |
| --- | --- | --- | --- | --- | --- |
| 4 | cand_55348b7606e4dece | exposure=lingerie | "woman in white lace lingerie bends over a dark leather couch" | OK | OK |
| 5 | cand_2859320f6d73f0f1 | exposure=lingerie | "a woman wearing blue lace lingerie" | OK | OK |
| 6 | cand_9bfb237b2872fb9a | exposure=lingerie, mood=pleasure | "blue floral lingerie"; "facial expressions of pleasure or exertion" | OK/OK | OK |
| 8 | cand_4198a0b0ee9049d0 | exposure=lingerie | "dark blue lace lingerie is being sexually penetrated from behind" | OK | OK |
| 9 | cand_b6009e63609f49a1 | exposure=underwear | "woman in red lace underwear stands in a kitchen" | OK | OK |
| 18 | cand_b1a9bee5dff25c64 | exposure=lingerie | "woman in black lingerie and fishnet stockings is positioned above a man" | OK | OK |
| 19 | cand_89616f905e6aa73d | exposure=lingerie | "wearing black lace lingerie with white floral embroidery" | OK | OK |
| 21 | cand_3f35b07e5f887ab4 | (none) | no dress-state or tone word verbatim in caption -> correct omission | n/a | OK |
| 33 | cand_882dfb19801522fe | exposure=lingerie | "woman in red lace lingerie leans back against a wall" | OK | OK |
| 44 | cand_3dbe54008f9f764c | exposure=lingerie | "younger woman wearing lace lingerie" | OK | OK |
| 53 | cand_72aab81702161283 | exposure=lingerie | "woman in dark lingerie straddles a man on a bed" | OK | OK |
| 57 | cand_d49323ec8eab6443 | mood=smiling | "sitting at a kitchen table, smiling gently while eating" | OK | OK |
| 66 | cand_6d64ea2a702a31c5 | exposure=lingerie | "woman in black lingerie lies on her stomach on a bed" | OK | OK |
| 72 | cand_332b8814c08b8ef6 | exposure=lingerie, mood=distressed | "woman in lace lingerie"; "she appears distressed or emotional" | OK/OK | OK |
| 77 | cand_938e624b800508c9 | exposure=lingerie | "woman in black lingerie and fishnet stockings lies prone on a bed" | OK | OK |
| 78 | cand_7342e1e8f67fbda3 | exposure=underwear, mood=pleasure | "black lace underwear"; "shift from neutral to intense pleasure" | OK/OK | OK |
| 84 | cand_fs_722cd9e7b48ab91e9b19307bd2df88898f9ed0d5e24a4ad91bcf43d796f89ecc | exposure=lingerie | "woman in black lingerie straddles a man on a couch" | OK | OK |
| 85 | cand_fs_b954eef7566862010db1b6abec1e452c364ad2a18675ad1202101e631e1b5e41 | exposure=nude | "nude man and woman are in a steamy shower" | OK | OK |
| 91 | cand_fs_514e3ff27aebd9c7a79a1e50f3e3e8d0e88befbc46d05d6addf99870be226bbd | exposure=shirtless | "shirtless man stands in a dimly lit shower" | OK | OK |
| 96 | cand_fs_6c243ac15263d1c31b49bda574e687546554be9446e296ed2d55dd43275d8de8 | exposure=underwear | "she is bent over wearing black underwear" | OK | OK |

## 5. Notes / deviations

- `fidelity_rate_high_stratum` is `null` (denominator 0): the marker-retention heuristic
  keys off `stratum`/`vlm_summary_json`/`tags_json`/`scenario_keys_json` input fields,
  which the caption-only Stage C input intentionally does not carry. The Stage C fidelity
  gate is the seed-20260829 manual spot check above. Keeping raw `vlm_summary_json` out of
  the input also avoids a false-positive vector: enriched rows all contain the numeric key
  `"sex_act"`, whose name alone would trip the `sex` marker for every row.
- No `suspected_sanitized` rows; explicit captions were annotated explicitly (v1 hard rule 3).
- Publication: `.ready` sidecar written atomically (tmp + `os.replace`) with
  `producer=w1-stagec-executor`; ready-file SHA-256
  `f7e78de722405e5b3ff52a26747aa9ca6bdff59a4753256449e6c968dc1ce021`.
- Red lines honored: production DB opened read-only only; no GIF/JPG/video bytes opened;
  no direct model-endpoint code added; `pyproject.toml`/`uv.lock` untouched; shard/ready
  files stay in the sprint root and are not committed to git.
