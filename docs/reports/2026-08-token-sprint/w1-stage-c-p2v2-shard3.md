# W1 Stage C — P2 scene metadata, third p2/v2 shard (rows 380–579)

- Date: 2026-08-29
- Executor: w1-stagec-executor (Stage C W1 data-track ramp, shard 3)
- Branch: `sprint/w1-stagec3` (worktree `.worktrees/sprint-w1-stagec3`, off `master` @
  `23158a68` — a docs commit landed on master in parallel after `eceebdc5`, this run
  branched from the current tip as instructed)
- Standard: same schema and gates as the first two p2/v2 shards
  (`w1-stage-c-p2v2.md`, `w1-stage-c-p2v2-shard2.md`); manifest rev15 ruling —
  `prompt_version p2/v2`, optional facets (`exposure`, `mood`) restricted to
  source-verbatim mapping of the input caption.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`, byte-identical content to the first two
  shard runs; NOT modified. Canonical (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note: with `core.autocrlf=true` the fresh worktree materializes the
  file with CRLF (raw bytes hash `d4971361…`). As in shard 2, the worktree copy was
  normalized to the canonical LF bytes for the QC run only (content unchanged), so
  `prompts.prompt_sha256("p2","v2")` and the validator reported the frozen `01a161b0…`
  hash; the stock CRLF checkout was restored afterwards and `git status` is clean for
  the prompts directory.

## 2. Input set (production DB, read-only)

- Source: `dist/GifAgentUI/data/library.db`, opened with SQLite URI `mode=ro` plus
  `PRAGMA query_only=ON` and `busy_timeout=30000`; no writes; no media bytes read.
- At extraction time the production `candidate_gifs` table held 13,625 rows of which
  12,827 were enriched (`json_extract(vlm_summary_json,'$.enriched_by') IS NOT NULL`;
  single producer `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`). The large growth
  vs shard 1 (235 enriched then) is the ongoing background enrichment; the first 300
  enriched rowids are unchanged since shards 1/2.
- Window check before slicing: enriched rowids are contiguous over 80–579
  (500/500 present), `OFFSET 300` lands exactly on rowid 380 and `OFFSET 499` on
  rowid 579, and all 300 prior input rows (rowids 80–379) were re-verified
  caption/candidate_id-identical against the current DB. So `OFFSET 300 LIMIT 200` and
  a rowid filter agree exactly: **rowids 380–579**, as prescribed.
- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-003.input.jsonl` — 200 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design; raw
  `vlm_summary_json` kept out of the input avoids the `"sex_act"` marker false-positive
  vector documented in shard 1).
- Input SHA-256: `807a87da38891527b9d82216e51d0a5dceb92320faada1a090d66f7334df9079`

## 3. Output shard

- File: `data/token_sprint/shards/shard-003.p2.v2.acb9e825-dc34-4a5a-942a-e5c4d1bba993.jsonl`
- Lines: 200 (one per input row, input order; `candidate_id` copied programmatically
  from the input shard rather than retyped, eliminating the id-swap defect class shard 2
  hit; per-line `prompt_version: "p2/v2"` and `prompt_sha256: 01a161b0…`)
- SHA-256: `5d745146c747b18e87deb0bc54f389720325281adbbdaf0993d216ca1b59d72f`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).
- Written incrementally with atomic tmp+`os.replace` flushes every 25 lines (8 flushes),
  so an interrupted run always leaves a complete 25-line prefix on disk; the full
  mechanical check ran before the first flush.
- One-off helper script used for the merge lived only in the worktree and was deleted
  after the run; it is not part of the commit.

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
| Refusal rate | <= 15% | 0% (0 refusals; unlike shards 1/2 this window contains no pure brand-graphic rows, so every row has a describable scene) | PASS |
| Fidelity | full-facet mechanical verbatim check over ALL optional facets (0 failures required) + seed=20260829 20-row semantic sample | 195 facets checked mechanically, 0 failures; 20 rows sampled, 0 verbatim failures, 0 semantic errors (see 4.3) | PASS |

Optional facet emission over all 200 rows: 195 facets (165 `exposure`, 30 `mood`);
168/200 rows carry at least one optional facet. `finish` was emitted on 3 rows
(`climax`, where the caption states ejaculation/semen: rowids 446, 528, 532) and null
elsewhere; `setting` null on 12 rows (captions with only a "surface"/"wall"/"dark
background" and no location), `act` null on 2 rows (rowids 465 "moving slightly" and
551 "move intimately" — no act named, not guessed).

