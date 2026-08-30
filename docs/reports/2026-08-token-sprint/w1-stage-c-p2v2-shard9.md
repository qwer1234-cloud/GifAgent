# W1 Stage C — P2 scene metadata, ninth p2/v2 shard (rowids 2480–3098, 500 rows)

- Date: 2026-08-29
- Executor: w1-stagec-executor (Stage C W1 data track, shard 9, second 500-row shard)
- Branch: `sprint/w1-stagec9` (worktree `.worktrees/sprint-w1-stagec9`, off `master` @
  `b5e273b9`, the current tip — the shard-7 merge commit)
- Standard: same schema and gates as p2/v2 shards 1–7
  (`w1-stage-c-p2v2.md`, `w1-stage-c-p2v2-shard2.md` … `w1-stage-c-p2v2-shard7.md`);
  manifest rev15 ruling — `prompt_version p2/v2`, optional facets (`exposure`,
  `mood`) restricted to source-verbatim mapping of the input caption.

## 0. Window deviation and coordinator ruling (read first)

The prescribed window check **failed on the last rowid** and the shard was halted
before anything was written, per the "mismatch → stop and report" gate:

- The prescribed query (enriched rows, `rowid` ASC, `LIMIT 500 OFFSET 2400`) landed
  on **rowid 2480** exactly as planned (first-rowid check passed), but its **last
  rowid is 3098, not the planned 2979**.
- Cause: the enriched-rowid sequence is contiguous 80–2753 but contains **119
  un-enriched rows inside 2480–2979** — gaps at **2754–2813 (60 rows)** and
  **2815–2873 (59 rows)**; enriched resumes at 2814 and 2874. These rows carry a
  `vlm_summary_json` but **no caption and no `enriched_by`**, so they can never
  enter any enriched-row shard; the enriched-row filter skips them by design.
  The plan's last-rowid prediction simply assumed contiguity through 2979.
- Rowid-filter alternative (`rowid BETWEEN 2480 AND 2979` over enriched rows)
  yields only **381 rows**, not 500.
- Production DB state is unchanged vs the shard-7 run (13,625 total /
  12,827 enriched / single producer `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`),
  so this is plan arithmetic drift, not mid-sprint DB mutation. Under pure OFFSET
  slicing the next slice (`OFFSET 2900`) starts at rowid 3099 — no overlap.
