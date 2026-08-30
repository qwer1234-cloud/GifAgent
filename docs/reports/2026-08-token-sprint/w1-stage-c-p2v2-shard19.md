# W1 Stage C — P2 scene metadata, nineteenth p2/v2 shard (rowids 7695–8194) — recovery run

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec19` (worktree `.worktrees/sprint-w1-stagec19`, created
  from `81cfd105`, the master tip that landed shard-018)
- Nature of this run: **recovery** — the previous executor session appended the
  first 400 of 500 rows (10 planned sub-batches, batches 1–8 done) and then
  aborted with a model failure. This session kept those 400 bytes untouched,
  mechanically re-verified all of them, and generated the remaining 100 rows
  (batches 9–10) to completion.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–18): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The
  worktree copy was normalized to the canonical LF bytes for the QC run only
  (content unchanged), so the validator reported the frozen `01a161b0…` hash;
  the stock CRLF checkout was restored byte-exactly immediately after
  validation (re-read back: `d4971361…`, 74 CRLFs) and the worktree `git
  status` shows no tracked-file modification. The main-sprint-root prompt file
  was never touched.

## 2. Input set (relay window after shard-018, continuity verified)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-019.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption taken from
  `vlm_summary_json.$.caption`; caption-only by design, same as shards 1–18).
- Input SHA-256: `5d9210f0aa9f74bd01fd0d172d112ae314084791502b11dc3c20063dfef28a0e`
- Window: **rowids 7695–8194**, strictly increasing, relayed from shard-018's
  verified endpoint 7694 (input shard was built and continuity-checked by the
  earlier session; re-verified this session: 500 lines, first rowid 7695 /
  last 8194, ids unique).

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-019.p2.v2.298f5679-4308-487b-8bda-b16d17d71577.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` copied
  programmatically from the input shard by the append tooling, never typed by
  hand)
- SHA-256: `85c87b38404d3bbac0baeec103b232d62744b5c355cf120fa6fbe4002458cf55`
- Custody / recovery provenance:
  - Lines 1–400 (rowids 7695–8094, batches 1–8 of the aborted session) were
    **kept byte-for-byte**. Before any new work, a fresh independent audit
    script (re-reading both files from disk, not the prior tooling) verified
    all 400: strict schema (exact top-level key set, prompt literals,
    required/optional scene keys, snake_case values, optional facets never
    null), `candidate_id` order identical to input lines 1–400, rowids
    strictly increasing, and the source-verbatim rule over every one of the
    263 optional facet values present (172 exposure + 91 mood) — **0
    failures**. Required-facet nulls in that segment: setting 84, act 9,
    finish 397.
  - Lines 401–500 (rowids 8095–8194) were generated this session in **2
    sub-batches of 50**. Each sub-batch draft (keyed by rowid) passed
    pre-write mechanical assertions (exactly 50 drafts; rowid alignment
    against the input slice; strict schema; source-verbatim rule over every
    optional facet value) before any byte was written, was appended
    **atomically (tmp + `os.replace`)**, and was followed by a full-file
    re-assertion of everything written so far:
    - batch 9 `8095–8144`: file 450 lines, full re-assertion passed;
      contributed exposure 25 / mood 14; setting null +18, act null +3,
      finish null +50.
    - batch 10 `8145–8194`: file 500 lines, full re-assertion passed;
      contributed exposure 46 / mood 20; setting null +35, act null +1,
      finish null +50.
  - No pre-write assertion failure was hit this session (both batch drafts
    passed their checks on the first run; the earlier session's catches are
    part of its own custody, not this one).

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
  (empty fidelity denominator), same as shards 1–18.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit over **all 500 rows** (separate fresh script
re-reading both files from disk, run once over the finished 500):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to
  the input shard; all ids unique; rowids 7695–8194 (min 7695, max 8194,
  strictly increasing).
- Strict schema (exact top-level key set `{candidate_id, prompt_version,
  prompt_sha256, scene}`, `prompt_version`/`prompt_sha256` literals, `scene`
  with required `setting`/`act`/`finish`, no unknown scene keys, snake_case
  `^[a-z0-9_]+$` values, optional facets never null): **500/500 = 100%**,
  0 failures.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (368 values: 243 exposure + 125 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the source caption as a contiguous,
  case-insensitive substring): **0**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)

## 5. Facet statistics (recomputed from the artifact)

- Optional facets emitted: **368 total** — `exposure` 243, `mood` 125.
- Nulls (required facets, null-when-unknown rule): `setting` 137, `act` 13,
  `finish` 497.