### 4.3 Fidelity sample table (random.Random(20260829), 20 of 200)

| Line | candidate_id | Facets emitted | Caption evidence (fragment) | Verbatim | Semantics |
| --- | --- | --- | --- | --- | --- |
| 8 | cand_fs_3f60fd2686ff61b58de49a340824f317b4213ab47f0549f84b75faeaecbe9662 | setting=indoor, act=kissing_and_fondling, exposure=underwear | "touching her buttocks as they kiss"; "woman in white underwear" | OK | OK |
| 9 | cand_fs_6010dcf8177b7378a30246b58a389fb591152d4b0e5edb5f1539c10d72eaec1d | setting=indoor, act=penetration, exposure=underwear | "penetrating her from behind"; "white underwear" | OK | OK |
| 11 | cand_fs_05d5232cead1de0b8b74465d2832c92f1d12d71e14912c1acb7cb3c996da0840 | setting=indoor, act=fondling, exposure=underwear | "runs his hand over her buttocks"; "white underwear" | OK | OK |
| 15 | cand_fs_3e49a2449ab8bdc8c12d993bca950c50593023317df0cabffa1f15cdcf06d2e0 | setting=swimming_pool, act=oral_sex, exposure=bikini | "blue inflatable pool engaging in oral sex"; "woman in a bikini" | OK | OK |
| 17 | cand_fs_8f43b009b5cad0a6bd9d1dfe72383a56d9e5f0efa2571099e09af3d942a939bf | setting=indoor, act=oral_sex, exposure=bikini | "transitions to indoors where she performs oral sex"; "in a bikini" | OK | OK |
| 18 | cand_fs_80776cd70ffb5d395ed50777385db437d58a4e49ef922f1482c7e9eb41c10a48 | setting=indoor, act=anal_intercourse | "engaging in anal intercourse"; "takes place indoors" | OK | OK |
| 36 | cand_fs_db7b8801036e59fad4d5abdd8784ba71db9494986cab382261edcb0f2ae9904c | setting=bedroom, act=intercourse, exposure=underwear | "sexual intercourse on a bed in a warmly lit bedroom"; "partially clothed in underwear" | OK | OK |
| 37 | cand_fs_d311409feba6b9f9f82725c93402d1c851518a21421cb422e339b1db5a8eab57 | setting=bedroom, act=intercourse, exposure=nude | "Two nude individuals are engaged in intercourse on a bed" | OK | OK |
| 42 | cand_fs_ddba20325085517bf8e5c4381f584648cc2f3a228d6ff440e8d674018fc00b3d | setting=outdoor, act=sex, exposure=leggings | "engaging in sexual activity"; "pink leggings"; "outdoors on a wooden deck" | OK | OK |
| 66 | cand_fs_89ed806f44e57c0ffdc1aec6a365da352cec13175436f5ca1d663a32d9f0aa4e | setting=sauna, act=sex, exposure=nude | "sexual activity on a wooden bench inside a sauna"; "nude individuals" | OK | OK |
| 88 | cand_fs_1245248d25eac700ceb01d8fd7af796aff77853251c4cc9c279c6467ba960125 | setting=indoor, act=sex, exposure=tank_top | "engaging in sexual activity"; "white tank top and jeans"; "indoors near a door" | OK | OK |
| 106 | cand_fs_9d447a5f6a4f383f5ba863806ed125e5d06bb65f954d02c18ea438f44950cede | setting=apartment, act=sex, exposure=lingerie | "having sex … in a modern apartment"; "blonde woman in lingerie" | OK | OK |
| 113 | cand_fs_24ec47953b3b41f552963420e2c27b33de4bc6b649c004086b6edfb95627db02 | setting=indoor, act=intercourse, exposure=nude | "nude blonde woman … engaging in sexual intercourse" | OK | OK |
| 143 | cand_fs_815197376c31a6813cbe51b14f996f351cdcb6dfdc74f078f88b77a3d3baeddb | setting=shower, act=sex, exposure=nude | "nude man and woman are in a shower stall … sexual activity" | OK | OK |
| 153 | cand_fs_199dafaed1340f52c7731faf3bba3b7b88b1dc4582d2a7866a76a32254eb38fc | setting=bedroom, act=intercourse, finish=climax | "during intercourse"; "another frame shows an ejaculation"; "indoors on a bed" | OK | OK |
| 155 | cand_fs_a1bae6c3925d6476e24e940c051b0d88459172aeae7a4370b67e316c8fd70162 | setting=indoor, act=massage, exposure=bare, mood=intimate | "hand massages her back"; "touching her bare skin"; "The scene is intimate" | OK/OK | OK |
| 168 | cand_fs_5dc832caf57b7e20a3bd53b85d5a8e8aa24a46014d1f3019310be538bb3de4d8 | setting=bedroom, act=embracing, exposure=bra | "embracing the woman from behind"; "white bra and panties" | OK | OK |
| 169 | cand_fs_df23a75d3db44dae977543b2d6c1ada28136e93a8e43440fe5a969e10a8fc368 | setting=bedroom, act=intercourse, exposure=nude | "Two nude individuals are engaged in sexual intercourse on a bed" | OK | OK |
| 181 | cand_fs_46819cfaf0d3a8e09ac0ffebdfff9c330f42e17405ef2c85bd9c4997d0eee015 | setting=indoor, act=oral_sex, exposure=tank_top | "engaging in oral sex"; "'1win' tank top" | OK | OK |
| 191 | cand_fs_aeb5db2924476368f33a88ec9a37c78538230751b94a32a12b7ad351b1fb1788 | setting=indoor, act=intercourse | "engaged in sexual intercourse … indoors near a window"; no dress-state word -> correct omission | n/a | OK |

