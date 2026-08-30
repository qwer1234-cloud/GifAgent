# W1 Stage C — P2 scene metadata, twenty-eighth p2/v2 shard (rowids 12278–12777, 500 rows)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec28` (worktree `.worktrees/sprint-w1-stagec28`, created from
  the shard-027 landing commit `6bbf5c94`)

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–27): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  validator run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-027)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-028.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only, same as
  shards 1–27). Built by this run from the production library DB with
  read-only access (`file:…mode=ro`, `PRAGMA query_only=ON`,
  `busy_timeout=30000`); no write connection was ever opened and no media
  bytes were read. Input SHA-256:
  `176651ad048c5d31e0d08096d466100d82b0407171298818b01b7ac8ea870e1e`
- Window: **rowids 12278–12777** — the first 500 enriched rows with
  `rowid > 12277` (relay window: rows whose `vlm_summary_json` contains
  `enriched_by` with non-empty `$.caption`), ordered by rowid ascending.
  Continuity verified mechanically by the builder script:
  - shard-027's input on disk ends at rowid **12277** (re-read pre-check);
    a SQL probe confirmed the first enriched row after 12277 is **12278**,
    and it is the first picked row — no overlap, no gap.
  - Span completeness: every enriched rowid in (12277, 12777] is picked;
    picked rowids are strictly ascending **and consecutive** (this window
    contains no unenriched rows at all — 0 designed exclusions inside the
    span). The next enriched row after the window is 12778.
  - 500 unique `candidate_id`s; no empty captions; no empty `enriched_by`;
    candidate_id sets of the shard-027 and shard-028 input shards are
    disjoint.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-028.p2.v2.f6f02f4b-70a4-4642-9807-13e8867a199d.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` joined
  programmatically from the input shard by the assembler, never typed)
- SHA-256: `a4b056dadab1ff7a2cff34cb39150e56f9796da20be5885c58ccc72499f0d2dc`
- Custody: fresh generation in **10 sub-batches of 50 rows**. Caption dumps
  (`dump_b01`–`dump_b10`) were written from the input shard; the annotator
  wrote scene drafts keyed by rowid only (`draft_b01`–`draft_b10`); the
  assembler joined candidate_ids from the input by rowid and, for every
  sub-batch, asserted the strict schema and the source-verbatim rule
  pre-append, appended the 50 lines atomically (tmp + `os.replace`), then
  re-asserted cumulatively over the whole file (rowid sequence == input
  slice, id order, schema, verbatim over every optional facet).
  - Assertion corrections: **4** (all caught by the pre-append assertion
    before anything was written; each re-submitted batch then passed both the
    pre-append and cumulative re-assertion; 434 optional facets verified
    verbatim in total):
    - batch 07, rowid 12583: mood `smiling` → `smiles` (caption says
      "she smiles");
    - batch 07, rowid 12586: mood `moaning` → `moans` (caption says
      "she moans");
    - batch 07, rowid 12600: mood `smiling` → `smiles` (caption says
      "she smiles");
    - batch 10, rowid 12755: exposure `stockings` → `thigh_highs` (caption
      says "lace thigh-highs" and never contains the word "stockings").
    All four were word-exactness/inflection fixes — the verbatim rule requires
    the caption's own surface form, and the mechanical assertion rejected the
    first drafts before any bytes reached the shard.

## 4. QC results (stock validator + independent mechanical audit, all pass)

Stock validator (`scripts/token_sprint/validate_shard.py`, run from the
worktree with the prompt normalized to LF as per §1, `--t0-gate`, exit 0),
verbatim output:

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
  (empty fidelity denominator), same as shards 1–27.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit (`scratch_stagec28/audit_shard_028.py`, fresh
script written for this shard, re-reading both files from disk, **full
coverage — no sampling**; verdict PASS):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to
  the input shard line-by-line (0 mismatches); all 500 ids unique; rowids
  strictly ascending 12278–12777.
- Strict schema over all 500 rows (exact top-level key set `{candidate_id,
  prompt_version, prompt_sha256, scene}`, `prompt_version`/`prompt_sha256`
  literals, `scene` with required `setting`/`act`/`finish` + optional
  `exposure`/`mood` only, no unknown scene keys, snake_case `^[a-z0-9_]+$`
  values, optional facets never null): **500/500, 0 failures**.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (434 values: 362 exposure + 72 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the row caption as a contiguous,
  case-insensitive substring; the audit's normalizer additionally collapses
  whitespace runs, making it a superset of the pre-append check): **0
  failures**. (`setting`/`act`/`finish` are controlled-vocabulary mappings per
  the standard, not verbatim-bound.)
- Semantic review: every one of the 500 captions was read in full during
  annotation (10×50 caption dumps, batch by batch) — full-coverage semantic
  grounding rather than a sampled spot-check.

## 5. Facet statistics (recomputed from the artifact by the audit)

- Optional facets emitted: **434 total** — `exposure` 362 (24 distinct),
  `mood` 72 (17 distinct). Facet presence: 55 rows carry both optional
  facets, 324 exactly one, 121 none. Optional facets are never null
  (absent key when the caption gives no evidence).
- Nulls (required facets, null-when-unknown rule): `setting` 0, `act` 0,
  `finish` 499. Every caption in this window names a locatable environment
  and a describable act (0 nulls for both). Exactly one caption states an
  ending/resolution ("clear genital stimulation and climax", rowid 12597),
  giving the window's single non-null `finish` (`climax`); the other 499 are
  null — same pattern as the other p2/v2 shards.
- `exposure` (362, 24 distinct — surface forms reflect the verbatim-only rule):
  `nude` 138, `lingerie` 124, `underwear` 46, `shirtless` 13, 4 each of
  `thong`/`thigh_highs`/`bodysuit`/`stockings`, `bikini` 3, 2 each of
  `bra`/`minimal_clothing`/`topless`/`no_clothing`/`unclothed`, and 1 each of
  `no_clothes`/`no_shirt`/`panties`/`shorts`/`towel`/`harness`/
  `bare_chest`/`bare_back`/`pajamas`/`garter_belt` (the `no_*` and
  `bare_*` surface forms are the caption's own wording for nudity states,
  kept verbatim per the rule).
- `mood` (72, 17 distinct): `pleasure` 16, `smiling` 10, `passionately` 9,
  `smile` 7, `smiles` 6, `focused`/`playful` 4 each, `moans` 3, 2 each of
  `moaning`/`distress`/`serious`/`intense`, and 1 each of `playfully`/
  `sensual`/`intensely`/`passionate`/`distressed` (surface-form variance is
  inherent to the verbatim-only rule; all values are caption-verbatim).
- `setting` (500 non-null, 8 distinct): `bedroom` 142, `indoor` 139,
  `office` 127, `living_room` 56, `kitchen` 21, `bathroom` 13, and 1 each of
  `outdoor`/`car`. The window opens with the tail of the office/pink-lingerie
  block from shard-027 (rowids 12278–12373), shifts to red-haired bedroom
  scenes, moves through solo/kitchen/bathroom blocks, an extended office-desk
  lingerie block, a rose-petal couch/kitchen block, and closes with a
  bedroom/group block; `car` (rowid 12548) is a new distinct setting value.
- `act` (500 non-null, 21 distinct): `oral_sex` 104,
  `intercourse_from_behind` 90, `intercourse` 88, `posing` 61, `caressing` 36,
  `kissing` 35, `sex`/`masturbation` 21 each, `anal_sex` 13, `group_sex` 9,
  `grinding`/`manual_stimulation`/`makeup` 4 each, `undressing` 2, and 1 each
  of `massage`/`conversation`/`dancing`/`wiping`/`foreplay`/`sleeping`/
  `carrying`; null 0.
- `finish` (1 non-null): `climax` 1, null 499.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `a4b056dadab1ff7a2cff34cb39150e56f9796da20be5885c58ccc72499f0d2dc` and the
  full QC summary (validator output + `stage_c_manual` block carrying the
  relay window and continuity evidence, the read-only DB access note, the
  generation custody model including the four assertion corrections, the
  independent audit numbers, the verbatim rule including the hyphen ruling,
  the facet statistics, and the prompt-file CRLF/LF note, same shape as
  shards 8–27).
- Ready sidecar:
  `data/token_sprint/shards/shard-028.p2.v2.f6f02f4b-70a4-4642-9807-13e8867a199d.jsonl.ready`
- Ready SHA-256: `e54cfebc418724d8dfaf281a60c44f3716d1217e691f0e87637b77e3f3efb5b7`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL and text prompt only). DB access was
  strictly read-only (`mode=ro` + `query_only=ON` + `busy_timeout=30000`,
  single connection closed in a `finally` block); the production library DB
  was never written. `pyproject.toml`/`uv.lock` untouched; no existing
  artifact in the sprint root was modified — this run created only: the input
  shard, the output shard, its stock `.qc.json`, the `.ready` sidecar, and
  this report.
- Scratch tooling (worktree-local, untracked under `scratch_stagec28/`:
  `build_input_028.py`, `batch_tool_028.py`, `run_validator_028.py`,
  `audit_shard_028.py`, `publish_ready_028.py`, per-batch caption dumps
  `dump_b01`–`dump_b10` and scene drafts `draft_b01`–`draft_b10`) is **not
  committed**; nothing outside the shard-028 product set was touched in the
  sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
