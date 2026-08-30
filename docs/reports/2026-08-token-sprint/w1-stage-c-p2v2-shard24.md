# W1 Stage C — P2 scene metadata, twenty-fourth p2/v2 shard (rowids 10278–10777, 500 rows)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec24` (worktree `.worktrees/sprint-w1-stagec24`, created from
  master `a647c604`, the master tip that landed shard-023)
- Nature of this run: fresh generation, 10 sub-batches of 50 rows, per-batch
  verbatim assertion, independent full-coverage audit, `.ready` published.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–23): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  validator run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-023, continuity verified)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-024.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only, same as
  shards 1–23). Input SHA-256:
  `5022bcb8e9672800da448ef79db006ea30ff1830457ad075fd41a2a116333fbe`
- Window: **rowids 10278–10777** — the first 500 enriched rows
  (`vlm_summary_json` contains `enriched_by`, `$.caption` non-empty) with
  `rowid > 10277`, ordered by rowid ascending. Built from the production library
  `dist/GifAgentUI/data/library.db` strictly read-only (`mode=ro` +
  `PRAGMA query_only=ON` + `PRAGMA busy_timeout=30000`); never written.
- Relay continuity (asserted by the input builder and re-verified by the
  independent audit):
  - shard-023's input on disk ends at rowid **10277** (re-read from disk);
    the first row of this window is **10278**, the next enriched row after the
    baseline (probed independently; no overlap, no gap); candidate_id sets of
    the two input shards are disjoint.
  - The relay is over the **enriched-row sequence**, not a fixed OFFSET: 3,427
    rows total follow the baseline in the DB and unenriched rows are expected
    later in the library by design. For this window the span `(10277, 10777]`
    happens to cover exactly 500 rowids and all of them are enriched — the
    read-only DB probe confirms the enriched rowids in the span **exactly
    equal** the 500 input rowids (no skipped enriched row, no invented rowid),
    so the window is contiguous. The next enriched row after the window is
    **10778**.
  - Input rowids strictly increasing, 500 unique candidate_ids, no empty
    captions, no empty `enriched_by`.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-024.p2.v2.1f1c3298-d517-46eb-9784-b0d67961677f.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` joined
  programmatically from the input shard by the assembler, never typed)
- SHA-256: `42d9ce6f09c8468945af376879a09195d28be61912faaecb8091d496795ea766`
- Custody: generated in **10 sub-batches of 50**; each sub-batch was appended
  atomically (tmp + `os.replace`) and immediately re-asserted cumulatively
  (rowid sequence == input slice, id order, strict schema, source-verbatim rule
  over every optional facet). The per-batch assertion caught and forced 1 draft
  correction **before** any bytes were appended (rowid 10661 `exposure:
  lingerie` removed — the caption states no clothing at all). After batch 10 the
  file holds exactly 500 rows and every cumulative assertion re-passed (431
  optional facets).

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
  (empty fidelity denominator), same as shards 1–23.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit (`scratch_stagec24/audit_shard_024.py`, fresh
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
  (431 values: 327 exposure + 104 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the row caption as a contiguous,
  case-insensitive substring): **0 failures**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)
- Semantic review: every one of the 500 captions was read in full during
  annotation (10×50 caption dumps, batch by batch) — full-coverage semantic
  grounding rather than a sampled spot-check.

## 5. Facet statistics (recomputed from the artifact by the audit)

- Optional facets emitted: **431 total** — `exposure` 327 (25 distinct),
  `mood` 104 (27 distinct). Facet presence: 64 rows carry both optional
  facets, 303 exactly one, 133 none.
- Nulls (required facets, null-when-unknown rule): `setting` 22, `act` 2,
  `finish` 493. Almost no caption in this window states the clip's resolution;
  the 7 `finish` non-null rows are the ones whose captions explicitly state a
  climax/ejaculation outcome (consistent with the null-when-unknown rule;
  shard-023 had 0). The 22 `setting` nulls are tight close-ups whose captions
  state no locatable environment; the 2 `act` nulls are close-ups with no
  describable act.
- `exposure` (327, 25 distinct — surface forms reflect the verbatim-only
  rule): `nude` 97, `top` 54, `lingerie` 40, `underwear` 28, `bra` 26,
  `bodysuit` 19, `shirtless` 17, `bikini` 7, `shorts` 6, `thong` 5,
  `no_clothing` 4, `hoodie` 3, `stockings` 3, `topless` 3, `no_clothes` 2,
  `fishnet_stockings` 2, `pajamas` 2, `bare_chest` 2, and 1 each of
  `outfit`/`pantyhose`/`unclothed`/`naked`/`bralette`/`socks`/`tights`.
- `mood` (104, 27 distinct): `pleasure` 31, `smiling` 13, `smiles` 11,
  `focused` 7, `relaxed` 5, `smile` 5, `sensual` 4, `playful` 4,
  `passionate` 3, and 1–2 each of `intensely`/`passionately`/`playfully`/
  `arousal`/`aroused`/`amusement`/`concentration`/`enjoying`/`intense`/
  `intimacy`/`laugh`/`moans`/`passion`/`seductive`/`seductively`/`tender`/
  `vocalizations`/`vocalizing` (surface-form variance is inherent to the
  verbatim-only rule; the per-batch assertion forced 1 correction to the
  verbatim evidence before append).
- `setting` (478 non-null, 5 distinct): `bedroom` 342, `indoor` 61,
  `living_room` 48, `hotel_room` 26, `bathroom` 1; null 22 (`hotel_room` is a
  new distinct value vs shard-023 — 26 captions explicitly state a hotel-room
  environment).
- `act` (498 non-null, 17 distinct): `intercourse` 111, `oral_sex` 90,
  `intercourse_from_behind` 71, `posing` 54, `sex` 38, `kissing` 30,
  `foreplay` 20, `sex_toy_play` 20, `caressing` 15, `anal_sex` 15,
  `manual_stimulation` 10, `grinding` 9, `masturbation` 6, `massage` 4,
  `undressing` 3, `footjob` 1, `fingering` 1; null 2.
- `finish` (7 non-null, 2 distinct): `climax` 5, `ejaculation` 2; null 493.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `42d9ce6f09c8468945af376879a09195d28be61912faaecb8091d496795ea766` and the
  full QC summary (validator output + `stage_c_manual` block carrying the
  independent audit numbers, the relay window with the continuity explanation,
  the verbatim rule including the hyphen ruling, the facet statistics, the
  per-batch generation provenance with the 1 pre-append correction, and the
  prompt-file CRLF/LF note, same shape as shards 8–23).
- Ready sidecar:
  `data/token_sprint/shards/shard-024.p2.v2.1f1c3298-d517-46eb-9784-b0d67961677f.jsonl.ready`
- Ready SHA-256: `4d425468d8b904b2b642b564f2e165dd4168174cbbec7ad9083b8d476adb0148`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL and text prompt only); the production
  database was opened strictly read-only (`mode=ro` + `query_only=ON` +
  `busy_timeout=30000`) for the window build/probe and never written;
  `pyproject.toml`/`uv.lock` untouched; no existing artifact in the sprint
  root was modified — this run only added: the input shard, the output shard,
  its stock `.qc.json`, the `.ready` sidecar, and this report.
- Scratch tooling (worktree-local, untracked under `scratch_stagec24/`:
  `build_input_024.py`, `dump_batch_024.py`, `make_draft_024.py`,
  `append_batch_024.py`, `audit_shard_024.py`, `run_validator_024.py`,
  `publish_ready_024.py`, `prompt_crlf_backup_024.bin` (deleted after restore),
  per-batch caption dumps, scene drafts and `batch_01`–`batch_10` drafts) is
  **not committed**; nothing outside the shard-024 product set was touched in
  the sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
