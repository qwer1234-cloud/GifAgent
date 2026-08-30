# W1 Stage C — P2 scene metadata, twenty-fifth p2/v2 shard (rowids 10778–11277, 500 rows)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec25` (worktree `.worktrees/sprint-w1-stagec25`, created from
  master `00a1ad8f`, the master tip that landed shard-024)
- Nature of this run: fresh generation, 10 sub-batches of 50 rows, per-batch
  verbatim assertion, independent full-coverage audit, `.ready` published.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–24): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  validator run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-024, continuity verified)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-025.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only, same as
  shards 1–24). Input SHA-256:
  `dbe33e94337dd35a2cb668fdc9ecda778627988df1c2077c187bb1839945daa8`
- Window: **rowids 10778–11277** — the first 500 enriched rows
  (`vlm_summary_json` contains `enriched_by`, `$.caption` non-empty) with
  `rowid > 10777`, ordered by rowid ascending. Built from the production library
  `dist/GifAgentUI/data/library.db` strictly read-only (`mode=ro` +
  `PRAGMA query_only=ON` + `PRAGMA busy_timeout=30000`); never written.
- Relay continuity (asserted by the input builder and re-verified by the
  independent audit):
  - shard-024's input on disk ends at rowid **10777** (re-read from disk);
    the first row of this window is **10778**, the next enriched row after the
    baseline (probed independently; no overlap, no gap); candidate_id sets of
    the two input shards are disjoint.
  - The relay is over the **enriched-row sequence**, not a fixed OFFSET: 2,927
    rows total follow the baseline in the DB and unenriched rows are expected
    later in the library by design. For this window the span `(10777, 11277]`
    happens to cover exactly 500 rowids and all of them are enriched — the
    read-only DB probe confirms the enriched rowids in the span **exactly
    equal** the 500 input rowids (no skipped enriched row, no invented rowid),
    so the window is contiguous with 0 gap intervals. The next enriched row
    after the window is **11278**.
  - Input rowids strictly increasing, 500 unique candidate_ids, no empty
    captions, no empty `enriched_by`.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-025.p2.v2.76577c08-3f18-42b9-839f-d9a27caae50d.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` joined
  programmatically from the input shard by the assembler, never typed)
- SHA-256: `62ca7379403a6941f2148ea193f0e7e68ec8de1a4021c51df733dee117460c4a`
- Custody: generated in **10 sub-batches of 50**; each sub-batch was appended
  atomically (tmp + `os.replace`) and immediately re-asserted cumulatively
  (rowid sequence == input slice, id order, strict schema, source-verbatim rule
  over every optional facet). After batch 10 the file holds exactly 500 rows
  and every cumulative assertion re-passed (508 optional facets). All 10
  batches passed the pre-append and cumulative re-assertion on first
  submission — no corrections were needed this run.

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
  (empty fidelity denominator), same as shards 1–24.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit (`scratch_stagec25/audit_shard_025.py`, fresh
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
  (508 values: 439 exposure + 69 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the row caption as a contiguous,
  case-insensitive substring): **0 failures**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)
- Semantic review: every one of the 500 captions was read in full during
  annotation (10×50 caption dumps, batch by batch) — full-coverage semantic
  grounding rather than a sampled spot-check.

## 5. Facet statistics (recomputed from the artifact by the audit)

- Optional facets emitted: **508 total** — `exposure` 439 (21 distinct),
  `mood` 69 (21 distinct). Facet presence: 63 rows carry both optional
  facets, 382 exactly one, 55 none.
- Nulls (required facets, null-when-unknown rule): `setting` 7, `act` 0,
  `finish` 498. The 7 `setting` nulls are tight close-ups whose captions state
  no locatable environment; every caption described a describable act (0 `act`
  nulls, unlike shard-024's 2). The 2 `finish` non-null rows are the ones
  whose captions explicitly state an orgasmic/climax resolution (consistent
  with the null-when-unknown rule; shard-024 had 7).
- `exposure` (439, 21 distinct — surface forms reflect the verbatim-only
  rule): `lingerie` 149, `bodysuit` 51, `nude` 49, `top` 43, `underwear` 27,
  `bikini` 26, `stockings` 17, `bra` 16, `shirtless` 13, `fishnet_lingerie` 12,
  `thigh_highs` 9, `bikini_top` 6, `skirt` 5, `thong` 3, and 1–2 each of
  `corset`/`dress`/`panties`/`minimal_clothing`.
- `mood` (69, 21 distinct): `pleasure` 24, `focused` 10, `intense` 5,
  `moans` 4, `smiling` 3, `focus` 3, `passionate` 2, `concentration` 2,
  `sensual` 2, `intensely` 2, `vocalizing` 2, and 1 each of `smile`/
  `passionately`/`tender`/`playful`/`enjoying`/`grimaces`/`arousal`/
  `startled`/`laughing` (surface-form variance is inherent to the
  verbatim-only rule).
- `setting` (493 non-null, 8 distinct): `bedroom` 240, `indoor` 162,
  `poolside` 35, `living_room` 21, `bathroom` 14, `hotel_room` 13,
  `kitchen` 7, `hot_tub` 1; null 7 (`poolside`, `kitchen` and `hot_tub` are
  new distinct values vs shard-024 — this window opens with a 35-row outdoor
  pool sequence before moving indoors).
- `act` (500 non-null, 20 distinct): `oral_sex` 138, `intercourse` 88,
  `intercourse_from_behind` 78, `anal_sex` 32, `kissing` 31, `sex_toy_play` 28,
  `posing` 27, `sex` 17, `masturbation` 14, `caressing` 14,
  `manual_stimulation` 13, `massage` 8, `fingering` 3, `showering` 2, and 1
  each of `undressing`/`grinding`/`dressing`/`walking`/`resting`; null 0.
- `finish` (2 non-null, 2 distinct): `orgasm` 1, `climax` 1; null 498.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `62ca7379403a6941f2148ea193f0e7e68ec8de1a4021c51df733dee117460c4a` and the
  full QC summary (validator output + `stage_c_manual` block carrying the
  independent audit numbers, the relay window with the continuity explanation,
  the verbatim rule including the hyphen ruling, the facet statistics, the
  per-batch generation provenance, and the prompt-file CRLF/LF note, same
  shape as shards 8–24).
- Ready sidecar:
  `data/token_sprint/shards/shard-025.p2.v2.76577c08-3f18-42b9-839f-d9a27caae50d.jsonl.ready`
- Ready SHA-256: `8eccb5833974b52b3accff91d88d4c539a7224b279e61d1df3ece2ca3989f86d`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL and text prompt only); the production
  database was opened strictly read-only (`mode=ro` + `query_only=ON` +
  `busy_timeout=30000`) for the window build/probe and never written;
  `pyproject.toml`/`uv.lock` untouched; no existing artifact in the sprint
  root was modified — this run only added: the input shard, the output shard,
  its stock `.qc.json`, the `.ready` sidecar, and this report.
- Scratch tooling (worktree-local, untracked under `scratch_stagec25/`:
  `build_input_025.py`, `dump_batch_025.py`, `make_draft_025.py`,
  `append_batch_025.py`, `audit_shard_025.py`, `run_validator_025.py`,
  `publish_ready_025.py`, `prompt_crlf_backup_025.bin` (deleted after restore),
  per-batch caption dumps, scene drafts and `batch_01`–`batch_10` drafts) is
  **not committed**; nothing outside the shard-025 product set was touched in
  the sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
