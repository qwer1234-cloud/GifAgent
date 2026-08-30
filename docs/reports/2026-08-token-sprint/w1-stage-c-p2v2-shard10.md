# W1 Stage C — P2 scene metadata, tenth p2/v2 shard (rowids 3099–3725)

- Date: 2026-08-29 (sub-batch flushes 2026-08-29/30 UTC; artifacts dated accordingly)
- Executor: w1-stagec-executor (Stage C W1 data-track ramp, shard 10)
- Branch: `sprint/w1-stagec10` (worktree `.worktrees/sprint-w1-stagec10`, branched from
  `master` @ `19515b3f`, the current tip — the shard-8 report commit)
- Standard: same schema and gates as p2/v2 shards 1–9
  (`w1-stage-c-p2v2.md` … `w1-stage-c-p2v2-shard8.md`); manifest rev15 ruling —
  `prompt_version p2/v2`, optional facets (`exposure`, `mood`) restricted to
  source-verbatim mapping of the input caption.

## 1. Window ruling (coordinator update mid-run)

- The original task brief hardcoded the expectation "OFFSET 2900 over enriched rows
  lands on rowids 2980–3479 (shards 1–9 span 80–2979)". The live DB check
  **disproved** it: `OFFSET 2899` landed on rowid **3098** — shard-009's actual last
  row — because shard-009's accepted window is 2480–3098 (119 un-enriched rows inside
  2480–2979 are a by-design skip). Per the standing tripwire ("verify, stop and report
  on mismatch"), the run halted before writing anything and reported to the
  coordinator.
- Coordinator ruling 2026-08-29 (window rule update): the hardcoded 2980–3479 range is
  obsolete; the correct verification is enriched-sequence continuity — `OFFSET 2900`
  must land first on **3099** (the enriched successor of 3098), no overlap, no
  omission, 500 rows all enriched with non-empty captions; un-enriched gaps inside the
  rowid span are by-design skips.
- Verified under the updated rule (production DB, strict read-only):
  - `OFFSET 2899` → rowid 3098 (shard-009 last row); window → **rowids 3099–3725, n=500**.
  - Global anchors: exactly **2900** enriched rows before the window start and exactly
    **3400** enriched rows up to and including the window end — no overlap, no omission.
  - 127 un-enriched rows inside the rowid span 3099–3725 (e.g. 3264–3274…) are
    by-design skips; zero null/empty captions in the window.
  - **candidate_id overlap with shard-009's input: 0** (shard-009 input: 500 rows,
    last rowid 3098).
- DB totals unchanged vs shards 5–9: 13,625 rows, 12,827 enriched, single producer
  `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`.

## 2. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`, byte-identical content to the
  shards 1–9 runs; NOT modified. Canonical (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–8): with `core.autocrlf=true` the worktree
  materializes the file with CRLF (raw bytes hash `d4971361…`). The worktree copy was
  normalized to the canonical LF bytes for the QC run only (content unchanged), so the
  validator reported the frozen `01a161b0…` hash; the stock CRLF checkout was restored
  immediately after validation and `git status` is clean for the prompts directory.

## 3. Input set (production DB, read-only)

- Source: `dist/GifAgentUI/data/library.db`, opened with SQLite URI `mode=ro` plus
  `PRAGMA query_only=ON` and `PRAGMA busy_timeout=30000`; no writes; no media bytes
  read (text fields only).
- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-010.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same as
  shards 1–9).
- Input SHA-256: `9cbdf68906f97fedbbf4f4cfc28bcf1ad9491c408b28df538d5162bae5398f6f`

## 4. Output shard

- File: `data/token_sprint/shards/shard-010.p2.v2.2861e57b-a1b2-426e-adc2-0d0dedd41697.jsonl`
- Lines: 500 (one per input row, input order; `candidate_id` copied programmatically
  from the input shard with a positional rowid/candidate_id alignment assertion on
  every line and a prefix-length assertion per sub-batch; per-line
  `prompt_version: "p2/v2"` and `prompt_sha256: 01a161b0…`)
- SHA-256: `f1baa6af5fef3cbe23db6116b993e9292762ba33e59acaec9996b2df5d741643`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).
- Written incrementally in **10 sub-batches of 50 rows**: each sub-batch was gated
  (strict schema, snake_case, positional candidate_id alignment, source-verbatim over
  every optional facet of the batch) **before** an atomic whole-file flush
  (tmp + `os.replace`), and after every flush the **re-read artifact was re-verified
  over all lines written so far** — so the full 500-row mechanical check effectively
  ran ten times, the last one covering the complete file.

