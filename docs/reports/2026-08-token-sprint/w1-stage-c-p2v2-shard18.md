# W1 Stage C — P2 scene metadata, eighteenth p2/v2 shard (rowids 7195–7694)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec18` (worktree `.worktrees/sprint-w1-stagec18`, created
  from `70ee09c5`, the master tip that merged shard-017)
- Nature of this run: fully fresh — input shard built from the production
  library (read-only) and all 500 output rows authored by this session.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–17): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The
  worktree copy was normalized to the canonical LF bytes for the QC run only
  (content unchanged), so the validator reported the frozen `01a161b0…` hash;
  the stock CRLF checkout was restored byte-exactly immediately after
  validation (re-read back: `d4971361…`) and the worktree `git status` shows no
  tracked-file modification. The main-sprint-root prompt file was never touched.

## 2. Input set (relay window after shard-017, continuity verified)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-018.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption taken from
  `vlm_summary_json.$.caption`; caption-only by design, same as shards 1–17).
- Input SHA-256: `3812f014b7ee6199626ca9f07101f5fb1b38cadbf55ef27912b46ed3058a2312`
- Built read-only from `dist/GifAgentUI/data/library.db`
  (`mode=ro` + `PRAGMA query_only=ON` + `PRAGMA busy_timeout=30000`); the DB
  was never written.
