# W1 Stage C — P2 scene metadata, sixth p2/v2 shard (rows 980–1479, 500 rows)

- Date: 2026-08-29
- Executor: w1-stagec-executor (Stage C W1 data-track ramp, shard 6)
- Branch: `sprint/w1-stagec6` (worktree `.worktrees/sprint-w1-stagec6`, off `master` @
  `e9ca5bab`, the current tip and the same commit shard 4 landed on)
- Standard: same schema and gates as p2/v2 shards 1–4 (`w1-stage-c-p2v2.md`,
  `w1-stage-c-p2v2-shard2.md`, `w1-stage-c-p2v2-shard3.md`, `w1-stage-c-p2v2-shard4.md`);
  manifest rev15 ruling — `prompt_version p2/v2`, optional facets (`exposure`,
  `mood`) restricted to source-verbatim mapping of the input caption. This shard
  scales the shard-4 protocol to 500 rows (10 sub-batches of 50).

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`, byte-identical content to shards 1–4;
  NOT modified. Canonical (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note: with `core.autocrlf=true` the fresh worktree materializes the
  file with CRLF (raw bytes hash `d4971361…`). As in shards 2–4, the worktree copy was
  normalized to the canonical LF bytes for the QC run only (content unchanged), so
  `prompts.prompt_sha256("p2","v2")` and the validator reported the frozen `01a161b0…`
  hash; the stock CRLF checkout was restored afterwards (`git status` clean for the
  prompts directory).

## 2. Input set (production DB, read-only)

- Source: `dist/GifAgentUI/data/library.db`, opened with SQLite URI `mode=ro` plus
  `PRAGMA query_only=ON` and `busy_timeout=30000`; no writes; no media bytes read.
- At extraction time the production `candidate_gifs` table held 13,625 rows of which
  12,827 were enriched (`json_extract(vlm_summary_json,'$.enriched_by') IS NOT NULL`;
  single producer `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`) — identical counts
  to the shard 3/4 runs, so the enriched prefix has not drifted. Caption source is
  `vlm_summary_json.'$.caption'`.
- Window check before slicing: `OFFSET 899` lands exactly on rowid 979 and `OFFSET 900`
  on rowid 980. The boundary row 979 was re-verified caption/candidate_id-identical to
  the last line of `shard-w1stagec-005.input.jsonl` (which itself spans rowids 780–979,
  200 lines), so shards 1–5 cover the enriched prefix through rowid 979 as prescribed.
  `LIMIT 500 OFFSET 900` yielded **rowids 980–1479**, verified first=980, last=1479,
  count=500 and fully contiguous (rowids 980..1479 all present). The program stops on
  any mismatch; none occurred.
- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-006.input.jsonl` — 500 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, as in
  shards 1–4).
- Input SHA-256: `848df873204414c9e0ca1d77a0dfe1e411f2ce41bce448cf5e862774406925c5`

## 3. Output shard

- File: `data/token_sprint/shards/shard-006.p2.v2.0d5a3f21-9c47-4e0b-a1d8-2f6b8e5c7a41.jsonl`
- Lines: 500 (one per input row, input order; `candidate_id` copied programmatically
  from the input shard by the helper script rather than retyped; per-line
  `prompt_version: "p2/v2"` and `prompt_sha256: 01a161b0…`)
- SHA-256: `22662157a33b492816d020998ecb33d412cf6319342698a2f939eb062d97f041`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).
- Written in 10 sub-batches of 50 lines. After each sub-batch the accumulated file was
  rewritten through a tmp file + `os.replace` (atomic flush), so an interrupted run
  always leaves a complete 50-line multiple on disk; after every flush the source-verbatim
  mechanical assertion re-ran over the whole file-so-far against the input shard
  (per-line candidate_id binding, prompt binding, scene keys, optional-facet verbatim
  rule). Two annotation arrays initially misaligned (one dropped row each) were caught
  by the exact-count/alignment assertion before any bytes were appended and fixed in
  place; no bad line ever reached the shard.
- One-off helper scripts used for the build lived only in the worktree `_oneoff/` and
  are deleted before the commit; they are not part of the commit.

## 4. QC gates (Stage C per-shard standard)

### 4.1 validate_shard.py (run from the worktree, unmodified; p2/v2 supported as-is)

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

Exit code 0 with `--t0-gate`. The `.qc.json` sidecar written by the validator sits next
to the shard in the sprint root (data product, not committed).

### 4.2 Gate results

