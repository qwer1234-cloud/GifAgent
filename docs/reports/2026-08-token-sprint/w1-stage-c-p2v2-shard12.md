# W1 Stage C — P2 scene metadata, twelfth p2/v2 shard (rowids 4355–4974)

- Date: 2026-08-30
- Executor: w1-stagec-executor (fresh generation session; a prior shard-012 attempt
  died to a transient model failure before producing any artifact — no rows,
  QC, or sidecars existed; this session built everything from scratch)
- Branch: `sprint/w1-stagec12` (worktree `.worktrees/sprint-w1-stagec12`, created
  from `master` @ `da8a3ef1`, the shard-11 merge tip)
- Standard: same schema and gates as p2/v2 shards 1–11
  (`w1-stage-c-p2v2.md` … `w1-stage-c-p2v2-shard11.md`); manifest rev15 ruling —
  `prompt_version p2/v2`, optional facets (`exposure`, `mood`) restricted to
  source-verbatim mapping of the input caption.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–11): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The
  worktree copy was normalized to the canonical LF bytes for the QC run only
  (content unchanged), so the validator reported the frozen `01a161b0…` hash; the
  stock CRLF checkout was restored immediately after validation (re-read back:
  `d4971361…`) and `git status` is clean.

## 2. Input set (relay window, continuity verified against shard-011)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-012.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same as
  shards 1–11).
- Input SHA-256: `cadc106bc97c70a4ac688219b2ddde05d430c7e1db48d6be124d6dae8fa60f62`
- Window rule (relay, no fixed OFFSET): shard-011's input ends at rowid **4354**
  (verified: last line of `shard-w1stagec-011.input.jsonl`, SHA-256
  `069d936f…8a80ea`, 500 lines); this shard takes the next 500 enriched rows
  (`vlm_summary_json` contains `enriched_by`, JSON `$.caption` non-empty) in
  ascending rowid order → **rowids 4355–4974, strictly increasing, 0 empty
  captions**. Continuity checks:
  - first row is rowid **4355**, and the minimum enriched rowid greater than
    4354 is exactly 4355 → no overlap, no gap;
  - the window query was **replayed against shard-011's range (3726–4354) and
    reproduced the on-disk `shard-w1stagec-011.input.jsonl` field-for-field
    (500/500 exact match)** before being extended past 4354 — the row-selection
    semantics are identical to shards 1–11;
  - the 4474 rowids inside 4355–4974 absent from the shard are by-design skips
    (un-enriched rows), consistent with the shards 1–11 window rule
    (8927 enriched rows total exist after 4354; only the first 500 taken).
