# W1 — Stage A（试验集 P3 全量 + P1a 词表候选）报告

- 会话：W1 executor / Stage A（ZCode，worktree `sprint/w1-enrichment`）
- `as_of`：2026-08-29
- 判定：**两片 P3 全量完成并发布（.ready），P1a 词表候选 565 词已发布；按约停在人工保真审查门之前——本会话不冻结 `tag_vocab_v1.json` 正式版。**

## 1. 输入：试验集分片（零模型）

- 语料：exports 可探针池 **306 条**（caption 非空 ∧ GIF exists），`make_export_shards.py --size 153 --out-prefix shard`，分层 high 263 / mid 2 / low 41，层间 round-robin 交错。

| 分片 | 行数 | sha256 | 字段检查 |
|---|---|---|---|
| `shards/shard-001.input.jsonl` | 153 | `8fa2ecea807783158d1c40bc445e624be67b820ffc8fb45f049af725bd78e716` | 通过 |
| `shards/shard-002.input.jsonl` | 153 | `dba0d39578962cf41fbd1087e6a070d53c85fdb68cae72e6b501a79b5bc8d9c5` | 通过 |

- 字段检查口径（一次性脚本，逐行）：6 文本字段齐全（candidate_id / vlm_summary_json / tags_json / scenario_keys_json / final_score / stratum）、三个溯源字段齐全且 `gif_exists=true`、正文（summary/tags/scenario 文本）无路径样文本、嵌入 JSON 可解析、306 个 candidate_id 全局唯一。**问题数 0。**
- 工具已原生支持常规分片（`--size/--out-prefix`），未改动 `make_export_shards.py`。

## 2. P3 全量（两片）

| | shard-001 | shard-002 |
|---|---|---|
| 输出文件 | `shards/shard-001.p3.v1.b507bd60-e000-4b3c-85d3-7136f11a4ff7.jsonl` | `shards/shard-002.p3.v1.2aa1b628-0c01-4eaf-99aa-2f653ed10910.jsonl` |
| attempt_id | `b507bd60-e000-4b3c-85d3-7136f11a4ff7` | `2aa1b628-0c01-4eaf-99aa-2f653ed10910` |
| artifact sha256 | `0bdde0b53646841da68023a891e0b26a2b24b24ad207c743dc96ef5afbc8e4a7` | `92a14acb3e1b56a874415c088713b3a0e4bc52e91147d384070b2ac929ef03fa` |
| 行数 | 153 | 153 |
| 复用探针行 / 新生成 | 60 / 93 | 0 / 153 |
| schema 合法率 | 1.00 | 1.00 |
| 拒绝率 | 0.00 | 0.00 |
| 高显式保真率（启发式） | 1.00 | 1.00 |
| 覆盖率 | 1.00 | 1.00 |
| QC errors / suspected_sanitized | 0 / 0 | 0 / 0 |
| T0 三门 | 全过 | 全过 |

- 复用来源：`shards/shard-probe-export.p3.v1.10b722cd-fdf2-46f6-b1d7-491a36ccb4eb.jsonl`（candidate_id 精确匹配后逐行原样复制，保持输入行序；探针行未重新生成）。全部 60 条探针行都落在 shard-001。
- 新生成 246 行：caption 非空一律产出、无拒绝、无消毒；keywords 3–12 个英文小写 snake_case，与 caption/tags/scenario 语义对齐；高显式行保留显式词或同义干（`sensual`/`erotic`/`nude` 系）。
- prompt：`prompts/p3/v1.md`，`prompt_sha256=9aa8e205007605d5a0806a74fd90f8bc40d81dae8a352a449307b48801b25a45`。
- 组装为脚本化确定性流程（id 精确复制、行序对齐、写前校验 3–12 词/词形/无行内重复），模型只产出 keywords 数据本身。
- `.ready` 已由 `protocol.write_ready(producer="w1-executor")` 发布（qc_summary 含上述全部 QC 数字）。**`.ready` 之后两文件不再改动。**

## 3. P1a 词表候选

- 产物：`data/token_sprint/tag_vocab_v1.candidate.json`（注意 `.candidate.`；正式冻结由协调会话在人工保真审查后做）
- sha256：`3f9c3f36bf196fb27dbf0167426dc3d3b5a433fb2f10f4881ab4e39a6e5e5010`
- `.ready`：同目录 `tag_vocab_v1.candidate.json.ready`（producer=w1-executor）
- **词数 565，在 300–800 契约内**；全部匹配 `^[a-z0-9_]+$`，无重复，0 个零计数词。

