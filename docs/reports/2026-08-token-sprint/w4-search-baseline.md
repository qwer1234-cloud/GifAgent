# W4 搜索基线 — v2 快照上的首个真实 NDCG（w4-search-baseline）

- workstream: W4（分支 `sprint/w4-evalbase`，基于 master `f27567c1`）
- as_of: 2026-08-29（W4 executor 会话）
- producer: `w4-evalbase-executor`
- 快照: `data/token_sprint/t1v2/library_snapshot.db`，sha256 `b541e634129c…1c58683c`（canonical manifest rev29 的 `library_snapshot_v2` artifact，harness 报告 `manifest_check: "ok"`）

## 1. 结论（原文数字）

`eval_harness.py --ranker fts_v1 --rerun`（live rerun，36/36 fully judged queries，
disputed 排除契约、`base_rag_similarity` 不读取契约均未触碰）：

| 指标 | fts_v1（基线） | hybrid（对照，nomic-embed 0.5/0.5 融合） |
|------|---------------|------------------------------------------|
| **NDCG@10** | **0.693505** | 0.685018 |
| **NDCG@24** | **0.829042** | 0.816118 |
| judged queries | 36 | 36 |
| coverage（bank 40 条） | 0.9 | 0.9 |
| ranker_status | `ok:live_rerun` | `ok:live_rerun` |

- 两次 fts_v1 运行**逐字节一致**（确定性契约实测通过）。
- hybrid 是"在同池 fts 命中集合内重排"的对照：36 条中 16 条 @10 变好、11 条变差、
  9 条持平；最大退化是 `q_emo_excitement`（-1.0：fts_v1 把唯一 3 分候选顶在第 1 位，
  向量相似度把它挤下去）。**总均值上 hybrid 未超过 fts_v1**——本库 caption/tags 文本
  质量高（富化 94.1%），精确词法匹配已经很强。

## 2. 方法

1. **query bank v1**（40 条，`data/token_sprint/t1v2/evalsets/query_bank_v1.jsonl`）：
   从快照 `scenario_keys_json`/`tags_json` 实际分布选取词汇，覆盖五类：
   - 场景词 8 条（bedroom/kitchen/bathroom/poolside/office/couch/hallway/outdoor）
   - 情绪词 6 条（joy/serenity/excitement/tension/longing/intimacy；注意情绪词汇
     主要存在于 `scenario_keys_json`，而 FTS v1 **不索引** scenario——这是刻意的
     词法压力测试组）
   - 外观/衣着 6 条（blonde/glasses/tattoo/redhead/nude/black lingerie）
   - 动作/表情 4 条（kissing/dancing/smiling/shower）
   - 组合查询 8 条（两词 AND：blonde woman、kissing couch、woman glasses、
     poolside woman、office woman、bedroom lingerie、woman mirror、woman dancing）
   - 客体/歧义 4 条（mirror/phone/pool/sunset）
   - **必然零命中对照 4 条**（submarine torpedo / volcano eruption / formula racing /
     penguin colony，逐一验证 0 FTS 命中）
2. **池构建**：新工具 `scripts/token_sprint/w4_pool_builder.py`（本分支新增，未改
   `eval_harness.py`）以 `mode=ro` 只读打开快照，调用与 harness `live_rank_ids`
   完全相同的路径（`LibrarySearchService(embedder=None)` + `SearchQuery(text)`,
   limit=24）取 top-24 池；40 条中 36 条有非空池（828 个候选），4 条零命中对照池为空。
3. **判定**：由本 executor 作为判定模型，只依据 caption/tags/scenario **文本**对照查询
   意图分级（3=高度相关 / 2=部分 / 1=边缘 / 0=不相关），未打开任何媒体字节；对每个
   池内候选给出 full-coverage 判定（fully-judged 契约）。**每条查询池内全部候选都有
   判定**（828/828），等级分布 {0:137, 1:103, 2:105, 3:483}。
   label_tier=silver（单模型单轮，非双模板 gold）；status=judged（无 disputed）。
