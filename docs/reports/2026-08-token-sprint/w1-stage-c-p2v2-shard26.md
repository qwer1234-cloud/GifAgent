# W1 Stage C — P2 scene metadata, twenty-sixth p2/v2 shard (rowids 11278–11777, 500 rows)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec26` (worktree `.worktrees/sprint-w1-stagec26`, created from
  the shard-025 landing commit `d1713e1f`)
- **Nature of this run: recovery closeout.** The prior executor was interrupted by a
  model failure at 50/500 rows. This run mechanically re-verified the existing 50 rows,
  retained them **byte-for-byte**, and generated the remaining 450 rows in 9 sub-batches
  of 50 (input lines 51–500) with per-batch verbatim assertion, then ran the standard
  full QC, independent audit, and `.ready` publication.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–25): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  validator run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-025)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-026.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only, same as
  shards 1–25). Input SHA-256:
  `9a0aa14f03e33013f0c17a806db056de52ee4255b33a8952bb5d5326b2a2f1d3`
- Window: **rowids 11278–11777** — the first 500 enriched rows with
  `rowid > 11277`, ordered by rowid ascending. The shard was built and
  relay-verified upstream (coordinator handoff); this recovery run re-verified
  continuity **from the files on disk only** (no database access at all):
  - shard-025's input on disk ends at rowid **11277** (re-read); the first row
    here is **11278** — no overlap, no gap; candidate_id sets of the two input
    shards are disjoint.
  - Input rowids strictly increasing over the window, 500 unique candidate_ids,
    no empty captions, no empty `enriched_by`.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-026.p2.v2.e3c2a7d4-91b8-4f56-a0d3-8c5f27b1d94e.jsonl`
  (same artifact the interrupted run had started; the pre-recovery 50 rows were
  kept, not regenerated)
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` joined
  programmatically from the input shard by the assembler, never typed)
- SHA-256: `b189218a9d3ff9c1578a04b325481fe6145ccc3f9e7d291111a6d96fe98c507f`
- Pre-recovery artifact SHA-256 (the 50-row prefix, recorded before any append):
  `131c5a2259d6a924427ff2c3dc4a3cac5ba27a790d2a9895f8c09f750e6d304e`
- Custody:
  - **Retained rows 1–50** (rowids 11278–11327): mechanically re-verified before
    any append — strict schema 0 failures, candidate_id order identical to input
    lines 1–50, 52 optional facets source-verbatim 0 failures — then preserved
    byte-for-byte. The independent audit re-confirmed the first 50 lines
    concatenate exactly to the pre-recovery artifact bytes.
  - **Fresh rows 51–500** (rowids 11328–11777): generated in **9 sub-batches of
    50**; each sub-batch was appended atomically (tmp + `os.replace`) and
    immediately re-asserted cumulatively (rowid sequence == input slice, id
    order, strict schema, source-verbatim rule over every optional facet).
    Caption dumps were regenerated fresh from the input shard at the start of
    this run (the interrupted run's leftover dump files were overwritten).
  - Assertion corrections: **1** — batch 07's first submission was rejected
    pre-append by the assembler (rowid 11584 draft `exposure: "stockings"` is
    not verbatim; the caption states "sheer **tights**"). The draft was
    corrected to `tights` and resubmitted; the failed submission never touched
    the output file (the assertion fires before any write). Batches 02–06 and
    08–10 passed on first submission. After batch 10 the file holds exactly 500
    rows and every cumulative assertion re-passed (447 optional facets).

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
  (empty fidelity denominator), same as shards 1–25.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit (`scratch_stagec26/audit_shard_026.py`, fresh
script written for this shard, re-reading both files from disk, **full
coverage — no sampling**):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to
  the input shard line-by-line (0 mismatches); all 500 ids unique; rowids
  strictly ascending 11278–11777.
- Retained-prefix integrity: first 50 output lines byte-identical to the
  pre-recovery artifact (`131c5a22…`): **true**.
- Strict schema over all 500 rows (exact top-level key set `{candidate_id,
  prompt_version, prompt_sha256, scene}`, `prompt_version`/`prompt_sha256`
  literals, `scene` with required `setting`/`act`/`finish` + optional
  `exposure`/`mood` only, no unknown scene keys, snake_case `^[a-z0-9_]+$`
  values, optional facets never null): **500/500, 0 failures**.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (447 values: 368 exposure + 79 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the row caption as a contiguous,
  case-insensitive substring): **0 failures**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)
- Semantic review: every one of the 500 captions was read in full during
  annotation (the retained 50 in the pre-append verification, the fresh 450 in
  9×50 caption dumps, batch by batch) — full-coverage semantic grounding rather
  than a sampled spot-check.

