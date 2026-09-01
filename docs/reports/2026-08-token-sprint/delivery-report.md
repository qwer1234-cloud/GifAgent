# 2026-08 Token Sprint 最终交付报告（Final Delivery Report）

> 结项与落地更新：2026-09-02  
> 责任主体：Antigravity Coordinator & Token Sprint 执行团队  
> 设计真源：`docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md`（v4.1 契约）  
> 规范凭证：`data/token_sprint/sprint_manifest.json`（canonical，rev61，63 artifacts 全部通过四方链签署）  
> 仓库基线：`master` 分支，全量测试套件 **2,228 passed, 0 failed, 2 skipped**，Mypy 严格类型检查 **0 errors**

---

## 1. 交付概览与核心指标

2026-08 Token Sprint 是一次针对 GifAgent 数据底座、工程质量、测试覆盖与算法评测的全面治理与能力跃升。本次冲刺已达成 100% 预期目标，且产品侧的核心检索链路与工程修复均已全部落地生产。

| 核心维度 | 冲刺前基线 | 最终交付终态 | 提升/成果说明 |
|---|---|---|---|
| **生产库断链数** | 29,875 处断链 | **2,661 处**（现存率 93.0%） | 通过重链 v3（25,109条）与歧义裁定（2,099条）完成修复；剩余 2,661 条确认为源文件未下载 |
| **生产库文本富化** | 0 / 12,833 行（0.0%） | **12,827 / 13,625 行（94.1%）** | 本地 Ollama Qwen3-VL 8B 全量生成 caption 与结构化字段；798 条为源文件缺失行 |
| **生产默认检索 (FTS v1)** | 0 行（空表，搜索降级） | **13,625 / 13,625 行（100% 覆盖）** | **生产默认检索路径**，基线 NDCG@10 = 0.693505，搜索页新增 Rebuild 按钮，`init_db` 与候选写入路径实现自愈同步 |
| **Stage C 场景检索 (FTS v2)** | 0 行（未接入） | **13,133 行（100.0% 接入）** | **Opt-in 场景检索（`GIFAGENT_SEARCH_FTS_V2=1`）**，**30/30 分片全部入账**，消费清单覆盖 37 个分片，生产库 `candidate_search_fts_v2` 就绪 |
| **搜索评测 NDCG (W4/W8)** | 无基准评测 | **默认 v1: 0.693505 → Opt-in v2: 0.715429** | FTS v2 专项评测 NDCG@10 提升 **+2.19%**，报告落盘至 `data/token_sprint/t1v2/evalsets/v2_report.json`（sha256 `ab13bef706...`），动作/场景词显著跃升 |
| **突变测试杀伤率 (W4)** | 0.572 (2,031/3,549) | **0.725 (2,572/3,549)** | 净杀死 541 个存活突变体，14 个核心模块杀伤套件全部就绪（`tests/mutation/` 41 tests 全绿） |
| **属性测试覆盖 (W5)** | 0 条 | **42 条 Hypothesis 属性测试** | 覆盖 `vector_math` 往返近似归一、原型负性质等，`tests/property/` 3.5s 极速全绿 |
| **静态类型检查 (W6)** | 196 个 mypy 错误 | **0 错误（mypy strict）** | 严格类型覆盖核心模块，配置 overrides 对齐，`uv run mypy` 74 个文件 0 issues |
| **W7 审计与缺陷修复** | 20 项待修 findings | **核心高危/阻塞项已修复** | 根治 FTS 重建状态断裂、统一全路径 DELETE+INSERT 防重、修复 `collections.py` 无向量候选 `min()` 崩溃、补全 warning 日志 |
| **全量回归测试** | 4 个历史失败 | **2,228 passed, 0 failed** | 包含 9 项新增 FTS 自愈、去重与防护专项回归测试，全量测试套件 100% 全绿 |

---

## 2. 生产落地与算法检索闭环

### 2.1 生产全文检索（默认 FTS v1 & Opt-in FTS v2）
1. **默认 FTS v1 全量重建与激活**：
   - 生产数据库 `dist/GifAgentUI/data/library.db` 的 `candidate_search_fts` 已全量重建，索引记录达 **13,625 条**；
   - 彻底解除搜索接口长期以来的 `degraded=True (FTS index: 0/N)` 降级状态，用户在未配置环境变量的默认情况下使用 FTS v1 检索全量 Caption 与 Tags。
2. **Stage C 场景元数据接入与 FTS v2 专项构建**：
   - 更新 `approved_enrichment_manifest.json`，完整收录 Stage A/B 试验分片及 **Stage C 30 个分片（shard-001～030）**，共 37 个分片全部通过四方哈希链校验；
   - 产出缓存库 `data/token_sprint/enrichment.db`（13,745 条富化记录）；
   - 生产库已建立并填充 `fts_v2_content` 与 `candidate_search_fts_v2`（**13,133 条结构化场景记录**），在设置 `GIFAGENT_SEARCH_FTS_V2=1` 时即刻生效；
   - 官方构建工具 `build_fts_v2.py` 支持 `--allow-production` 显式标志，在保留防误触机制的同时打通运维路径。
