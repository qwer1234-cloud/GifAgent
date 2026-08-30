# W1 Stage C — P2 scene metadata, fifteenth p2/v2 shard (rowids 5695–6194)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec15` (worktree `.worktrees/sprint-w1-stagec15`, created
  from `master` @ `7b09dd0f`, the shard-014 tip)
- Standard: same schema and gates as p2/v2 shards 1–14
  (`w1-stage-c-p2v2.md` … `w1-stage-c-p2v2-shard14.md`); manifest rev15 ruling —
  `prompt_version p2/v2`, optional facets (`exposure`, `mood`) restricted to
  source-verbatim mapping of the input caption.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–14): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The
  worktree copy was normalized to the canonical LF bytes for the QC run only
  (content unchanged), so the validator reported the frozen `01a161b0…` hash; the
  stock CRLF checkout was restored immediately after validation (re-read back:
  `d4971361…`) and the worktree `git status` is clean. The main-sprint-root
  prompt file was never touched.

## 2. Input set (relay window after shard-014, continuity verified)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-015.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same as
  shards 1–14).
- Input SHA-256: `51fafbc590efc6c96a3dd169e79e219ed8ede7ad9845b059bbdec490a319760b`
- Window rule (relay, baseline = shard-014): shard-014's input ends at rowid
  **5694** (verified: 500 rows, first 4975 / last 5694, SHA-256
  `8fef3f32…31a03`); this shard takes the next 500 enriched rows
  (`vlm_summary_json` has non-empty `$.enriched_by` and non-empty `$.caption`)
  in ascending rowid order → **rowids 5695–6194, strictly increasing, 0 empty
  captions**. Continuity checks:
  - first row is rowid **5695**, and the minimum enriched rowid greater than
    5694 is exactly 5695 → no overlap, no gap;
  - the window query was **replayed against shard-014's range (4975–5694) and
    reproduced the on-disk `shard-w1stagec-014.input.jsonl` field-for-field
    (500/500 exact match)** before being extended past 5694 — the row-selection
    semantics are identical to shards 1–14;
  - the rowids inside 5695–6194 absent from the shard are by-design skips
    (un-enriched rows), consistent with the shards 1–14 window rule;
  - all 500 rows enriched by the same model
    (`huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`), same as recent shards.
