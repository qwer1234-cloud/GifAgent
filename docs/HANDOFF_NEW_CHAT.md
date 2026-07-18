# GifAgent 新对话交接文档

更新时间：2026-06-27

用途：新开一个 Codex/Agent 对话时，先让对方阅读本文件，再继续实现、评估或讨论 GifAgent。本文是项目状态、关键决策和下一步实施顺序的压缩上下文。

## 新对话启动提示

可以在新对话开头直接发送：

```text
请先阅读 C:\Users\sunhao\Desktop\code\GifAgent\docs\HANDOFF_NEW_CHAT.md，理解 GifAgent 当前项目状态、Preference Memory 方案、LangGraph 重构顺序、运行中数据保护要求和已有实施文档。随后再根据我的新需求继续工作。除非我明确要求，不要修改 data/*.db*、data/*.log、FAISS 文件或正在运行的入库流程。
```

## 项目位置和工作方式

- 工作区：`C:\Users\sunhao\Desktop\code\GifAgent`
- Shell：Windows PowerShell
- 用户主要使用中文沟通。
- 代码改动必须保护当前数据和运行流程，尤其是 SQLite WAL/SHM、日志、FAISS 索引和长时间 RAG/VLM 入库任务。
- 当前仓库可能有运行中产生的 dirty files，例如：
  - `data/adaptive_test_result.json`
  - `data/library.db-shm`
  - `data/library.db-wal`
  - `data/vlm_loop.log`
  - `data/pipeline_stage2.log`
- 除非用户明确要求，不要 stage、删除、重置或覆盖这些运行数据文件。

## 项目一句话

GifAgent 是一个本地影视/GIF 偏好挖掘 Agent：把用户喜欢的 GIF 导入 RAG，使用本地 VLM/LLM/Embedding/FAISS 分析收藏偏好，再从新视频中自动采样、排序并导出符合个人偏好的候选 GIF。

## 当前核心模型和数据栈

- VLM：`llava:13b`，通过 Ollama 本地调用，用于逐帧视觉分析。
- LLM：`hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M`，用于文本综合标注。
- Embedding：`nomic-embed-text:latest`，768 维。
- 向量索引：FAISS，当前向量空间和 `nomic-embed-text` 绑定。
- 数据库：SQLite `data/library.db`。
- 当前 README 记录的规模：约 `9221` 媒体，`1062` 向量。
- 重要约束：即使另一个 embedding 模型也是 768 维，也不能直接混用旧 FAISS。换 embedding 等于新建完整索引版本并做 A/B。

## 当前主流程

```text
收藏 GIF/视频
  -> SHA256/pHash 去重入库
  -> ffmpeg 抽帧
  -> llava:13b 逐帧视觉分析
  -> json_guard + quality 门禁
  -> Qwen3-14B 合成媒体级标签
  -> nomic-embed-text 向量化
  -> FAISS 索引
  -> 新视频 adaptive 测试
  -> VLM 打分 + RAG 检索 + clip 合并
  -> 导出候选 GIF
```

关键脚本：

- `scripts/vlm_loop.py`：稳定 VLM 处理循环，会在 VLM 帧处理完成后自动启动 `scripts/pipeline_stage2.py`。
- `scripts/pipeline_stage2.py`：LLM synthesis 和 FAISS rebuild，当前有 top-level side effects，后续 LangGraph 前需要先服务化。
- `scripts/test_video_adaptive.py`：新视频候选 GIF 测试和导出脚本。
- `scripts/reset_derived_quality_data.py`：重置衍生质量数据，支持 dry-run 和备份。

## 当前 adaptive 测试参数

`scripts/test_video_adaptive.py` 当前配置偏向“为 Preference Memory 收集尽可能多的候选”：

```python
SAMPLE_INTERVAL = 15
REFINE_THRESHOLD = 0.3
WORTHINESS_THRESHOLD = 0.2
MERGE_GAP = 6
OUTPUT_RATIO = 1.0
MAX_OUTPUT = 0
```

含义：

- 更密集采样。
- 更低 worthiness 阈值。
- 更短合并间隔，生成更多独立 clip。
- `OUTPUT_RATIO=1.0` 且 `MAX_OUTPUT=0`，表示最终候选尽量全量输出。

不要在 Preference Memory 第一阶段修改这些参数。Preference Memory 只影响后续 rerank，不改变抽帧、VLM、合并或 GIF 导出策略。