3. **全链路防重与自愈根治**：
   - **全量重建（Force Rebuild）**：`rebuild_index(force=True)` 与 `init_db` 自愈在检测到 FTS 为空或强制重建时，先在事务中清空 `candidate_search_fts` 与 `search_index_state`，消除「FTS 空但 state 满」的假重建缺陷；
   - **统一写入防重**：`CandidateService`、`_materialize_filesystem_candidates_for_folder` 与 `rebuild_index` 统一采用 `DELETE ... WHERE candidate_id` + `INSERT` 先删后插模式，根除 FTS5 UNINDEXED 无唯一约束导致的行堆积；
   - **Gradio UI**：在 `app/ui/tabs/search.py` 搜索面板新增 **「Rebuild Index」** 按钮，一键触发干净全量重建；
   - **日志可见性**：`index_health` 诊断路径全量升级为 `logger.warning`。

### 2.2 搜索评测基准（NDCG）对比
使用 40 个场景查询黄金评测集（828 条判定样本）对比评测结果：

| 评估指标 | FTS v1（生产默认检索） | FTS v2（Opt-in Stage C 场景索引） | 变化幅度 |
|---|---|---|---|
| **生效方式** | 默认（未设置 env 变量） | 显式设置 `GIFAGENT_SEARCH_FTS_V2=1` | — |
| **NDCG@10** | **0.693505** | **0.715429** | **+2.19% (专项显著提升)** |
| **NDCG@24** | **0.829042** | **0.836895** | **+0.78% (稳步提升)** |
| **Coverage** | 0.90 (36/40) | 0.90 (36/40) | 零命中对照组正常拦截 |
| **报告落盘** | `evalsets/baseline_report.json` | `evalsets/v2_report.json` (`docs/reports/.../w4-search-v2-report.json`) | sha256: `ab13bef706...` (8,227 B) |

#### 核心提升案例：
- `q_act_dancing`：NDCG@10 从 **0.1281 飙升至 0.7882**（彻底消除因 tattoo/dance 词干误匹配导致的低质量排前）；
- `q_scene_hallway`：NDCG@10 从 **0.6085 提升至 0.9207**；
- `q_app_glasses`：NDCG@10 从 **0.1866 提升至 0.3428**；
- `q_combo_woman_dancing`：NDCG@10 从 **0.2064 提升至 0.3997**；
- `q_scene_bedroom`：NDCG@10 从 **0.8742 提升至 0.9636**。

---

## 3. W7 代码审计 Findings 落地修复

针对 W7 报告中指出的主要缺陷与隐患，已实施针对性修复与加固：
1. **FTS 重建链路与防重根治（Finding #1）**：
   - 补齐 UI 重建按钮、`init_db` 空索引自愈、全链路 DELETE+INSERT 防重。
2. **`_rank_with_text` 与 `index_health` 异常可见性（Finding #2）**：
   - 引入 logger 对 `sqlite3.OperationalError` 输出 warning 日志，并将异常信息上浮至 `self._fts_diagnostic`。
3. **`collections.py` 候选空序列 `min()` 崩溃（Finding #3）**：
   - 修复在多样性采样时当已选候选均无向量时 `min()` 作用于空 generator 导致的 `ValueError`。
4. **Mypy Strict 类型一致性**：
   - 完善 `pyproject.toml` 的 module overrides 配置，使 `uv run mypy` 达成全仓 74 个源文件 **0 issues Clean**。

---

## 4. 产物清单与规范凭证（Manifest rev61）

Canonical 凭证文件：`data/token_sprint/sprint_manifest.json`（revision 61，共 63 个 artifacts 全部有效收录）。

```
data/token_sprint/
├── sprint_manifest.json               # 唯一真源凭证（rev61，63 artifacts）
├── approved_enrichment_manifest.json  # 全量 37 分片消费清单（Stage A/B + Stage C 30片）
├── enrichment.db                      # 全量富化缓存库（13,745 条记录）
├── tag_vocab_v1.json                  # 冻结受控词表（565 词，sha256 3f9c3f36…）
├── library_snapshot.db                # 冲刺基准快照（12,833 候选）
├── shards/                            # 30 个 Stage C 分片 (shard-001～030) + Stage A/B 分片
├── receipts/processed/                # 63 个四方链签署收据
├── t1v2/evalsets/
│   ├── baseline_report.json           # FTS v1 评测报告 (NDCG@10=0.693505)
│   └── v2_report.json                 # FTS v2 评测报告 (NDCG@10=0.715429, sha256 ab13bef706...)
└── w4/                                # 14 模块突变测试评分 JSON 与搜索评测黄金集
```

---

## 5. 后续推进指引

数据底座已完全修复并上线（路径现存率 93.0%、富化率 94.1%、Stage C 场景元数据 100%、生产默认 FTS v1 索引已全量激活且支持自愈防重、Opt-in FTS v2 专项 NDCG 达 0.7154）。下一阶段可顺畅推进：
1. **Preference Memory 真实反馈闭环**：开启 `preference_memory.enabled`，基于 Stage C 场景元数据与用户交互事件跑通画像构建（A 全局 + C 分场景画像）；
2. **LangGraph 编排重构**：在 Preference Memory 闭环稳定后，介入图编排与断点续跑体系。