## 5. Facet statistics (recomputed from the artifact by the audit)

- Optional facets emitted: **447 total** — `exposure` 368 (24 distinct),
  `mood` 79 (20 distinct). Facet presence: 55 rows carry both optional
  facets, 337 exactly one, 108 none. Optional facets are never null
  (absent key when the caption gives no evidence).
- Nulls (required facets, null-when-unknown rule): `setting` 17, `act` 0,
  `finish` 499. The 17 `setting` nulls are rows whose captions state no
  locatable environment (tight close-ups, plain-background studio shots, and
  the three night-time car-hood scenes that name no location). Every caption
  described a describable act (0 `act` nulls). The single non-null `finish` is
  rowid 11501, whose caption states a climax-like resolution ("arches back as
  if climaxing").
- `exposure` (368, 24 distinct — surface forms reflect the verbatim-only rule):
  `lingerie` 129, `nude` 107, `underwear` 36, `stockings` 22, `top` 15, `bra` 8,
  `police_uniform` 8, `dress` 7, `shirtless` 6, `thigh_highs` 6, `lace_mask` 4,
  `shorts` 3, `tank_top`/`bikini_top`/`blazer`/`bikini`/`thong` 2 each, and 1
  each of `topless`/`panties`/`bare_chest`/`uniform`/`black_outfit`/
  `undressed`/`tights`.
- `mood` (79, 20 distinct): `pleasure` 22, `smiles` 8, `smiling` 7, `moans` 7,
  `focused` 6, `sensual` 5, `passionate` 5, `passionately`/`tender`/`arousal`/
  `vocal`/`intense`/`vocalizing` 2 each, and 1 each of `calmness`/`laughs`/
  `intensity`/`laughing`/`relaxed`/`laughter`/`aroused` (surface-form variance
  is inherent to the verbatim-only rule; all values are caption-verbatim).
- `setting` (483 non-null, 12 distinct): `indoor` 211, `bedroom` 120,
  `living_room` 74, `garage` 29, `office` 19, `warehouse` 12, `library` 9,
  `outdoor` 4, `bathroom` 2, and 1 each of `alleyway`/`kitchen`/`apartment`;
  null 17. This window contains a themed police/indoor-garage sequence and a
  large group-scene living-room block — `garage`, `warehouse`, `library`,
  `office`, `alleyway` and `apartment` are new distinct values vs shard-025.
- `act` (500 non-null, 23 distinct): `oral_sex` 148, `intercourse` 92,
  `intercourse_from_behind` 73, `sex` 65, `caressing` 27, `posing` 26,
  `manual_stimulation` 15, `kissing` 12, `anal_sex` 9, `masturbation` 9,
  `grinding` 4, `massage` 3, `resting`/`walking`/`undressing`/`rimming`/
  `bathing`/`foot_worship` 2 each, and 1 each of `dressing`/`sitting`/
  `titjob`/`fingering`/`double_penetration`; null 0.
- `finish` (1 non-null, 1 distinct): `climax` 1; null 499.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `b189218a9d3ff9c1578a04b325481fe6145ccc3f9e7d291111a6d96fe98c507f` and the
  full QC summary (validator output + `stage_c_manual` block carrying the
  recovery provenance (50 retained + 450 regenerated), the retained-prefix
  integrity binding, the one pre-append assertion correction, the independent
  audit numbers, the relay window, the verbatim rule including the hyphen
  ruling, the facet statistics, and the prompt-file CRLF/LF note, same shape
  as shards 8–25).
- Ready sidecar:
  `data/token_sprint/shards/shard-026.p2.v2.e3c2a7d4-91b8-4f56-a0d3-8c5f27b1d94e.jsonl.ready`
- Ready SHA-256: `dba8600e0237fce75babf9c93c04bec99183e1afc284cff4ae373d5615e75cf7`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL and text prompt only); **no database
  access** in this recovery run — the input shard was already built and
  relay-verified upstream, so continuity was re-verified from the files on disk
  only; `pyproject.toml`/`uv.lock` untouched; no existing artifact in the sprint
  root was modified — this run only completed: the output shard (50 retained +
  450 appended rows), its stock `.qc.json`, the `.ready` sidecar, and this
  report.
- Scratch tooling (worktree-local, untracked under `scratch_stagec26/`:
  `verify_existing_026.py`, `batch_tool_026.py`, `run_validator_026.py`,
  `audit_shard_026.py`, `publish_ready_026.py`, per-batch caption dumps
  `dump_b01`–`dump_b10.jsonl` and scene drafts `draft_b01`–`draft_b10.jsonl`)
  is **not committed**; nothing outside the shard-026 product set was touched
  in the sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