## 已确认产品决策

### Preference Memory 采用 A+C

用户已确认采用：

- A：全局偏好画像。
- C：分场景偏好画像。

不采用“把新 GIF 直接混入主库”的方式。新视频输出的 GIF 应先进入候选层，再由反馈和手动 promote 决定是否进入主收藏库。

### 主数据库稳定，新 GIF 进候选层

可以理解为：

- 主 `media`、`frames`、`annotations`、`vector_refs` 和主 FAISS 不自动改变。
- 新视频候选 GIF 进入新表，例如 `candidate_gifs`、`candidate_vectors`。
- 用户对候选做 `like`、`neutral`、`dislike`、`quality_reject`、`skip`。
- 只有明确 `promote` 的候选才写入主 `media` 和主 FAISS。

### 反馈不是每个视频单独达标

“有效反馈数量”是跨视频累计、去重后的数量，不是每个视频都要单独达到阈值。

建议阈值：

- 粗略首版：总有效反馈 >= 30，like >= 15，dislike >= 10，来源视频 >= 3，单个视频贡献 <= 40%。
- 全局线性模型：100 到 300 条有效反馈，来自 5 到 10 个视频。
- 分场景画像：每个场景 20 到 50 条反馈，且至少来自 3 个视频。

不计入有效权重贡献：

- `neutral`
- `skip`
- `quality_reject`
- API/content blocked
- 重复候选

### 页面可以手动触发画像贡献

建议做成两个按钮或两个阶段：

1. Build candidate profile version：数量达标后手动构建候选画像版本。
2. Publish current profile version：检查分布和 A/B 结果后手动发布为当前版本。

默认建议：构建不等于发布。发布必须是单独确认动作，便于回滚和避免错误反馈污染后续排序。

## Preference Memory 评分思路

RAG 本身不是可训练模型，没有可直接“提取权重”的神经网络权重。可以实现一个独立的偏好评分层：

```text
final_score =
  base_rag_similarity
  + global_like_similarity
  + scenario_like_similarity
  - dislike_similarity
  + optional_diversity_bonus
```

首版建议：

```text
Preference Memory disabled:
  final_score = base_rag_similarity

Preference Memory enabled:
  raw_score =
    0.55 * base_rag_similarity
  + 0.25 * global_like_similarity
  + 0.15 * scenario_like_similarity
  - 0.20 * global_dislike_similarity

  final_score = clamp(raw_score, 0.0, 1.0)
```

要求：

- 缺失分量必须记录 `inactive_reasons`。
- 运行开始时固定 `preference_profile_version`。
- 历史运行候选和评分不可被后续反馈覆盖。
- Profile version 不可变，可重建、可比较、可回滚。

## 成人内容和云 API 决策

用户的视频可能包含成人内容。已讨论结论：

- 敏感或未知内容必须留在本地处理。
- 可以用云 API 的部分，只能是本地安全路由判断为 SAFE 的片段或纯文本任务。
- 最稳妥策略是本地预扫描：
  - ffmpeg 低频抽帧。
  - 本地 NSFW/safety 分类。
  - 片段标记为 `SAFE`、`SENSITIVE`、`UNKNOWN`。
  - `SENSITIVE` 和 `UNKNOWN` 全部走本地。
  - `SAFE` 才允许云端处理。
- 云端只上传压缩安全帧，不上传整段视频和原始路径。
- 如果要求零泄漏，所有视觉工作都必须本地。

当前实施文档中明确：云 API 路由不是 Preference Memory 和 LangGraph 第一轮实施范围。

## LangGraph 决策

已确认实施顺序：

1. 先做 Preference Memory。
2. 再做 LangGraph 重构。

理由：

- Preference Memory 是产品闭环，会直接提升后续个性化精度。
- LangGraph 是编排层，适合在服务边界稳定后接入。
- 现有脚本仍有 top-level side effects，直接 LangGraph 化会把状态管理、断点恢复、DB 事务、Web 事件和错误恢复全部耦合在一起。

LangGraph 的边界：

- LangGraph 只做 orchestration、checkpoint、resume、streaming、human approval gate。
- 不承载长期偏好记忆。
- Checkpointer 独立放在类似 `data/langgraph_checkpoints.sqlite`，不要混入 `library.db` 或 `runs.db`。
- Graph state 只放小 ID 和阶段状态，大 payload 留在 SQLite 或 artifact 目录。