| Gate | Standard | Result | Verdict |
| --- | --- | --- | --- |
| Schema compliance | >= 98% | 100% by validator p2 check; 100% under a stricter manual check (per-line `prompt_version`/`prompt_sha256` exact, `candidate_id` sequence identical to input 500/500 and unique, optional facets str-or-absent never null, all values snake_case or null) | PASS |
| Refusal rate | <= 15% | 0% (0 refusals; every row in this window has a describable scene — including the no-people kitchen row 1440 and the day/night field walk 1335, annotated with `act: null` rather than refused) | PASS |
| Fidelity | full-facet mechanical verbatim check over ALL optional facets (0 failures required) | 357 facets checked mechanically (re-checked independently after the final flush), 0 failures | PASS |

Optional facet emission over all 500 rows: 357 facets (280 `exposure`, 77 `mood`);
306/500 rows carry at least one optional facet. `finish` was emitted on 4 rows
(`climax`, where the caption states semen/ejaculation/climax: rowids 1270, 1333, 1334,
1430) and null elsewhere; `setting` null on 68 rows (bare wall/floor/backdrop, curtain
backdrop, close-up-only scenes), `act` null on 36 rows (captions with only positioning,
preparing-for-penetration, leaning/shifting, holding an object without use, or unnamed
physical contact — not guessed, per the shard 3/4 precedent).

Value distributions: settings `indoor` 161, `bedroom` 140, `kitchen` 56, `outdoor` 26,
`bathroom` 16, `living_room` 13, `car` 10, `apartment` 4, `studio` 3, `staircase` 2,
`office` 1; top acts `oral_sex` 92, `penetration` 62, `oral_stimulation` 35,
`intercourse` 34, `touching_herself` 23, `posing` 22, `handjob` 21, `fondling` 20,
`adjusting_clothing` 19, `anal_intercourse` 14, `massage` 12, `anal_penetration` 11,
`toying` 11, `masturbation` 10, `undressing` 9, `exposing_genitalia` 9, `sex` 8,
`exposing_buttocks` 7, `washing` 7, `riding` 5, `embracing` 5, plus singles/singles-pairs
(`caressing` 4, `speaking` 4, `kissing` 3, `anal_fingering` 3, `stretching` 2,
`using_laptop` 2, and one each of `grinding`, `crawling`, `eating`,
`moving_dog_between_legs`, `cooking`, `dancing`, `using_nipple_clamps`, `using_phone`,
`walking`, `footjob`); exposure `nude` 75, `lingerie` 57, `underwear` 43, `tank_top` 22,
`bra` 21, `shirtless` 17, `stockings` 11, `bare` 11, `crop_top` 6, `partially_clothed` 4,
`bikini` 4, `thong` 3, `bikini_top` 2, and one each of `no_shirt`, `tube_top`,
`garter_belt`, `no_clothes`; mood `intimate` 29, `pleasure` 10, `focused` 9, `smile` 8,
`smiling` 4, `intimacy` 3, `discomfort`/`animated`/`neutral`/`passionate`/`suggestive` 2
each, `relaxed`/`intense`/`surprise`/`energetic` 1 each.

## 5. Notes / deviations

- `fidelity_rate_high_stratum` is `null` (denominator 0): the caption-only Stage C input
  carries no `stratum`/`vlm_summary_json`/`tags_json`/`scenario_keys_json` fields, so
  `make_shards.row_content_text` is empty, same as shards 1–4; the Stage C fidelity gate
  is the full mechanical verbatim check (357/357 optional facets, asserted per sub-batch
  inline and re-run independently over the finished file with 0 failures).
- No `suspected_sanitized` rows; explicit captions were annotated explicitly (hard
  rule 3), including `anal_intercourse`, `anal_penetration`, `anal_fingering`,
  `strap`-device penetration (1438 annotated `penetration` per the caption's "during
  penetration"), `handjob`/`footjob` rows, the object-insertion rows (wooden object
  1331, bottle 1324, dildo-via-bottle 1323, butt plug 1315/1316/1319/1322), nipple-clamp
  rows (1261, 1264) and the dog row 1203 (act `moving_dog_between_legs`, straight from
  "holding and moving a small fluffy dog between their legs" — described as the source
  states it, not euphemized).
