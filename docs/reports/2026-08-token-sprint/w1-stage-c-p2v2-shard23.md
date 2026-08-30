# W1 Stage C — P2 scene metadata, twenty-third p2/v2 shard (rowids 9778–10277, 500 rows)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec23` (worktree `.worktrees/sprint-w1-stagec23`, created from
  master `99c7716e`, the master tip that landed shard-022)
- Nature of this run: fresh generation, 10 sub-batches of 50 rows, per-batch
  verbatim assertion, independent full-coverage audit, `.ready` published.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–22): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  validator run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-022, continuity verified)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-023.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only, same as
  shards 1–22). Input SHA-256:
  `30b1af1626bd36c8accbef1562e9adcf00f8d8f9c14756dadb58edd674d0d39a`
- Window: **rowids 9778–10277** — the first 500 enriched rows
  (`vlm_summary_json` contains `enriched_by`, `$.caption` non-empty) with
  `rowid > 9777`, ordered by rowid ascending. Built from the production library
  `dist/GifAgentUI/data/library.db` strictly read-only (`mode=ro` +
  `PRAGMA query_only=ON` + `PRAGMA busy_timeout=30000`); never written.
- Relay continuity (asserted by the input builder and re-verified by the
  independent audit):
  - shard-022's input on disk ends at rowid **9777** (re-read from disk);
    the first row of this window is **9778**, the next enriched row after the
    baseline (no overlap, no gap); candidate_id sets of the two input shards
    are disjoint.
  - Unlike shards 2–22, the rowids in this window are **fully contiguous**:
    the span 9778–10277 covers exactly 500 rowids and all of them are enriched
    (the read-only DB probe confirms the enriched rowids in `(9777, 10277]`
    **exactly equal** the 500 input rowids — no skipped enriched row, no
    invented rowid). The next enriched row after the window is **10278**.
  - Input rowids strictly increasing, 500 unique candidate_ids, no empty
    captions, no empty `enriched_by`.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-023.p2.v2.92e779f2-8e89-429c-824b-172d52048cbb.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` copied
  programmatically from the input shard by the append tool, never typed)
- SHA-256: `c00897677534370068669f0e223a54e515a881ba637f9670f663789820e5f005`
- Custody: generated in **10 sub-batches of 50**; each sub-batch was appended
  atomically (tmp + `os.replace`) and immediately re-asserted cumulatively
  (draft rowid set == input slice rowid set, id order, strict schema,
  source-verbatim rule over every optional facet). The per-batch assertion
  caught and forced 3 draft corrections **before** any bytes were appended
  (rowid 10081 `smiling` → `smiles`; rowid 10090 `laughing` → `laughs`;
  rowid 10111 `smiling` → `smiles` — all non-verbatim surface forms; the
  captions state `smiles`/`laughs`). After batch 10 the file holds exactly
  500 rows and every cumulative assertion re-passed (502 optional facets).

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
  (empty fidelity denominator), same as shards 1–22.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit (`scratch_stagec23/audit_shard_023.py`, fresh
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
  (502 values: 418 exposure + 84 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the row caption as a contiguous,
  case-insensitive substring): **0 failures**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)
- Semantic review: every one of the 500 captions was read in full during
  annotation (10×50 caption dumps, batch by batch) — full-coverage semantic
  grounding rather than a sampled spot-check.

## 5. Facet statistics (recomputed from the artifact by the audit)

- Optional facets emitted: **502 total** — `exposure` 418 (20 distinct),
  `mood` 84 (17 distinct). Facet presence: 72 rows carry both optional
  facets, 358 exactly one, 70 none.
- Nulls (required facets, null-when-unknown rule): `setting` 2, `act` 0,
  `finish` 500. No caption in this window states the clip's resolution, so
  `finish` is null everywhere (consistent with the null-when-unknown rule;
  shard-022 had 2 stated-resolution rows).
- `exposure` (418, 20 distinct — surface forms reflect the verbatim-only
  rule): `lingerie` 171, `nude` 89, `top` 52, `underwear` 33, `dress` 22,
  `stockings` 12, `bra` 7, `thigh_highs`/`shorts` 5 each,
  `bodysuit`/`shirt`/`fishnet_stockings` 4 each, `topless` 3, and 1 each of
  `no_shirt`/`leggings`/`thong`/`socks`/`unclothed`/`no_clothes`/`bare_chest`.
- `mood` (84, 17 distinct): `pleasure` 44, `focused` 7, `smiling` 6,
  `moans` 5, `smiles` 4, `playful` 3, `intensely`/`passionate`/`laughs`/
  `arousal` 2 each, and 1 each of `vocalizing`/`passionately`/
  `concentration`/`smile`/`distress`/`moaning`/`vocalization`
  (surface-form variance is inherent to the verbatim-only rule; the
  per-batch assertion forced 3 corrections to the verbatim surface form
  before append).
- `setting` (498 non-null, 5 distinct): `bedroom` 223, `living_room` 176,
  `indoor` 63, `studio` 35, `bathroom` 1; null 2 (tight close-ups whose
  captions state no locatable environment).
- `act` (500 non-null, 20 distinct): `intercourse` 105, `oral_sex` 94,
  `intercourse_from_behind` 78, `posing` 60, `sex` 35, `foreplay` 27,
  `sex_toy_play` 19, `masturbation` 18, `caressing` 16,
  `manual_stimulation` 10, `massage`/`bondage_play`/`kissing` 6 each,
  `grinding` 5, `anal_sex` 4, `handjob`/`undressing`/`fingering` 3 each,
  `dressing`/`dancing` 1 each; null 0.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `c00897677534370068669f0e223a54e515a881ba637f9670f663789820e5f005` and the
  full QC summary (validator output + `stage_c_manual` block carrying the
  independent audit numbers, the relay window with the contiguous-span
  explanation, the verbatim rule including the hyphen ruling, the facet
  statistics, the per-batch generation provenance with the 3 pre-append
  corrections, and the prompt-file CRLF/LF note, same shape as shards 8–22).
- Ready sidecar:
  `data/token_sprint/shards/shard-023.p2.v2.92e779f2-8e89-429c-824b-172d52048cbb.jsonl.ready`
- Ready SHA-256: `08470d026378920ddc6366038d3726d66781a622a5aa5a41ed2757ebefdc593d`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL and text prompt only); the production
  database was opened strictly read-only (`mode=ro` + `query_only=ON` +
  `busy_timeout=30000`) for the window build/probe and never written;
  `pyproject.toml`/`uv.lock` untouched; no existing artifact in the sprint
  root was modified — this run only added: the input shard, the output shard,
  its stock `.qc.json`, the `.ready` sidecar, and this report.
- Scratch tooling (worktree-local, untracked under `scratch_stagec23/`:
  `build_input_023.py`, `dump_batch_023.py`, `append_batch_023.py`,
  `audit_shard_023.py`, `publish_ready_023.py`, `prompt_crlf_backup.bin`,
  per-batch caption dumps and `batch_01`–`batch_10` drafts) is **not
  committed**; nothing outside the shard-023 product set was touched in the
  sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
