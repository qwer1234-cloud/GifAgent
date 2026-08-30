# W1 Stage C — P2 scene metadata, seventh p2/v2 shard (rows 1480–1979)

- Date: 2026-08-29
- Executor: w1-stagec-executor (Stage C W1 data-track ramp, shard 7, first 500-row shard)
- Branch: `sprint/w1-stagec7` (worktree `.worktrees/sprint-w1-stagec7`, off `master` @
  `65839a0d`, the current tip — the shard-5 merge commit)
- Standard: same schema and gates as p2/v2 shards 1–5
  (`w1-stage-c-p2v2.md`, `w1-stage-c-p2v2-shard2.md` … `w1-stage-c-p2v2-shard5.md`);
  manifest rev15 ruling — `prompt_version p2/v2`, optional facets (`exposure`,
  `mood`) restricted to source-verbatim mapping of the input caption.
- Rowid plan: shards 1–6 cover enriched rowids 80–1479 (1400 rows); this shard
  continues at OFFSET 1400 with rowids 1480–1979 (500 rows).

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`, byte-identical content to the
  shards 1–5 runs; NOT modified. Canonical (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–5): with `core.autocrlf=true` the fresh
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The worktree
  copy was normalized to the canonical LF bytes for the QC run only (content
  unchanged), so the validator reported the frozen `01a161b0…` hash; the stock CRLF
  checkout was restored immediately after the `.ready` publish and `git status` is
  clean for the prompts directory.

## 2. Input set (production DB, read-only)

- Source: `dist/GifAgentUI/data/library.db`, opened with SQLite URI `mode=ro` plus
  `PRAGMA query_only=ON` and `PRAGMA busy_timeout=30000`; no writes; no media bytes
  read (text fields only).
- At extraction time the production `candidate_gifs` table held 13,625 rows of which
  12,827 were enriched (`json_extract(vlm_summary_json,'$.enriched_by') IS NOT NULL`;
  single producer `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`) — unchanged
  totals vs shard 5.
- Window check before slicing: enriched rowids are contiguous over 80–1979
  (1900/1900 present), `OFFSET 1400` lands exactly on **rowid 1480** and the window's
  last row is **rowid 1979**, as prescribed; a rowid-filter query
  (`rowid BETWEEN 1480 AND 1979` over enriched rows) returns the identical 500-rowid
  sequence, so `OFFSET 1400 LIMIT 500` and a rowid filter agree exactly. No rows in
  the window have a null/empty caption.
- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-007.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same as
  shards 1–5).
- Input SHA-256: `b69464ff71cbb353d9e37ea7f80fcb883f79b4563e7be2cbb9f0910e975b188c`

## 3. Output shard

- File: `data/token_sprint/shards/shard-007.p2.v2.6488bb57-2fcb-4f93-ab99-47a4bba1bca3.jsonl`
- Lines: 500 (one per input row, input order; `candidate_id` copied programmatically
  from the input shard with a positional rowid/candidate_id assertion on every line
  and an append-alignment assertion per sub-batch, eliminating the id-swap and
  double-append defect classes; per-line `prompt_version: "p2/v2"` and
  `prompt_sha256: 01a161b0…`)
- SHA-256: `3780c8ab5cda70541fe5d81f2b3bcbe55cdb6f14e96cd79937cd1e61b4602c8c`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).
- Written incrementally in **10 sub-batches of 50 rows**: each sub-batch was flushed
  atomically (whole-file rewrite via tmp + `os.replace`) and the mechanical check
  (schema, snake_case, source-verbatim over every optional facet of the written
  prefix) re-ran inside the same process before the flush was accepted, so no
  assertion failure could ever reach the artifact.

### Mid-run fixes (all caught by the mechanical gates themselves)

1. Sub-batch 3 flush attempt: rowid 1609 caption says "she smiles" — mood
   `smiling` is not a contiguous substring; fixed to key omission before any write.
2. Sub-batch 6 flush attempt: rowid 1741 caption says "black striped stockings" —
   exposure `fishnet_stockings` is not verbatim; corrected to `stockings` before any
   write.
3. Sub-batch 7: two alignment aborts (scene count 49 != 50, plus a one-position
   shift around rowid 1816) caught by the count/alignment assertions; no write
   occurred until the alignment was exact.
4. Post-write 20-row semantic fidelity sample (seed 20260829) caught rowid 1788:
   caption says "engaging in intercourse" but `act` was `foreplay`; fixed to
   `intercourse` via an atomic single-line rewrite, after which the full mechanical
   check and the validator were re-run (final SHA-256 above).

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
  denominator), same as shards 1–5.

Stage-C manual mechanical checks over **all 500 rows** (not a sample):

- Strict schema (exact top-level key set, `scene` with `setting`/`act`/`finish`,
  snake_case values, optional facets never null): **500/500 = 100%** (gate ≥ 98%)
- Refusals: **0** (gate ≤ 15%)
- Source-verbatim failures over every optional facet of every row: **0**
- Optional facets emitted: 410 total — `exposure` 359, `mood` 51 — over 373 rows
- Facet distributions (top values): exposure `lingerie` 140, `fishnet_stockings` 70,
  `nude` 44, `underwear` 37, `stockings` 18, `fishnet_bodysuit` 11, `bra` 10;
  mood `pleasure` 17, `smiling` 11, `playful` 7, `passionate` 4, `relaxed` 3,
  `focused` 3. `finish` is `climax` on 5 rows (captions stating
  ejaculation/orgasm), null elsewhere; top `act` values: `intercourse` 82,
  `foreplay` 79, `posing` 76, `oral_sex` 70, `intercourse_from_behind` 53;
  top `setting` values: `bedroom` 259, `living_room` 83, null 63, `indoor` 51,
  `car_interior` 15, `hotel_room` 14.

## 5. Ready publication

- `.ready` written via `scripts/token_sprint/coordinator/protocol.py::write_ready`
  with `producer: "w1-stagec-executor"`, binding the final artifact SHA-256
  `3780c8ab5cda70541fe5d81f2b3bcbe55cdb6f14e96cd79937cd1e61b4602c8c` and the full
  QC summary (validator output + `stage_c_manual` block, same shape as shard 5).
- Ready sidecar: `data/token_sprint/shards/shard-007.p2.v2.6488bb57-2fcb-4f93-ab99-47a4bba1bca3.jsonl.ready`
- Ready SHA-256: `267d2991ff8ff83c38fedb44d3d9a1e18f011dac4d3480e1eb2e691a4c7377e3`

## 6. Constraints honored

- No media bytes opened (text fields only); production DB opened read-only and never
  written; `pyproject.toml`/`uv.lock` untouched; no existing sprint-root artifacts
  modified (this shard only adds new `shard-007` / `shard-w1stagec-007` files).
- Verdict: **pass** — reviewer may now write `.done` against the `.ready` binding.
