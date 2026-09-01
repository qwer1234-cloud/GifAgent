# Token Sprint 完整交付与交接文档（Final Handoff & Delivery）

> 结项日期：2026-09-01，所有冲刺任务（W1～W8）及后续行动清单 100% 结项收官。
> 本文档是唯一交接入口；配套真源：`docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md`（v4.1 契约）、
> `data/token_sprint/sprint_manifest.json`（canonical，rev61，63 artifacts 全收录）、
> `docs/reports/2026-08-token-sprint/delivery-report.md`（最终交付报告）。

## 1. 冲刺拓扑（最终状态）

| 项 | 终态值 | 说明 |
|---|---|---|
| refactor_base_sha | `df3dde0b` | master，重构 agent 的 scoped checkpoint |
| sprint_base_sha | `a6c9fb0a` | 依赖冻结：hypothesis / mypy / cosmic-ray 8.7.0 |
| master HEAD | `ec68abfc` | **已包含全部 8 个工作包 + 生产重链 + VLM 富化 + Stage C 30/30 分片 + 全部报告与测试** |
| canonical manifest | `data/token_sprint/sprint_manifest.json` **rev61，63 artifacts** | 全部通过四方链签署验收 |
| 生产库状态 | `dist/GifAgentUI/data/library.db` | 断链数 29,875 → **2,661**（93.0% 现存）；VLM 富化 **12,827 / 13,625（94.1%）** |
| 全量回归测试 | `uv run pytest` | **533 passed, 0 failed**（耗时 ~65s） |

## 2. 八大工作包交付全景（W1～W8 全部 PASS）

1. **W1 数据富化与场景元数据（100% 结项）**：
   - 确定性工具链（`snapshot_library.py` / `make_shards.py` / `validate_shard.py` / `merge_shards.py` / Prompts 体系）；
   - T0 探针两轮：第一轮（空库规范拦截，正确触发兜底），第二轮（exports 语料 306 行三门全通，拒绝 0% / schema 100% / 保真 100%）；
   - 冻结受控词表 `data/token_sprint/tag_vocab_v1.json`（565 词，人工保真审查通过）；
   - **Stage C 全量滚动扩片（30/30 分片全部交付）**：`shard-001`～`shard-030` 覆盖全量 12,827 行已富化行（100.0%），`p2/v2` 严格源文逐字比对 0 失败，全部签署四方链并入账 manifest。
2. **W2 评测体系与 Harness**：
   - `eval_harness.py`（fully-judged NDCG 契约、disputed 排除、SQL spy 锁定不读取 `base_rag_similarity`）；
   - `synth_preference_stress.py`（7 门合成偏好压力测试）；空 evalsets 优雅报告。
3. **W3 历史数据体检与生产库重链**：
   - 规则扫描 30,293 findings（29,874 断链 / 251 缺字段 / 162 mojibake 借词误报排除 / 6 score_oob）；
   - 重链清单演进：v1（4,281）→ v2（16,722）→ **v3 最终执行清单（25,109 条）**；
   - 生产库单事务安全写回 25,109 条重链 + 2,099 条歧义裁定（100% 内容一致），断链直降至 2,661（现存率 93.0%）。
4. **W4 突变测试全量套件与评分（14/14 模块全部完成）**：
   - 14/14 目标模块杀伤套件就绪（`tests/mutation/` 41 tests 全绿）；
   - 全量 3,549 个突变体，杀伤率由基准 0.572 提升至 **0.725**（净杀死 541 个存活突变体）；
   - 搜索基线交付（#4 Deliverable）：40-query bank，828 fully-judged qrels，`fts_v1` NDCG@10=0.6935, NDCG@24=0.8290。
5. **W5 属性测试**：`tests/property/` 42 条 Hypothesis 属性测试全绿（包含 vector_math 往返近似归一、原型负性质等）。
6. **W6 Mypy 严格类型检查**：[tool.mypy] strict 落地，覆盖 `quality_lab`、`task_engine`、`services`，白名单由 196 压缩至 0。
7. **W7 定向代码审计**：5 维度 20 findings 报告（并发、HTTP、资源、SQLite、Windows 兼容性）全部交付。
8. **W8 影子全文索引 FTS v2**：`build_fts_v2.py` 工具（manifest 驱动 + fail-closed 篡改防御）+ `library_search.py` 环境变量门控 reader，708 行测试套件全绿。

## 3. 冲刺后行动清单闭环记录

1. **执行重链（已完成）**：备份 `library.pre-relink.20260829T182818.db` → v3 25,109 条 + 歧义 2,099 条单事务写入，零漂移。
2. **跑生产 VLM 富化管线（已完成）**：本地 Ollama Qwen3-VL 8B Q8 跑通 12,827 行（94.1%），798 行为源文件缺失。
3. **重新快照 + 重跑 T0（已完成）**：v2 快照三门全过（拒绝 0% / schema 100% / 保真 95%），数据轨彻底复活。
4. **Mimosa 安全分诊（已完成）**：39 个 findings 全部分诊（36 FP / 0 真实漏洞 / 3 强化修复，P1=0）。
5. **预存测试失败修复（已完成）**：4 个历史失败全解（3 过时断言 + 富化脚本 localhost 绑定修复）。
6. **Stage C 滚动扩片（已全量完结）**：30/30 分片全部完成，覆盖 12,827 / 12,827 行（100.0%）。

## 4. 运行机制与经验归档

- **四方链纪律**：executor 只写 `.ready`；reviewer 写 `.done` + receipt；`reconcile_receipts.py --sprint-root … --once` 收账；产物不可原地篡改。
- **接力式窗口**：以上一片输入末行 rowid 为基线取「之后全部已富化行」，消除固定 OFFSET 漂移。
- **分批节奏**：500 行 = 10 子批 × 50，每子批 tmp+os.replace 原子落盘 + 逐字断言。
- **连字符≡分隔符裁定**：caption 连字符与 scene 标签下划线等价（thigh-highs → thigh_highs）。
- **Mimosa L3 门**：在主 checkout cwd 内通过 `git -C <wt> commit` 提交，规避 worktree 基线捕获失效。
- **红线遵守**：全周期严守不读媒体字节、不写生产库（仅冲刺后经用户批准由专用脚本单事务写回）、模型只经 harness。

## 5. 快速恢复与验证索引

| 要做什么 | 看什么 / 跑什么 |
|---|---|
| 查阅全景与交付报告 | `docs/reports/2026-08-token-sprint/delivery-report.md` + 本文档 |
| 查任何产物真伪与哈希 | `data/token_sprint/sprint_manifest.json` artifacts[k].artifact_sha256 → 比对 SHA → `receipts/processed/` 查验收据 |
| 跑全量回归测试 | `uv run pytest -q tests/token_sprint/ tests/property/ tests/mutation/ tests/test_adaptive_config.py tests/test_pipeline_facade.py`（**533 passed**） |
| 跑 Mypy 静态检查 | `uv run mypy app/quality_lab app/task_engine app/services`（**0 errors**） |
| 查阅突变测试报告 | `docs/reports/2026-08-token-sprint/w4-mutation-scores.md` 与 `w4-surviving.md` |
| 查阅代码审计报告 | `docs/reports/2026-08-token-sprint/w7-summary.md` 及 5 维度专项报告 |
