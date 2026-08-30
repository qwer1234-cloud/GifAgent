# W1 Stage C — P2 scene metadata, fourteenth p2/v2 shard (rowids 4975–5694)

- Date: 2026-08-30
- Executor: w1-stagec-executor (this dispatch is a retry: the previous shard-014
  dispatch died to a transient model-request rejection before producing any
  artifact — no rows, QC, or sidecars existed; this session built everything
  from scratch)
- Branch: `sprint/w1-stagec14` (worktree `.worktrees/sprint-w1-stagec14`, created
  from `master` @ `4048681a`, the rev44 rolling tip)
- Standard: same schema and gates as p2/v2 shards 1–12
  (`w1-stage-c-p2v2.md` … `w1-stage-c-p2v2-shard12.md`); manifest rev15 ruling —
  `prompt_version p2/v2`, optional facets (`exposure`, `mood`) restricted to
  source-verbatim mapping of the input caption.

## 1. Relay-window deviation (coordinator-approved, recorded here and in `.ready`)

The dispatch specifies a relay window after `shard-w1stagec-013.input.jsonl`.
Shard-013's executor **never started**: after the full 10-minute grace (2-minute
re-polls × 5) the input file did not exist, and the usual start traces are
absent — no `.worktrees/sprint-w1-stagec13` worktree, no shard-013 input or
output artifacts anywhere under `data/token_sprint/shards/`, and the review
chain's latest receipt is shard-012 (11:36Z). This matches the transient
model-failure pattern that killed the first shard-014 dispatch. Per explicit
coordinator decision (2026-08-30), this shard proceeded with the window
**directly after shard-012's end (rowid 4974)** and thereby occupies what would
have been shard-013's position. Consequence for the relay: the next Stage C
shard must start after **this** shard's last rowid (5694) — blindly
re-dispatching 013 after 4974 would duplicate 500 rows.

## 2. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–12): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The
  worktree copy was normalized to the canonical LF bytes for the QC run only
  (content unchanged), so the validator reported the frozen `01a161b0…` hash; the
  stock CRLF checkout was restored immediately after validation (re-read back:
  `d4971361…`) and the worktree `git status` is clean.

## 3. Input set (relay window after 4974, continuity verified against shard-012)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-014.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same as
  shards 1–12).
- Input SHA-256: `8fef3f3228e901cea0f23419137d4194cf62fd3f3b93ec8f9c960eb55bd31a03`
- Window rule (relay, baseline = shard-012 per §1): shard-012's input ends at
  rowid **4974** (verified: last line of `shard-w1stagec-012.input.jsonl`,
  SHA-256 `cadc106b…8b28`, 500 lines); this shard takes the next 500 enriched
  rows (`vlm_summary_json` has non-empty `$.enriched_by` and non-empty
  `$.caption`) in ascending rowid order → **rowids 4975–5694, strictly
  increasing, 0 empty captions**. Continuity checks:
  - first row is rowid **4975**, and the minimum enriched rowid greater than
    4974 is exactly 4975 → no overlap, no gap;
  - the window query was **replayed against shard-012's range (4355–4974) and
    reproduced the on-disk `shard-w1stagec-012.input.jsonl` field-for-field
    (500/500 exact match, byte-identical serialization)** before being extended
    past 4974 — the row-selection semantics are identical to shards 1–12;
  - the 220 rowids inside 4975–5694 absent from the shard are by-design skips
    (un-enriched rows), consistent with the shards 1–12 window rule.
- DB access: production `dist/GifAgentUI/data/library.db` opened **read-only**
  (`mode=ro` URI + `PRAGMA query_only=ON` + `busy_timeout=30000`), SELECT-only
  over text columns (`rowid`, `candidate_id`, `json_extract` of caption /
  enriched_by). No media bytes opened; nothing written to any database.

## 4. Output shard

- File:
  `data/token_sprint/shards/shard-014.p2.v2.d8dae091-7222-4a23-b9d2-dbf080b9c517.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version: "p2/v2"`
  and `prompt_sha256: 01a161b0…`; `candidate_id` copied programmatically from
  the input shard by the append tooling, never typed by hand)
- SHA-256: `2f3dcacfecf72114ab0e6365988d8993bdd6c991c9e4b36df0f3c9ee8a078fa1`
- Generation pacing (context-hard-constraint protocol): 10 sub-batches of 50
  rows. Each sub-batch was appended **atomically (tmp + `os.replace`)** and then
  passed a per-sub-batch mechanical assertion (fragment rowids == input slice in
  order, strict schema, source-verbatim rule over every optional facet value)
  before the next batch started, followed by a full-file re-assertion of
  everything written so far. One assertion failure occurred (batch 8, rowid
  5538: `mood "smiles"` — caption says "smiling"); it was **caught before any
  byte was written**, the fragment was corrected to `smiling`, and the batch
  re-appended. No partial or corrupt state existed at any point; the 10 batch
  summaries sum to 500/500 (optional facets per batch: 47, 53, 46, 38, 44, 51,
  48, 47, 45, 51 = 470).

