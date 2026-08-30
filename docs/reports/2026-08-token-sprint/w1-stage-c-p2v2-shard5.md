# W1 Stage C — P2 scene metadata, fifth p2/v2 shard (rows 780–979)

- Date: 2026-08-29
- Executor: w1-stagec-executor (Stage C W1 data-track ramp, shard 5)
- Branch: `sprint/w1-stagec5` (worktree `.worktrees/sprint-w1-stagec5`, off `master` @
  `1a092fe1`, the current tip — the shard-3 report commit)
- Standard: same schema and gates as p2/v2 shards 1–3
  (`w1-stage-c-p2v2.md`, `w1-stage-c-p2v2-shard2.md`, `w1-stage-c-p2v2-shard3.md`);
  manifest rev15 ruling — `prompt_version p2/v2`, optional facets (`exposure`,
  `mood`) restricted to source-verbatim mapping of the input caption.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`, byte-identical content to the first
  three shard runs; NOT modified. Canonical (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note (same as shards 2/3): with `core.autocrlf=true` the fresh
  worktree materializes the file with CRLF (raw bytes hash `d4971361…`). The worktree
  copy was normalized to the canonical LF bytes for the QC run only (content
  unchanged), so the validator reported the frozen `01a161b0…` hash; the stock CRLF
  checkout was restored immediately after the QC run and `git status` is clean for the
  prompts directory.

## 2. Input set (production DB, read-only)

- Source: `dist/GifAgentUI/data/library.db`, opened with SQLite URI `mode=ro` plus
  `PRAGMA query_only=ON` and `busy_timeout=30000`; no writes; no media bytes read.
- At extraction time the production `candidate_gifs` table held 13,625 rows of which
  12,827 were enriched (`json_extract(vlm_summary_json,'$.enriched_by') IS NOT NULL`;
  single producer `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`) — unchanged
  totals vs shard 3.
- Window check before slicing: enriched rowids are contiguous over 80–979 (900/900
  present), `OFFSET 700` lands exactly on **rowid 780** and the window's last row is
  **rowid 979**, as prescribed; a rowid-filter query (`rowid BETWEEN 780 AND 979` over
  enriched rows) returns the identical 200-rowid sequence, so `OFFSET 700 LIMIT 200`
  and a rowid filter agree exactly.
- Prior-input stability: all 500 prior input rows (rowids 80–579 from
  `shard-w1stagec-001/002/003.input.jsonl`) were re-verified caption/candidate_id
  identical against the current DB before writing anything.
- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-005.input.jsonl` — 200 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design, same as
  shards 1–3).
- Input SHA-256: `ae3d0c261dc13ca4c8b188fc7f4caced15a36159353a1bd6de247fefe054f511`

## 3. Output shard

- File: `data/token_sprint/shards/shard-005.p2.v2.5621072f-10b6-4b83-b492-404a279e1551.jsonl`
- Lines: 200 (one per input row, input order; `candidate_id` copied programmatically
  from the input shard with a positional rowid assertion on every line, eliminating
  the id-swap defect class; per-line `prompt_version: "p2/v2"` and
  `prompt_sha256: 01a161b0…`)
- SHA-256: `e091c97f1e855678e87295a2ffab4787a33ecaec7bed7382e2c3f0070fe6d863`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).
- Written incrementally with atomic tmp+`os.replace` flushes every 25 lines; the full
  mechanical check (schema + snake_case + source-verbatim over every optional facet of
  the prefix) re-ran before every flush. One mid-run fix: line 148 (rowid 927) had
  `exposure: "lingerie"` despite a caption with no dress-state word — caught by the
  flush-time mechanical check itself, corrected to key omission, and re-flushed from
  150 with 0 further failures.
- One-off helper scripts lived only in the worktree and were deleted after the run;
  they are not part of the commit.

## 4. QC gates (Stage C per-shard standard)

### 4.1 validate_shard.py (run from the worktree, unmodified; p2/v2 supported as-is)