### 分组分布

| group | 词数 | group | 词数 |
|---|---|---|---|
| mood | 90 | quality | 35 |
| action | 89 | aesthetic | 32 |
| setting | 70 | subject | 21 |
| body | 55 | object | 16 |
| pose | 54 | | |
| clothing | 54 | | |
| explicit | 49 | **合计** | **565** |

### Top 词（按语料出现行数）

`montage` 282、`intimacy` 257、`woman` 249、`intimate` 192、`desire` 171、`bedroom` 166、`man` 151、`sex` 126、`motion` 122、`close_up` 109、`nude` 109、`body` 105、`hands` 105、`bed` 103、`erotic` 103、`couple` 96、`expression` 86、`nudity` 78、`explicit` 75、`composition` 72。

### 方法论

1. **底座**：306 条 P3 输出 keywords 的全部去重词形（657 个 surface 词）。
2. **归并**：屈折/变体合并为单一规范词条（如 kiss→kissing、smile→smiling、breast→breasts、intercourse→sexual_intercourse、blow_job→blowjob、soft_light→soft_lighting、green_bikini→bikini 等 72 项）；drop 掉 meta 词（`action`/`depth`/`lighting` 等 5 项）。
3. **补充**：从输入文本（caption/tags/scenario）补入 P3 未覆盖的内容词 14 个（`montage`、`composition`、`statue`、`van`、`easel`、`oven`、`counter`、`appliances`、`dancing`、`laughing`、`grooming`、`haircut`、`relationship`、`body`）。
4. **同义处理**：明显同义不合并而保留双词条并加 `see_also`（共 24 个簇），例如 `nude↔nudity↔naked`、`erotic↔sensual↔sensuality`、`oral_sex↔fellatio↔blowjob↔oral_stimulation`、`anal↔anal_penetration↔anus`、`intimate↔intimacy`、`orgasm↔ejaculation↔semen`。
5. **分组**：11 个语义域（explicit/action/pose/body/clothing/setting/object/aesthetic/quality/mood/subject），人工 overrides + 顺序模式规则（人工复核修正了 `fireplace→clothing`、`anticipation→setting`、`relatable→setting` 等 53 处误配）。
6. **count 语义**：306 行中「P3 keywords ∪ 输入文本（caption/core/reason/tags/scenario，轻词干匹配 + 合并变体）」命中该词的行数；`example_candidate_ids` 最多 3 个，已校验全部属于试验集 306 id。
7. 全程零媒体读取、零路径写入词表。

## 4. 契约与红线核对

- 每片 50–200 条：153+153 ✅；编号从 001 起，未覆盖任何已存在产物 ✅
- 复用探针行未重新生成 ✅（60 行逐字节复制）
- schema ≥95% / 拒绝 ≤15% / 保真 ≥90%：两片全部 1.00/0.00/1.00 ✅
- 词表 300–800 ✅（565）；只写 `.candidate.` 文件，不冻结正式版 ✅
- 不碰 12,833 空 snapshot 行、不读 GIF/JPG/MP4、不写生产库、无 `uv add`、不改 coordinator/ 与已有 .ready/.done ✅
- 未做 P1b/P2（Stage B 留待词表冻结后另派）✅

## 5. 交接给协调会话

1. **人工保真审查门**：审查 `tag_vocab_v1.candidate.json`（565 词）与两片 P3 输出后，由协调会话冻结 `tag_vocab_v1.json` 并把 SHA 记入 approved enrichment manifest；冻结前 Stage B（P1b/P2）不开工。
2. reviewer 可对两片 P3 输出走 `validate_shard.py` + `.done` + receipt 四方链（`.ready` 已就绪）。
3. 已知取舍（供审查参考）：a) 探针合成 tags 在少量非成人画面（如母婴、圣诞摆拍）也带 `erotic` 字样，P3 保留源词以过保真门，语义保真以源文本为准；b) 词表 `count` 是「行命中数」，含输入文本命中，非纯 keyword 频次；c) `montage` 等场景键来自 scenario_keys_json，几乎全场命中（282 行）。

## 6. 消耗（estimated）

本会话 ZCode token 消耗粗估 ~80k（读入两片摘要 + 生成 246 行 keywords + 词表整理；确定性脚本与 QC 为零模型）。