## 已写好的关键设计和实施文档

新对话应优先阅读以下文件：

1. `README.md`
   - 当前项目能力、模型、数据流、API 和脚本说明。

2. `docs/superpowers/specs/2026-06-18-rag-observability-workbench-design.md`
   - RAG 可视化、测试运行、候选层和长期 Preference Memory 的统一设计。

3. `docs/superpowers/plans/2026-06-18-rag-observability-preference-memory-implementation.md`
   - 原始完整实施计划，覆盖 Web 工作台、runs.db、候选层、Preference Memory、UMAP、Docker 等。

4. `docs/superpowers/plans/2026-06-27-preference-memory-first-langgraph-second-implementation.md`
   - 最新阶段化实施计划。
   - 明确先 Preference Memory，后 LangGraph。
   - Phase 1：`P1-1` 到 `P1-7`。
   - Phase 2：`L2-1` 到 `L2-8`。

5. `version2_新样本处理.md`
   - 早期新样本处理和候选层思路，可能有未提交或 dirty 状态。以最新 `docs/superpowers/plans/2026-06-27-preference-memory-first-langgraph-second-implementation.md` 为准。

## 当前实施状态

截至本交接文档：

- VLM + FAISS 重建和/或 RAG 入库流程已基本跑完或正在跑。
- Preference Memory 主要还处于设计和实施文档阶段。
- 候选层、反馈事件、画像构建、rerank 服务、LangGraph 图编排尚未真正落地到代码。
- 最新阶段化计划文件可能仍是未提交状态：
  - `docs/superpowers/plans/2026-06-27-preference-memory-first-langgraph-second-implementation.md`
- 本交接文档本身也可能是未提交状态，执行前先看 `git status --short`。

## 推荐后续执行顺序

### 第一步：等当前入库或测试跑完

如果 `data/library.db-wal` 正在增长，或 VLM/FAISS/RAG 入库正在运行，不要做生产库迁移。

可做的安全工作：

- 写测试。
- 写纯服务代码。
- 用临时 SQLite 测试 schema。
- 写 dry-run CLI。
- 写前端静态页面或类型定义。

不可做的工作：

- 修改生产 `data/library.db`。
- 重建或覆盖主 FAISS。
- 清理 WAL/SHM。
- 改 adaptive 测试参数。

### 第二步：Preference Memory MVP

按最新阶段化计划执行：

```text
P1-1  Freeze Baseline And Protect Production Data
P1-2  Add Candidate And Preference Tables
P1-3  Materialize Run Candidates Into Long-Term Candidates
P1-4  Record Append-Only Feedback Events
P1-5  Build Immutable Global And Scenario Profiles
P1-6  Add Availability-Aware Reranking Behind A Feature Flag
P1-7  Add Holdout Evaluation And Manual Enablement
```

目标：

- 新样本先入 `candidate_gifs`。
- like/dislike 写入 `preference_events`。
- 达标后手动 build profile。
- 手动 publish profile。
- 默认关闭 Preference Memory。
- 关闭状态下输出和 baseline 一致。

### 第三步：服务边界清理

在接 LangGraph 前，先把脚本式流程拆成可调用服务：

- `RunService`
- `CandidateService`
- `FeedbackService`
- `PreferenceMemoryService`
- `PreferenceReranker`
- `PromotionService`
- `ServiceOrchestrator`

重点是去掉 top-level side effects：

- import `scripts/pipeline_stage2.py` 不应自动开始 LLM synthesis 或 FAISS rebuild。
- import `scripts/vlm_loop.py` 不应自动开始 VLM 处理。
- CLI 行为放到 `if __name__ == "__main__"` 下。

### 第四步：LangGraph 重构

只有 Preference Memory 通过 Phase 1 验证后再做：

```text
L2-1  Extract Script Logic Into Side-Effect-Controlled Services
L2-2  Add LangGraph Dependencies And Checkpoint Boundary
L2-3  Define Minimal Graph State
L2-4  Wrap Stable Services As Graph Nodes
L2-5  Build The Gif RAG StateGraph
L2-6  Add Resume And Idempotency Tests
L2-7  Add LangGraph Runner Behind Config Switch
L2-8  Add Human Approval Interrupts Only For Risky Actions
```

默认仍保持 `orchestrator.mode=service`，LangGraph 作为可切换模式上线。