- Window rule (relay, baseline = shard-017): shard-017's input ends at rowid
  **7194** (verified on disk: 500 rows, first 6695 / last 7194, and the last
  line's `candidate_id` re-checked against the production row 7194). This
  shard takes the next 500 enriched rows (`vlm_summary_json` contains a truthy
  `enriched_by` and a non-empty `caption`) in ascending rowid order →
  **rowids 7195–7694, strictly increasing, 0 empty captions**.
- Continuity checks (all pass): (a) the DB holds **0 rows between 7194 and
  7195**, and rowid 7195 is itself enriched → the first selected row is exactly
  the next row after the baseline, no overlap, no gap at the relay seam;
  (b) the window `[7195, 7694]` contains exactly 500 rows, all 500 distinct
  rowids, all 500 enriched → no enriched row was skipped inside the window;
  (c) candidate_ids unique; first rowid > 7194 (no overlap with shard-017).

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-018.p2.v2.77b33963-92c6-4da6-a23d-bf1f97711309.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` copied
  programmatically from the input shard by the append tooling, never typed by
  hand)
- SHA-256: `0ada86c32505d9efabc07a3cf9108aef05f6e2b9b9354ffa9c4e21f46d61dc2a`
- Custody: single session, generated in **10 sub-batches of 50**. Each
  sub-batch was appended **atomically (tmp + `os.replace`)** and then passed a
  per-sub-batch mechanical assertion (exactly 50 scenes; rowid-tag and
  `candidate_id` alignment against the input slice; strict schema;
  source-verbatim rule over every optional facet value) plus a full-file
  re-assertion of everything written so far, before the next batch started.
  Sub-batch summaries (rowids, optional facets emitted, required-facet nulls):
  batch 1 `7195–7244` 30 / setting 4; batch 2 `7245–7294` 30 / setting 4,
  act 2; batch 3 `7295–7344` 23 / setting 6, act 2; batch 4 `7345–7394` 27 /
  setting 6, act 2; batch 5 `7395–7444` 24 / setting 7; batch 6 `7445–7494`
  30 / setting 1; batch 7 `7495–7544` 23 / setting 2; batch 8 `7545–7594` 23 /
  act 1; batch 9 `7595–7644` 16 / setting 6; batch 10 `7645–7694` 29 /
  setting 1 — 255 optional facets over 500 rows; setting null 37; act null 7;
  finish null 494.
- Pre-write assertion catches (each caught before any byte of its batch was
  written; corrected then appended): (1) the rowid 7202 scene draft carried
  `exposure: "lingerie"` but that caption states no dress state at all — the
  key was removed; (2) the rowid 7385 mood was drafted as `smiling` but the
  caption reads "as she smiles" — corrected to `smiles`; (3) the batch-6 scene
  draft misaligned 4 rows of its rowid mapping — caught by the rowid-tag
  alignment assertion and the scene file rewritten with the correct
  7495–7544 mapping before append. No partial or corrupt state existed at any
  point; the 10 batch summaries sum to 500/500 appended.

## 4. QC results (validator + independent mechanical audit, all pass)

`validate_shard.py` (exit 0 with `--t0-gate`; run from the worktree, prompt
normalized to LF as per §1), verbatim output:

```json
{
  "coverage_rate": 1.0,
  "errors": [],
  "fidelity_rate_high_stratum": null,
  "n": 500,
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

- `fidelity_rate_high_stratum: null` is expected for caption-only input shards
  (empty fidelity denominator), same as shards 1–17.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit over **all 500 rows** (separate fresh script
re-reading both files from disk, not the per-batch tooling; run once over the
finished 500):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to
  the input shard; all ids unique; rowids 7195–7694 (min 7195, max 7694,
  strictly increasing); 0 empty captions.
- Strict schema (exact top-level key set `{candidate_id, prompt_version,
  prompt_sha256, scene}`, `prompt_version`/`prompt_sha256` literals, `scene`
  with required `setting`/`act`/`finish`, no unknown scene keys, snake_case
  `^[a-z0-9_]+$` values, optional facets never null): **500/500 = 100%**,
  0 failures.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (255 values: 161 exposure + 94 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the source caption as a contiguous,
  case-insensitive substring): **0**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)

## 5. Facet statistics (recomputed from the artifact)

- Optional facets emitted: **255 total** — `exposure` 161, `mood` 94 — over
  **232 rows** (both facets on 23 rows).
- Nulls (required facets, null-when-unknown rule): `setting` 37, `act` 7,
  `finish` 494.
- `finish` non-null only where the caption states an ending/resolution —
  `climax` on 6 rows: rowid 7328 ("opens her mouth in climax"), 7384 / 7495 /
  7678 (caption states visible semen/ejaculation), 7546 ("semen is ejaculated
  into her throat"), 7452 ("visible ejaculation"). Rowid 7638 states only an
  ambiguous "fluid dripped" without naming semen, so `finish` was left null
  there.
- `exposure` (161, 18 distinct — surface forms reflect the verbatim-only rule):
  `nude` 86, `lingerie` 29, `shirtless` 13, `no_clothing` 8,
  `minimal_clothing` 5, `bare_buttocks` 4, `no_clothes` 3, `underwear` 3, and
  `bare_bodies`/`bare_chest`/`bare_shoulder`/`bare_skin`/`bare_torso`/
  `no_top`/`pants`/`partially_nude`/`shorts`/`thong` 1 each.
- `mood` (94, 24 distinct): `pleasure` 31, `passionately` 11, `focused` 9,
  `smiles` 6, `moans`/`smile` 4 each, `aroused`/`intense`/`relaxed`/
  `smiling` 3 each, `discomfort`/`focus`/`vocalizing` 2 each, and `arousal`/
  `confident`/`contemplative`/`delight`/`excitement`/`focusing`/`passive`/
  `satisfied`/`serious`/`startled`/`vulnerable` 1 each.
- `setting` (463 non-null, 11 distinct): `bedroom` 302, `indoor` 114,
  `kitchen` 15, `living_room` 10, `bathroom` 9, `outdoor` 4, `beach` 4,
  `spa` 2, and `dining_room`/`pier`/`studio` 1 each; null 37 (close-ups and
  plain-background shots whose captions state no locatable environment).
- `act` (493 non-null, 21 distinct): `oral_sex` 144,
  `intercourse_from_behind` 97, `intercourse` 65, `anal_sex` 50, `group_sex`
  41, `kissing` 22, `foreplay` 18, `massage` 12, `conversation` 8, `posing`/
  `walking` 7 each, `masturbation`/`undressing` 4 each, `handjob` 3,
  `reading`/`sex`/`talking_on_phone`/`waking_up` 2 each, and
  `applying_makeup`/`observing`/`sleeping` 1 each; null 7 (captions stating
  only gazing/positioning/genital-region contact with no describable action).
- Non-explicit rows (10 non-sex scenes, e.g. waking/resting, conversation over
  champagne, Santa Monica Pier boardwalk, lifeguard-tower texting, reading) are
  described with their actual acts (`waking_up`, `sleeping`, `conversation`,
  `walking`, `talking_on_phone`, `reading`) — no forcing of adult semantics
  onto clean rows.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `0ada86c32505d9efabc07a3cf9108aef05f6e2b9b9354ffa9c4e21f46d61dc2a` and the
  full QC summary (validator output + `stage_c_manual` block covering the relay
  continuity, the 10-batch pacing provenance with the three pre-write catches,
  the verbatim rule/scope including the hyphen ruling, and the facet statistics
  above, same shape as shards 8–17).
- Ready sidecar:
  `data/token_sprint/shards/shard-018.p2.v2.77b33963-92c6-4da6-a23d-bf1f97711309.jsonl.ready`
- Ready SHA-256: `c8bb2c1aa34e1e6223ea05091aa4bb80388bc23e74ef4713d9683054796ceeb4`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL and DB metadata only); the production DB
  was opened strictly read-only (`mode=ro` + `query_only=ON`) and never
  written; `pyproject.toml`/`uv.lock` untouched; no existing artifact bytes
  modified (the prompt file's temporary LF normalization was reverted
  byte-exactly, §1). This shard only adds new `shard-018` products (input
  shard, output shard, `.qc.json`, `.ready`) in the gitignored
  `data/token_sprint/` area plus this report; scratch tooling (worktree-local,
  untracked: input builder, per-batch appender/assert, full-file auditor,
  ready publisher, 10 scene drafts) was removed after QC.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