- Multi-word verbatim values were used where the caption's exact phrase is multi-word
  (`tank_top`, `tube_top`, `crop_top`, `bikini_top`, `partially_clothed`, `garter_belt`);
  single-word states otherwise. Hyphenated garments (`thigh-highs`, `t-shirt`) cannot be
  expressed as snake_case (the `_`-to-space read-back would not match the hyphen), so on
  those rows `exposure` either used the contiguous non-hyphenated state word the caption
  also contains (`stockings` from "thigh-high stockings") or was omitted rather than
  paraphrased (rows 982, 1248, 1251, 1253, 1255).
- Caption-literal dress states `no_shirt` (1159, "no shirt") and `no_clothes` (1264,
  "wearing no clothes") map the caption's own words; `topless` was never emitted because
  no caption in this window contains that word.
- Mood/adjective values derived by the locked contiguous-substring rule from inflected
  forms in the caption are explicitly listed for the reviewer: `smile` ⊂ "she smiles" /
  "smiles upward" / "with a smile" (994, 996, 1054, 1115, 1156, 1264, 1397, 1403),
  `animated` ⊂ "speaks animatedly" / "speaks animatedly about" (1032) and "appears
  animated and engaged" (1028), `passionate` ⊂ "kissing passionately" / "passionately"
  (1117, 1079), `intimate` ⊂ "intimate atmosphere"/"intimate physical contact"/"intimate
  embrace"/"embracing intimately"/"intimate act"/"intimate setting" (many),
  `intimacy` ⊂ "emphasizing intimacy" (1027) / "suggesting intimacy" (1259, 1279).
  `focused`, `smiling`, `pleasure`, `discomfort`, `neutral`, `suggestive`, `relaxed`,
  `intense`, `surprise`, `energetic` appear as literal words in their captions.
- For mixed male/female dress states the value follows the caption's first-mentioned
  usable state (shard 3/4 precedent), e.g. `shirtless` (987, 988, 1012, 1076, 1061,
  1211, 1389, 1410) vs `underwear`/`nude` where that word comes first (1015, 1016, 1017,
  1099–1104, 1434). Row 1076 ("A shirtless man stands while two nude women kneel")
  follows the convention with `shirtless`.
- New settings this window: `office` (981), `car` (1022–1031), `bathroom` (1113–1115,
  1148–1152, 1229–1233, 1235, 1257, 1258, 1360), `living_room` (994, 1052, 1269, 1270,
  1272, 1281, 1283–1285, 1289, 1290, 1397, 1424), `outdoor` (1092–1095, 1116–1120, 1158,
  1292, 1324, 1330–1335, 1387, 1390, 1395, 1396, 1413, 1414, 1453, 1454), `staircase`
  (1405, 1406), `studio` (1091, 1294, 1297); `apartment` (1048, 1051, 1055, 1096) and
  `indoor` conventions follow shards 1–4.
  `setting=indoor` was used only where the caption states indoors/indoor room/indoor
  lighting or a couch/room context; bare wall/floor/backdrop scenes stay `null`.
- Act vocabulary continuity with shards 1–4 was kept (`intercourse`, `oral_sex`/
  `oral_stimulation` split follows the caption's wording, `fondling`, `penetration`,
  `massage`, `posing`, `adjusting_clothing`, `undressing`, `toying` for named-sex-toy
  use, `masturbation` for self-pleasure wording, `riding` for "rides him", mundane
  `washing`/`cooking`/`eating`/`dancing`/`speaking`/`walking`/`stretching`/
  `using_phone`/`using_laptop`); new caption-derived acts this window:
  `exposing_genitalia`, `exposing_buttocks`, `touching_herself` (carried over),
  `handjob`, `footjob`, `moving_dog_between_legs`, `using_nipple_clamps`, `kissing`,
  `embracing`, `caressing`, `crawling`, `grinding`.
- Publication: `.ready` sidecar written via
  `scripts.token_sprint.coordinator.protocol.write_ready` (atomic; producer
  `w1-stagec-executor`); ready-file SHA-256
  `827b7c03a09aeeb3e1c345b28a8cc365f77eab51a8901f7cfc3910d867313187`; it binds artifact
  `22662157a33b492816d020998ecb33d412cf6319342698a2f939eb062d97f041`.
- Red lines honored: production DB opened read-only only (`mode=ro` +
  `query_only=ON` + `busy_timeout`), zero writes; no GIF/JPG/video bytes opened; no
  direct model-endpoint code added; `pyproject.toml`/`uv.lock` untouched; existing
  sprint-root artifacts unmodified (only new input/output/qc/ready files added);
  shard/qc/ready files stay in the sprint root and are not committed to git (only this
  report is committed, from the worktree, explicit path).
