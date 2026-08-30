# W1 Stage C — P2 scene metadata, twenty-second p2/v2 shard (rowids 9237–9777, 500 rows)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec22` (worktree `.worktrees/sprint-w1-stagec22`, created from
  master `6d526e14`, the master tip that landed shard-021)
- Nature of this run: fresh generation. The previous dispatch of shard-022 died
  early from a transient model fault with no residue (no input shard, no output
  shard, no `.ready`, no report); this run rebuilt everything from scratch.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–21): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  validator run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-021, continuity verified)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-022.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only, same as
  shards 1–21). Input SHA-256:
  `70e38d036cae8598d3d100f317fd2f5b3e30ddf5510a61d7673bd44ceed8a3fe`
- Window: **rowids 9237–9777** — the first 500 enriched rows
  (`vlm_summary_json` contains `enriched_by`, `$.caption` non-empty) with
  `rowid > 9236`, ordered by rowid ascending. Built from the production library
  `dist/GifAgentUI/data/library.db` strictly read-only (`mode=ro` +
  `PRAGMA query_only=ON` + `PRAGMA busy_timeout=30000`); never written.
- Relay continuity (asserted by the input builder and re-verified by the
  independent audit):
  - shard-021's input on disk ends at rowid **9236** (re-read from disk);
    the first row of this window is **9237**, the next enriched row after the
    baseline (no overlap, no gap); candidate_id sets of the two input shards
    are disjoint (0 shared ids).
  - As with shards 2–21, the rowids here are **not contiguous**: the span
    9237–9777 holds 541 rowids, of which 41 are non-enriched and skipped by the
    enriched-row window rule. The read-only DB probe confirms the enriched
    rowids in `(9236, 9777]` **exactly equal** the 500 input rowids (no skipped
    enriched row, no invented rowid), and the next enriched row after the
    window is **9778**.
  - Input rowids strictly increasing, 500 unique candidate_ids, no empty
    captions, no empty `enriched_by`.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-022.p2.v2.e72d9fda-c43e-4f0d-9039-fb5d865e7e77.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` copied
  programmatically from the input shard by the append tool, never typed)
- SHA-256: `98dd75bc04a83e1a64100eeb8feb785cfef86a60392a1ac4999da2d70460589d`
- Custody: generated in **10 sub-batches of 50**; each sub-batch was appended
  atomically (tmp + `os.replace`) and immediately re-asserted cumulatively
  (draft rowid set == input slice rowid set, id order, strict schema,
  source-verbatim rule over every optional facet). The per-batch assertion
  caught and forced 2 draft corrections **before** any bytes were appended
  (rowid 9528 `pajamas` → `pajama_set`; rowid 9580 `focused` → `focus` —
  both non-verbatim surface forms). After batch 10 the file holds exactly 500
  rows and every cumulative assertion re-passed.

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
  (empty fidelity denominator), same as shards 1–21.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit (`scratch_stagec22/audit_shard_022.py`, fresh
script written for this shard, re-reading both files from disk, **full
coverage — no sampling**):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to
  the input shard line-by-line (0 mismatches); all 500 ids unique.
- Strict schema over all 500 rows (exact top-level key set `{candidate_id,
  prompt_version, prompt_sha256, scene}`, `prompt_version`/`prompt_sha256`
  literals, `scene` with required `setting`/`act`/`finish` + optional
  `exposure`/`mood` only, no unknown scene keys, snake_case `^[a-z0-9_]+$`
  values, optional facets never null): **500/500, 0 failures**.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (493 values: 418 exposure + 75 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the row caption as a contiguous,
  case-insensitive substring): **0 failures**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)
- 20-row fidelity sample (fixed seed 20260830, freshly drawn): caption vs
  scene triple reviewed — **0 semantic errors**, 0 sample verbatim failures.

## 5. Facet statistics (recomputed from the artifact by the audit)

- Optional facets emitted: **493 total** — `exposure` 418 (38 distinct),
  `mood` 75 (22 distinct). 62 rows carry both optional facets, 369 exactly
  one, 69 none.
- Nulls (required facets, null-when-unknown rule): `setting` 13, `act` 0,
  `finish` 498. `finish` non-null only on 2 rows whose captions state the
  clip's resolution (`climax` 2, both explicit-ejaculation resolutions).
- `exposure` (418, 38 distinct — surface forms reflect the verbatim-only
  rule): `lingerie` 115, `nude` 91, `underwear` 38, `dress` 36, `shirtless`
  26, `thigh_highs` 20, `fishnet_stockings` 10, `bra` 9,
  `thigh_high_stockings` 8, `patterned_top` 7, `bodysuit` 7, `floral_top` 5,
  `top` 4, `bikini_top`/`bikini`/`topless`/`bare_shoulders`/`thong`/`socks` 3
  each, `bare_chest`/`no_shirt`/`leggings`/`tights`/`gold_top` 2 each, and 1
  each of `green_top`/`thigh_high_socks`/`stockings`/`lace_top`/`black_top`/
  `pajama_set`/`pajamas`/`bare_back`/`bare_buttocks`/`white_top`/
  `long_sleeved_top`/`tank_top`/`no_visible_clothing`/`knee_high_socks`.
- `mood` (75, 22 distinct): `pleasure` 16, `focused` 9, `passionately` 9,
  `smiling` 6, `smile` 5, `playful` 4, `smiles` 3, `affection`/`relaxed`/
  `moans`/`aroused`/`shock`/`intensely`/`focus`/`laughing` 2 each, and 1 each
  of `laughter`/`vocalizing`/`passionate`/`vocalization`/`moaning`/`tense`/
  `surprised` (surface-form variance is inherent to the verbatim-only rule).
- `setting` (487 non-null, 12 distinct): `bedroom` 254, `indoor` 153,
  `studio` 21, `living_room` 15, `bathroom` 14, `locker_room` 12, `office` 8,
  `hallway` 3, `hotel_room` 3, `outdoor` 2, and 1 each of
  `interrogation_room`/`clinical`; null 13 (tight close-ups whose captions
  state no locatable environment).
- `act` (500 non-null, 30 distinct): `oral_sex` 106, `posing` 78,
  `intercourse` 57, `intercourse_from_behind` 35, `kissing` 34, `foreplay`
  28, `sex_toy_play` 22, `caressing` 22, `sex` 20, `manual_stimulation` 16,
  `anal_sex` 16, `masturbation` 14, `undressing` 12, `massage` 7, `grinding`
  5, `dancing` 4, `handjob`/`fingering`/`bondage_play` 3 each, `spanking`/
  `dressing`/`foot_fetish`/`conversation` 2 each, and 1 each of
  `anilingus`/`ejaculation`/`yawning`/`washing`/`titty_fuck`/`walking`/
  `resting`; null 0.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `98dd75bc04a83e1a64100eeb8feb785cfef86a60392a1ac4999da2d70460589d` and the
  full QC summary (validator output + `stage_c_manual` block carrying the
  independent audit numbers, the relay window with the non-contiguous span
  explanation, the verbatim rule including the hyphen ruling, the facet
  statistics, the per-batch generation provenance, and the prompt-file
  CRLF/LF note, same shape as shards 8–21).
- Ready sidecar:
  `data/token_sprint/shards/shard-022.p2.v2.e72d9fda-c43e-4f0d-9039-fb5d865e7e77.jsonl.ready`
- Ready SHA-256: `5ebfb21f4ade11f72bc4886c8c4887afa7f739b44b88b3fac8f049fb5f0cbc6a`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL and text prompt only); the production
  database was opened strictly read-only (`mode=ro` + `query_only=ON` +
  `busy_timeout=30000`) for the window build/probe and never written;
  `pyproject.toml`/`uv.lock` untouched; no existing artifact in the sprint
  root was modified — this run only added: the input shard, the output shard,
  its stock `.qc.json`, the `.ready` sidecar, and this report.
- Scratch tooling (worktree-local, untracked under `scratch_stagec22/`:
  `build_input_022.py`, `dump_batch_022.py`, `append_batch_022.py`,
  `audit_shard_022.py`, `publish_ready_022.py`, per-batch caption dumps and
  `batch_01`–`batch_10` drafts) is **not committed**; nothing outside the
  shard-022 product set was touched in the sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
