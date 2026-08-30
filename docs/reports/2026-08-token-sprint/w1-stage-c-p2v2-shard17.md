# W1 Stage C — P2 scene metadata, seventeenth p2/v2 shard (rowids 6695–7194)

- Date: 2026-08-30
- Executor: w1-stagec-executor
- Branch: `sprint/w1-stagec17` (worktree `.worktrees/sprint-w1-stagec17`, created
  from `62ea322a`, the shard-015 tip)
- Nature of this run: fresh generation. The input shard for this window had
  already been built by a prior session (its builder scratch remained untracked
  in the worktree and was removed after QC); no output rows existed — the prior
  session was interrupted before generation by a provider captcha challenge, so
  all 500 rows were authored by this session.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`; NOT modified. Canonical
  (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2–16): with `core.autocrlf=true` the
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The
  worktree copy was normalized to the canonical LF bytes for the QC run only
  (content unchanged), so the validator reported the frozen `01a161b0…` hash;
  the stock CRLF checkout was restored byte-exactly immediately after
  validation (re-read back: `d4971361…`) and the worktree `git status` shows no
  tracked-file modification. The main-sprint-root prompt file was never touched.

## 2. Input set (relay window after shard-016, continuity verified)

- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-017.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same
  as shards 1–16).
- Input SHA-256: `b58c0fdc6393f0d9ef81d1845e3824fb68f66958e833092e40dfc94dedb6e092`
- Window rule (relay, baseline = shard-016): shard-016's input ends at rowid
  **6694** (verified on disk: 500 rows, first 6195 / last 6694, per its
  `.ready` binding); this shard takes the next 500 enriched rows in ascending
  rowid order → **rowids 6695–7194, strictly increasing, 0 empty captions**.
  First row is rowid 6695 → no overlap, no gap at the relay seam.

## 3. Output shard

- File:
  `data/token_sprint/shards/shard-017.p2.v2.f1f9c941-d6ef-4df6-8bba-afe181ede49b.jsonl`
- Lines: 500 (one per input row, input order; per-line `prompt_version: "p2/v2"`
  and `prompt_sha256: 01a161b0…`; `candidate_id` copied programmatically from
  the input shard by the append tooling, never typed by hand)
- SHA-256: `7197966f17dcc60ee6ed2e6d0fef72056858c186a53a214a4e65bf9df43812bd`
- Custody: single session, generated in **10 sub-batches of 50**. Each
  sub-batch was appended **atomically (tmp + `os.replace`)** and then passed a
  per-sub-batch mechanical assertion (exactly 50 scenes; `candidate_id`
  alignment against the input slice; strict schema; source-verbatim rule over
  every optional facet value) plus a full-file re-assertion of everything
  written so far, before the next batch started. Sub-batch summaries (rowids,
  optional facets emitted, required-facet nulls):
  batch 1 `6695–6744` 45 / act 1; batch 2 `6745–6794` 38 / none; batch 3
  `6795–6844` 54 / none; batch 4 `6845–6894` 37 / act 1; batch 5 `6895–6944`
  25 / act 1; batch 6 `6945–6994` 32 / finish 49 null (1 climax); batch 7
  `6995–7044` 26 / none; batch 8 `7045–7094` 34 / none; batch 9 `7095–7144`
  32 / none; batch 10 `7145–7194` 34 / finish 49 null (1 climax) — 357 optional
  facets over 500 rows; setting null 0; act null 3; finish null 498.
- Pre-write assertion catch (caught before any byte of its batch was written;
  corrected then appended): the rowid 6986 scene draft carried a stray note key
  and omitted the stated finish; corrected to `finish: "climax"` (caption
  states "the camera moves closer to focus on the genital area during
  climax"). No partial or corrupt state existed at any point; the 10 batch
  summaries sum to 500/500 appended.

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

- `fidelity_rate_high_stratum: null` is expected for caption-only input shards
  (empty fidelity denominator), same as shards 1–16.

Gates vs shard targets: schema_ok_rate **1.0** (target ≥ 0.98), refuse_rate
**0.0** (target ≤ 0.15), full facet verbatim **0 failures** (target 0).

Independent mechanical audit over **all 500 rows** (separate fresh script
re-reading both files from disk, not the per-batch tooling; run once over the
finished 500):

- Input/output alignment: 500/500 lines; `candidate_id` order **identical** to
  the input shard; all ids unique; rowids 6695–7194 (min 6695, max 7194,
  strictly increasing); 0 empty captions.
- Strict schema (exact top-level key set `{candidate_id, prompt_version,
  prompt_sha256, scene}`, `prompt_version`/`prompt_sha256` literals, `scene`
  with required `setting`/`act`/`finish`, no unknown scene keys, snake_case
  `^[a-z0-9_]+$` values, optional facets never null): **500/500 = 100%**,
  0 failures.
- Refusals: **0** (gate ≤ 15%); coverage_rate 1.0.
- Source-verbatim failures over **every optional facet value of every row**
  (357 values: 257 exposure + 100 mood; rule: value lowercased with `_`/`-`
  read back as spaces — hyphen ruled separator-equivalent per the 2026-08-30
  coordinator decision — must appear in the source caption as a contiguous,
  case-insensitive substring): **0**. (`setting`/`act`/`finish` are
  controlled-vocabulary mappings per the standard, not verbatim-bound.)

## 5. Facet statistics (recomputed from the artifact)

- Optional facets emitted: **357 total** — `exposure` 257, `mood` 100 — over
  **314 rows** (both facets on 43 rows).
- Nulls (required facets, null-when-unknown rule): `setting` 0, `act` 3
  (rowids 6707 and 6880: landscape/pool scenes with no people, 6908: close-up
  reaction shot with no describable action stated), `finish` 498.
- `finish` non-null only where the caption states an ending/resolution:
  rowid 6986 `climax` ("…focus on the genital area during climax") and rowid
  7146 `climax` ("…a detailed shot of ejaculation").
- `exposure` (257, 17 distinct — surface forms reflect the verbatim-only rule):
  `lingerie` 108, `nude` 63, `shirtless` 19, `thigh_highs` 18, `underwear` 18,
  `socks` 11, `stockings` 7, `nude_from_the_waist_down` 3,
  `minimal_clothing` 2, and `bra`/`no_visible_clothing`/`bare_chested`/
  `legwear`/`leg_warmers`/`leg_wraps`/`unclothed`/`no_clothing` 1 each.
- `mood` (100, 24 distinct): `pleasure` 30, `smiles` 11, `focused` 9,
  `smiling` 7, `passionately` 6, `focus` 5, `surprised`/`intense` 4 each,
  `arousal`/`smile` 3 each, `aroused`/`moaning`/`moans`/`strained` 2 each, and
  `distressed`/`contemplation`/`downcast`/`contemplative`/`tense`/
  `affectionately`/`somber`/`laughing`/`delight`/`vocalizing` 1 each.
- `setting` (500 non-null, 10 distinct): `bedroom` 281, `indoor` 160,
  `living_room` 37, `outdoor` 7, `kitchen` 4, `bathroom` 4, `office` 2,
  `staircase` 2, `dining_room` 2, `restaurant` 1 (rowid 7005) — no row lacked
  a locatable setting.
- `act` (497 non-null, 23 distinct): `oral_sex` 157, `intercourse_from_behind`
  84, `intercourse` 61, `foreplay` 50, `group_sex` 33, `anal_sex` 33,
  `kissing` 18, `posing` 15, `conversation` 9, `massage` 7, `dressing` 5,
  `straddling`/`sex` 4 each, `walking`/`talking_on_phone` 3 each,
  `reading`/`handjob`/`masturbation` 2 each, and `waking_up`/`drinking`/
  `making_coffee`/`opening_door`/`writing` 1 each.
- `finish` (2 non-null): `climax` on rowids 6986 and 7146 (§5 above); null
  elsewhere.

## 6. Ready publication

- `.ready` written via
  `scripts/token_sprint/coordinator/protocol.py::write_ready` with
  `producer: "w1-stagec-executor"`, binding the artifact SHA-256
  `7197966f17dcc60ee6ed2e6d0fef72056858c186a53a214a4e65bf9df43812bd` and the
  full QC summary (validator output + `stage_c_manual` block covering the relay
  continuity, the 10-batch pacing provenance with the pre-write catch, the
  verbatim rule/scope including the hyphen ruling, and the facet statistics
  above, same shape as shards 8–16).
- Ready sidecar:
  `data/token_sprint/shards/shard-017.p2.v2.f1f9c941-d6ef-4df6-8bba-afe181ede49b.jsonl.ready`
- Ready SHA-256: `e4f6d78309d66a6d371bf2ebbe00b521925ef7b53491ecc28112e6ac40e3254c`
- No `.done` and no receipt were written — those are reviewer-exclusive actions.

## 7. Constraints honored

- No media bytes opened (text JSONL only; the DB was not touched this session —
  the input shard was already on disk); `pyproject.toml`/`uv.lock` untouched;
  no existing artifact bytes modified (the prompt file's temporary LF
  normalization was reverted byte-exactly, §1). This shard only adds new
  `shard-017` products (output shard, `.qc.json`, `.ready`) in the gitignored
  `data/token_sprint/` area plus this report; per-sub-batch scratch tooling
  (worktree-local, untracked, including the prior session's input-builder
  scratch) was removed after QC.
- Verdict: **pass** — reviewer may now write `.done` against the `.ready`
  binding.
