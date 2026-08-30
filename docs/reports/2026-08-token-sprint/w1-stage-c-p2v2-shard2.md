# W1 Stage C — P2 scene metadata, second p2/v2 shard (rows 180–379)

- Date: 2026-08-29
- Executor: w1-stagec-executor (Stage C W1 data-track ramp, shard 2; retry run — the
  previous attempt was lost to a stream interruption before any artifact landed, so this
  run re-used the already-generated input shard and produced everything fresh)
- Branch: `sprint/w1-stagec2` (worktree `.worktrees/sprint-w1-stagec2`, off `master` @
  `f27567c1`; branch/worktree already existed with no new commits and was re-used)
- Standard: same schema and gates as the first p2/v2 shard
  (`w1-stage-c-p2v2.md`); manifest rev15 ruling — `prompt_version p2/v2`, optional
  facets (`exposure`, `mood`) restricted to source-verbatim mapping of the input caption.

## 1. Prompt p2/v2 (frozen, unchanged)

- File: `scripts/token_sprint/prompts/p2/v2.md`, byte-identical to the first shard run;
  NOT modified. Canonical (committed blob, LF) SHA-256:
  `01a161b0bee5e2aaa87550209da0b9cef3e263c9c79a329a3c2d27f0c05ced69`
- Checkout artifact note: with `core.autocrlf=true`, a fresh `git worktree add`
  materializes the file with CRLF, whose raw bytes hash to
  `d4971361216d4689376c1b065fc4a47aae93a6f50be342dede0b5572d11843a7`. For QC the
  worktree copy was normalized back to the canonical LF bytes (content unchanged), so
  `prompts.prompt_sha256("p2","v2")` and the validator reported the frozen
  `01a161b0…` hash; the file was restored to the stock checkout afterwards.

## 2. Input set (no production DB access this run)

- Input shard (already on disk from the shard prep step, re-used as instructed):
  `data/token_sprint/shards/shard-w1stagec-002.input.jsonl` — 200 lines of
  `{rowid, candidate_id, caption, enriched_by}` (rowids 180–379, caption-only).
- Production database was not opened this run (red line: read-only if touched; no
  media bytes read anywhere).

## 3. Output shard

- File: `data/token_sprint/shards/shard-002.p2.v2.fb1b7674-b598-4b24-b8b2-6d023b918f36.jsonl`
- Lines: 200 (one per input row, input order; candidate_ids copied verbatim)
- SHA-256: `695c7cddd15efd75ce4116c9dea0f21b4f9e35358e1a9a48a08b2e783bfad6ab`
- Annotation model: this harness session (no new model endpoints; no GLM/Zhipu calls).
- Written incrementally in 8 chunks of 25 lines with atomic tmp+`os.replace` flushes.
- Two transcription defects were caught by the pre-publication mechanical QC and fixed
  in place before validation/publication: line 16 `mood: "smiling"` → `"smiles"`
  (verbatim rule), and a candidate_id swap between lines 112/113 (scenes stayed with
  their captions; ids re-aligned).

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

Exit code 0 with `--t0-gate`.

### 4.2 Gate results

| Gate | Standard | Result | Verdict |
| --- | --- | --- | --- |
| Schema compliance | >= 98% | 100% by validator p2 check; 100% under a stricter manual check (per-line `prompt_version`/`prompt_sha256` exact, `candidate_id` order matches input 200/200, optional facets str-or-absent never null, all values snake_case or null) | PASS |
| Refusal rate | <= 15% | 0% (0 refusals; the seven 1win brand-graphic rows output all-null scenes instead of refusing — nothing was guessed, same precedent as shard 1) | PASS |
| Fidelity | full-facet mechanical verbatim check over ALL optional facets (0 failures required) + seed=20260829 20-row semantic sample | 170 facets checked mechanically, 0 failures; 20 rows sampled, 0 verbatim failures, 0 semantic errors (see 4.3) | PASS |

Optional facet emission over all 200 rows: 170 facets (147 `exposure`, 23 `mood`);
156/200 rows carry at least one optional facet. `finish` was emitted on 4 rows
(`climax`, where the caption states ejaculation/cum) and null elsewhere; `setting`
null on 13 rows (7 promo graphics + 6 no-location rows), `act` null on 10 rows
(7 promo graphics + 3 rows whose captions name no act — not guessed).

### 4.3 Fidelity sample table (random.Random(20260829), 20 of 200)

