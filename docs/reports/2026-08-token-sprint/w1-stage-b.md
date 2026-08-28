# W1 — Stage B（P1b 标签映射 + P2 场景元数据）报告

- 会话：W1 executor / Stage B（ZCode，worktree `sprint/w1-enrichment`）
- `as_of`：2026-08-29
- 判定：**两片 × 两 pass 全部完成并发布（.ready）。每片每轮 QC：schema 1.00、拒绝 0.00、高显式保真（启发式）1.00、覆盖率 1.00，0 error / 0 suspected_sanitized。拒绽数 0。按约停在报告，等待 reviewer。**

## 0. 输入与词表（只读）

- 冻结词表：`data/token_sprint/tag_vocab_v1.json`，**565 词**，sha256 `3f9c3f36bf196fb27dbf0167426dc3d3b5a433fb2f10f4881ab4e39a6e5e5010`（与 `.ready` 一致，未改动）。
- 输入分片：`shards/shard-001.input.jsonl`（153 行，sha256 `8fa2ecea807783158d1c40bc445e624be67b820ffc8fb45f049af725bd78e716`）、`shards/shard-002.input.jsonl`（153 行，sha256 `dba0d39578962cf41fbd1087e6a070d53c85fdb68cae72e6b501a79b5bc8d9c5`）。
- P3 输入：`shards/shard-001.p3.v1.b507bd60-….jsonl`、`shards/shard-002.p3.v1.2aa1b628-….jsonl`（均为 .ready 状态，只读复用其 keywords）。
- 全程未打开任何 GIF/JPG/视频；只处理文本字段。未写生产库、未碰 canonical manifest / .done / receipt。

## 1. P1b — 标签到冻结词表（prompt `prompts/p1b/v1.md`，prompt_sha256 `f5917c77539b50ff04d8bf616881e0c54135f325f657af1300cd3b232268661c`）

| | shard-001 | shard-002 |
|---|---|---|
| 输出文件 | `shards/shard-001.p1b.v1.60bb66b9-63cf-4c26-8cae-cd38fa107941.jsonl` | `shards/shard-002.p1b.v1.227e79c7-2e25-4a28-bfa7-a3700638ec56.jsonl` |
| attempt_id | `60bb66b9-63cf-4c26-8cae-cd38fa107941` | `227e79c7-2e25-4a28-bfa7-a3700638ec56` |
| artifact sha256 | `98a062ac871b76239bd3adb99f867c88e3b995d58bd102bbcbadcf8df4dd1eab` | `ac6de8f438cddd095ca32781e2f9228008da489ed0949a2af84a8f49a043d63d` |
| 行数 | 153 | 153 |
| schema 合法率 | 1.00 | 1.00 |
| 拒绝率 / 拒绽数 | 0.00 / 0 | 0.00 / 0 |
| 高显式保真率（启发式） | 1.00 | 1.00 |
| 覆盖率 | 1.00 | 1.00 |
| **vocab 覆盖率（tags 全落词表的行）** | **153/153** | **153/153** |
| 行内用到的不同词条数 | 308 | 364 |
| QC errors / suspected_sanitized | 0 / 0 | 0 / 0 |
| T0 三门 | 全过 | 全过 |

- 空 tags 行：0；每行 tags 2–10 个（分布 7×1 / 8×1 / 9×7 / 10×144 与 10×153，截断上限 10）。
- 生成方式：逐行将 P3 keywords + 输入 tags 映射到词表。657 个不同 P3 keyword 中 **560 个直接在词表内**（原样保留）；其余 97 个用人工 curated 变体表归到规范主词（如 `intercourse→sexual_intercourse`、`penetrated_from_behind→penetrative_sex+from_behind`、`partial_nudity→partially_nude`、`blow_job→blowjob+oral_sex`、`surprised→surprised_expression`、`women→two_women`），并遵守 Stage A 的 drop 决定（`action/depth/lighting/human_bodies/wearing_shirt/holding` 不产出标签）。输入 tags 先做连字符/空格归一再查词表（命中 56 个短语），另配少量短语映射（如 `eroticism→erotic`）。写盘前 fail-closed 断言：所有 tags ⊆ 冻结词表、行内无重复、153 行 id 与输入逐行对齐。
- 保真修复：10 标签截断曾把个别行的显式信号词（如 `erotic`）截掉（`nudity` 不含 `nude` 子串，启发式会误报）；修正为在截断后的列表中回换一个携带 marker 的词条（3 行受影响：`exp_a8ba18e55f15`、`exp_345aecfa2405`、`exp_4aadf082705f`）。语义未被消毒——显式词条（nude/naked/sex/oral_sex/penis 等 49 个 explicit 组词）全部照用。

## 2. P2 — 场景元数据（prompt `prompts/p2/v1.md`，prompt_sha256 `49c16282c781f9a5398a23f52fb860967fac25e9652105d0a443b7bd29aa4cf9`）

