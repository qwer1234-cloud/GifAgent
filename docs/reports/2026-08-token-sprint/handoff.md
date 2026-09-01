# Token Sprint 完整交付与交接文档（Final Handoff & Delivery）

> 结项日期：2026-09-02，所有冲刺任务（W1～W8）及产品侧落地行动 100% 结项收官。
> 本文档是唯一交接入口；配套真源：`docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md`（v4.1 契约）、
> `data/token_sprint/sprint_manifest.json`（canonical，rev61，63 artifacts 全收录）、
> `docs/reports/2026-08-token-sprint/delivery-report.md`（最终交付报告）。

## 1. 冲刺与生产拓扑（终态事实）

| 项 | 终态值 | 说明 |
|---|---|---|
| refactor_base_sha | `df3dde0b` | master，重构 agent 的 scoped checkpoint |
| sprint_base_sha | `a6c9fb0a` | 依赖冻结：hypothesis / mypy / cosmic-ray 8.7.0 |
| master HEAD | 最新提交 | 已包含全部 8 个工作包 + 生产重链 + VLM 富化 + Stage C 30/30 分片 + 生产 FTS 全面激活 + W7 审计修复 + 全部报告与测试 |
| canonical manifest | `data/token_sprint/sprint_manifest.json` **rev61，63 artifacts** | 全部通过四方链签署验收 |
| 生产库状态 | `dist/GifAgentUI/data/library.db` | 断链数 29,875 → **2,661**（93.0% 现存）；VLM 富化 **12,827 / 13,625（94.1%）**；默认 FTS v1 索引 **13,625 / 13,625（100%）**；FTS v2 场景索引 **13,133** |
| 检索基线评测 | NDCG@10 | **生产默认 FTS v1: 0.693505 → Opt-in FTS v2: 0.715429 (+2.19%)** |
| 全量回归测试 | `uv run pytest` | **2,228 passed, 0 failed, 2 skipped** |
| Mypy 静态检查 | `uv run mypy` | **0 errors / 74 files clean** |

## 2. 八大工作包与落地成果（W1～W8 全部完成）

1. **W1 数据富化与场景元数据（100% 结项）**：
   - 确定性工具链（`snapshot_library.py` / `make_shards.py` / `validate_shard.py` / `merge_shards.py` / Prompts 体系）；
   - T0 探针两轮：第一轮（空库规范拦截，正确触发兜底），第二轮（exports 语料 306 行三门全通，拒绝 0% / schema 100% / 保真 100%）；
   - 冻结受控词表 `data/token_sprint/tag_vocab_v1.json`（565 词，人工保真审查通过）；
   - **Stage C 全量滚动扩片（30/30 分片全部交付）**：`shard-001`～`shard-030` 覆盖全量 12,827 行已富化行（100.0%），`p2/v2` 严格源文逐字比对 0 失败，全部签署四方链并入账 manifest。
2. **W2 评测体系与 Harness**：
   - `eval_harness.py`（fully-judged NDCG 契约、disputed 排除、SQL spy 锁定不读取 `base_rag_similarity`，支持 `fts_v2` live ranking）；
   - `synth_preference_stress.py`（7 门合成偏好压力测试）；空 evalsets 优雅报告。
3. **W3 历史数据体检与生产库重链**：
   - 规则扫描 30,293 findings（29,874 断链 / 251 缺字段 / 162 mojibake 借词误报排除 / 6 score_oob）；
   - 重链清单演进：v1（4,281）→ v2（16,722）→ **v3 最终执行清单（25,109 条）**；
   - 生产库单事务安全写回 25,109 条重链 + 2,099 条歧义裁定（100% 内容一致），断链直降至 2,661（现存率 93.0%）。
4. **W4 突变测试全量套件与评分（14/14 模块全部完成）**：
   - 14/14 目标模块杀伤套件就绪（`tests/mutation/` 41 tests 全绿）；
   - 全量 3,549 个突变体，杀伤率由基准 0.572 提升至 **0.725**（净杀死 541 个存活突变体）；
   - 搜索基线交付（#4 Deliverable）：40-query bank，828 fully-judged qrels，默认 `fts_v1` NDCG@10=0.693505, Opt-in `fts_v2` NDCG@10=**0.715429**（报告落盘 `data/token_sprint/t1v2/evalsets/v2_report.json`）。
5. **W5 属性测试**：`tests/property/` 42 条 Hypothesis 属性测试全绿（包含 vector_math 往返近似归一、原型负性质等）。
6. **W6 Mypy 严格类型检查**：[tool.mypy] strict 落地，覆盖 `quality_lab`、`task_engine`、`services`，配置 overrides 对齐，`uv run mypy` 74 个文件 0 errors。
7. **W7 定向代码审计与修复**：5 维度 20 findings 报告全部交付；核心阻塞项（FTS 状态断裂根治、全路径 DELETE+INSERT 防重、`collections.py` 候选空序列 `min()` 崩溃、warning 日志全覆盖）全部修复。
8. **W8 影子全文索引与生产落地 FTS v2**：`build_fts_v2.py` 工具（支持 `--allow-production`）+ `approved_enrichment_manifest.json` 覆盖全量 37 分片 + 生产库 `candidate_search_fts_v2` 写入完毕。

## 3. 生产环境落地核销表

| 序号 | 任务项 | 落地结果 | 状态 |
|---|---|---|---|
| 1 | 生产 FTS v1 全量重建 | 生产库 `candidate_search_fts` 达到 13,625 行（100% 覆盖，生产默认路径），解除搜索降级 | **已落地** |
| 2 | Stage C 接入与 FTS v2 构建 | `approved_enrichment_manifest.json` 包含 37 分片，生产库已注入 13,133 条结构化场景元数据，支持 `--allow-production` | **已落地** |
| 3 | 搜索 UI 重建按钮与自愈防重 | Gradio 搜索页增加 Rebuild 按钮；`init_db` 与 `rebuild_index` 走清空重灌；候选导入与重建均走 DELETE+INSERT 彻底去重 | **已落地** |
| 4 | FTS v2 搜索评估与落盘 | live rerun 跑通，NDCG@10 为 **0.715429**（+2.19% 增益），报告落盘至 `v2_report.json` (sha256 `ab13bef706...`) | **已完成** |
| 5 | W7 核心缺陷修复 | 修复 `collections.py` 候选空序列 `min()` 崩溃、`index_health` 诊断日志升级 warning | **已修复** |
| 6 | 静态检查与回归测试 | `uv run mypy` 0 errors；`uv run pytest -q tests/` 2,228 passed 全绿 | **全绿** |

## 4. 快速恢复与验证索引

| 要做什么 | 看什么 / 跑什么 |
|---|---|
| 查阅全景与交付报告 | `docs/reports/2026-08-token-sprint/delivery-report.md` + 本文档 |
| 查任何产物真伪与哈希 | `data/token_sprint/sprint_manifest.json` artifacts[k].artifact_sha256 → 比对 SHA → `receipts/processed/` 查验收据 |
| 跑全量回归测试 | `uv run pytest -q tests/`（**2,228 passed**） |
| 跑 Mypy 静态检查 | `uv run mypy`（**Success: 0 issues**） |
| 查阅突变测试报告 | `docs/reports/2026-08-token-sprint/w4-mutation-scores.md` 与 `w4-surviving.md` |
| 查阅代码审计报告 | `docs/reports/2026-08-token-sprint/w7-summary.md` 及 5 维度专项报告 |
| 查阅 v2 评测报告 | `docs/reports/2026-08-token-sprint/w4-search-v2-report.json` |