```json
{
  "coverage_rate": 1.0,
  "errors": [],
  "fidelity_rate_high_stratum": null,
  "n": 200,
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
| Schema compliance | >= 98% | 100% by validator p2 check; 100% under a stricter manual check (per-line `prompt_version`/`prompt_sha256` exact, `candidate_id` order matches input 200/200, optional facets str-or-absent never null, all values snake_case or null) | PASS |
| Refusal rate | <= 15% | 0% (0 refusals; every row in this window has a describable scene) | PASS |
| Fidelity | full-facet mechanical verbatim check over ALL optional facets (0 failures required) + seed=20260829 20-row semantic sample | 205 facets checked mechanically (169 `exposure` + 36 `mood`), 0 failures; 20 rows sampled, 0 verbatim failures, 0 semantic errors (see 4.3) | PASS |

Optional facet emission over all 200 rows: 205 facets (169 `exposure`, 36 `mood`);
173/200 rows carry at least one optional facet. `finish` was emitted on 1 row
(`climax`, where the caption states ejaculation/semen: rowid 840) and null elsewhere;
`setting` null on 13 rows (surface-only captions — "patterned bedding", "textured red
surface", "white bedspread/sheet", "plain background" — plus a furniture-only desk
row), `act` null on 0 rows.

### 4.3 Fidelity sample table (random.Random(20260829), 20 of 200)

Same seed and range as shards 1–3, so the sampled line indices are identical to the
shard-3 table — deterministic, not copied. Rowids are 779 + line number (780–979).

| Line | candidate_id | Facets emitted | Caption evidence (fragment) | Verbatim | Semantics |
| --- | --- | --- | --- | --- | --- |
| 8 | cand_fs_f6dc98419f3a851366c04476304f7594778f2e48310e69331a8bec1353ce2565 | setting=indoor, act=receiving_oral_sex, mood=calm | "she appears to be receiving oral sex"; "expressions ranging from calm to surprised" | OK | OK |
| 9 | cand_fs_c5baca0cfca4c66cb0b74676ac43cf45588522fb612d64d7c256d1061f179dd5 | setting=indoor, act=swallowing | "appearing to swallow while drool or fluid drips from her chin"; "in front of a window with grey curtains" | n/a | OK |
| 11 | cand_fs_7525b9a1e596f2443b1ca1cfa6fb29764cdf01d65f9ed4b76853ee9a7a50bb4c | setting=indoor, act=penetration, exposure=shirtless | "engaging in penetration"; "A shirtless man and a nude woman" (first-mentioned state) | OK | OK |
| 15 | cand_fs_42157bb84607345f2126d7726792e586a891902416da2fffbf1db24e2e3dcf37 | setting=kitchen, act=brushing_hair, exposure=crop_top, mood=frustrated | "stands in a modern kitchen"; "a gesture of brushing her hair back"; "white crop top"; "as if frustrated" | OK | OK |
| 17 | cand_fs_51f1c6bd201be6a97d46b9b81417a3e82f72f91c915c2ec2ef78d672a646d0b3 | setting=kitchen, act=anal_penetration, exposure=lingerie | "penetrating her anus"; "appears to be a modern kitchen"; "black lingerie and stockings" | OK | OK |
| 18 | cand_fs_40965728bf099b485afe1e719e3903895bf34b69eb1b1213e7e37e6c8aa433d8 | setting=indoor, act=skirt_lifting, exposure=tights | "takes place indoors near large windows"; "hands lift her skirt"; "grey skirt with black tights" | OK | OK |
| 36 | cand_fs_5aae808f28a30b09a924dcd5d6fc6ab6279b04e19882a6974a4df1e265313402 | setting=outdoor, act=kissing, exposure=purple_shirt, mood=intimate | "The setting is outdoors"; "kisses or nuzzles their neck"; "in a purple shirt"; "the intimate physical contact" | OK | OK |
| 37 | cand_fs_bcb134ebcbd3864188f9548b5f6687c8c389fa56d6373e5d11fa5298fcf35981 | setting=indoor, act=massage, exposure=towel | "performs a massage on a woman"; "wrapped in a white towel" | OK | OK |
| 42 | cand_fs_d71eeca82ba18d178a21fae259a6ad893550c33950b17d9f3ce824dab2b5185a | setting=bedroom, act=penetration, exposure=underwear | "indoor bedroom with dark curtains"; "being penetrated from behind"; "man wearing white underwear" | OK | OK |
| 66 | cand_fs_db1086e3b71fa61c89fe579d9b424949d6aa46691378d77ad4a0c9dc005255fa | setting=indoor, act=intercourse, exposure=lingerie | "indoors near a large window"; "engaging in sexual intercourse"; "leopard-print lingerie" | OK | OK |
| 88 | cand_fs_8cac47265b3bba39c8e33102c6c5356b7187b8d710b49554ab211279d3c069a1 | setting=bedroom, act=adjusting_clothing, exposure=lingerie | "dimly lit bedroom"; "she adjusts her harness"; "black lingerie" | OK | OK |
| 106 | cand_fs_7284eb7d859a081498855fbae36b738298d03a73813a8af2a70f8ff7add1e4f8 | setting=bedroom, act=anal_penetration, exposure=garter_straps, mood=pleasure | "The setting is a bedroom"; "penetrates her anus"; "blue satin garter straps"; "expression of pleasure or exertion" | OK | OK |
| 113 | cand_fs_a5e4112c8fc1487a37b6110c7796955c0dedd594a59f8249c1c9bc1eb76a63dd | setting=indoor, act=undressing, exposure=lingerie | "dimly lit room"; "pulling his t-shirt over his head"; "black lingerie" | OK | OK |
| 143 | cand_fs_3e44da6847bfd5ae3b6938a070df540b62b0eb57bce278eebc004f0b16f90a72 | setting=bedroom, act=posing, exposure=lingerie | "dimly lit bedroom near a bed"; "turns her head and body slightly"; "black lingerie and stockings" | OK | OK |
| 153 | cand_fs_9d3a49739b6e41851794fdb77eca16422484c15a673b6eeabd3ce1ed10086592 | setting=bedroom, act=oral_sex | "performing oral sex on a man lying on a bed"; "dimly lit bedroom"; "black lace thigh-highs" hyphenated -> correct omission | n/a | OK |
| 155 | cand_fs_3390c4b03ab3e492b08c50f1564f89044acc94f5689cec1e37f68140dc28040b | setting=indoor, act=oral_sex, exposure=shirtless | "she performs oral sex on him"; natural light from a window; "A shirtless man" (first-mentioned state) | OK | OK |
| 168 | cand_fs_c6a9940cdc8768896a9c782d0381022f9eca3f7a9d70af00ff8f9bcbabf868f2 | setting=indoor, act=posing, exposure=athletic_wear | couch/gray-wall scene; "kneels before him … shifts slightly"; "gray athletic wear" | OK | OK |
| 169 | cand_fs_635420bc92248d57f3b0ab1ae9c160e58b4e8d6db3d1c15c30efe1225892413e | setting=bedroom, act=oral_sex | "engaging in oral sex"; "modern bedroom backdrop"; "grey zip-up shirt" hyphenated -> correct omission | n/a | OK |
| 181 | cand_fs_8b1b551007e35d0de6599f6dba91a555212d3d27ae93c2c79c867811fc53cab3 | setting=bathroom, act=kissing, exposure=towel, mood=surprise | "modern tiled bathroom"; "a man kisses her neck"; "blue towel wrapped around her lower body"; "including surprise and pleasure" | OK | OK |
| 191 | cand_fs_78c5b5452390ace748a38d275411f595a742925bb7494d33c242292393b57d80 | setting=bedroom, act=posing | "on a couch in a bedroom"; "positions herself over his chest … looking at the camera"; no dress-state word -> correct omission | n/a | OK |

## 5. Notes / deviations

- `fidelity_rate_high_stratum` is `null` (denominator 0): the caption-only Stage C
  input carries no `stratum`/`vlm_summary_json`/`tags_json`/`scenario_keys_json`
  fields, same as shards 1–3; the Stage C fidelity gate is the full mechanical
  verbatim check (205/205 optional facets) plus the seed-20260829 manual sample above.
- No `suspected_sanitized` rows; explicit captions were annotated explicitly (hard
  rule 3), including `anal_intercourse`, `anal_penetration`, `anal_sex`, `penetration`,
  `intercourse`, `oral_sex`/`oral_stimulation`, `masturbation`, dildo/butt-plug toy
  rows, and the `climax` row (rowid 840, caption states ejaculation).
- Hyphenated garments cannot be expressed as snake_case (the `_`-to-space read-back
  would not match the hyphen), so `exposure` was omitted on those rows rather than
  paraphrased (`t-shirt`, `zip-up top/shirt`, `off-shoulder top`, bare
  `thigh-highs`); where the caption says "thigh-high **stockings**" the bare noun
  `stockings` is a contiguous substring and was used (rowids 923, 925, 976).
  For mixed male/female dress states the value follows the caption's first-mentioned
  usable state (e.g. `shirtless`, `underwear`, `red_shirt`, `green_shorts`).
- Multi-word verbatim values used where the caption's exact phrase is multi-word
  (`no_clothes`, `no_clothing`, `minimal_clothing`, `gray_top`, `black_top`,
  `white_top`, `blue_top`, `purple_top`, `purple_shirt`, `dark_shirt`, `red_shirt`,
  `white_blouse`, `red_shorts`, `green_shorts`, `latex_shorts`, `blue_dress`,
  `athletic_wear`, `spaghetti_strap_top`, `fishnet_bodysuit`, `bikini_top`,
  `garter_straps`); standard single-word states otherwise (`nude`, `underwear`,
  `lingerie`, `bikini`, `bra`, `tights`, `stockings`, `towel`, `harness`, `swimsuit`,
  `bare`, `shirtless`, `tank_top`, `crop_top`).
- Vocabulary continuity with shards 1–3 was kept for shared scenes (`intercourse`,
  `oral_sex`/`oral_stimulation` split follows the caption's wording, `massage`,
  `touching_herself`, `adjusting_clothing`, `kissing_and_fondling`, `fondling`,
  `posing`, `masturbation`, `bedroom`/`kitchen`/`bathroom`/`indoor`/`outdoor`);
  new settings this window: `studio`, `beach`, `shower` (precedent), `apartment`
  (precedent), `living_room` (precedent); new acts: `bondage`, `leash_pulling`,
  `shaving`, `skirt_lifting`, `dildo_play`, `using_dildo`, `using_butt_plug`,
  `manual_stimulation`, `positioning`/`positioning_for_sex`/
  `positioning_for_penetration`, `applying_lotion`, `grinding`, `riding`,
  `undressing`, `swallowing`, `receiving_oral_sex`, `touching_genitals`,
  `pulling_back_sheets`.
- This window is heavily scene-clustered (the same shoot recurs across consecutive
  rowids); settings follow each caption's own statement, which yields e.g. `shower`
  (879/880) next to `bathroom` (881) and `kitchen` next to `bedroom` clusters —
  per-caption, not per-cluster.
- Publication: `.ready` sidecar written via
  `scripts.token_sprint.coordinator.protocol.write_ready` (atomic; producer
  `w1-stagec-executor`); ready-file SHA-256
  `8ef4f43d724571e5dcc99d379fa2f1c665638156c1bfdbe692c015a553b498bd`; it binds
  artifact `e091c97f1e855678e87295a2ffab4787a33ecaec7bed7382e2c3f0070fe6d863`.
- Red lines honored: production DB opened read-only only (`mode=ro` + `query_only=ON`
  + `busy_timeout`), zero writes; no GIF/JPG/video bytes opened; no direct
  model-endpoint code added; `pyproject.toml`/`uv.lock` untouched; existing
  sprint-root artifacts unmodified (only new shard/qc/ready files added);
  shard/qc/ready files stay in the sprint root and are not committed to git (only
  this report is committed, from the worktree, explicit path).
