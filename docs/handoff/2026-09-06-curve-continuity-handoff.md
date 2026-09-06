# 交接文档：人物连续性 / 曲线偏好工作流（截至 2026-09-06）

> 面向接续执行的 agent 或开发者。本文件总结自 2026-09-05 里程碑 0A 启动以来的全部状态、
> 中断点、以及命令级的续作序列。接手前请先读
> `docs/superpowers/plans/2026-09-05-curve-preference-baseline-replay-qualification.md`（当前权威计划）。

## 1. 一句话状态

曲线偏好模型的资格评估 harness（Task 1–8）已全部完成并提交；资格重跑被"696 个训练目标
GIF 已从磁盘删除"结构性阻断；用户已选定修复方案 1（撤销这 696 个候选的反馈事件）；
撤销脚本 `scripts/undo_missing_artifact_feedback.py` 已写好并修复两处字段错误，但
**尚未重跑 dry-run、尚未执行、尚未提交**——这是当前的精确中断点。

## 2. 成果时间线与提交清单（全部在 master）

| 提交 | 内容 |
|---|---|
| `e31a17e6` | 0A T1：冻结 rank_dedup 排序的 characterization 测试 |
| `54bfe663` | 0A T2：spike 契约（manifest/result/backend + 模型加载器前身） |
| `1e753984` | 0A T3：可行性指标聚合与 go/no-go 报告 |
| `7eb17efe` | 0A T4：只读 Ollama/GPU 遥测（含 SSRF 边界校验 getter） |
| `3117314b` | 0A T5：不可变 spike CLI（dry-run 零副作用 / 原子 JSON / 源批次隔离） |
| `e1c1f5a6` | 0A T6：操作员契约文档 |
| `38a1b784` | 0B：真实媒体 spike（YOLOv8n-pose + YuNet 后端、50 视频候选 + 200 GIF、三迭代修缺陷） |
| `89ea8c89` | 历史曲线特征回刷（13,919 行，89.4% 可用，4.09 GPU 小时） |
| `eaaf9a3c` | 曲线模型训练与（首次）发布 |
| `be3fcacd` | 复审修复 R1：配对基线门禁 + 有效事件标签语义 |
| `a2fc3dc4` | 复审修复 R2：前置门禁失败禁发布 + 清单绑定特征/基线/事件身份 |
| `69cf4ef3` | 资格 T1：模型/资格 schema 集中化，训练器只写 DRAFT |
| `db6a9391` | 资格 T2：curve-aware reranker（默认关闭，特征化测试冻结旧行为） |
| `1c1112c4` | 资格 T3：源隔离画像快照服务 |
| `7de36d14` | 资格 T4：可重构证据冻结（enrich 解析抽取 + 逐行验证） |
| `5c9860e5` | 资格 T5：配对源组指标 + 两遍置信度 + fail-closed 决策 |
| `a5c3a7f5` | 资格 T6：资格 CLI 与不可变运行包 |
| `851337f1` | 资格 T7：显式原子 promotion（8 项拒绝路径） |
| `aa72f817` | 资格 T8：预检 + BLOCKED 报告 |
| `56d4b27d` | 证据补齐 driver（work 快照内 VLM + 向量） |
| `01b25b94` | 结构性阻断报告与三个修复选项 |

**未提交（中断点）**：`scripts/undo_missing_artifact_feedback.py`（已修两处
`note` 字段错误，dry-run 待重跑）。

## 3. 生产数据关键事实（数字会随 GUI 使用漂移，执行前重新测量）

- 生产库：`dist/GifAgentUI/data/library.db`（WAL；GUI `GifAgentUI.exe` 正在运行且
  **活跃写入**——补齐期间几分钟内 +90 事件/+131 候选）。只读连接安全；快照易过期。
- 曲线模型：三条记录全部 `DRAFT`（11952 / 14426 / **120400 最新**）；最新模型
  holdout 2,510 行 / 117 源，训练 467 源，零重叠；holdout 内 pairwise 0.6734、
  NDCG@20 0.9617；发布被门禁阻塞（基线覆盖 0/2,510）。
- 标签语义：应用在每次 favorite 后紧跟一条 like；有效标签一律用
  `app.services.preference_events.load_latest_scoring_events`（undone 或被
  correction 取代的事件失效，每候选取最新；非 like/dislike/favorite 整体排除）。
- `curve_features`：13,919 行（curve-v1），12,445 可用；尺寸列已修复为真实源尺寸。
- 资格重放缺口测量（03:0x 时点）：有效训练目标 10,536；有向量 9,446；
  缺向量但 GIF 在盘 1,090（可富化）；**GIF 已删除 696**（`cand_fs_*` 文件夹导入行）。
  最大可达覆盖 9,840 < 10,536 → 画像门禁结构性不可通过。