## 5. Notes / deviations

- `fidelity_rate_high_stratum` is `null` (denominator 0): the caption-only Stage C input
  carries no `stratum`/`vlm_summary_json`/`tags_json`/`scenario_keys_json` fields, same
  as shards 1/2; the Stage C fidelity gate is the full mechanical verbatim check
  (195/195 optional facets) plus the seed-20260829 manual sample above.
- No `suspected_sanitized` rows; explicit captions were annotated explicitly (hard
  rule 3), including `anal_intercourse`, `anal_penetration`, `anal_fingering`,
  `vaginal_intercourse`, `penetration`, `oral_sex`, `masturbation`, `cum`/climax rows.
- Multi-word verbatim values were used where the caption's exact phrase is multi-word
  (`partially_clothed`, `garter_belts`, `fishnet_stockings`, `dark_clothing`,
  `sports_bra`, `tank_top`, `crop_top`, `wrap_dress`); single-word states otherwise.
  Hyphenated garments (`t-shirt`, `lace-up top`) cannot be expressed as snake_case
  (the `_`-to-space read-back would not match the hyphen), so `exposure` was omitted on
  those rows rather than paraphrased. For mixed male/female dress states the value
  follows the caption's first-mentioned usable state (e.g. `shirtless`, `underwear`).
- Vocabulary continuity with shards 1/2 was kept for shared scenes (`intercourse`,
  `oral_sex`/`oral_stimulation` split follows the caption's wording, `sauna`,
  `touching_herself`, `using_phone`, `arching_back`, `adjusting_clothing`,
  `petting_dog`-style mundane acts e.g. `gaming`, `washing`, `watching_tv`); new
  settings this window: `swimming_pool`, `shower`, `apartment`, `living_room`,
  `bathtub` (precedent), `outdoor`/`deck`.
- Publication: `.ready` sidecar written via
  `scripts.token_sprint.coordinator.protocol.write_ready` (atomic; producer
  `w1-stagec-executor`); ready-file SHA-256
  `44f74db778c36ae26157d54a7dabb1f9fc49c241929c112002e7483322a11ed5`; it binds artifact
  `5d745146c747b18e87deb0bc54f389720325281adbbdaf0993d216ca1b59d72f`.
- Red lines honored: production DB opened read-only only (`mode=ro` +
  `query_only=ON` + `busy_timeout`), zero writes; no GIF/JPG/video bytes opened; no
  direct model-endpoint code added; `pyproject.toml`/`uv.lock` untouched; existing
  sprint-root artifacts unmodified (only new shard/qc/ready files added); shard/qc/ready
  files stay in the sprint root and are not committed to git (only this report is
  committed, from the worktree, explicit path).
