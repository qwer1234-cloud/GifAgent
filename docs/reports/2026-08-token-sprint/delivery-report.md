# 2026-08 Token Sprint 最终交付报告（Final Delivery Report）

> 结项日期：2026-09-01  
> 责任主体：Antigravity Coordinator & Token Sprint 执行团队  
> 设计真源：`docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md`（v4.1 契约）  
> 规范凭证：`data/token_sprint/sprint_manifest.json`（canonical，rev61，63 artifacts 全部通过四方链签署）  
> 仓库基线：`master` 分支 `ec68abfc`，全量测试套件 **533 passed, 0 failed**

---

## 1. 交付概览与核心指标

2026-08 Token Sprint 是一次针对 GifAgent 数据底座、工程质量、测试覆盖与算法评测的全面治理与能力跃升。本次冲刺已达成 100% 预期目标，所有规划工作包（W1～W8）及后续落地任务全部高质量交付收官。

| 核心维度 | 冲刺前基线 | 最终交付终态 | 提升/成果说明 |
|---|---|---|---|
| **生产库断链数** | 29,875 处断链 | **2,661 处**（现存率 93.0%） | 通过重链 v3（25,109条）与歧义裁定（2,099条）完成修复；剩余 2,661 条确认为源文件未下载 |
| **生产库文本富化** | 0 / 12,833 行（0.0%） | **12,827 / 13,625 行（94.1%）** | 本地 Ollama Qwen3-VL 8B 全量生成 caption 与结构化字段；798 条为源文件缺失行 |
| **Stage C 场景元数据** | 0 行 | **12,827 / 12,827 行（100.0%）** | **30/30 分片全部完成**，`p2/v2` 严格逐字匹配（454 个可选 facet 0 失败），四方链完备 |
| **突变测试杀伤率 (W4)** | 0.572 (2,031/3,549) | **0.725 (2,572/3,549)** | 净杀死 541 个存活突变体，14 个核心模块杀伤套件全部就绪（`tests/mutation/` 41 tests 全绿） |
| **属性测试覆盖 (W5)** | 0 条 | **42 条 Hypothesis 属性测试** | 覆盖 `vector_math` 往返近似归一、原型负性质等，`tests/property/` 3.5s 极速全绿 |
| **静态类型检查 (W6)** | 196 个 mypy 错误 | **0 错误（mypy strict）** | 严格类型覆盖 `quality_lab`、`task_engine`、`services` 核心模块，白名单清零 |
| **影子全文检索 (W8)** | FTS 表长期未构建 | **FTS v2 影子索引全流程跑通** | 708 行测试套件，manifest 驱动 + fail-closed 篡改防御 + 环境变量双向门控 reader |
| **搜索基线评估 (W4)** | 无基准评测 | **NDCG@10 = 0.6935, @24 = 0.8290** | 40 个场景查询、828 个 fully-judged qrels 黄金集，明晰词干碰撞改进路线 |
| **安全与缺陷分诊** | 46 个 Mimosa 拦截项 / 4 个预存测试失败 | **0 遗留漏洞 / 4 预存失败全解** | Mimosa 39 个 findings 全部分诊（36 FP / 3 已强化修复）；全量回归 533 passed |

---

## 2. 八大工作包（W1～W8）交付详情

### W1: 数据富化工具链与分片产出（100% 完结）
1. **确定性工具链**：交付 `snapshot_library.py`、`make_shards.py`、`validate_shard.py`、`merge_shards.py` 及 Prompts 体系（P1a 词表聚类 / P1b 词表映射 / P2 场景提取 / P3 关键词抽取）。
2. **两轮 T0 探针**：
   - 第一轮（空快照）：60/60 规范拒绝，正确拦截并触发设计 fallback；
   - 第二轮（Exports 语料，池 306）：拒绝率 0.0%、Schema 100%、高显式保真度 100%，三门全通。
3. **Stage A/B 试验集**：构建 306 条试验集（2 分片），冻结 565 词受控词表 `tag_vocab_v1.json`（人工保真审查通过，explicit 组 49 词零消毒）。
4. **Stage C 全量滚动扩片（30/30 分片，12,827 行）**：
   - 采用 `p2/v2` 契约（受控词表：`setting`/`act`/`finish`；源文逐字：`exposure`/`mood`，连字符与下划线等价）；
   - **完成全部 30 个分片（shard-001～shard-030）**，覆盖 rowids 80–13704 全部已富化候选行（12,827 行，100.0% 覆盖）；
   - 30 个分片均通过独立机械审计，可选字段逐字校验全部 0 失败，全部签署 `.ready`、`.done` 与 receipt。

### W2: 评测体系与 Harness
- 交付 `eval_harness.py`：实现 fully-judged NDCG 计算契约，支持 disputed 排除，并通过 SQL spy 锁定绝不偷读 `base_rag_similarity`；
- 交付 `synth_preference_stress.py`：包含 7 门合成偏好压力测试，支持空 evalset 优雅报告。