- 696 行不在模型训练数据内（curve 回刷要求 artifact 存在），**撤销后无需重训模型**。
- 已完成 30/1,090 行富化，存于 `data/curve_preference_qualification/qual-backfill-01/work.db`
  （仅 work 快照；撤销后须新建快照重跑，损失约 3 分钟工作量）。

## 4. 续作操作序列（命令级，按序执行）

```bash
# 1) 撤销脚本 dry-run（先看数字是否合理：候选 ~696，事件数 >= 候选数）
./.venv/Scripts/python.exe -u scripts/undo_missing_artifact_feedback.py \
  --model-version curve-pref-v1-20260905120400

# 2) 数字合理后执行（自动做时间戳备份 + 事务内审计清单 data/backups/artifact-undo-audit.*.json）
./.venv/Scripts/python.exe -u scripts/undo_missing_artifact_feedback.py \
  --model-version curve-pref-v1-20260905120400 --execute

# 3) 提交撤销脚本（本交接文档已单独提交）
git add scripts/undo_missing_artifact_feedback.py
git commit -m "feat: undo feedback for candidates with missing artifacts"

# 4) 重新发现 WSL Ollama 地址（重启后会变）
WSL_IP=$(wsl.exe -d Ubuntu-20.04 -- hostname -I | awk '{print $1}')

# 5) 新建补齐运行（不要 resume qual-backfill-01：撤销后源身份已变，会被拒绝）
#    !! 绝不用管道接 head/tail —— EPIPE 曾把后台进程杀掉
./.venv/Scripts/python.exe -u scripts/backfill_qualification_evidence.py \
  --db dist/GifAgentUI/data/library.db \
  --model-version curve-pref-v1-20260905120400 \
  --work-dir data/curve_preference_qualification \
  --qualification-id qual-backfill-02 \
  --base-url http://$WSL_IP:11434
# 后台运行（run_in_background），监控：data/curve_preference_qualification/qual-backfill-02/backfill_checkpoint.jsonl
# 预计 ~1,214 行 × 5.5s ≈ 2 小时（VLM 首次加载 ~100s）

# 6) GUI 空闲时执行全量资格重放（2,510 行）
./.venv/Scripts/python.exe -u scripts/qualify_curve_preference_model.py \
  --db dist/GifAgentUI/data/library.db \
  --model-version curve-pref-v1-20260905120400 \
  --work-dir data/curve_preference_qualification \
  --base-url http://$WSL_IP:11434

# 7) 用实际数字更新 docs/reports/2026-09-curve-preference-qualification/README.md 第 4 节
#    （GO_FOR_EXPLICIT_PROMOTION / NO_GO_RETRAIN_OR_REDESIGN / BLOCKED_EVIDENCE_INCOMPLETE 三选一）
#    并运行独立校验（重算 bundle SHA 与 metrics；生产库前后逻辑身份一致）

# 8) 停下。promotion 仅在用户明确批准后执行（计划 Task 9）：
./.venv/Scripts/python.exe scripts/promote_curve_preference_model.py \
  --db dist/GifAgentUI/data/library.db \
  --model-version curve-pref-v1-20260905120400 \
  --qualification-dir data/curve_preference_qualification/<qualification_id> --dry-run
# 确认后再去掉 --dry-run
```

注意：资格 CLI 会对比资格前后的源逻辑身份，GUI 活跃写入会导致 NOT COMPLETED——
选 GUI 空闲时段跑第 6 步；中断可按 `run_state.json` 续跑（身份一致时）。

## 5. 基础设施速查

- **主 venv**：`./.venv/Scripts/python.exe`（3.14.2，含全部 app 依赖）——资格 CLI、
  补齐、撤销、训练一律用它。
- **spike venv**：`./.venv-spike/Scripts/python.exe`（3.12 + onnxruntime-directml +
  Pillow）——仅用于 0A/0B 真实媒体后端；不能 import app（缺 yaml 等）。
- **模型文件**：`data/continuity_spike_models/`（yolov8n-pose.onnx、
  face_detection_yunet_2023mar.onnx）。
- **Ollama**：WSL Ubuntu-20.04 内 `ollama serve`，监听 `*:11434`；按需加载
  `huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0`（VLM）与
  `nomic-embed-text:latest`（768 维文本向量）。IP 用
  `wsl.exe -d Ubuntu-20.04 -- hostname -I` 取第一个 token。绝不 `wsl --shutdown`。
- **GPU**：RTX 4070 Ti SUPER 16GB；DirectML 供 spike 后端；峰值实测 4,210 MiB。
- **备份**：`data/backups/`（回刷前 `0ec5cab8...`；撤销/晋升各有时间戳备份）。
- **运行产物**：`data/curve_preference_qualification/`（资格运行包）、
  `data/continuity_spike_runs/`（0B 报告，最终 run-74c8addb…）、
  `data/continuity_spike_manifests/`。