### Verbatim-gate catches (all fixed pre-flush; nothing invalid ever reached the artifact)

1. Rowid 3134: mood first drafted as `smiling`, but the caption says "smiles" —
   stemming is forbidden, corrected to the verbatim `smiles`.
2. Rowid 3177: same "smiles" vs "smiling" correction (caption: "smiles and laughs").
3. Near-misses avoided during annotation (caught by hand before assembly, e.g.
   "thigh-highs" without the word "stockings" → exposure omitted rather than forced;
   "with intense focus" → mood `intense` not `focused`; "she smiles" → `smiles` not
   `smiling`; "stages of arousal" → `arousal` not `aroused`).
4. No schema, alignment, or count assertion ever failed on a first accepted assembly
   (contrast: shard-8 had two 49/50 line-count aborts; none here).

## 5. QC results (three gates, all pass)

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
  denominator), same as shards 1–9.

Stage-C manual mechanical checks over **all 500 rows** (not a sample; independent
audit script re-read the artifact from disk, exit 0):

- Strict schema (exact top-level key set, `prompt_version`/`prompt_sha256` literals,
  `scene` with `setting`/`act`/`finish`, snake_case values, optional facets never
  null): **500/500 = 100%** (gate ≥ 98%)
- Refusals: **0** (gate ≤ 15%)
- Source-verbatim failures over every optional facet of every row: **0**
- Optional facets emitted: 431 total — `exposure` 344, `mood` 87 — over 373 rows
- Nulls (required facets, null-when-unknown rule): `finish` 499 (single evidence-backed
  `climax` at rowid 3713, caption states climaxing/orgasm), `setting` 10, `act` 15
- Facet distributions: exposure `lingerie` 123, `underwear` 98, `nude` 88, `shirtless` 8,
  `stockings` 8, `bikini` 5, `bra` 3, `topless` 2, `bare` 2, `shorts` 2, `thong` 2, and
  `tights`/`naked`/`undressed` 1 each (14 distinct values);
  mood `pleasure` 20, `smiling` 19, `smiles` 12, `playful` 8, `focused` 7,
  `passionate` 3, `aroused` 2, `relaxed` 2, `surprise` 2, `smile` 2, and
  `distressed`/`laughs`/`laughing`/`intense`/`frustrated`/`confused`/`shock`/
  `distress`/`tired`/`arousal` 1 each (20 distinct values — several reflect the
  verbatim-only rule surface forms, e.g. `smiles`/`smile` vs `smiling`);
  `finish` `climax` 1; top `act` values: `intercourse` 90, `foreplay` 82, `oral_sex` 70,
  `posing` 65, `intercourse_from_behind` 63, `fingering` 21, `handjob` 15, `grinding` 15,
  `massage` 14, `anal_sex` 13, `toy_play` 11, `masturbation` 8, `group_sex` 7,
  `undressing` 6, `kissing` 2, `bathing` 2, `titjob` 1 (17 distinct);
  top `setting` values: `bedroom` 336, `indoor` 102, `living_room` 24, `bathroom` 10,
  `kitchen` 6, `hotel_room` 5, `poolside` 3, `studio` 2, `outdoor` 2, null 10.

## 6. Ready publication

- `.ready` written via `scripts/token_sprint/coordinator/protocol.py::write_ready`
  with `producer: "w1-stagec-executor"`, binding the final artifact SHA-256
  `f1baa6af5fef3cbe23db6116b993e9292762ba33e59acaec9996b2df5d741643` and the full QC
  summary (validator output + `stage_c_manual` block including the window ruling and
  overlap check, same shape as shards 8–9).
- Ready sidecar: `data/token_sprint/shards/shard-010.p2.v2.2861e57b-a1b2-426e-adc2-0d0dedd41697.jsonl.ready`
- Ready SHA-256: `4e6c2d309f3b77aeddd16d9c7737be02839bb416e29dde2afe830edae1629030`

## 7. Constraints honored

- No media bytes opened (text fields only); production DB opened read-only and never
  written; `pyproject.toml`/`uv.lock` untouched; no existing sprint-root artifacts
  modified (this shard only adds new `shard-010` / `shard-w1stagec-010` files).
- Verdict: **pass** — reviewer may now write `.done` against the `.ready` binding.
