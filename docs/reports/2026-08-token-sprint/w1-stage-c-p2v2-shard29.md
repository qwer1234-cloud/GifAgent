# W1 Stage C — P2 scene metadata, twenty-ninth p2/v2 shard (rowids 12778–13277, 500 rows)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec29` (worktree `.worktrees/sprint-w1-stagec29`, created from
  the shard-028 landing commit `cd71c9bf`, i.e. master)

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–28): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  validator run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-028)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-029.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only, same as
  shards 1–28). Built by this run from the production library DB with
  read-only access (`file:…mode=ro`, `PRAGMA query_only=ON`,
  `busy_timeout=30000`); no write connection was ever opened and no media
  bytes were read. Input SHA-256:
  `d1944055dfa2511c758f0cc2a79779e398a67d9bf44b352d9fad2a66473229fa`
- Window: **rowids 12778–13277** — the first 500 enriched rows with
  `rowid > 12777` (relay window: rows whose `vlm_summary_json` contains
  `enriched_by` with non-empty `$.caption`), ordered by rowid ascending.
  Continuity verified mechanically by the builder script:
  - shard-028's input on disk ends at rowid **12777** (re-read pre-check);
    a SQL probe confirmed the first enriched row after 12777 is **12778**,
    and it is the first picked row — no overlap, no gap.
  - Span completeness: every enriched rowid in (12777, 13277] is picked;
    picked rowids are strictly ascending **and consecutive** (this window
    contains no unenriched rows at all — 0 designed exclusions inside the
    span). The next enriched row after the window is 13278.
  - 500 unique `candidate_id`s; no empty captions; no empty `enriched_by`;
    candidate_id sets of the shard-028 and shard-029 input shards are
    disjoint.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-029.p2.v2.aa8112a4-3b18-4aee-bfc8-3936477f1759.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` joined
  programmatically from the input shard by the assembler, never typed)
- SHA-256: `b64e7b4edc44736d51cef8c0df0b5f494fa9468cdf5e6725015178e0e9338ddd`
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
    pre-append and cumulative re-assertion; 401 optional facets verified
    verbatim in total):
    - batch 03, rowid 12910: mood `smiles` → `smiling` (caption says
      "smiling at the camera");
    - batch 05, rowid 12978: mood `smiles` → `smiling` (caption says
      "smiling and laughing");
    - batch 06, rowid 13074: mood `smiles` → `smile` (caption says
      "both smile at the camera");
    - batch 07, rowid 13083: mood `smiles` → `smiling` (caption says
      "her face smiling").
    All four were word-exactness/inflection fixes — the verbatim rule requires
    the caption's own surface form, and the mechanical assertion rejected the
    first drafts before any bytes reached the shard. Separately, two
    tool-side null-handling bugs in the assembler (required-facet null
    rejection, snake_case check over null `finish`) were fixed before any
    bytes were written.

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
  (empty fidelity denominator), same as shards 1–28.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit (`scratch_stagec29/audit_shard_029.py`, fresh
script written for this shard, re-reading both files from disk, **full
coverage — no sampling**; verdict PASS):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to
  the input shard line-by-line (0 mismatches); all 500 ids unique; rowids
  strictly ascending 12778–13277.
- Strict schema over all 500 rows (exact top-level key set `{candidate_id,
  prompt_version, prompt_sha256, scene}`, `prompt_version`/`prompt_sha256`
  literals, `scene` with required `setting`/`act`/`finish` + optional
  `exposure`/`mood` only, no unknown scene keys, snake_case `^[a-z0-9_]+$`
  values, optional facets never null): **500/500, 0 failures**.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (401 values: 317 exposure + 84 mood; rule: value lowercased with `_`/`-`
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

- Optional facets emitted: **401 total** — `exposure` 317 (26 distinct),
  `mood` 84 (19 distinct). Facet presence: 52 rows carry both optional
  facets, 297 exactly one, 151 none. Optional facets are never null
  (absent key when the caption gives no evidence).
- Nulls (required facets, null-when-unknown rule): `setting` 32, `act` 2,
  `finish` 496. This window is dominated by tight close-up/generic-surface
  shots ("gray couch", "green surface", "teal surface", "dark surface") where
  the caption names no locatable environment, hence the 32 null settings; 2
  captions name no describable act (rowids 12877 — genital close-up only —
  and 12935 — man leaning over, no act stated). Exactly four captions state
  an ending/resolution, giving the window's four non-null `finish` values
  (all `climax`: rowids 12894 and 12994 "as he ejaculates", 12974 "as they
  both appear to climax", 12964 "as he ejaculates"); the other 496 are null —
  same pattern as the other p2/v2 shards.
- `exposure` (317, 26 distinct — surface forms reflect the verbatim-only rule):
  `nude` 163, `lingerie` 35, `bodysuit` 34, `underwear` 21, `bikini` 12,
  `stockings` 7, `bikini_top` 7, `thong` 6, 4 each of
  `thigh_highs`/`shirtless`/`bikini_bottom`, `no_clothing` 3, 2 each of
  `bare_chest`/`bra`/`unclothed`, and 1 each of `bare_buttocks`/
  `partial_nudity`/`bare_bodies`/`minimal_clothing`/`briefs`/`bare_skin`/
  `topless`/`no_clothes`/`bare`/`panties`/`minimal_swimwear` (the `no_*`,
  `bare_*` and `unclothed` surface forms are the caption's own wording for
  nudity states, kept verbatim per the rule). Notable new distinct values in
  this window: `bodysuit` (the blue-bodysuit block, rowids 12913–12950),
  `bikini_top`/`bikini_bottom`/`minimal_swimwear` (the rooftop/pool block),
  `briefs` (rowid 13089).
- `mood` (84, 19 distinct): `pleasure` 28, `smiling` 14, `smiles` 12,
  `aroused` 5, `moans` 5, `focused` 4, `intense` 3, `arousal` 2, and 1 each
  of `passionate`/`intensely`/`seductively`/`suggestively`/`smile`/`playful`/
  `passionately`/`discomfort`/`distressed`/`moaning`/`distress` (surface-form
  variance is inherent to the verbatim-only rule; all values are
  caption-verbatim).
- `setting` (468 non-null, 6 distinct): `indoor` 197, `bedroom` 191,
  `living_room` 41, `outdoor` 34, `bathroom` 4, `studio` 1 (rowid 12899);
  null 32. The window opens with the tail of the office-free living-room
  threesome block (12778–12827), moves through a long Christmas/Santa block
  (12828–12877), a pop-art bedroom block (12878–12912), the blue-bodysuit
  bedroom block (12913–12950), pink-haired couch/home blocks
  (12951–13027), a three-women bedroom block (13028–13077), teal-bedding
  bedroom blocks (13078–13165), a dim gray/green-couch block (13166–13244),
  and closes with an outdoor pool/rooftop block (13245–13277) that contributes
  all 34 `outdoor` settings; `studio` (12899) is a new distinct setting value
  for this shard.
- `act` (498 non-null, 15 distinct): `group_sex` 134, `oral_sex` 87,
  `intercourse` 83, `intercourse_from_behind` 57, `caressing` 36, `sex` 28,
  `anal_sex` 19, `posing` 19, `kissing` 14, `masturbation` 7,
  `manual_stimulation` 6, `massage`/`undressing` 3 each, `foreplay`/`grinding`
  1 each; null 2.
- `finish` (4 non-null): `climax` 4, null 496.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `b64e7b4edc44736d51cef8c0df0b5f494fa9468cdf5e6725015178e0e9338ddd` and the
  full QC summary (validator output + `stage_c_manual` block carrying the
  relay window and continuity evidence, the read-only DB access note, the
  generation custody model including the four assertion corrections, the
  independent audit numbers, the verbatim rule including the hyphen ruling,
  the facet statistics, and the prompt-file CRLF/LF note, same shape as
  shards 8–28).
- Ready sidecar:
  `data/token_sprint/shards/shard-029.p2.v2.aa8112a4-3b18-4aee-bfc8-3936477f1759.jsonl.ready`
- Ready SHA-256: `6261db55d52c450c927260e9ffe034917cd303fea24a15de3780eced32017337`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL and text prompt only). DB access was
  strictly read-only (`mode=ro` + `query_only=ON` + `busy_timeout=30000`,
  single connection closed in a `finally` block); the production library DB
  was never written. `pyproject.toml`/`uv.lock` untouched; no existing
  artifact in the sprint root was modified — this run created only: the input
  shard, the output shard, its stock `.qc.json`, the `.ready` sidecar, and
  this report.
- Scratch tooling (worktree-local, untracked under `scratch_stagec29/`:
  `build_input_029.py`, `batch_tool_029.py`, `audit_shard_029.py`,
  `publish_ready_029.py`, per-batch caption dumps `dump_b01`–`dump_b10` and
  scene drafts `draft_b01`–`draft_b10`) is **not committed**; nothing outside
  the shard-029 product set was touched in the sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