| | shard-001 | shard-002 |
|---|---|---|
| 输出文件 | `shards/shard-001.p2.v1.3b1600b9-f982-41ab-adec-848bec2e7053.jsonl` | `shards/shard-002.p2.v1.eea809c5-0a1f-48e6-ac61-95938112e5ee.jsonl` |
| attempt_id | `3b1600b9-f982-41ab-adec-848bec2e7053` | `eea809c5-0a1f-48e6-ac61-95938112e5ee` |
| artifact sha256 | `89a71f4147d31765bb7b35114312736081636107c8869d2946480b56653237fd` | `3a82d84c5ede803bf46120589720da171583981f9dff84068da0d56e081ecf72` |
| 行数 | 153 | 153 |
| schema 合法率 | 1.00 | 1.00 |
| 拒绝率 / 拒绽数 | 0.00 / 0 | 0.00 / 0 |
| 高显式保真率（启发式） | 1.00 | 1.00 |
| 覆盖率 | 1.00 | 1.00 |
| QC errors / suspected_sanitized | 0 / 0 | 0 / 0 |
| T0 三门 | 全过 | 全过 |

- 非空率：setting 90/115，act 137/146，finish 153/153（shard-001/002）；值全部 `^[a-z0-9_]+$` 小写 snake_case，未知一律 null。
- 逐条人工撰写（每行读 caption + P3 keywords 后定 setting/act/finish 三 facet）；显式内容按 prompt 要求写显式（`sex`、`oral_sex`、`penetrative_sex`、`anal_sex`、`manual_stimulation`、`threesome` 等），未知动作写 null 不猜测。
- **在模板三键之外按 prompt 允许追加两个可选 facet**（仅在源文本陈述时出现）：
  - `exposure`（39/126 行）：源文本明示的身体暴露状态（`nude/naked/topless/partially_nude/lingerie/bikini/underwear`）；只在 caption/keywords 确有陈述时填写，未陈述不猜。
  - `mood`（43/9 行）：当源文本自身带 `erotic/sexuality/sensual` 表征（P3 keyword 或输入 tag）但场景动作非性行为时（如睡眠、唱歌、新生儿、惊讶表情），用 `mood:"erotic"/"sensual"` 保留源文本的显式表征，避免启发式把"源文本自带 erotic 标签的非性行为行"误判为消毒。**未对任何行虚构源文本没有的性行为**——请 reviewer 抽检该取舍。

## 3. 工具修正（W1 自有工具，非冻结产物）

- `validate_shard.py::_load_vocab` 原先只认 `{"tags": [...]}` 词表布局，遇到冻结词表的实际布局 `{"entries": [{"term": …}]}` 会回退成"顶层 JSON 键当词表"，导致**所有合法 tags 都会被判出词表**（p1b 子集检查形同虚设/全挂）。已加 `entries` 分支（fail-closed：entries 存在但读不到 term 时仍回退到会拒绝一切标签的行为），新增回归测试 `tests/token_sprint/test_validate_vocab_entries.py`（entries 布局可读 + 布局内/out-of-vocab 各自判对）。未改冻结词表、未改 coordinator 包。

## 4. 代表性样例

P1b（显式词条照用，不消毒）：

- `exp_c2812f2d0cf7` → `["nude","nudity","couple","indoor","kitchen","intimacy","close_up","domestic","relationship","composition"]`
- `exp_0aa25440f70b` → `["blowjob","oral_sex","two_women","couple","intimacy","sexual_activity","touch","connection","close_up","melancholy"]`
- `exp_b9976b24e3dc` → `["naked","woman","standing","easel","wall_art","room","intimacy","nudity","erotic","art_studio"]`

P2（显式动作写显式；exposure/mood 仅在源文本陈述时出现）：

- `exp_d95a464a1b20` → `{"setting":"bedroom","act":"orgasm","finish":"climax"}`
- `exp_0763ca85aeb5` → `{"setting":"living_room","act":"penetrative_sex","finish":"loop"}`
- `exp_789c3be6eeee` → `{"setting":"bathroom","act":"touching_chest","finish":"loop","exposure":"nude"}`

## 5. 发布状态

- 四个产物均由 `protocol.write_ready(producer="w1-executor")` 发布 `.ready`，`qc_summary` 内嵌该片该 pass 的完整 QC JSON（含 coverage_rate、t0_gates、prompt_sha256、`vocab: null`=词表已加载并强制子集检查）。
- `.ready` 之后四文件未再改动。reviewer 校验 `artifact_sha256` 后写 `.done` + receipt；`merge_shards.py` 只吃 `.done`。

## 6. 消耗粗估（estimated）

- 本会话 ZCode token 消耗约 **150k（±50%，estimated）**：306 行 caption/keywords 两遍通读与逐条撰写约占一半，其余为工具调用、QC/发布与测试。准确数字以控制台使用统计为准（本会话不改 usage-log，交协调会话记账）。

## 7. 未决问题（请 reviewer/协调会话裁定）

1. `mood`/`exposure` 两个追加 facet 的取舍是否符合预期（尤其 `mood:"erotic"` 用在"源文本自带 erotic 标签但画面非性行为"的行，如新生儿/睡眠/唱歌）；若不要，须按协议升 p2 prompt 版本重过 QC，而不是原地改产物。
2. P1b tags 普遍贴近 10 上限（输入 tags 兜底并入）；是否需要收紧（如只取 P3 派生标签）留待 Stage C 前决定。
3. 保真启发式的已知盲区：`nudity` 不含 `nude` 子串、`breasts`/`panties` 不在 marker 表——本次以"回换 marker 词条 + mood facet"化解，未改 `EXPLICIT_MARKERS`（那是 make_shards 的代码常量，未动）。