- `finish` non-null only on the 3 rows where the caption states an
  ending/resolution — `climax` on 2 rows and `affection` on 1 (all inherited
  from the verified 400-line segment; none of rows 8095–8194 state an
  ending).
- `exposure` (243, 37 distinct — surface forms reflect the verbatim-only
  rule): `nude` 72, `lingerie` 67, `thigh_highs` 21, `underwear` 10,
  `denim_shorts` 7, `pink_top` 7, `shirtless` 6, `bikini` 6, `no_clothing` 5,
  `thigh_high_stockings` 3, then 2 each of `bare_torso`/`bare_chest`/
  `unclothed`/`lace_top`/`exposed`/`tank_top`/`tights`/`minimal_clothing`/
  `crop_top`/`robe`/`black_top`/`swimsuit`, and 1 each of `bare_skin`/
  `partially_undressed`/`fully_exposed`/`thong`/`bodysuit`/`ribbed_top`/
  `white_top`/`no_clothing_visible`/`bare_shouldered`/`bare_legs`/
  `bare_lower_abdomen`/`bare_back`/`partially_nude`/`no_visible_clothing`/
  `bra`.
- `mood` (125, 32 distinct): `pleasure` 40, `intense` 12, `focused` 10,
  `smiling` 7, `moans`/`passionately`/`vocalizing` 6 each, `smiles` 4,
  `intently`/`smile`/`relaxed` 3 each, `passionate`/`concentration`/
  `thoughtfully`/`surprised` 2 each, and `attentive`/`intensity`/`playful`/
  `startled`/`pensive`/`arousal`/`neutral`/`moaning`/`downcast`/`joyfully`/
  `sensual`/`intensely`/`vulnerability`/`serious`/`enjoying`/`concern`/
  `longing` 1 each.
- `setting` (363 non-null, 9 distinct): `bedroom` 242, `indoor` 87,
  `outdoor` 15, `studio` 9, `bathroom` 4, `kitchen`/`office` 2 each,
  `shower`/`living_room` 1 each; null 137 (close-ups and blue-wall/blue-couch
  staged shots whose captions state no locatable environment; `studio`-like
  light quality alone, e.g. "even and studio-like lighting", was not counted
  as a studio setting).
- `act` (487 non-null, 25 distinct): `oral_sex` 110,
  `intercourse_from_behind` 102, `intercourse` 76, `anal_sex` 35,
  `group_sex` 34, `foreplay` 24, `kissing` 21, `posing` 18, `sex` 13,
  `anal_stimulation` 13, `massage`/`conversation` 11 each, `handjob` 3,
  `observing`/`anilingus`/`manual_stimulation`/`vibrator_stimulation` 2 each,
  and `undressing`/`looking_at_phone`/`walking`/`sleeping`/
  `pouring_champagne`/`piercing_adjustment`/`applying_makeup`/`embracing` 1
  each; null 13 (positioning/approach-only shots with no describable action).
- Non-explicit rows (makeup/vanity prep, crew conversation about lunch,
  phone-cuff staging, piercing-jewelry adjustment, champagne pouring, sleep,
  walking) are described with their actual acts — no forcing of adult
  semantics onto clean rows.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `85c87b38404d3bbac0baeec103b232d62744b5c355cf120fa6fbe4002458cf55` and the
  full QC summary (validator output + `stage_c_manual` block covering the
  relay window, the recovery custody of §3, the verbatim rule/scope including
  the hyphen ruling, the facet statistics, and the prompt-file CRLF/LF note,
  same shape as shards 8–18).
- Ready sidecar:
  `data/token_sprint/shards/shard-019.p2.v2.298f5679-4308-487b-8bda-b16d17d71577.jsonl.ready`
- Ready SHA-256: `6aaa51ce92a5f4c2663ce9e9d8ba185dcb0bab482a19f8ed72760a12241bcf88`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL only); no production database was opened
  or written this session (the input shard already existed on disk);
  `pyproject.toml`/`uv.lock` untouched; no existing artifact bytes modified —
  the first 400 output lines were appended-to, never rewritten, and the worktree
  prompt file's temporary LF normalization was reverted byte-exactly (§1).
  This session only added: output lines 401–500, the `.qc.json`, the `.ready`
  sidecar, and this report. Scratch tooling (worktree-local, untracked:
  inherited append/assert tooling, recovery audit script, 2 scene drafts,
  ready publisher) was removed after QC; nothing outside the shard-019
  product set was touched in the sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
