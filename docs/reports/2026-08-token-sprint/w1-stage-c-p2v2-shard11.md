# W1 Stage C — P2 scene metadata, eleventh p2/v2 shard (rowids 3726–4354) — recovery closeout

- Date: 2026-08-30
- Executor: w1-stagec-executor — **recovery closeout session** (QC + publication only;
  see §1)
- Branch: `sprint/w1-stagec11` (worktree `.worktrees/sprint-w1-stagec11`, pre-existing,
  branched from `master` @ `6ab00e28`, the shard-9 merge tip at branch creation;
  shard-10's later merge `f0c989ff` contains no shard-011 inputs and was not needed)
- Standard: same schema and gates as p2/v2 shards 1–10
  (`w1-stage-c-p2v2.md` … `w1-stage-c-p2v2-shard10.md`); manifest rev15 ruling —
  `prompt_version p2/v2`, optional facets (`exposure`, `mood`) restricted to
  source-verbatim mapping of the input caption.

## 1. Recovery provenance (honest record)

- The 500-row annotation artifact was **generated in full by the prior shard-011
  executor session**, which died during the QC/publication phase (model failure)
  before any QC gate, `.ready` sidecar, report, or commit existed.
- This session **did not regenerate any annotation row** and did not modify the
  artifact bytes (SHA-256 stable across every re-read, see §4). The coordinator had
  pre-verified the artifact (500 lines; ID order identical to input; full facet
  verbatim 0 failures; no schema violations); this session re-verified everything
  independently rather than trusting the pre-check.
- This session performed: the full validator QC (§5), an independent mechanical
  audit over all 500 rows (§5), `.ready` publication (§6), this report, and the
  commit.

## 2. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–10): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The
  worktree copy was normalized to the canonical LF bytes for the QC run only
  (content unchanged), so the validator reported the frozen `01a161b0…` hash; the
  stock CRLF checkout was restored immediately after validation (re-read back:
  3369 bytes, `d4971361…`) and `git status` is clean.

## 3. Input set

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-011.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same as
  shards 1–10).
- Input SHA-256: `069d936f911a278d8c7e02c63ffd3b9e3c33431eaf35836dbd5d58d47c8a80ea`
- Rowids: 3726–4354, strictly increasing; the 129 rowids inside that span that are
  absent from the shard are by-design skips (un-enriched rows), consistent with the
  shards 1–10 window rule. No database access was needed this session (the input
  shard was already on disk); no DB was opened, no media bytes read (text fields
  only), nothing written to any database.

## 4. Output shard

- File:
  `data/token_sprint/shards/shard-011.p2.v2.2c4ef3f6-be34-400f-975d-b8b45f57af26.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version: "p2/v2"`
  and `prompt_sha256: 01a161b0…`)
- SHA-256: `c23638bda2286ad8ceee54b33dd0e9b94be44d71d770b9eb5271112f712522a6`
- Generation: prior shard-011 executor session (see §1); byte-identical artifact
  carried through this closeout.

## 5. QC results (validator + independent mechanical audit, all pass)

`validate_shard.py` (exit 0; run from the worktree, prompt normalized to LF as per
§2), verbatim output:

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
  denominator), same as shards 1–10.

Independent mechanical audit by this session over **all 500 rows** (not a sample;
separate script re-read both files from disk):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to the
  input shard; all 500 ids unique; rowids 3726–4354 (min 3726, max 4354,
  strictly increasing).
- Strict schema (exact top-level key set `{candidate_id, prompt_version,
  prompt_sha256, scene}`, `prompt_version`/`prompt_sha256` literals, `scene` with
  required `setting`/`act`/`finish`, no unknown scene keys, snake_case
  `^[a-z0-9_]+$` values, optional facets never null): **500/500 = 100%**, 0 failures.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (375 values: exposure + mood; rule: value must appear in the source caption
  verbatim, case-insensitive, snake_case/hyphen normalized to spaces): **0**.
  (`setting`/`act`/`finish` are controlled-vocabulary mappings per the standard, not
  verbatim-bound.)

Facet statistics (recomputed by this session from the artifact):

- Optional facets emitted: **375 total** — `exposure` 307, `mood` 68 — over **348
  rows** (both facets on 27 rows).
- Nulls (required facets, null-when-unknown rule): `finish` 498 (2 evidence-backed
  `climax`: rowid 3881, caption "appears to be climaxing"; rowid 4111, caption
  "vocalization during climax"), `setting` 2 (rowids 3832 and 3838 — setting not
  stated in caption), `act` 0.
- `exposure` (307, 13 distinct): `lingerie` 90, `nude` 83, `underwear` 68, `bra` 19,
  `bodysuit` 13, `stockings` 9, `shirtless` 7, `towel` 6, `unclothed` 4,
  `partially_nude` 3, `swimsuit` 2, `thong` 2, `no_clothes` 1.
- `mood` (68, 23 distinct — several surface forms reflect the verbatim-only rule,
  e.g. `amused`/`amusement`): `pleasure` 20, `smiling` 12, `playful` 6,
  `focused` 3, `serious` 3, `shock` 2, `frustrated` 2, `startled` 2, `pensive` 2,
  `contemplative` 2, `joyful` 2, and `unease`/`distressed`/`stress`/`laughing`/
  `contentment`/`relaxed`/`amused`/`aroused`/`frustration`/`calm`/`amusement`/
  `passionate` 1 each.
- `finish`: `climax` 2.
- `act` (500, 38 distinct): `foreplay` 92, `intercourse` 89, `oral_sex` 66,
  `intercourse_from_behind` 63, `posing` 35, `talking` 20, `sex` 17, `handjob` 15,
  `anal_sex` 14, `group_sex` 14, `straddling` 10, `conversation` 7, `reacting` 7,
  `fingering` 6, `massage` 5, `cuddling` 5, `penetration` 4, `self_touch` 3,
  `drinking` 2, `masturbation` 2, `dildo_play` 2, `grinding` 2, `stretching` 2,
  `exercising` 2, `striptease` 2, `toy_insertion` 2, and `murmuring`/`walking`/
  `bartending`/`anilingus`/`carrying`/`kissing`/`gaming`/`mouth_play`/`texting`/
  `unwrapping`/`eating`/`skiing` 1 each.
- `setting` (498, 14 distinct): `bedroom` 266, `indoor` 164, `living_room` 36,
  `balcony` 7, `outdoor` 5, `hotel_room` 4, `bathroom` 3, `apartment` 3, `gym` 3,
  `bar` 2, `hallway` 2, `observation_deck` 1, `studio` 1, `spa` 1 (null 2).

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the unchanged artifact SHA-256
  `c23638bda2286ad8ceee54b33dd0e9b94be44d71d770b9eb5271112f712522a6` and the full
  QC summary (validator output + `stage_c_manual` block including the recovery
  provenance, the verbatim rule/scope, and the facet statistics above, same shape
  as shards 8–10).
- Ready sidecar:
  `data/token_sprint/shards/shard-011.p2.v2.2c4ef3f6-be34-400f-975d-b8b45f57af26.jsonl.ready`
- Ready SHA-256: `e2ab2c4bf5fcd84b47c15f9985f3c5bc4178fd0b2812d20985624c93774b5e03`

## 7. Constraints honored

- No media bytes opened (text JSONL fields only); no database opened at all this
  session (input shard already on disk); `pyproject.toml`/`uv.lock` untouched
  (worktree `git status` clean); no existing artifact bytes modified — the prompt
  file's temporary LF normalization was reverted byte-exactly (§2), and this shard
  only adds new `shard-011` sidecars (`.qc.json`, `.ready`) plus this report.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready` binding.
