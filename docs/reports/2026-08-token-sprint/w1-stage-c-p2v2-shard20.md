# W1 Stage C — P2 scene metadata, twentieth p2/v2 shard (rowids 8195–8694)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec20` (worktree `.worktrees/sprint-w1-stagec20`, created
  from `f997455f`, the master tip that landed shard-019)
- Nature of this run: fresh full 500-row shard, generated in **10 sub-batches of
  50** with per-batch assertions and atomic appends (no recovery needed).

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–19): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  QC run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The
  main-sprint-root prompt file was never touched.

## 2. Input set (relay window after shard-019, continuity verified)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-020.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption taken from
  `vlm_summary_json.$.caption`; caption-only by design, same as shards 1–19).
- Input SHA-256: `d37f8a9a7cc6110c638038517ecf6bd60566fa54a370826eff05187402f3b438`
- Window: **rowids 8195–8694** — exactly the first 500 enriched rows
  (`vlm_summary_json` contains `enriched_by` and its `$.caption` is non-empty)
  with `rowid > 8194`, ordered by rowid ascending, read from the production
  library `dist/GifAgentUI/data/library.db` strictly read-only
  (`mode=ro` + `PRAGMA query_only=ON` + `PRAGMA busy_timeout=30000`).
- Relay continuity (mechanically verified at build time and re-verified by the
  independent audit): shard-019's input on disk ends at rowid **8194**; the
  first row of this window is 8195 = the next enriched row after 8194 (no
  overlap, no gap — the window's 500 rowids are contiguous integers
  8195–8694, and the next enriched row after the window is 8695); rowids
  strictly increasing, ids unique, no empty captions, no reuse of shard-019
  candidate_ids.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-020.p2.v2.1ad0e579-3f87-4968-bf8f-acd9fc393a3e.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` copied
  programmatically from the input shard by the append tooling, never typed by
  hand)
- SHA-256: `ec6c03abc39ffa63370cf1fa673af2a60e0fde7a0fa7986e91e5e28cc115789f`
- Custody: generated in 10 sub-batches of 50 (input lines 1–10 × 50). Each
  sub-batch draft (keyed by rowid) passed pre-write mechanical assertions
  (exactly 50 drafts; rowid set alignment against the input slice; strict
  schema; source-verbatim rule over every optional facet value) before any
  byte was written, was appended **atomically (tmp + `os.replace`)**, and was
  followed by a full-file re-assertion of everything written so far (cumulative
  after each batch: 50/100/150/200/250/300/350/400/450/500 lines).
- Pre-write assertion catches (fixed in the draft before any byte was written;
  the discipline worked as designed — 6 catches across the run, all
  caption-word-form mismatches of the same kind, e.g. `smiling` where the
  caption has `smiles`/`smile`, `laughing` where the caption has `laughs`):
  rowids 8248, 8432, 8486, 8552 (+8571/8575/8580 in one pass), 8607, 8676
  (+8678). No post-write failure was ever hit; every batch passed its
  pre-write checks on the corrected draft on the first append.

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
  (empty fidelity denominator), same as shards 1–19.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit over **all 500 rows** (separate fresh script
re-reading both files from disk, run once over the finished 500):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to
  the input shard; all ids unique; rowids 8195–8694 (min 8195, max 8694,
  strictly increasing, contiguous; baseline 8194 respected).
- Strict schema (exact top-level key set `{candidate_id, prompt_version,
  prompt_sha256, scene}`, `prompt_version`/`prompt_sha256` literals, `scene`
  with required `setting`/`act`/`finish`, no unknown scene keys, snake_case
  `^[a-z0-9_]+$` values, optional facets never null): **500/500 = 100%**,
  0 failures.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (477 values: 340 exposure + 137 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the source caption as a contiguous,
  case-insensitive substring): **0**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)

## 5. Facet statistics (recomputed from the artifact)

- Optional facets emitted: **477 total** — `exposure` 340, `mood` 137.
- Nulls (required facets, null-when-unknown rule): `setting` 74, `act` 29,
  `finish` 499.
- `finish` non-null only on the 1 row where the caption states the clip's
  resolution — `climax` (rowid 8561, caption "…as he ejaculates into her
  mouth…").
- `exposure` (340, 21 distinct — surface forms reflect the verbatim-only rule):
  `nude` 136, `lingerie` 114, `bikini` 27, `shirtless` 18, `underwear` 8,
  `pink_top` 5, `bare_buttocks` 4, `black_top` 4, `robe` 4, `thong` 3,
  `no_clothing` 3, `bra` 3, `bare_chest` 2, `crop_top` 2, and 1 each of
  `black_turtleneck`/`bare_torso`/`bare_skin`/`thigh_highs`/`bare_buttock`/
  `halter_top`/`garter_belt`.
- `mood` (137, 36 distinct): `pleasure` 44, `focused`/`smiling` 10 each,
  `smiles` 9, `laughing` 7, `moans` 5, `smile` 5, `intense`/`relaxed`/
  `laughs`/`intensely` 4 each, `passionately` 3, `passionate`/`enjoying`/
  `sensual`/`confidently` 2 each, and 1 each of `vocalizing`/`focus`/
  `pensive`/`arousal`/`intensity`/`tense`/`somber`/`contemplative`/
  `pensively`/`affection`/`playful`/`joyfully`/`surprised`/`surprise`/
  `laughter`/`concentration`/`downcast`/`confident`/`playfully`/`moaning`
  (surface-form variance is inherent to the verbatim-only rule).
- `setting` (426 non-null, 8 distinct): `outdoor` 191 (the long beach/pool
  series), `indoor` 111, `bedroom` 66, `kitchen` 26, `living_room` 15,
  `office` 14, `bathroom` 2, `studio` 1; null 74 (close-ups and staged
  blue-wall/blue-couch or bare-"on a bed"/"on a couch" shots whose captions
  state no locatable environment; `indoor`/`studio` light quality alone, e.g.
  "warm indoor lighting", was not counted as an environment; `living_room`/
  `kitchen`/`office`/`bathroom` only when the caption names them, including
  hedged first-named options like "likely an office").
- `act` (471 non-null, 22 distinct): `intercourse_from_behind` 94,
  `oral_sex` 75, `intercourse` 65, `group_sex` 42, `anal_sex` 39, `sex` 28,
  `massage` 26, `kissing` 25, `posing` 23, `conversation` 16, `foreplay`/
  `masturbation` 6 each, `handjob` 5, `undressing`/`walking`/`embracing` 4
  each, `vibrator_stimulation`/`looking_at_phone`/`anal_stimulation` 2 each,
  and `manual_stimulation`/`anilingus`/`sleeping` 1 each; null 29
  (approach/reaction-only shots with no describable action: phone-and-bag
  vignettes, window-reaction shots, food prep, greetings, sleeping-adjacent
  lounging).
- Multi-person scenes map to `group_sex` only when a second person actively
  engages (penetrating, receiving, being stroked/touched as part of the act);
  pure watchers (e.g. "another shirtless man watches") keep the single-actor
  act. Non-explicit rows (conversation, massage, posing, food prep, phone
  scenes, sleep) are described with their actual acts — no forcing of adult
  semantics onto clean rows.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `ec6c03abc39ffa63370cf1fa673af2a60e0fde7a0fa7986e91e5e28cc115789f` and the
  full QC summary (validator output + `stage_c_manual` block covering the
  relay window, the verbatim rule/scope including the hyphen ruling, the facet
  statistics, and the prompt-file CRLF/LF note, same shape as shards 8–19).
- Ready sidecar:
  `data/token_sprint/shards/shard-020.p2.v2.1ad0e579-3f87-4968-bf8f-acd9fc393a3e.jsonl.ready`
- Ready SHA-256: `4f9b41e4a970a01bcd2264cf316771dd10f396ba0e5fc448c55cd5f00ee16bce`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL only); the production database was opened
  strictly read-only (`mode=ro` + `query_only=ON` + `busy_timeout=30000`) and
  never written; `pyproject.toml`/`uv.lock` untouched; no existing artifact in
  the sprint root was modified — this run only added: the input shard, the
  output shard, the `.qc.json`, the `.ready` sidecar, and this report.
- Scratch tooling (worktree-local, untracked under `scratch_stagec20/`: input
  builder, sub-batch append/assert tooling, batch drafts, independent audit
  script, ready publisher) was removed after QC; nothing outside the shard-020
  product set was touched in the sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