| Line | candidate_id | Facets emitted | Caption evidence (fragment) | Verbatim | Semantics |
| --- | --- | --- | --- | --- | --- |
| 8 | cand_fs_800f6815bd02315cee004f525529f7cc7cc8c535b6c6ca5f3ec6744ab9cd71f9 | exposure=nude, mood=pleasure | "nude woman … sexual intercourse with a shirtless man"; "expressions of pleasure and exertion" | OK/OK | OK |
| 9 | cand_fs_c94011697fd28847482e7914cab7bedf68f7066498dccaa71fb2209294f888cd | exposure=bare | "massages their bare back and buttocks" | OK | OK |
| 11 | cand_fs_863129f8d28fb9aa649db289d59968ec6a41b89ccbb25a9790295de18a182196 | exposure=nude | "A nude man and woman are in a modern kitchen" | OK | OK |
| 15 | cand_fs_4affeaa4e8b8aad4b1d503832097fd748c423c564e58b55c0cc81067305989b9 | exposure=nude, mood=playful | "nude woman … smiling and playfully posing while cum drips down her torso" | OK/OK | OK |
| 17 | cand_fs_15e9dcd82c2c3cd63449dab295dd2ad79f96f316ae1455f1b84fe3965f352bbb | exposure=nude | "nude woman … being penetrated from behind while cum drips onto her chest" | OK | OK |
| 18 | cand_fs_3a451ce298a5c8a28ee4207e1901c14d7fd7ea4a57a687c30414954b93b910c2 | exposure=nude | "A nude woman lies on her back on a bed … penetrates her from behind" | OK | OK |
| 36 | cand_fs_d08b50512b55cd979f4e05087d7933cb2ae71bd7ecd30abef54f0f48f1716b89 | (none) | no dress-state or tone word verbatim in caption -> correct omission | n/a | OK |
| 37 | cand_fs_aaa661aaeb8726b2513effee0695d1d70084becbcc1e98914fceb74bb687abed | (none) | no verbatim dress-state/tone word -> correct omission | n/a | OK |
| 42 | cand_fs_518b96e66c89bf4da692a408073450041724c63bbbbfa1078367d8f8d0ec2970 | mood=pleasure | "her expression shifts from surprise to pleasure" | OK | OK |
| 66 | cand_fs_c596aa922bfdec4b5ad6dc42407c4d2791eb8e0124916335aaa5e2f5f68d019f | exposure=nude | "A man and woman are nude on a bed" | OK | OK |
| 88 | cand_fs_b6bbdffe96a518009f428c227813723dc79bc23c22dbfd03719d20af79cd1260 | (none) | 1win promo panels, "No people or bodies are visible" -> all-null scene, not a refusal | n/a | OK |
| 106 | cand_fs_69379389c025281374b5c13f9b97f4a3fe256de78ab8848965e6b1bb1149d5a7 | exposure=shirtless | "straddles a shirtless man … engaged in sexual intercourse" | OK | OK |
| 113 | cand_fs_844d0409a1f45152e8aed0e19e8560004b5778657f16e04319703e366d345e7d | exposure=nude | "Two nude men are engaged in intercourse on a dark couch indoors" | OK | OK |
| 143 | cand_fs_479b53068b0e0c0843c43d0e4c45aa514fb86f85481e579bf2e7898d38e1edc9 | exposure=lingerie | "blonde woman in black lingerie positioned behind him … kiss his neck" | OK | OK |
| 153 | cand_fs_e1e469b224bf270a13e195490f32e805c3bc0cd98eb9c709893e69d0e8036a91 | exposure=bikini | "woman in a light blue bikini top … on a blue blanket outdoors" | OK | OK |
| 155 | cand_fs_7d30dffab03d135d48e76e6adcdc7b3fab68e5e524a3f38eda6c8bd41cb7825e | (none) | display close-up, no verbatim dress-state/tone word -> correct omission | n/a | OK |
| 168 | cand_fs_f8786b62edcdd149f68b505b52496f40b648e9b35290fa7de9fb9f4fdbadc64d | exposure=underwear | "woman in light blue underwear … orally stimulate him" | OK | OK |
| 169 | cand_fs_091919a99bc3ed5348ed077e805a25fc13b38dc151c30d9f2386314a14780fdf | exposure=bikini | "light blue bikini top … rubs her buttocks" | OK | OK |
| 181 | cand_fs_2eee885da4eebb2966d3ff408dfbeff6d1a97564f414c3cf09c1f54ccdf1c855 | exposure=nude | "A nude man kisses and penetrates a nude woman from behind … kitchen counter" | OK | OK |
| 191 | cand_fs_dbacd4c8fc57002e48a57fff679b643ca38102f49b176de28ac50e56e1547d37 | exposure=lingerie | "woman in light blue lingerie … engaging in oral sex" | OK | OK |

## 5. Notes / deviations

- `fidelity_rate_high_stratum` is `null` (denominator 0): the caption-only Stage C input
  carries no `stratum`/`vlm_summary_json`/`tags_json`/`scenario_keys_json` fields, same
  as shard 1; the Stage C fidelity gate is the full mechanical verbatim check (170/170
  optional facets) plus the seed-20260829 manual sample above.
- No `suspected_sanitized` rows; explicit captions were annotated explicitly (hard
  rule 3), including `anal_intercourse`, `penetration`, `oral_sex`, `cum`/climax rows.
- Multi-word verbatim values were used where the caption's exact phrase is multi-word
  (e.g. `breasts_exposed`, `exposed_from_the_waist_up`, `buttocks_exposed`,
  `bare_torso`); single-word states otherwise. `smiles` (not `smiling`) is used where
  the caption literally says "smiles".
- Publication: `.ready` sidecar written via
  `scripts.token_sprint.coordinator.protocol.write_ready` (atomic; producer
  `w1-stagec-executor`); ready-file SHA-256
  `0b8e002dbbace30b21eed7f80e47e694d04689c5b9ed67a29c11431c65e7b722`.
- Red lines honored: production DB untouched this run; no GIF/JPG/video bytes opened;
  no direct model-endpoint code added; `pyproject.toml`/`uv.lock` untouched; existing
  sprint-root artifacts unmodified; shard/qc/ready files stay in the sprint root and
  are not committed to git (only this report is committed).