## 5. QC results (validator + independent mechanical audit, all pass)

`validate_shard.py` (exit 0 with `--t0-gate`; run from the worktree, prompt
normalized to LF as per §2), verbatim output:

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
  denominator), same as shards 1–12.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit over **all 500 rows** (separate fresh script
re-reading both files from disk, not the per-batch tooling):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to the
  input shard; all 500 ids unique; rowids 4975–5694 (min 4975, max 5694,
  strictly increasing).
- Strict schema (exact top-level key set `{candidate_id, prompt_version,
  prompt_sha256, scene}`, `prompt_version`/`prompt_sha256` literals, `scene` with
  required `setting`/`act`/`finish`, no unknown scene keys, snake_case
  `^[a-z0-9_]+$` values, optional facets never null): **500/500 = 100%**, 0 failures.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (470 values: 357 exposure + 113 mood; rule: value lowercased with `_`/`-` read
  back as spaces must appear in the source caption as a contiguous,
  case-insensitive substring): **0**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)

## 6. Facet statistics (recomputed from the artifact)

- Optional facets emitted: **470 total** — `exposure` 357, `mood` 113 — over **405
  rows** (both facets on 65 rows).
- Nulls (required facets, null-when-unknown rule): `finish` 498 (2 evidence-backed:
  `loop` rowid 5091 — "the scene repeats in a loop"; `climax` rowid 5445 — "as they
  both appear to climax"), `act` 27 (posture/close-up/dialogue frames with no
  stated action: rowids 4999–5002, 5065, 5075, 5076, 5091, 5103, 5205–5208, 5210,
  5282, 5287, 5342, 5367, 5438, 5480, 5508, 5593–5595, 5597, 5630, 5668),
  `setting` 1 (rowid 5003 — white tiled floor low-angle close-up, no room stated).
- `exposure` (357, 15 distinct): `lingerie` 143, `underwear` 87, `nude` 52,
  `bra` 32, `bodysuit` 19, `bikini` 7, `thong` 7, `swimsuit` 3, and
  `shirtless`/`partially_undressed`/`partially_clothed`/`breasts`/`bralette`/
  `partially_nude`/`minimal_clothing` 1 each.
- `mood` (113, 32 distinct — surface forms reflect the verbatim-only rule):
  `smiles` 20, `smiling` 19, `pleasure` 11, `playful` 8, `smile` 5,
  `thoughtful` 4, `surprise` 4, `contemplative` 4, `annoyed` 4, `relaxed` 3,
  `passionate` 3, `suggestive` 3, `distressed` 3, `frustration` 2, `laughing` 2,
  `focused` 2, and `upset`/`confusion`/`calm`/`contentment`/`seductive`/
  `hesitation`/`concerned`/`submission`/`questioning`/`pensive`/`amused`/`focus`/
  `annoyance`/`contemplation`/`embarrassment`/`frustrated` 1 each.
- `finish`: `loop` 1 (rowid 5091), `climax` 1 (rowid 5445).
- `setting` (499 non-null, 11 distinct): `bedroom` 414, `indoor` 41, `kitchen` 11,
  `living_room` 9, `outdoor` 8, `bathroom` 6, `train` 5, `hallway` 2,
  `hotel_room`/`balcony`/`airplane_cabin` 1 each (null 1).
- `act` (473 non-null, 23 distinct): `foreplay` 101, `intercourse_from_behind` 81,
  `posing` 59, `conversation` 53, `intercourse` 45, `oral_sex` 45, `sex` 28,
  `massage` 13, `straddling` 11, `anal_sex` 7, `handjob` 5, `group_sex` 4,
  `sleeping`/`kissing`/`dressing` 3 each, `penetration`/`fingering`/
  `dildo_play`/`striptease` 2 each, `walking`/`grinding`/`self_touch`/`typing` 1 each.

## 7. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `2f3dcacfecf72114ab0e6365988d8993bdd6c991c9e4b36df0f3c9ee8a078fa1` and the full
  QC summary (validator output + `stage_c_manual` block including the relay
  deviation ruling, the window continuity replay, the verbatim rule/scope, the
  batch pacing provenance, and the facet statistics above, same shape as
  shards 8–12).
- Ready sidecar:
  `data/token_sprint/shards/shard-014.p2.v2.d8dae091-7222-4a23-b9d2-dbf080b9c517.jsonl.ready`
- Ready SHA-256: `1ec9b89bd87bfb7bc3f3afc4059194c18ba127f6820b48a3b6f6d61705045366`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 8. Constraints honored

- No media bytes opened (text JSONL/DB text columns only); production DB opened
  read-only with `query_only=ON` for the input window only; `pyproject.toml`/
  `uv.lock` untouched (worktree `git status` clean after the §2 prompt restore);
  no existing artifact bytes modified — the prompt file's temporary LF
  normalization was reverted byte-exactly (§2), and this shard only adds new
  `shard-014` products (`shard-w1stagec-014.input.jsonl`, output shard, `.qc.json`,
  `.ready`) in the gitignored `data/token_sprint/` area plus this report.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready` binding.