- DB access: production `dist/GifAgentUI/data/library.db` opened **read-only**
  (`mode=ro` URI + `PRAGMA query_only=ON` + `busy_timeout=30000`), SELECT-only
  over text columns (`rowid`, `candidate_id`, `json_extract` of caption /
  enriched_by). No media bytes opened; nothing written to any database.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-015.p2.v2.a3eb6e7a-8616-47d7-8d04-6ba325f1e704.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version: "p2/v2"`
  and `prompt_sha256: 01a161b0…`; `candidate_id` copied programmatically from
  the input shard by the append tooling, never typed by hand)
- SHA-256: `a4701a8b42f04e120e78438224f9c6876b563022c27620d2c2dbdd47290e4d2a`
- Generation pacing (context-hard-constraint protocol): 10 sub-batches of 50
  rows. Each sub-batch was appended **atomically (tmp + `os.replace`)** and then
  passed a per-sub-batch mechanical assertion (fragment rowids == input slice in
  order, strict schema, source-verbatim rule over every optional facet value)
  plus a full-file re-assertion of everything written so far, before the next
  batch started. Three assertion failures occurred, all the same class (mood
  surface form) and all **caught before any byte was written**; each fragment
  was corrected and the batch re-appended: batch 4 rowid 5884 (`mood "smiles"`
  — caption says "smiling" → corrected to `smiling`), batch 6 rowid 5970
  (`mood "smiles"` — caption says "a smiling woman" → `smiling`), batch 10
  rowid 6174 (`mood "smiles"` — caption says "smile" → `smile`). No partial or
  corrupt state existed at any point; the 10 batch summaries sum to 500/500
  (optional facets per batch: 40, 28, 33, 40, 31, 41, 45, 45, 39, 33 = 375 rows
  with at least one optional facet).

## 4. QC results (validator + independent mechanical audit, all pass)

`validate_shard.py` (exit 0 with `--t0-gate`; run from the worktree, prompt
normalized to LF as per §1), verbatim output:

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
  denominator), same as shards 1–14.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit over **all 500 rows** (separate fresh script
re-reading both files from disk, not the per-batch tooling):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to the
  input shard; all 500 ids unique; rowids 5695–6194 (min 5695, max 6194,
  strictly increasing).
- Strict schema (exact top-level key set `{candidate_id, prompt_version,
  prompt_sha256, scene}`, `prompt_version`/`prompt_sha256` literals, `scene` with
  required `setting`/`act`/`finish`, no unknown scene keys, snake_case
  `^[a-z0-9_]+$` values, optional facets never null): **500/500 = 100%**, 0 failures.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (447 values: 326 exposure + 121 mood; rule: value lowercased with `_`/`-` read
  back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the source caption as a contiguous,
  case-insensitive substring): **0**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)

## 5. Facet statistics (recomputed from the artifact)

- Optional facets emitted: **447 total** — `exposure` 326, `mood` 121 — over **375
  rows** (both facets on 72 rows).
- Nulls (required facets, null-when-unknown rule): `finish` 499 (1 evidence-backed:
  `climax` rowid 5777 — "she arches back with eyes closed and mouth open as if
  climaxing"), `setting` 22 (no room/location stated: rowids 5730, 5731, 5772,
  5773, 5775, 5783, 5798, 5801, 5805, 5827, 5844, 5850, 5854, 5873, 5908, 5932,
  5982, 6108, 6160, 6177, 6178, 6192), `act` 0.
- `exposure` (326, 12 distinct): `lingerie` 235, `nude` 33, `underwear` 24,
  `bra` 10, `thong` 7, `bodysuit` 5, `shirtless` 5, `garter_belt` 2, `breasts` 2,
  and `minimal_clothing`/`panties`/`bikini` 1 each.
- `mood` (121, 22 distinct — surface forms reflect the verbatim-only rule):
  `pleasure` 55, `passionate` 12, `smiles` 9, `smiling` 8, `focused` 8,
  `relaxed` 3, `concentration` 3, `smile` 3, `surprise` 2, `surprised` 2,
  `pensive` 2, `affection` 2, `playful` 2, `laughing` 2, and `concern`/
  `questioning`/`contemplation`/`frustration`/`distressed`/`serious`/
  `thoughtful`/`annoyance` 1 each.
- `finish`: `climax` 1 (rowid 5777).
- `setting` (478 non-null, 9 distinct): `bedroom` 344, `indoor` 94,
  `living_room` 14, `kitchen` 9, `hotel_room` 7, `outdoor` 5, `bathroom` 3,
  `hallway` 1, `balcony` 1 (null 22).
- `act` (500 non-null, 20 distinct): `oral_sex` 122, `intercourse_from_behind` 84,
  `intercourse` 54, `foreplay` 40, `kissing` 38, `posing` 35, `massage` 28,
  `conversation` 21, `anal_sex` 20, `sex` 18, `dressing` 15, `straddling` 7,
  `walking`/`dildo_play` 4 each, `group_sex`/`handjob` 3 each, and `sleeping`/
  `striptease`/`fingering`/`typing` 1 each.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `a4701a8b42f04e120e78438224f9c6876b563022c27620d2c2dbdd47290e4d2a` and the full
  QC summary (validator output + `stage_c_manual` block covering the window
  continuity replay, the verbatim rule/scope including the hyphen ruling, the
  batch pacing provenance with the three pre-write catches, and the facet
  statistics above, same shape as shards 8–14).
- Ready sidecar:
  `data/token_sprint/shards/shard-015.p2.v2.a3eb6e7a-8616-47d7-8d04-6ba325f1e704.jsonl.ready`
- Ready SHA-256: `8fb2d1458d7e6cf953457534f226b3ea68bd8f6f72e0fb3d6c7521a0e902a047`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL/DB text columns only); production DB opened
  read-only with `query_only=ON` for the input window only; `pyproject.toml`/
  `uv.lock` untouched; no existing artifact bytes modified — the prompt file's
  temporary LF normalization was reverted byte-exactly (§1), and this shard only
  adds new `shard-015` products (`shard-w1stagec-015.input.jsonl`, output shard,
  `.qc.json`, `.ready`) in the gitignored `data/token_sprint/` area plus this
  report; per-sub-batch scratch tooling was removed after QC.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready` binding.