- **Coordinator ruling (2026-08-29, interactive): proceed with the canonical
  OFFSET window** — 500 rows, rowids **2480–3098**, skipping the 119 structurally
  un-enriched gap rows. This ruling is recorded in the `.ready` `qc_summary`
  (`stage_c_manual.window_note`).

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`, byte-identical content to the
  shards 1–7 runs; NOT modified. Canonical (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–7): with `core.autocrlf=true` the fresh
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The worktree
  copy was normalized to the canonical LF bytes for the QC run only (content
  unchanged), so the validator reported the frozen `01a161b0…` hash; the stock CRLF
  checkout was restored immediately after the `.ready` publish and `git status` is
  clean for the prompts directory.

## 2. Input set (production DB, read-only)

- Source: `dist/GifAgentUI/data/library.db`, opened with SQLite URI `mode=ro` plus
  `PRAGMA query_only=ON` and `PRAGMA busy_timeout=30000`; no writes; no media bytes
  read (text fields only).
- Window invariants verified before slicing: enriched rowids contiguous **80–2479**
  (2400/2400, the shards 1–8 span) and **2480–2753** (274/274); the two un-enriched
  gaps confirmed to be exactly 2754–2813 and 2815–2873; the `OFFSET 2400 LIMIT 500`
  window and the rowid-filter window (`rowid BETWEEN 2480 AND 3098` over enriched
  rows) return the identical 500-row sequence; **no rows in the window have a
  null/empty caption**.
- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-009.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same as
  shards 1–7). Rowids 2480–3098: 2480–2753 (274) + 2814 (1) + 2874–3098 (225).
- Input SHA-256: `ca6effda1b05ff28a54cfcb03e4f20f78c93e2886afcbb1ebdc8d8fce5308386`

## 3. Output shard

- File: `data/token_sprint/shards/shard-009.p2.v2.2601b5d6-1568-4873-bc27-014764a6a1b0.jsonl`
- Lines: 500 (one per input row, input order; `candidate_id` copied programmatically
  from the input shard — facet dicts were rowid-keyed and a script attached the ids,
  with a positional rowid/candidate_id assertion on every line and an
  append-alignment assertion per sub-batch; per-line `prompt_version: "p2/v2"` and
  `prompt_sha256: 01a161b0…`)
- SHA-256: `d54f6c7fff92da06fbbfd910467418b3bec0b7af597d8c31fc2ee1efb185e7db`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).
- Written incrementally in **10 sub-batches of 50 rows**: each sub-batch passed the
  mechanical gates (strict schema, snake_case, source-verbatim over every optional
  facet of the written prefix, count/alignment) inside the same process **before**
  the atomic flush (whole-file rewrite via tmp + `os.replace`), so no assertion
  failure could ever reach the artifact.

### Mid-run gate catches (each fixed before any write)

1. Sub-batch 3: rowid 2609 caption says "an expression of intense **focus**" — mood
   `focused` is not a contiguous substring; fixed to key omission before any write.
2. Sub-batch 4: rowid 2648 caption says "with increasing **intensity**" — mood
   `intense` is not a substring of `intensity`; fixed to key omission before any write.

## 4. QC results (three gates, all pass)

`validate_shard.py` (exit 0), verbatim output:

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

- `fidelity_rate_high_stratum: null` is expected for caption-only input shards (no
  `stratum` field and no `vlm_summary_json` content text → empty fidelity
  denominator), same as shards 1–7.

Stage-C manual mechanical checks over **all 500 rows** (not a sample), re-run
standalone against the final file:

- Strict schema (exact top-level key set, `scene` with `setting`/`act`/`finish`,
  snake_case values, optional facets never null): **500/500 = 100%** (gate ≥ 98%)
- Refusals: **0** (gate ≤ 15%)
- Source-verbatim failures over every optional facet of every row: **0**
- Optional facets emitted: 406 total — `exposure` 282, `mood` 124 — over 349 rows
- Facet distributions (top values): exposure `nude` 111, `lingerie` 78,
  `underwear` 23, `bra` 20, `bikini` 14, `shirtless` 10, `swimsuit` 8;
  mood `pleasure` 35, `passionate` 11, `focused` 10, `smiling` 9, `intense` 8,
  `aroused` 5; top `act` values: `oral_sex` 106, `intercourse_from_behind` 74,
  `intercourse` 61, `foreplay` 56, `posing` 44, `kissing` 27, `conversation` 25,
  `anal_sex` 19, `group_sex` 16; top `setting` values: `bedroom` 190, `indoor` 86,
  `kitchen` 46, `living_room` 36, `classroom` 34, `outdoor` 20,
  `interrogation_room` 20, `bathroom` 17, null 19; `finish` is `climax` on 3 rows
  (captions stating climax/ejaculation), null on the other 497.
- Post-write 20-row semantic fidelity sample (seed 20260829): every sampled row
  re-reviewed against its caption; **0 semantic errors, 0 verbatim failures**.

## 5. Ready publication

- `.ready` written via `scripts/token_sprint/coordinator/protocol.py::write_ready`
  with `producer: "w1-stagec-executor"`, binding the final artifact SHA-256
  `d54f6c7fff92da06fbbfd910467418b3bec0b7af597d8c31fc2ee1efb185e7db` and the full
  QC summary (validator output + `stage_c_manual` block incl. the window ruling,
  same shape as shard 7).
- Ready sidecar: `data/token_sprint/shards/shard-009.p2.v2.2601b5d6-1568-4873-bc27-014764a6a1b0.jsonl.ready`
- Ready SHA-256: `65c4275a06e3aa07b2f7cb500f6da26b1079bfe05f2298125e2dba5118fbb0c3`

## 6. Constraints honored

- No media bytes opened (text fields only); production DB opened read-only and never
  written; `pyproject.toml`/`uv.lock` untouched; no existing sprint-root artifacts
  modified (this shard only adds new `shard-009` / `shard-w1stagec-009` files; the
  in-flight shard-008 worktree was not touched).
- Verdict: **pass** — reviewer may now write `.done` against the `.ready` binding.