## 测试和验证策略

优先使用临时数据库和 fake inference，不要让自动测试依赖真实 Ollama/VLM。

已有测试：

```powershell
python -m pytest tests/test_indexer_manifest.py tests/test_json_guard.py tests/test_quality.py tests/test_reset_derived_quality_data.py -v
```

Preference Memory 计划中的测试：

```powershell
python -m pytest tests/test_candidate_schema.py tests/test_candidate_materialization.py tests/test_preference_events.py tests/test_preference_profiles.py tests/test_preference_reranker.py tests/test_preference_evaluation.py -v
```

LangGraph 计划中的测试：

```powershell
python -m pytest tests/test_langgraph_state.py tests/test_langgraph_graph.py tests/test_langgraph_resume.py tests/test_langgraph_parity.py tests/test_langgraph_human_gates.py -v
```

安全检查：

```powershell
git status --short
```

不要提交：

- `data/*.db`
- `data/*.db-shm`
- `data/*.db-wal`
- `data/*.log`
- FAISS 二进制索引
- 导出的 GIF
- `.superpowers/`

## 常见风险

### 风险 1：把候选反馈误写进主库

普通 like/dislike 只更新候选层和事件流，不能写主 `media` 或主 FAISS。只有 `promote` 才能进入主收藏。

### 风险 2：用新反馈覆盖历史运行

历史 run candidate 是不可变快照。后续反馈只能生成新事件和新 profile version，不能改历史评分。

### 风险 3：先做 LangGraph 导致大迁移

LangGraph 前必须先完成服务边界。不然会把现有脚本副作用、DB 写入、模型调用和 UI 状态混到一个大图里，难以测试和回滚。

### 风险 4：成人内容误上传云端

任何云 API 路由都必须先有本地安全分类。`UNKNOWN` 默认按敏感处理。

### 风险 5：混用 embedding 空间

不能把其他 embedding 模型的新向量直接写进现有 FAISS。换模型必须新建索引版本，并做兼容检查和 A/B。

## 给新 Agent 的工作原则

1. 先读文档，再改代码。
2. 先看 `git status --short`，不要碰运行数据。
3. 用 `rg` 查找，不要盲改。
4. 手工编辑文件用 `apply_patch`。
5. Preference Memory 第一阶段必须 TDD，所有 DB 测试用临时 SQLite。
6. 不要在生产入库运行时迁移 `data/library.db`。
7. 不要改当前 adaptive 测试参数，除非用户明确要求。
8. 不要提前启用 Preference Memory 默认开关。
9. 不要把 LangGraph checkpointer 和业务数据库混用。
10. 最终回复要明确说明改了哪些文件、跑了哪些验证、哪些没跑。

## 如果用户让你继续实施

建议回复并执行：

```text
我会按 docs/superpowers/plans/2026-06-27-preference-memory-first-langgraph-second-implementation.md 的顺序，从 P1-1 开始。先只做临时库测试和 dry-run，不迁移生产 data/library.db，也不修改当前 adaptive 测试参数。
```

然后从 `P1-1` 开始做：

- 添加 preflight/status。
- 写临时库 schema 测试。
- 创建 Preference Memory 表。
- 增加候选物化。
- 增加 append-only feedback。
- 增加 profile build/publish。
- 增加 disabled baseline-equivalence rerank 测试。

## 如果用户只想讨论

优先围绕这些决策讨论：

- 候选层是否只保存元数据，还是也保存 GIF 文件路径和 preview。
- profile build 按钮是否只 build，还是 build 后允许立即 publish。
- 是否先做 Web Review Queue，再做 UMAP Preference Map。
- 是否保留 Gradio 审核页，还是迁移到 React/Vite 工作台。
- 是否在本地模型升级到 Qwen3-VL-32B 前先完成 Preference Memory。

## 当前推荐路线

最稳路线：

```text
完成当前 RAG 入库
  -> 备份 library.db 和 FAISS
  -> Preference Memory MVP
  -> Web Review Queue
  -> Holdout A/B 评估
  -> 手动发布 profile
  -> 服务边界清理
  -> LangGraph 编排
  -> Docker/WSL 部署强化
```

不要把“模型升级”“云 API”“LangGraph”“Web 工作台全量实现”“Preference Memory”全部混在同一轮改动里。当前最值得优先落地的是 Preference Memory 的候选反馈闭环。