- DB access: production `dist/GifAgentUI/data/library.db` opened **read-only**
  (`mode=ro` URI + `PRAGMA query_only=ON` + `busy_timeout=30000`), SELECT-only
  over text columns (`rowid`, `candidate_id`, `json_extract` of caption /
  enriched_by). No media bytes opened; nothing written to any database.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-012.p2.v2.93a7a107-8f7f-4874-a18e-ddbd9e76a9b1.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version: "p2/v2"`
  and `prompt_sha256: 01a161b0…`; `candidate_id` copied programmatically from
  the input shard by the append tooling, never typed by hand)
- SHA-256: `6058439bc18711c20e584b504cc5ae7129996cb11ae0560621c24bd63f7b8b28`
- Generation pacing (context-hard-constraint protocol): 10 sub-batches of 50
  rows. Each sub-batch was appended **atomically (tmp + `os.replace`)** and then
  passed a per-sub-batch mechanical assertion (fragment rowids == input slice in
  order, strict schema, source-verbatim rule over every optional facet value)
  before the next batch started. One assertion failure occurred (batch 9,
  rowid 4898: `exposure "stockings"` — caption says "thigh-highs" not
  "stockings"); it was **caught before any byte was written**, the fragment was
  corrected to `thigh_highs`, and the batch re-appended. No partial or corrupt
  state existed at any point; the 10 batch summaries sum to 500/500.

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

- `fidelity_rate_high_stratum: null` is expected for caption-only input shards (no
  `stratum` field and no `vlm_summary_json` content text → empty fidelity
  denominator), same as shards 1–11.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit over **all 500 rows** (separate fresh script
re-reading both files from disk, not the per-batch tooling):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to the
  input shard; all 500 ids unique; rowids 4355–4974 (min 4355, max 4974,
  strictly increasing).
- Strict schema (exact top-level key set `{candidate_id, prompt_version,
  prompt_sha256, scene}`, `prompt_version`/`prompt_sha256` literals, `scene` with
  required `setting`/`act`/`finish`, no unknown scene keys, snake_case
  `^[a-z0-9_]+$` values, optional facets never null): **500/500 = 100%**, 0 failures.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (389 values: 337 exposure + 52 mood; rule: value lowercased with `_`/`-` read
  back as spaces must appear in the source caption as a contiguous,
  case-insensitive substring): **0**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)

## 5. Facet statistics (recomputed from the artifact)

- Optional facets emitted: **389 total** — `exposure` 337, `mood` 52 — over **365
  rows** (both facets on 24 rows).
- Nulls (required facets, null-when-unknown rule): `finish` 499 (1 evidence-backed
  `climax`: rowid 4656, caption "climaxing with her arching upward and moaning"),
  `setting` 23 (location not stated, e.g. close-up-only rows), `act` 2 (rowid 4370 —
  decorated tree + fireplace close-ups, no human action; rowid 4457 — coastal town
  and sailboat scenery).
- `exposure` (337, 14 distinct): `lingerie` 111, `nude` 79, `underwear` 54,
  `stockings` 46, `bodysuit` 19, `shirtless` 8, `garter_belt` 5, `thong` 5, `bra` 3,
  `thigh_highs` 3, `unclothed` 1, `no_clothes` 1, `breasts_exposed` 1,
  `partially_nude` 1.
- `mood` (52, 19 distinct — surface forms reflect the verbatim-only rule):
  `pleasure` 14, `focused` 10, `passionate` 9, `playful` 2, `thoughtful` 2,
  `relaxed` 2, and `delight`/`enjoying`/`calm`/`contemplative`/`composed`/
  `questioning`/`suggestive`/`serious`/`frustrated`/`pensive`/`surprise`/
  `arousal`/`smiling` 1 each.
- `finish`: `climax` 1 (rowid 4656).
- `setting` (477 non-null, 9 distinct): `bedroom` 300, `indoor` 129,
  `living_room` 23, `outdoor` 13, `dining_room` 7, `balcony` 2, `bathhouse` 1,
  `studio` 1, `kitchen` 1 (null 23).
- `act` (498 non-null, 37 distinct): `intercourse` 96, `oral_sex` 92,
  `intercourse_from_behind` 64, `foreplay` 58, `posing` 23, `anal_sex` 22,
  `kissing` 17, `group_sex` 15, `sex` 14, `dildo_play` 12, `conversation` 11,
  `straddling` 11, `massage` 9, `talking` 6, `walking` 6, `fingering` 4,
  `bondage` 4, `reacting` 4, `cleaning` 4, `eating` 3, `handjob` 3, `texting` 2,
  `unwrapping` 2, `grinding` 2, `striptease` 2, and `penetration`/`self_touch`/
  `toy_insertion`/`playing_piano`/`gazing`/`dressing`/`grooming`/`drinking`/
  `stretching`/`exercising`/`dining`/`masturbation` 1 each.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `6058439bc18711c20e584b504cc5ae7129996cb11ae0560621c24bd63f7b8b28` and the full
  QC summary (validator output + `stage_c_manual` block including the window
  continuity replay, the verbatim rule/scope, the batch pacing provenance, and
  the facet statistics above, same shape as shards 8–11).
- Ready sidecar:
  `data/token_sprint/shards/shard-012.p2.v2.93a7a107-8f7f-4874-a18e-ddbd9e76a9b1.jsonl.ready`
- Ready SHA-256: `8995d04dd558a8000f0dd53eca0e7f1b2784f949cf4c8b36ea267237af77d4b4`

## 7. Constraints honored

- No media bytes opened (text JSONL/DB text columns only); production DB opened
  read-only with `query_only=ON` for the input window only; `pyproject.toml`/
  `uv.lock` untouched (worktree `git status` clean after the §1 prompt restore);
  no existing artifact bytes modified — the prompt file's temporary LF
  normalization was reverted byte-exactly (§1), and this shard only adds new
  `shard-012` products (`shard-w1stagec-012.input.jsonl`, output shard, `.qc.json`,
  `.ready`) in the gitignored `data/token_sprint/` area plus this report.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready` binding.