## 6. 工作树保护（用户未提交改动，绝不暂存/覆盖）

`app/pipeline/ranking.py`、`app/pipeline/stages/gif_clip.py`、
`app/pipeline/stages/materialize.py`、`app/routers/candidates.py`、
`app/routers/preference.py`、`app/services/candidate_vectors.py`、
`app/services/candidates.py`、`app/services/preference_evaluation.py`、
`app/services/preference_events.py`、`app/services/preference_memory.py`、
`build_exe.spec`、`scripts/enrich_library_candidates.py`、
`scripts/evaluate_preference.py`、`scripts/rebuild_exe.sh`、
`tests/test_preference_profile_v2.py`、`tests/test_preference_profiles.py`、
`tests/test_rank_pipeline_preference.py`；未跟踪的
`app/services/library_ingest.py`、`scripts/preference_holdout_split.py`、
`tests/conftest.py`、`tests/test_candidate_vector_ingest.py`、
`tests/test_library_ingest.py`、`tests/test_preference_holdout_split.py` 等。
每次提交只 `git add` 本任务文件，先 `git diff --cached --name-only` 复核。

因此本计划的有意偏离（已写入资格 README）：`preference_evaluation.py`/
`preference_memory.py`/`enrich_library_candidates.py` 的计划改动分别落在
`curve_preference_qualification.py` 与 `preference_profile_snapshot.py` 中。

## 7. 安全钩子约束（Mimosa，实测会拦截）

- 源码只能用 Write/Edit 写；Bash heredoc/echo 写源码被拒。
- SQL 必须内联在 execute/executemany 调用处 + 命名参数（:name）；SQL 存变量、
  f-string SQL、f-string 拼 sqlite3.connect 连接串都会被拦。表/列名硬编码。
- 新脚本不得出现 `torch|onnxruntime|glm|ollama stop|ollama run|ollama pull|
  /api/generate|/api/chat|/api/embed` 字样；VLM 调用复用
  `scripts/enrich_library_candidates._ask_vlm` 与 `app.quality_moe.evaluator._contact_sheet`。
- JSON 落盘 `sort_keys=True, allow_nan=False`；SHA-1 被禁用（用 SHA-256）。
- 每个 commit 时钩子报 `scanner_enobufs`（未完成扫描，兼容放行）——建议最终补一次完整审计。

## 8. 验证基线（最近一次全绿状态）

- 曲线五套件：`test_curve_preference_model / _scoring / _qualification /
  _promotion / test_preference_reranker` 合计 **115 passed**。
- mypy：`app/services/curve_preference.py`、
  `app/services/curve_preference_qualification.py` 均 clean。
- 全量非 slow 回归的基线失败（与本工作无关，勿修）：
  `tests/test_active_review_api.py::TestProfilePreview::test_preview_does_not_publish`
  与 `tests/token_sprint/test_snapshot_fts_rebuild.py::TestRebuildContract::test_rebuild_without_clear_skips_when_state_complete`。

## 9. 风险与注意事项

1. GUI 活跃写入 → 快照/身份校验易失败；资格重放选 GUI 空闲时段。
2. 撤销是生产数据变更：脚本自带备份与事务内验证（`remaining` 非空即回滚）；
   审计清单先写事务内再 commit。dry-run 数字与预期（~696）偏差大时停下报告。
3. 撤销后 `favorite_gifs` 会同步删除对应收藏、`candidate_gifs.status` 恢复
   `previous_status`——与 `PreferenceEventService.undo_last_candidate_action` 语义一致。
4. 长后台任务输出**不可接管道**（EPIPE 已杀过一次进程）；checkpoint 保证续跑。
5. promotion 之后生产排序仍未接入 curve（`configs/models.yaml` 未动，
   `app/pipeline/ranking.py` 未改）；shadow 集成需另立计划（见计划文末 Follow-up）。

## 10. 关键文档索引

- 规格：`docs/superpowers/specs/2026-09-04-person-semantic-continuity-body-curve-design.md`（§5.8/§9.6/§12/§17）
- 0A 计划/契约：`docs/superpowers/plans/2026-09-05-continuity-milestone-0a-implementation.md`、
  `docs/reports/2026-09-continuity-spike/README.md`
- 0B 结果 + 回刷 + 模型训练 + 两轮复审：`docs/reports/2026-09-continuity-spike/milestone-0b-findings.md`
- 资格计划 + 运行契约 + 阻断结论：`docs/superpowers/plans/2026-09-05-curve-preference-baseline-replay-qualification.md`、
  `docs/reports/2026-09-curve-preference-qualification/README.md`
- 仓库环境/WSL 说明：`Agent.md`（根目录）
