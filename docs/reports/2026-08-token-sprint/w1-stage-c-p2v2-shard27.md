# W1 Stage C — P2 scene metadata, twenty-seventh p2/v2 shard (rowids 11778–12277, 500 rows)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec27` (worktree `.worktrees/sprint-w1-stagec27`, created from
  the shard-026 landing commit `569cd06e`)

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–26): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  validator run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-026)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-027.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only, same as
  shards 1–26). Built by this run from the production library DB with
  read-only access (`file:…mode=ro`, `PRAGMA query_only=ON`,
  `busy_timeout=30000`); no write connection was ever opened and no media
  bytes were read. Input SHA-256:
  `ebe03917550d3c289de853f276024ae34859bfb01cef62fa0ce165d6e5d4bc27`
- Window: **rowids 11778–12277** — the first 500 enriched rows with
  `rowid > 11777` (relay window: rows whose `vlm_summary_json` contains
  `enriched_by` with non-empty `$.caption`), ordered by rowid ascending.
  Continuity verified mechanically by the builder script:
  - shard-026's input on disk ends at rowid **11777** (re-read pre-check);
    a SQL probe confirmed the first enriched row after 11777 is **11778**,
    and it is the first picked row — no overlap, no gap.
  - Span completeness: every enriched rowid in (11777, 12277] is picked;
    picked rowids are strictly ascending **and consecutive** (this window
    contains no unenriched rows at all — 0 designed exclusions inside the
    span). The next enriched row after the window is 12278.
  - 500 unique `candidate_id`s; no empty captions; no empty `enriched_by`;
    candidate_id sets of the shard-026 and shard-027 input shards are
    disjoint.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-027.p2.v2.74443b6b-8f26-4df6-a223-a8240df82270.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` joined
  programmatically from the input shard by the assembler, never typed)
- SHA-256: `54aaff69d325f985adeb80f9e6c7cb8dc5d12af77aea83f32a911313363f1649`
- Custody: fresh generation in **10 sub-batches of 50 rows**. Caption dumps
  (`dump_b01`–`dump_b10`) were written from the input shard; the annotator
  wrote scene drafts keyed by rowid only (`draft_b01`–`draft_b10`); the
  assembler joined candidate_ids from the input by rowid and, for every
  sub-batch, asserted the strict schema and the source-verbatim rule
  pre-append, appended the 50 lines atomically (tmp + `os.replace`), then
  re-asserted cumulatively over the whole file (rowid sequence == input
  slice, id order, schema, verbatim over every optional facet).
  - Assertion corrections: **0** — all 10 sub-batches passed the pre-append
    and cumulative re-assertion on first submission; 417 optional facets
    verified verbatim in total.

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
  (empty fidelity denominator), same as shards 1–26.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit (`scratch_stagec27/audit_shard_027.py`, fresh
script written for this shard, re-reading both files from disk, **full
coverage — no sampling**; verdict PASS):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to
  the input shard line-by-line (0 mismatches); all 500 ids unique; rowids
  strictly ascending 11778–12277.
- Strict schema over all 500 rows (exact top-level key set `{candidate_id,
  prompt_version, prompt_sha256, scene}`, `prompt_version`/`prompt_sha256`
  literals, `scene` with required `setting`/`act`/`finish` + optional
  `exposure`/`mood` only, no unknown scene keys, snake_case `^[a-z0-9_]+$`
  values, optional facets never null): **500/500, 0 failures**.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (417 values: 340 exposure + 77 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the row caption as a contiguous,
  case-insensitive substring): **0 failures**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)
- Semantic review: every one of the 500 captions was read in full during
  annotation (10×50 caption dumps, batch by batch) — full-coverage semantic
  grounding rather than a sampled spot-check.

## 5. Facet statistics (recomputed from the artifact by the audit)

- Optional facets emitted: **417 total** — `exposure` 340 (24 distinct),
  `mood` 77 (23 distinct). Facet presence: 52 rows carry both optional
  facets, 313 exactly one, 135 none. Optional facets are never null
  (absent key when the caption gives no evidence).
- Nulls (required facets, null-when-unknown rule): `setting` 15, `act` 0,
  `finish` 500. The 15 `setting` nulls are rows whose captions name no
  locatable environment (tight close-ups and dark-surface shots). Every
  caption described a describable act (0 `act` nulls). No caption in this
  window states an ending/resolution, so all 500 `finish` values are null —
  same pattern as the other p2/v2 shards.
- `exposure` (340, 24 distinct — surface forms reflect the verbatim-only rule):
  `lingerie` 166, `nude` 76, `top` 28, `shirtless` 13, `bodysuit` 13, `bra` 11,
  `thong` 7, `underwear` 4, and 2 each of `topless`/`outfit`/`bikini`/
  `minimal_clothing`/`bikini_bottom`/`thigh_highs`, plus 1 each of
  `bare_buttocks`/`black_outfit`/`robe`/`armor`/`bikini_top`/`no_clothing`/
  `corset`/`bare_chest`.
- `mood` (77, 23 distinct): `pleasure` 21, `focused` 8, `smiling` 8,
  `moaning` 6, `sensual` 4, `moans`/`passionately`/`playful` 3 each,
  `arousal`/`intense`/`concentration`/`intensely`/`vocalization`/
  `vocalizing` 2 each, and 1 each of `passionate`/`laughing`/`smiles`/
  `smile`/`gasps`/`moan`/`crying`/`desire`/`neutral` (surface-form variance
  is inherent to the verbatim-only rule; all values are caption-verbatim).
- `setting` (485 non-null, 8 distinct): `bedroom` 236, `indoor` 107,
  `outdoor` 93, `studio` 32, `living_room` 11, `office` 4, and 1 each of
  `bathroom`/`hallway`; null 15. The window opens with the tail of the
  red-leather-couch/living-room block, moves through a large poolside
  outdoor sequence, a bedroom block, a themed chainmail/forest
  photoshoot sequence (studio vs outdoor assigned per caption wording:
  painted/mural "backdrop" → `studio`, real trees/tunnels → `outdoor`),
  a purple-light group block, a lingerie/vibrator posing block, and closes
  with four `office` scenes — `studio`, `office` and `hallway` are new
  distinct values vs shard-026.
- `act` (500 non-null, 19 distinct): `intercourse` 122, `oral_sex` 103,
  `intercourse_from_behind` 75, `posing` 62, `caressing` 31, `sex` 26,
  `group_sex` 23, `kissing` 20, `masturbation` 13, `anal_sex` 12,
  `manual_stimulation` 3, `foreplay`/`massage` 2 each, and 1 each of
  `dressing`/`applying_lotion`/`titjob`/`fingering`; null 0.
- `finish` (0 non-null): null 500.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `54aaff69d325f985adeb80f9e6c7cb8dc5d12af77aea83f32a911313363f1649` and the
  full QC summary (validator output + `stage_c_manual` block carrying the
  relay window and continuity evidence, the read-only DB access note, the
  generation custody model, the independent audit numbers, the verbatim rule
  including the hyphen ruling, the facet statistics, and the prompt-file
  CRLF/LF note, same shape as shards 8–26).
- Ready sidecar:
  `data/token_sprint/shards/shard-027.p2.v2.74443b6b-8f26-4df6-a223-a8240df82270.jsonl.ready`
- Ready SHA-256: `114266bd3332391c32bcd7cf749a1231da826594a4d202427a83b22e91725ba1`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL and text prompt only). DB access was
  strictly read-only (`mode=ro` + `query_only=ON` + `busy_timeout=30000`,
  single connection closed in a `finally` block); the production library DB
  was never written. `pyproject.toml`/`uv.lock` untouched; no existing
  artifact in the sprint root was modified — this run created only: the input
  shard, the output shard, its stock `.qc.json`, the `.ready` sidecar, and
  this report.
- Scratch tooling (worktree-local, untracked under `scratch_stagec27/`:
  `build_input_027.py`, `batch_tool_027.py`, `run_validator_027.py`,
  `audit_shard_027.py`, `publish_ready_027.py`, per-batch caption dumps
  `dump_b01`–`dump_b10` and scene drafts `draft_b01`–`draft_b10`) is **not
  committed**; nothing outside the shard-027 product set was touched in the
  sprint root.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
