# W1 Stage C — P2 scene metadata, thirtieth and final p2/v2 shard (rowids 13278–13704, 427 rows)

- Date: 2026-09-01
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec30` (worktree `.worktrees/sprint-w1-stagec30`, created from master)

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–29): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`, 74
  CRLFs). The worktree copy was normalized to the canonical LF bytes for the
  validator run only (content unchanged), so the validator reported the frozen
  `01a161b0…` hash; the stock CRLF checkout was restored byte-exactly
  immediately after validation (re-read back: `d4971361…`, 74 CRLFs) and the
  worktree `git status` shows no tracked-file modification. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-029)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-030.input.jsonl` — 427 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only, same as
  shards 1–29). Built by this run from the production library DB with
  read-only access (`file:…mode=ro`, `PRAGMA query_only=ON`,
  `busy_timeout=30000`); no write connection was ever opened and no media
  bytes were read. Input SHA-256:
  `3c8ff5f5d9e136a0c2b2653e420486cac9f7f355cb21183824bbfb67321c288f`
- Window: **rowids 13278–13704** — all 427 remaining enriched rows with
  `rowid > 13277` (relay window: all rows whose `vlm_summary_json` contains
  `enriched_by` with non-empty `$.caption`), ordered by rowid ascending.
  Continuity verified mechanically by the builder script:
  - shard-029's input on disk ends at rowid **13277** (re-read pre-check);
    a SQL probe confirmed the first enriched row after 13277 is **13278**,
    and it is the first picked row — no overlap, no gap.
  - Span completeness: every enriched rowid in (13277, 13704] is picked;
    picked rowids are strictly ascending.
  - 427 unique `candidate_id`s; no empty captions; no empty `enriched_by`;
    candidate_id sets of the shard-029 and shard-030 input shards are
    disjoint.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-030.p2.v2.c760b35a-6cf0-45db-8de0-015c5d2ce8bf.jsonl`
- Lines: 427 (one per input row, input order; per-line `prompt_version:
  "p2/v2"` and `prompt_sha256: 01a161b0…`; `candidate_id` joined
  programmatically from the input shard by the assembler, never typed)
- SHA-256: `0884bce551c864a6dc4a9daf12a90c62184cfda38f42f1397ca449364111d0a1`
- Custody: fresh generation in **9 sub-batches (8x50 + 1x27)**. Strict schema
  and source-verbatim rule asserted pre-append and verified cumulatively
  over the whole shard.

## 4. QC results (stock validator + independent mechanical audit, all pass)

Stock validator (`scripts/token_sprint/validate_shard.py`, run from the
worktree with the prompt normalized to LF as per §1, `--t0-gate`, exit 0),
verbatim output:

```json
{
  "coverage_rate": 1.0,
  "errors": [],
  "fidelity_rate_high_stratum": null,
  "n": 427,
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
  (empty fidelity denominator), same as shards 1–29.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit (`audit_shard_030.py`, re-reading both files
from disk, **full coverage — no sampling**; verdict PASS):

- Input/output alignment: 427/427 lines; `candidate_id` order **identical** to
  the input shard line-by-line (0 mismatches); all 427 ids unique; rowids
  strictly ascending 13278–13704.
- Strict schema over all 427 rows (exact top-level key set `{candidate_id,
  prompt_version, prompt_sha256, scene}`, `prompt_version`/`prompt_sha256`
  literals, `scene` with required `setting`/`act`/`finish` + optional
  `exposure`/`mood` only, no unknown scene keys, snake_case `^[a-z0-9_]+$`
  values, optional facets never null): **427/427, 0 failures**.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (454 values: 254 exposure + 200 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the row caption as a contiguous,
  case-insensitive substring): **0 failures**.
- Semantic review: every one of the 427 captions was verified against extracted
  metadata — full-coverage semantic grounding.

## 5. Facet statistics (recomputed from the artifact by the audit)

- Optional facets emitted: **454 total** — `exposure` 254 (27 distinct),
  `mood` 200 (17 distinct). Facet presence: 128 rows carry both optional
  facets. Optional facets are never null.
- Nulls (required facets, null-when-unknown rule): `setting` 19, `act` 13,
  `finish` 422.
- `exposure` (254, 27 distinct): `nude` 161, `lingerie` 20, `shirtless` 18,
  `thigh_high_stockings` 6, `bikini` 5, `shirt` 5, `underwear` 4, `tank_top` 4,
  `socks` 3, `minimal_clothing` 3, `bra` 3, `thigh_highs` 2, `thong` 2,
  `bikini_bottoms` 2, `no_clothes` 2, `no_clothing` 2, `bare_bodies` 2,
  `stockings` 1, `garter_belt` 1, `bikini_top` 1, `high_heels` 1,
  `bare_chest` 1, `crop_top` 1, `bare_skin` 1, `topless` 1, `shorts` 1,
  `swimsuit` 1.
- `mood` (200, 17 distinct): `intimate` 64, `focused` 50, `pleasure` 46,
  `smiling` 9, `intense` 5, `arousal` 3, `intensely` 3, `playfully` 3,
  `smiles` 3, `smile` 3, `moaning` 2, `moans` 2, `passionately` 2,
  `discomfort` 2, `relaxed` 1, `distressed` 1, `aroused` 1.
- `setting` (408 non-null, 6 distinct): `bedroom` 174, `living_room` 151,
  `outdoor` 32, `indoor` 24, `bathroom` 20, `studio` 7; null 19.
- `act` (414 non-null, 10 distinct): `intercourse_from_behind` 127,
  `intercourse` 79, `oral_sex` 69, `posing` 60, `group_sex` 34, `anal_sex` 18,
  `kissing` 13, `caressing` 8, `masturbation` 4, `sex` 2; null 13.
- `finish` (5 non-null): `climax` 5, null 422.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `0884bce551c864a6dc4a9daf12a90c62184cfda38f42f1397ca449364111d0a1` and the
  full QC summary.
- Ready sidecar:
  `data/token_sprint/shards/shard-030.p2.v2.c760b35a-6cf0-45db-8de0-015c5d2ce8bf.jsonl.ready`
- Ready SHA-256: `079f81033e75c390dadf5e14e4469f504a13c868bfdd79a8ee83ef05dc002cd8`

## 7. Review and Receipt

- Independent review executed by `w1-stagec-reviewer`.
- `.done` written:
  `data/token_sprint/shards/shard-030.p2.v2.c760b35a-6cf0-45db-8de0-015c5d2ce8bf.jsonl.done`
- Done SHA-256: `a75305f3d4238fbcfd25747bdb3829dd2026e2411115d94fe18475023e055d1b`
- Receipt published: `receipt_id` `2fc3edce-1a21-415e-afb6-324a1ec7d6fc`.
- Hash chain verified and reconciled into canonical `sprint_manifest.json` (rev61).

## 8. Stage C Completion

With shard-030 accepted, all 30 shards of Stage C p2/v2 are complete.
Total row coverage: **12,827 / 12,827 (100.0%)** of all production-enriched candidate rows.
Stage C is fully concluded.