4. **可追溯**：`pool_version=pool_v2_snapshot_20260830`（自定；语义 = 在 2026-08-29
   冻结的 t1v2 富化快照上、由 fts_v1 top-24 构成的判定池）。`ranker_set_hash=
   sha256:d20cdfe28e88adf65c86a63a2cc704a1b805af5145921328e4b27e7ac9ba2811`
   （= sha256(规范描述串 "w4-search-baseline|pool_version=pool_v2_snapshot_20260830|
   rankers=fts_v1|pool_size=24|snapshot=library_snapshot_v2|judge=single-model-silver")，
   描述串定义在 `w4_pool_builder.py` 常量中）。全部 828 行单一 hash，pool_sources
   记录 fts_v1 1-based rank（hybrid/fts_v2 为 null）。
5. **fail-closed 实测**：build-qrels 首次运行抓出 2 个 grade 文件里的 candidate_id
   抄写错误（coverage mismatch 退出码 2）后修正重建；harness 侧 schema/hash/溯源
   校验全部通过。

## 3. 产出物（均在 `data/token_sprint/t1v2/evalsets/`，gitignore 内，不进库）

| 文件 | sha256（前缀） | 说明 |
|------|---------------|------|
| `query_bank_v1.jsonl` | `a5e8142b…` | 40 条查询（query_id/text/query_text/category/notes） |
| `pool_fts_v1_top24.jsonl` | `c463be43…` | 36 非空池 + 4 空池，含判定用全文（caption/tags/scenario） |
| `grades_batch1..7.json` | （中间产物） | 分批判定记录（intent + per-candidate grade） |
| `qrels_pool_v2_snapshot_20260830.jsonl` | `73f669d4…` | 828 行，schema 完全符合 W2 qrels 契约 |
| `baseline_report.json` | `d61fa0ed…` | harness fts_v1 --rerun 报告（NDCG 原文见 §1） |
| `hybrid_report.json` | `e6cfd002…` | harness hybrid --rerun 对照报告 |
| `pool_fts_v1_top24.render.txt` | （判定阅读用渲染） | 紧凑文本视图 |

## 4. 发现（定性，供后续工作流引用）

1. **词干碰撞是最大失分源**（porter unicode61）：`longing`→`long` 使
   "longing" 查询的 24 个候选全部是 "long hair" 内容（NDCG@10=0.0，0/24 含
   longing 语义）；`glasses`→`glass` 使 glasses 查询前 24 名大半是玻璃门窗/酒杯
   （@10=0.187）；`dance` 纹身让 "dancing" 查询前 6 名全部是纹身特写（@10=0.128）。
   `office` 池混入 "officer(s)"（jail interrogation 场景，5/24 判 0）。
2. **FTS v1 同时索引 `source_path`**：5 个 "redhead" 命中纯来自源文件名
   （`…busty.redhead.trying…`），caption/tags 无任何红发证据（判 0）。文件名有时
   携带真实元数据、有时是噪声——这是 W8 fts_v2 / 混合排序可以直接改进的点。
3. **场景主次不分**：单场景词查询（kitchen 全部"背景可见厨房"、@10 仅由 1 条 2 分
   支撑）说明词法命中≠场景主语义；bathroom 池大量"bedroom or bathroom"不确定表述。
4. **情绪词查询两极**：`intimacy`（词在 caption/tags 中常见）@10=0.78；而仅存在于
   scenario_keys 的词汇（joy 0/24 caption 命中、serenity 靠 "serene" 副词性出现）
   显著失分。emotion 维度是语义检索（向量/重排）最应发力之处。
5. hybrid 对照显示：在文本字段已高质量富化的库上，朴素 0.5/0.5 向量融合会稀释精确
   词法命中；若引入向量排序应做加权/门控，而不是等权融合。

## 5. 局限（如实声明）

1. **fully-judged 只覆盖 top-24 池**：池外未判定的候选不进入 NDCG；一个 ranker 若把
   相关候选排在 24 名之外，该候选按"unranked-tail"契约贡献 0 DCG 但不新增判定成本
   （harness 固有语义）。因此本基线对"深排/漏检"的惩罚是近似的。
2. **判定者是单一模型单轮**（silver，非双模板 gold，无一致性率）；判定依据是文本而非
   观看媒体，文本与画面不一致的候选（富化错误）会按文本判——这是本基线的定义而非缺陷。
3. **4 条零命中对照没有池、没有 qrels 行**：它们进入 bank 计数（coverage 分母 40），
   但 NDCG 均值只来自 36 条有池查询；"零命中查询如何计分"契约上属于 open question。
4. 查询为英文单词/双词组合，未覆盖自然语言长句与多意图查询；40 条规模仅达计划契约
   （100–200）下限的 1/5，NDCG 均值的标准误不小。
5. hybrid 对照依赖快照中 9,328/13,625 条 candidate_vectors（nomic-embed-text:latest,
   768 维）与 WSL Ollama live embedder；无向量候选不参与 hybrid 融合（服务端语义）。

## 6. 红线遵守

- 未打开任何 GIF/JPG/视频字节（全程 SQLite 只读文本 + 磁盘上的 JSON/MD）。
- 快照/生产库只读（`mode=ro`；pool 构建与 harness rerun 均只读路径）。
- 未修改 `eval_harness.py` / `synth_preference_stress.py`（未发现需要登记的 bug；
  fail-closed 行为符合测试契约）；未触碰 pyproject/uv.lock；无 git add -A。
- 唯一代码新增：`scripts/token_sprint/w4_pool_builder.py`（本分支）。