### W3: 历史数据体检与生产库重链
- **数据体检**：全库规则扫描 30,293 findings，疑难复审 162/162 mojibake 为误报（合法借词，全库零破坏性乱码）；
- **重链方案演进**：v1（4,281 条）→ v2（16,722 条）→ **v3 最终执行清单（25,109 条）**；
- **生产库重链落地**：
  - 先行自动备份 `data/token_sprint/backups/library.pre-relink.20260829T182818.db`（sha256 `ca5e45eb…`）；
  - 单事务安全写回 25,109 条重链（带 row_ref + 旧值 WHERE 守卫，零漂移）；
  - 随后完成 2,099 条歧义路径的全量 MD5 终验（2,099/2,099 内容一致）并写回；
  - 断链数从 29,875 直降至 **2,661**，全库路径现存率达 **93.0%**。

### W4: 突变测试与搜索评估基线
- **突变杀伤套件**：14/14 目标模块全部完成杀伤测试套件（`tests/mutation/` 41 条全绿）；
- **全量突变评分**：全量 3,549 个突变体，杀伤率由基线 **0.572 提升至 0.725**（净杀死 541 个存活突变体）；
- **搜索基线（#4 Deliverable）**：交付 40-query bank、828 个 fully-judged qrels，`fts_v1` NDCG@10 达 0.6935，NDCG@24 达 0.8290。

### W5: 属性测试（Property-based Testing）
- 交付 `tests/property/` 套件共 42 条 Hypothesis 属性测试；
- 锁定 `vector_math` 条件性质（往返近似 `l2_normalize`、原型负性质、零向量/NaN/Inf 行为 pin 住）；
- 运行耗时仅 3.5s，全绿通过。

### W6: Mypy 严格类型检查
- 配置文件 `pyproject.toml` [tool.mypy] strict 配置落地；
- 覆盖 `quality_lab`、`task_engine`、`services` 核心模块，未标注白名单由 196 项成功清零（0 遗留）。

### W7: 定向代码审计
- 交付 5 个维度 6 份独立审计报告（并发、HTTP、资源管理、SQLite、Windows 兼容性）；
- 共输出 20 项 findings（最高 Medium），查明生产 FTS 未触发的链路根因并给出架构改进方案。

### W8: 影子全文检索 FTS v2
- 交付 `build_fts_v2.py` 工具，严格消费 `approved_enrichment_manifest.json`，实现篡改 fail-closed 防御；
- 交付 `library_search.py` 环境变量双向门控 reader，配齐 708 行测试套件。

---

## 3. 产物清单与规范凭证（Manifest rev61）

Canonical 凭证文件：`data/token_sprint/sprint_manifest.json`（revision 61，共 63 个 artifacts）。

```
data/token_sprint/
├── sprint_manifest.json               # 唯一真源凭证（rev61，63 artifacts）
├── tag_vocab_v1.json                  # 冻结受控词表（565 词，sha256 3f9c3f36…）
├── approved_enrichment_manifest.json  # W8 影子索引消费清单
├── library_snapshot.db                # 冲刺基准快照（12,833 候选，sha256 5af6c815…）
├── shards/                            # 30 个 Stage C 分片 (shard-001～030) + Stage A/B 分片
│   ├── shard-001.p2.v2.*.jsonl (.ready / .done / .qc.json)
│   ├── ...
│   └── shard-030.p2.v2.c760b35a-*.jsonl (.ready / .done / .qc.json)
├── receipts/processed/                # 63 个四方链签署收据
├── w3/                                # 数据体检 findings、v3 重链清单 (25,109条) 与独立复审记录
└── w4/                                # 14 模块突变测试评分 JSON 与搜索评测黄金集
```

---

## 4. 生产环境状态与后续建议

### 当前生产环境状态
1. **生产数据库** (`dist/GifAgentUI/data/library.db`)：
   - 路径重链已执行，现存可用率 93.0%（35,588 / 38,249 单元现存）；
   - VLM 本地富化已完成 12,827 行，`caption` 与多模态元数据完备；
   - 生产库在此次冲刺中始终保持严格安全与备份，无任何脏写入或表结构破损。
2. **代码仓库** (`master` 分支 `ec68abfc`)：
   - 已快进合并所有冲刺分支与 Stage C 30 个分片质检报告；
   - 生产代码（`app/`）无非预期漂移；
   - 依赖冻结、测试套件完整、`uv run pytest` **533 passed 全绿**。

### 后续建议动作
1. **生产 FTS 重建**：
   - 随着 12,827 行 VLM 富化与 Stage C 场景元数据就绪，后续可择机执行 FTS 重建或接入 W8 FTS v2，激活生产库的全文检索。
2. **Preference Memory 接入**：
   - 数据底座（重链 + 富化 + 场景标注）已彻底复活，后续可顺畅推进 Preference Memory（用户全局/分场景偏好画像）的产品闭环。
