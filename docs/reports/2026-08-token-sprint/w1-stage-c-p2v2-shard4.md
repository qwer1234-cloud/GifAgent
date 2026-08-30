# W1 Stage C — P2 scene metadata, fourth p2/v2 shard (rows 580–779)

- Date: 2026-08-29
- Executor: w1-stagec-executor (Stage C W1 data-track ramp, shard 4)
- Branch: `sprint/w1-stagec4` (worktree `.worktrees/sprint-w1-stagec4`, off `master` @
  `1a092fe1`, the current tip and the same commit shard 3 landed on)
- Standard: same schema and gates as the first three p2/v2 shards
  (`w1-stage-c-p2v2.md`, `w1-stage-c-p2v2-shard2.md`, `w1-stage-c-p2v2-shard3.md`);
  manifest rev15 ruling — `prompt_version p2/v2`, optional facets (`exposure`,
  `mood`) restricted to source-verbatim mapping of the input caption.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`, byte-identical content to the first three
  shard runs; NOT modified. Canonical (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note: with `core.autocrlf=true` the fresh worktree materializes the
  file with CRLF (raw bytes hash `d4971361…`). As in shards 2/3, the worktree copy was
  normalized to the canonical LF bytes for the QC run only (content unchanged), so
  `prompts.prompt_sha256("p2","v2")` and the validator reported the frozen `01a161b0…`
  hash; the stock CRLF checkout was restored afterwards and `git status` is clean for
  the prompts directory.

## 2. Input set (production DB, read-only)

- Source: `dist/GifAgentUI/data/library.db`, opened with SQLite URI `mode=ro` plus
  `PRAGMA query_only=ON` and `busy_timeout=30000`; no writes; no media bytes read.
- At extraction time the production `candidate_gifs` table held 13,625 rows of which
  12,827 were enriched (`json_extract(vlm_summary_json,'$.enriched_by') IS NOT NULL`;
  single producer `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`) — identical counts
  to the shard 3 run, so the enriched prefix has not drifted. Caption source is
  `vlm_summary_json.'$.caption'` (the table has no caption column; same effective
  content as the prior shards' inputs).
- Window check before slicing: enriched rowids are contiguous over 580–779
  (200/200 present), `OFFSET 499` lands exactly on rowid 579, `OFFSET 500` on rowid 580,
  and `OFFSET 699` on rowid 779 (`OFFSET 700` → 780). The boundary row 579 was
  re-verified caption/candidate_id-identical to the last line of
  `shard-w1stagec-003.input.jsonl`. So `LIMIT 200 OFFSET 500` and a rowid filter agree
  exactly: **rowids 580–779**, as prescribed.
- Input shard (data product, not in git):
  `data/token_sprint/shards/shard-w1stagec-004.input.jsonl` — 200 lines of
  `{rowid, candidate_id, caption, enriched_by}` (caption-only by design; raw
  `vlm_summary_json` kept out of the input avoids the `"sex_act"` marker false-positive
  vector documented in shard 1).
- Input SHA-256: `5bb576f3fc1b1dfdeb39ab6f0de13ac625b738b21880188874d7e1f50ee806c1`

## 3. Output shard

- File: `data/token_sprint/shards/shard-004.p2.v2.369357df-1065-4ccd-b654-76344957e500.jsonl`
- Lines: 200 (one per input row, input order; `candidate_id` copied programmatically
  from the input shard rather than retyped; per-line `prompt_version: "p2/v2"` and
  `prompt_sha256: 01a161b0…`)
- SHA-256: `62e560f1dbb6892c89dc147b35facb32460371a5abd5e73e17e5e4874d818e7e`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).
- Written incrementally with atomic tmp+`os.replace` flushes every 25 lines (8 flushes),
  so an interrupted run always leaves a complete 25-line prefix on disk; the
  source-verbatim mechanical assertion for every optional facet ran inline, per line,
  before the line joined the flushed prefix.
- One-off helper script used for the build lived only in the worktree and was deleted
  before the commit; it is not part of the commit.

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
| Schema compliance | >= 98% | 100% by validator p2 check; 100% under a stricter manual check (per-line `prompt_version`/`prompt_sha256` exact, `candidate_id` sequence identical to input 200/200 and unique, optional facets str-or-absent never null, all values snake_case or null) | PASS |
| Refusal rate | <= 15% | 0% (0 refusals; every row in this window has a describable scene — the closest brand-graphic case, rowid 635, is a person holding a phone displaying a casino app, annotated as `using_phone` rather than refused) | PASS |
| Fidelity | full-facet mechanical verbatim check over ALL optional facets (0 failures required) + seed=20260829 20-row semantic sample | 190 facets checked mechanically, 0 failures; 20 rows sampled, 0 verbatim failures, 0 semantic errors (see 4.3) | PASS |

Optional facet emission over all 200 rows: 190 facets (169 `exposure`, 21 `mood`);
173/200 rows carry at least one optional facet. `finish` was emitted on 3 rows
(`climax`, where the caption states semen/ejaculation: rowids 604, 612, 613) and null
elsewhere; `setting` null on 38 rows (rug/floor/wall/"textured surface" scenes and one
phone-screen scene with no location noun stated), `act` null on 14 rows (captions with
only slight movement, looking, or unnamed physical contact — not guessed, per the
shard 3 precedent).

Value distributions: settings `bedroom` 77, `indoor` 69, `apartment` 8, `kitchen` 5,
`studio` 2, `gym` 1; top acts `intercourse` 34, `oral_sex` 27, `penetration` 24,
`fondling` 21, `adjusting_clothing` 13, `posing` 18, `touching_herself` 9, `massage` 8,
`sex` 7, `grinding` 6, plus singles (`masturbation`, `cumshot`, `anal_intercourse` ×3,
`anal_penetration` ×3, `anal_fingering`, `oral_stimulation`, `caressing`, `riding`,
`embracing`, `crawling`, `using_phone`, `speaking` ×2, `gesturing`, `entering`);
exposure `underwear` 81, `nude` 26, `lingerie` 22, `stockings` 11, `shirtless` 10,
`tank_top` 7, `bra` 4, `thong` 3, `crop_top`/`bralette`/`minimal_clothing`/`bare`/
`topless` 1 each; mood `intimate` 6, `smiling`/`focused` 4 each, `sensual` 3,
`pleasure` 2, `seductive`/`smile` 1 each.

### 4.3 Fidelity sample table (random.Random(20260829), 20 of 200)

| Line | candidate_id | Facets emitted | Caption evidence (fragment) | Verbatim | Semantics |
| --- | --- | --- | --- | --- | --- |
| 8 | cand_fs_a363297a0843… | setting=bedroom, act=adjusting_clothing, exposure=lingerie | "lies on her back on a bed with peach-colored bedding"; "slowly adjusts her corset"; "white lace lingerie" | OK | OK |
| 9 | cand_fs_3537cdda1bfb… | setting=bedroom, act=posing, exposure=nude | "on a patterned bed … indoor bedroom"; "shifts positions slightly … arms raised behind her head"; "nude woman" | OK | OK |
| 11 | cand_fs_d061373df0df… | setting=bedroom, act=posing, exposure=lingerie | "sits on a bed wearing white lace lingerie, her legs spread apart as she looks at the camera" | OK | OK |
| 15 | cand_fs_547b529a3b9e… | setting=indoor, act=posing, exposure=nude | "on a patterned couch … arms behind her head, showing various facial expressions"; "nude woman" | OK | OK |
| 17 | cand_fs_4a35fc8c8948… | setting=bedroom, act=penetration | "lies face-down on a bed"; "positioned behind her for penetration"; no dress-state word -> correct omission | n/a | OK |
| 18 | cand_fs_40f39fbf72f5… | setting=indoor, act=posing, exposure=bra | "sits on a brown leather couch in a dimly lit room"; "poses for the camera"; "white bra and plaid shirt" | OK | OK |
| 36 | cand_fs_a0bcce2051d7… | setting=bedroom, act=sex, exposure=shirtless | "in bed engaging in intimate sexual activity"; "the man is shirtless" | OK | OK |
| 37 | cand_fs_8dece5b4ce2b… | setting=null, act=fondling | "lie on a pink surface … against dark background" (no location noun); "grips another person's buttocks" | n/a | OK |
| 42 | cand_fs_ce8e2644b85d… | setting=studio, act=caressing, exposure=nude, mood=intimate | "mirrors reflect the studio setup"; "gently caressing and kissing"; "nude woman"; "creating an intimate atmosphere" | OK/OK | OK |
| 66 | cand_fs_9435f80865c1… | setting=null, act=oral_sex, exposure=underwear | "on a gray textured rug" (no location); "performs oral sex"; "patterned underwear" | OK | OK |
| 88 | cand_fs_5d792c01b4f0… | setting=indoor, act=fondling, exposure=underwear | "The setting is indoors near a window"; "rubs her buttocks"; "patterned underwear" | OK | OK |
| 106 | cand_fs_79b70fbe518c… | setting=bedroom, act=intercourse, exposure=shirtless | "engaged in intercourse on a bed"; "Two shirtless men" (first-mentioned state) | OK | OK |
| 113 | cand_fs_a96e7d3e047a… | setting=bedroom, act=touching_herself, exposure=underwear | "bent over on a bed"; "her hands moving across her buttocks"; "white underwear" | OK | OK |
| 143 | cand_fs_2611078ba7bc… | setting=bedroom, act=penetration, exposure=lingerie | "takes place in a dimly lit bedroom"; "penetrating her with his erect penis"; "black lingerie" | OK | OK |
| 153 | cand_fs_84b008f71d67… | setting=indoor, act=massage, exposure=nude, mood=intimate | "massage … on a white massage table" (room cluster, candles/curtains); "nude woman"; "creates an intimate ambiance" | OK/OK | OK |
| 155 | cand_fs_0c91a677b640… | setting=bedroom, act=intercourse, exposure=nude | "lies on her back on a bed … The room has dark curtains"; "engaging in intercourse"; "both are nude" | OK | OK |
| 168 | cand_fs_234a534f7869… | setting=indoor, act=oral_sex | "The setting is an indoor room with white tiled floors"; "orally stimulating a man's erect penis"; blazer is not a nudity state -> correct omission | n/a | OK |
| 169 | cand_fs_0abf60bf66e9… | setting=kitchen, act=intercourse, exposure=lingerie | "on a brown leather couch in a modern kitchen"; "engaged in sexual intercourse"; "black lingerie" (first-mentioned) | OK | OK |
| 181 | cand_fs_9769a465ccd5… | setting=null, act=null, exposure=stockings | "against the backdrop of a plain wall and tiled floor" (no location); "stands between her legs … as he moves rhythmically" — no act named, not guessed; "black thigh-high stockings" | n/a | OK |
| 191 | cand_fs_0e4195d90289… | setting=indoor, act=gesturing | "in a modern room with purple lighting"; "gesturing toward an open door"; "dark dress" is not a nudity state -> correct omission | n/a | OK |

## 5. Notes / deviations

- `fidelity_rate_high_stratum` is `null` (denominator 0): the caption-only Stage C input
  carries no `stratum`/`vlm_summary_json`/`tags_json`/`scenario_keys_json` fields, so
  `make_shards.row_content_text` is empty, same as shards 1–3; the Stage C fidelity gate
  is the full mechanical verbatim check (190/190 optional facets, asserted inline per
  line before each 25-line flush) plus the seed-20260829 manual sample above.
- No `suspected_sanitized` rows; explicit captions were annotated explicitly (hard
  rule 3), including `anal_intercourse`, `anal_penetration`, `anal_fingering`,
  `vaginal`-unspecified `intercourse`/`penetration`, `oral_sex`, `masturbation`,
  `cumshot`/`climax` rows.
- Multi-word verbatim values were used where the caption's exact phrase is multi-word
  (`minimal_clothing`, `tank_top`, `crop_top`); single-word states otherwise.
  Hyphenated garments (`thigh-highs`, `t-shirt`) cannot be expressed as snake_case (the
  `_`-to-space read-back would not match the hyphen), so on those rows `exposure` either
  used the contiguous non-hyphenated state word the caption also contains (`stockings`
  from "thigh-high stockings", `underwear` from "patterned underwear") or was omitted
  rather than paraphrased.
- Mood/adjective values derived by the locked contiguous-substring rule from inflected
  forms in the caption are explicitly listed for the reviewer: `seductive` ⊂ "poses
  seductively" (rowid 581), `intimate` ⊂ "intimately embracing" (738) / "intimate
  ambiance" (731, 732) / "intimate atmosphere" (621) / "intimate physical contact"
  (717, 758), `smile` ⊂ "with a smile" (664). `focused`, `smiling`, `sensual`,
  `pleasure` appear as literal words in their captions.
- For mixed male/female dress states the value follows the caption's first-mentioned
  usable state (shard 3 precedent), e.g. `shirtless` (614, 615, 624, 683, 685, 737, 740,
  742) vs `underwear`/`nude` where that word comes first (632, 633, 684, 678, 679, 681,
  709, 730, 739, 741).
- Vocabulary continuity with shards 1–3 was kept for shared scenes (`intercourse`,
  `oral_sex`/`oral_stimulation` split follows the caption's wording, `fondling`,
  `touching_herself`, `adjusting_clothing`, `posing`, `massage`, `anal_*` family,
  `using_phone`, mundane acts); new settings this window: `gym` (caption: "room with
  stone walls and exercise equipment"), `kitchen` (5, incl. "modern apartment kitchen"),
  `studio` (2, "studio setup"/"modern studio setup"); `apartment` (8) and `indoor`
  couch/room conventions follow shard 3. `setting=indoor` was used only where the
  caption states indoors/indoor room/indoor lighting or a couch context, matching the
  shard 1–3 convention; bare rug/floor/wall scenes stay `null`.
- Publication: `.ready` sidecar written via
  `scripts.token_sprint.coordinator.protocol.write_ready` (atomic; producer
  `w1-stagec-executor`); ready-file SHA-256
  `5e7e7d4babcc9a85da0c98306cd611cef5dd200dae9c144361f0066540f314aa`; it binds artifact
  `62e560f1dbb6892c89dc147b35facb32460371a5abd5e73e17e5e4874d818e7e`.
- Red lines honored: production DB opened read-only only (`mode=ro` +
  `query_only=ON` + `busy_timeout`), zero writes; no GIF/JPG/video bytes opened; no
  direct model-endpoint code added; `pyproject.toml`/`uv.lock` untouched; existing
  sprint-root artifacts unmodified (only new shard/qc/ready files added); shard/qc/ready
  files stay in the sprint root and are not committed to git (only this report is
  committed, from the worktree, explicit path).
