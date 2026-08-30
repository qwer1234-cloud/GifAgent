# W1 Stage C — P2 scene metadata, eighth p2/v2 shard (rows 1980–2479)

- Date: 2026-08-29
- Executor: w1-stagec-executor (Stage C W1 data-track ramp, shard 8, retry run)
- Branch: `sprint/w1-stagec8` (worktree `.worktrees/sprint-w1-stagec8`, fast-forwarded
  to `master` @ `b5e273b9`, the current tip — the shard-7 merge commit)
- Standard: same schema and gates as p2/v2 shards 1–7
  (`w1-stage-c-p2v2.md` … `w1-stage-c-p2v2-shard7.md`); manifest rev15 ruling —
  `prompt_version p2/v2`, optional facets (`exposure`, `mood`) restricted to
  source-verbatim mapping of the input caption.
- Rowid plan: shards 1–7 cover enriched rowids 80–1979 (1900 rows); this shard
  continues at OFFSET 1900 with rowids 1980–2479 (500 rows).
- Retry note: the first shard-8 attempt died early from a transient model
  failure and left no shard-008 artifact in the shared sprint root (verified:
  no `shard-w1stagec-008*` / `shard-008.*` files existed). Only reusable residue
  was an untracked `_oneoff/build_input.py` scratch in the worktree; it was
  reviewed against the spec, reused for input extraction (with a one-line
  Python 3.9 `write_text(newline=)` compatibility fix), and re-run from scratch.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`, byte-identical content to the
  shards 1–7 runs; NOT modified. Canonical (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–7): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The
  worktree copy was normalized to the canonical LF bytes for the QC run only
  (content unchanged), so the validator reported the frozen `01a161b0…` hash;
  the stock CRLF checkout was restored immediately after validation and
  `git status` is clean for the prompts directory.

## 2. Input set (production DB, read-only)

- Source: `dist/GifAgentUI/data/library.db`, opened with SQLite URI `mode=ro` plus
  `PRAGMA query_only=ON` and `PRAGMA busy_timeout=30000`; no writes; no media bytes
  read (text fields only).
- At extraction time the production `candidate_gifs` table held 13,625 rows of which
  12,827 were enriched (`json_extract(vlm_summary_json,'$.enriched_by') IS NOT NULL`;
  single producer `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`) — unchanged
  totals vs shards 5–7.
- Window checks before slicing: `OFFSET 1899` still lands on rowid 1979 (shard-7's
  last rowid) and `OFFSET 1900` lands exactly on **rowid 1980**; the window's last
  row is **rowid 2479** and `OFFSET 2400` (next shard) starts at rowid 2480, so the
  boundary is exact; rowids 1980–2479 are contiguous (500/500 present); rowid 1979
  is candidate_id/caption-identical to the last line of `shard-w1stagec-007.input.jsonl`;
  no rows in the window have a null/empty caption.
- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-008.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same as
  shards 1–7).
- Input SHA-256: `97afda6d9a23ef268ddc514b30f38b25de344979ded751c68ba0292b1465c671`

## 3. Output shard

- File: `data/token_sprint/shards/shard-008.p2.v2.8ca43a9f-c2d9-4fc3-8477-875688f19713.jsonl`
- Lines: 500 (one per input row, input order; `candidate_id` copied programmatically
  from the input shard with a positional rowid/candidate_id alignment assertion on
  every line and a prefix-length assertion per sub-batch, eliminating the id-swap
  and double-append defect classes; per-line `prompt_version: "p2/v2"` and
  `prompt_sha256: 01a161b0…`)
- SHA-256: `95e5af6255a73918af410c60f4cfa71ca069ccf81cdd9f0568445de2a79d6649`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).
- Written incrementally in **10 sub-batches of 50 rows**: each sub-batch was flushed
  atomically (whole-file rewrite via tmp + `os.replace`) and the mechanical check
  (strict schema, snake_case, positional candidate_id alignment, source-verbatim
  over every optional facet of the written prefix) ran before the flush was
  accepted and re-ran over the re-read file after it, so no assertion failure
  could ever reach the artifact.

### Mid-run aborts (all caught by the mechanical gates before any write)

1. Sub-batch 7 first assembly: the fragment file held 49/50 lines (rows 2339/2340
   had collapsed into one). The count assertion aborted with no write; the missing
   line was restored and the batch re-assembled cleanly.
2. Sub-batch 8 first assembly: same 49/50 count abort (rowid 2370 missing, its
   facets had been merged into rowid 2369's line). Caught pre-write, fixed, re-run.
3. No source-verbatim or schema failure ever reached a flush: all ten sub-batches
   passed the verbatim gate on their first accepted assembly.
4. Post-write 20-row semantic fidelity sample (seed 20260829) found 0 semantic
   errors and 0 verbatim failures.

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

Stage-C manual mechanical checks over **all 500 rows** (not a sample):

- Strict schema (exact top-level key set, `scene` with `setting`/`act`/`finish`,
  snake_case values, optional facets never null): **500/500 = 100%** (gate ≥ 98%)
- Refusals: **0** (gate ≤ 15%)
- Source-verbatim failures over every optional facet of every row: **0**
- Optional facets emitted: 256 total — `exposure` 187, `mood` 69 — over 242 rows
- Facet distributions: exposure `nude` 78, `underwear` 20, `lingerie` 19,
  `shirtless` 17, `fishnet_stockings` 12, `bare` 10, `bikini` 9, `mesh_top` 6,
  `towel` 3, `fishnet_lingerie` 2, `fishnet_underwear` 2, `minimal_clothing` 2,
  `no_clothing` 2, and `skirt`/`unclothed`/`stockings`/`mesh_lingerie`/`no_clothes`
  1 each (18 distinct values);
  mood `focused` 21, `smiling` 16, `pleasure` 11, `playful` 8, `relaxed` 7,
  `passionate` 3, `aroused` 2, `pensive` 1. `finish` is `climax` on 3 rows
  (captions stating ejaculation/orgasm), null elsewhere; top `act` values:
  `oral_sex` 234, `intercourse` 57, `posing` 47, `foreplay` 42,
  `intercourse_from_behind` 37, `anal_sex` 20, `handjob` 17, `masturbation` 15,
  `massage` 12, `toy_play` 9, `undressing` 6, `reading` 1, null 1;
  top `setting` values: `indoor` 246, `bedroom` 155, `outdoor` 29, `poolside` 24,
  `kitchen` 16, `hotel_room` 15, `car_interior` 5, `bathroom` 4, `cabin` 3,
  `living_room` 2, null 1 (rowid 2201 is a neon-sign promotional graphic with no
  scene to describe; both facets null per the prompt's null-when-unknown rule).

## 5. Ready publication

- `.ready` written via `scripts/token_sprint/coordinator/protocol.py::write_ready`
  with `producer: "w1-stagec-executor"`, binding the final artifact SHA-256
  `95e5af6255a73918af410c60f4cfa71ca069ccf81cdd9f0568445de2a79d6649` and the full
  QC summary (validator output + `stage_c_manual` block, same shape as shards 5–7).
- Ready sidecar: `data/token_sprint/shards/shard-008.p2.v2.8ca43a9f-c2d9-4fc3-8477-875688f19713.jsonl.ready`
- Ready SHA-256: `8e358bedea1f3deb0b5d3459a9a87a5554157407f7ea79ee59039f522cc32f81`

## 6. Constraints honored

- No media bytes opened (text fields only); production DB opened read-only and never
  written; `pyproject.toml`/`uv.lock` untouched; no existing sprint-root artifacts
  modified (this shard only adds new `shard-008` / `shard-w1stagec-008` files).
- Verdict: **pass** — reviewer may now write `.done` against the `.ready` binding.
