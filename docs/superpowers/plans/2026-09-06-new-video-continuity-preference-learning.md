# 新视频动作截取、曲线排序与自动学习 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 经用户明确授权后也可使用 superpowers:subagent-driven-development；本文不授权自动委派。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让新增原视频逐步具备动作连续性筛选、曲线偏好排序和反馈驱动自动训练能力；任何未取得资格的组件都不能悄悄改变生产输出。

**Architecture:** 三个可独立验收的子项目共用候选身份、不可变证据和配置快照。A 负责数据留存，B 负责连续性分析与人工复核，C 负责曲线影子排序及训练任务。代码部署、资格发布、线上启用是三个分离动作。

**Tech Stack:** 现有 Python / SQLite / NumPy / ffmpeg / Ollama / task_engine；重视觉推理放外置进程，不进入 PyInstaller 主程序依赖闭包。

**Spec:**

- `docs/superpowers/specs/2026-09-04-person-semantic-continuity-body-curve-design.md`，特别是 §5–10、§13、§15–18。
- `docs/superpowers/plans/2026-09-05-curve-preference-baseline-replay-qualification.md`：已实现资格评估与显式晋升契约。
- `docs/reports/2026-09-continuity-spike/milestone-0b-findings.md`：能力缺口，以当前实测为准，不以旧文档的完成声明为准。
- `data/curve_preference_qualification/qual-full-20260906-v3/metrics.json`：既有模型尚不合格，不是新配置的资格证明。

日期：2026-09-06。核对基线：HEAD `d08049a2` 加当前用户工作树；不是干净提交快照。本文只新增实施文档，未执行代码、配置、数据库或模型状态变更。

## Global Constraints

- 强保证是统计资格加 fail-closed 流程，不是零错误承诺。
- 原视频可访问且哈希匹配才允许取得 source-video 连续性资格；历史 GIF 不补造上下文、不追认 `AUTO_PASS`。
- GLM-5.3-Flash 只作为 Harness 内开发与显式辅助复核工具；没有生产 API 依赖，也没有等待其配额的排期。
- `AUTO_PASS`、`REVIEW_REQUIRED`、`HARD_REJECT`、`MANUAL_PASS` 保持独立。人工放行不绕过 Quality MoE 其他硬门。
- 最多两个共同主角；需要建模的主体达到三人或以上时 `primary_subject_ambiguous` → REVIEW。
- 姿态覆盖率初始 95%，最大连续缺口 0.25 秒，8 FPS 基础采样、16 FPS burst，前后各 2 秒上下文不得越过已确认镜头边界。
- 低幅度不等于低质量；证据无法观察必须显式记缺失。
- 面部外观属于 `appearance_identity`；人体 ReID 和人脸 ReID 不能被计作两个独立组。表情动态 `record_only`，不推断心理状态或同意与否。
- 镜头断裂或不同独立组确认同一失效才可能 HARD_REJECT；单一几何、幅度异常只进入 REVIEW。
- 首版正式 enforce：真实不合格样本错误自动放行率单侧 95% 精确上界 ≤0.5%；598 仅是零误放且独立样本时的最低数量，不是任意候选数。critical 失效族每族至少 50 个真实样本。
- 正样本 AUTO_PASS 覆盖率 ≥40%，代表性全体 REVIEW 比例 ≤60%；安全与可用性必须同时通过。150 个零误放负样本约 2% 上界只支持开发 checkpoint。
- 候选 p50 ≤10 秒、p95 ≤30 秒，视频 p95 ≤1,200 秒，峰值 VRAM ≤14 GB；完整模型链重新测量，不能沿用只跑姿态后端的成绩。
- 曲线名义权重 0.10 × 合格置信度。曲线不得抵消质量硬门；缺失、版本不符、零置信度时停用该通道，不填 0.5。
- 不变更历史 GIF、收藏、反馈或候选业务状态。写库前一致性备份，迁移幂等；不得 `git add .`、批量清理工作树或重新回刷全部历史库。
- 本计划只授权未来按任务实现的范围说明；生产开关、真实媒体大批量运行、模型晋升仍须相应明确授权。

---

## 1. 当前现状与需要交付的区别

| 部分 | 本次核对到的现状 | 本计划补齐 |
|---|---|---|
| 动作截取 | `materialize_action_candidates` → snap → dedup → Quality MoE → 排序 → 编码 | 完整时序证据、资格门禁、去重替补和复核续跑 |
| direct | `app/pipeline/direct.py` 已调用共享 stage | 在共享 stage 接一次，验证 direct 与 staged 一致，不再复制算法 |
| 连续性 | spike 50 项中 2 AUTO_PASS / 48 REVIEW；缺人工质量标签、完整 ReID/VLM 实测 | 先修证据适用性与复测；通过 go/no-go 再进入 B 的生产阶段 |
| 曲线 | reranker 支持可选曲线参数，生产 ranking 调用未传入 | 每候选提供兼容特征，固定已获资格模型，影子/启用两条输出 |
| 配置 | 工作树 `preference_memory.enabled=false`、权重 0.50/0.50 | 固定运行时实际配置；不得将旧 0.70/0.30 包用于其他配置 |
| 数据 | `curve_features`、模型/资格表和成功 GIF 入库已有 | 编码前候选账本、独立基线、原始向量、失败状态与正式 ID 映射 |
| 训练 | 训练/资格/晋升 CLI 已有；训练脚本绑定 `curve-v1` | 参数化特征域、自动触发、可恢复任务和待批准更新 |

### 1.1 排期和停点

```text
A1 身份与配置 → A2 证据账本 → A3 原视频曲线特征 ─→ C1 影子排序
                                     │                  │
                                     └→ C2 快照/训练 → C3 自动调度 → C4 受控启用

B0 人工标签 + spike 修正 → GO 复核 → B1–B3 生产连续性链 → B4 安全/可用性资格
```

A 和 C 的基础设施不等待当前曲线模型获准发布。B0 未通过时不得把 B1–B4 当作已批准生产工单：这些阶段在本文中是条件式验收合同，B0 报告复核通过后才冻结具体后端实现任务。这遵守原设计“spike 后再写生产执行计划”的止损原则。

首次可交付版本是 A1–A3 + C1：新视频留存可训练数据、显示影子差异，但旧排序和旧截取行为不变。曲线功能暂未启用时 UI 必须明确标记，不声称三项能力全部上线。

## 2. 新增契约与边界决策

### 2.1 两类标签和两类输入域

- `preference_events` 继续保存个人喜好，使用 `load_latest_scoring_events` 的有效事件语义；未评分不等于 dislike。
- 连续性人工标注另存 `continuity_review_events` 或 B0 不可变标注文件；like/favorite 不作为动作完整性真值。
- 历史 `curve-v1` 保持 GIF 域。新增 `curve-source-v1` 表示原视频候选窗口域，特征只取实际输出窗口，不混入前后上下文；上下文仅服务连续性。
- 不将历史模型直接用于新域。训练与资格 CLI 必须支持按 feature version、extractor hash、decode profile 精确选择；不足则保留 DRAFT。
- 即便字段同名，新增幅度或表情字段也不能插入旧模型向量。新增特征定义产生新版本、开发集消融和独立资格评估。
- 同一候选的 source 特征在导出前冻结、导出后关联实际 GIF 和反馈；`artifact_sha256` 不得拿整段源哈希冒充 GIF 哈希。

### 2.2 建议新模块与持久化载体

下列路径为**拟新增**，不是当前已有 API。

| 文件 | 单一职责 |
|---|---|
| `app/services/candidate_capture.py` | 候选 key、运行快照、幂等证据账本与正式 ID 映射 |
| `app/services/curve_source_features.py` | 原视频窗口特征请求/结果校验，不加载重模型 |
| `app/services/curve_online.py` | 固定部署版本、在线资格校验、影子/有效分数 |
| `app/services/curve_training.py` | 从不可变快照训练 DRAFT；CLI 仅做参数适配 |
| `app/services/learning_jobs.py` | 学习任务互斥、租约、检查点、取消与恢复 |
| `app/services/continuity/` | B 获准后实现配置、策略、证据和复核领域服务 |
| `scripts/run_learning_worker.py` | 独立后台 worker 入口，不运行在 GUI 请求线程 |

通过现有 `apply_preference_schema` 幂等入口建立三个增量载体，不修改 `candidate_gifs.status` 的 CHECK：

1. `candidate_analysis_records`：`record_id` 主键；unique `(run_id, preexport_key, capture_version)`；记录 full key、源真实 SHA、窗口、anchor、配置和模型身份、外部 payload 路径/hash、capture status、失败原因。payload 包含全部特征和分数快照；正式 GIF 与库 ID 通过同事务维护的映射表 `candidate_analysis_publications` 关联，允许不同 run 关联同一个正式候选。
2. `learning_jobs`：任务类型、唯一 `input_snapshot_sha256 + policy_sha256`、阶段、owner、lease、尝试次数、下一次重试时间、结果包路径/hash、错误码。阶段固定 `SNAPSHOT/TRAIN/QUALIFY/COMPLETE`；状态固定 `PENDING/RUNNING/RETRY_WAIT/NEEDS_ATTENTION/SUCCEEDED/CANCELLED`。
3. `curve_model_deployments`：追加式部署 revision、model/qualification/config/extractor hashes、批准人/时间、previous revision；单独 current 指针原子切换。`PUBLISHED` 不是自动 active；关闭功能不删除资格记录。

每项 schema 变更配套升级前/后计数和完整性测试。外部文件先写临时文件、计算 SHA、原子 rename，再短事务登记；文件存在但事务失败可重试，不用长事务等待推理。

### 2.3 配置增量（建议值，不是生产启用）

保留当前 `preference_memory` 配置，不修改既有 0.50/0.50 权重。新增配置统一经 `app/config.py` 和 `app/pipeline/config.py` 校验、快照；curve 开关与原设计 `continuity_gate.curve_preference.enabled` 对齐，**不另建第二套 enabled**。

```yaml
candidate_capture:
  enabled: false
  version: capture-v1
  failure_policy: record_and_retry
continuity_gate:
  enabled: false
  mode: shadow
  curve_preference:
    enabled: false
    mode: shadow             # shadow | active
    feature_version: curve-source-v1
    model_weight: 0.10
    deployment_revision: null
curve_learning:
  enabled: false
  min_new_effective_candidates: 100
  min_new_source_groups: 20
  min_positive_candidates: 20
  min_negative_candidates: 20
  min_interval_hours: 24
  max_concurrent_jobs: 1
  promotion_policy: manual
```

这些新增量和时间值是调度起点，不是模型有效性的统计证据；冷启动不足时返回 `INSUFFICIENT_NEW_FEEDBACK`。active 曲线必须同时启用 Preference Memory、拥有匹配部署资格，并且本次候选为 AUTO_PASS/MANUAL_PASS；否则禁止曲线影响生产分数。shadow 可对旧导出候选做诊断，但标记 `quality_guarantee_active=false`。

配置加载器要求 continuity enforce 或 curve active 同时开启 candidate_capture；否则拒绝启动对应模式。资格使用规范化的**算法配置 hash**（采样、模型、阈值、打分权重等），排除资格文件路径、资格自身 hash、enabled/mode 和部署 revision，避免自引用；完整运行配置另存 hash。允许 shadow→active 的模式变化不等于允许算法配置变化。

## 3. 子项目 A：新视频训练数据闭环

### Task A1：固定源身份、候选身份和实际配置

**Files:** 新建 `app/services/candidate_capture.py`、`tests/test_candidate_capture.py`；修改 `app/pipeline/ranking.py`、`app/pipeline/config.py`、`app/config.py`、`app/pipeline/stages/rank_dedup.py`。

**Interfaces:** `preexport_key(source_sha256: str, start_us: int, end_us: int, anchor_us: int) -> str`；`capture_config(snapshot: dict) -> dict` 返回 hash 与规范化配置。微秒转换采用 Decimal 四舍五入，拒绝 bool、非有限时间、end≤start、anchor 越界。没有语义 anchor 时以窗口中点为明确 anchor，并记录其来源。

- [ ] 先写并运行以下失败测试：

```python
def test_key_ignores_list_order():
    from app.services.candidate_capture import preexport_key
    a = preexport_key("a" * 64, 1_000_000, 4_000_000, 2_500_000)
    b = preexport_key("a" * 64, 1_000_000, 4_000_000, 2_500_000)
    assert a == b and len(a) == 64
    assert a != preexport_key("a" * 64, 1_000_000, 4_000_001, 2_500_000)
```

- [ ] 按原设计 canonical JSON（键排序、无空白、时间整数）SHA-256 实现；full key 入账本。旧 16 位展示 ID 映射要检测碰撞，不允许两个 full key 静默合并。
- [ ] capture 关闭时保持旧 ID/排序行为；新启用任务使用 versioned 身份策略，旧 manifest resume 继续按其冻结版本执行。不得改写历史 candidate_id。source 字节哈希每视频计算一次；已有路径指纹不能充当统计源身份。
- [ ] 检查 snap 后窗口写入 key，实际运行时解析配置而非任意同名 YAML 文件入快照。添加 reordered 候选、同名不同源、窗口改变、无效时间、旧 manifest 恢复测试。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_candidate_capture.py tests/test_direct_staged_parity.py tests/test_adaptive_config.py -q`；通过后只提交本任务文件，提交说明 `feat: freeze candidate capture identity`。

### Task A2：无曲线基线、实际向量与正式入库关联

**Files:** 修改 `candidate_capture.py`、`app/services/preference_schema.py`、`app/pipeline/ranking.py`、`app/pipeline/stages/gif_clip.py`、`app/pipeline/stages/materialize.py`、`app/services/library_ingest.py`；新建 `tests/test_candidate_capture_ingest.py`。

**Interfaces:** `validate_capture(payload: dict) -> list[str]`；`persist_capture(conn, payload: dict, evidence_root: Path) -> str` 返回 record ID；`link_publication(conn, record_id: str, candidate_id: str, gif_sha256: str) -> None`。Path 使用 `pathlib.Path`，conn 使用 `sqlite3.Connection`。

- [ ] 先用最小失败测试禁止伪造完整证据：

```python
def test_empty_capture_is_not_complete():
    from app.services.candidate_capture import validate_capture
    reasons = validate_capture({})
    assert "source_identity_missing" in reasons
    assert "baseline_missing" in reasons
```

- [ ] 建立 §2.2 账本与映射。rank_dedup 输出截断前记录全部可评分候选，包括未入选项；没有 GIF 的候选不插入虚假 candidate_gifs 行。记录 `selected/exported/not_selected/review_required` 原因，不能把未选中当 dislike。
- [ ] payload 冻结：源 SHA、窗口、实际解码尺寸/分析尺寸、base quality score、无曲线 preference score（若可用）、无曲线 final score、实际 final score、nominal/effective shadow score、profile/model/feature/config hashes、caption-gated 文本及 hash、float32 向量及字节 hash、scenario keys、有效性原因、质量状态、运行时间。
- [ ] `baseline_score` 必须来自本次共享无曲线打分路径；不能用 `COALESCE(final_score, profile_score, base_rag_similarity)` 代替语义不同的基线。原偏好通道关闭时记录实际旧基线与“偏好开启的假设基线”为两个字段，不混成一次在线评估。
- [ ] Preference 关闭不触发额外隐式 embedding；capture 显式启用后可由后台富化，但记录 enrichment 时间/配置，不能冒称当时在线已用。空 caption 保留向量缺失原因。
- [ ] GIF 成功编码后计算 GIF hash，使用 CandidateService 实际返回/查询到的正式库 ID 建立映射，不假设它等于 clip_id。分析记录不受 best-effort 入库丢失影响；失败入修复任务并显示“数据留存待修复”，不标完整训练样本。
- [ ] 测试两次 materialize 无重复、不同 run 映射、磁盘写完事务失败恢复、部分导出、入库失败、向量/text hash 漂移、缺失 baseline 不进入资格集。enforce 时缺审计记录不得正式发布，disabled/shadow 保留旧导出并明确留存失败。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_candidate_capture.py tests/test_candidate_capture_ingest.py tests/test_library_ingest.py tests/test_candidate_vector_ingest.py tests/test_stage_io.py -q`；通过后窄范围提交 `feat: persist replayable candidate evidence`。

### Task A3：原视频曲线特征版本与外置提取

**Files:** 新建 `app/services/curve_source_features.py`、`tests/test_curve_source_features.py`；修改 `candidate_capture.py`、`scripts/continuity_spike_backend_real.py`（只抽取可复用函数，不把实验脚本 import 进 app）。

**Interfaces:** `validate_feature_compatibility(model_meta: dict, features_meta: dict) -> list[str]`；`extract_source_features(request: dict) -> dict`。请求包含源 SHA/路径、start/end、feature version、extractor hash、decode profile、分析帧坐标、预算和取消令牌 ID；响应为 AVAILABLE/UNAVAILABLE、特征值、实际时间戳、missing reasons 和输入 hash。

- [ ] 先锁住跨输入域拒绝：

```python
def test_gif_model_cannot_consume_source_features():
    from app.services.curve_source_features import validate_feature_compatibility
    assert "feature_version_mismatch" in validate_feature_compatibility(
        {"feature_version": "curve-v1"},
        {"feature_version": "curve-source-v1"},
    )
```

- [ ] `curve-source-v1` 首版沿用现有 curve-v1 的数值特征名和算法定义，但冻结新的 source decode profile；字段全量列入 extractor manifest，不能从数据任意取列。缺字段、NaN、Inf 不填零。
- [ ] 仅评估 snap 后实际候选窗口。缓存键绑定源、窗口、extractor、采样/解码参数；不同窗口不复用聚合值。身体裁切用分析帧坐标，源尺寸单独记录。
- [ ] 通过独立进程版本协议执行，外置模型包/运行时。初期可使用已验证的 DirectML spike 环境执行 A 的特征提取，但它不等于 B 的完整生产 sidecar 获准上线；超时返回 UNAVAILABLE。不得将 torch/onnxruntime 引入 app 导入链。
- [ ] 按视频批量提交窗口，共享解码与模型驻留；先提供进程间可恢复资源锁供后续 B/C 复用，不能每候选启动进程、换入模型。只管理应用拥有的推理进程和 Ollama 模型；资源不允许时排队或记录缺失，不关闭用户其他任务。
- [ ] 编码前特征存账本；导出后写 curve_features 时 `artifact_sha256` 为对应 GIF，额外 lineage 保留实际分析源 SHA 和窗口。训练去重按 GIF/候选身份，源划分按源 SHA，不能因同源 SHA 相同把全部窗口只留一行。
- [ ] 测试窗口外帧不参与曲线、真实尺寸/分析尺寸分离、同源不同窗口保留、缺模型/取消/超时、hash 漂移和特征版本不匹配。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_curve_source_features.py tests/test_curve_preference_model.py tests/test_candidate_capture_ingest.py -q`；提交 `feat: capture versioned source curve features`。

**A 验收：**在获准的一小批原视频上，两种运行路径产生可关联记录；每个成功导出的 GIF 有完整记录或显式待修复状态；重复恢复不重复入库；关闭 capture 时旧行为不变。报告候选级特征可提取率、标签覆盖率和新增耗时，不能只报成功行数。

## 4. 子项目 B：动作连续性与复核（条件式生产阶段）

### Task B0：补真实标签、修复幅度适用性、完成 go/no-go

**Files:** 修改 `scripts/continuity_spike_backend_real.py`、`scripts/continuity_spike_metrics.py`；新建 `scripts/validate_continuity_labels.py`、`tests/continuity/test_label_contract.py`；新报告 `docs/reports/2026-09-continuity-spike/milestone-0c-findings.md`。

**Interfaces:** `validate_label_row(row: dict) -> list[str]`，实现于 label CLI，校验 candidate/input/window/bundle hashes、annotator、verdict、failure labels；系统允许 UNLABELED，但不能计入正式指标。

- [ ] 编写并运行失败测试，禁止喜好充当质量标签：

```python
def test_like_is_not_continuity_ground_truth():
    from scripts.validate_continuity_labels import validate_label_row
    assert "invalid_verdict" in validate_label_row({"verdict": "like"})
```

- [ ] 导出已有 50 项上下文复核包，由人确认 ACCEPTABLE/INVALID；无法判断保留 UNLABELED。GLM 只能建议标签，不自动成为人工真值。审美反馈事件不作连续性标签。
- [ ] 用开发样本对照：可信静止段/背景独立噪声底、主体占满画面导致的不可观察、运动触发 burst、完整周期与非周期阶段。原 30% 背景/50% affine 内点阈值改变须显式记录新配置，缺独立参照不能改用动作自身分布凑通过。
- [ ] 补 ReID 和本地 VLM 实测，分别记录 ABSTAIN/解析失败；记录人脸身份证据增益，面部动态只咨询。50 项用于可行性，不冒充正式 598 个独立负样本资格。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/continuity/test_label_contract.py -q`；重模型测试在外置环境运行并在报告记录精确命令、模型包 hash、逐样本结果和成本。
- [ ] 提交 `test: qualify continuity evidence feasibility`。报告给出 GO_FOR_SHADOW / REDESIGN / INSUFFICIENT_REAL_DATA，并请求复核。未通过就停止 B 的生产实现，A/C 可继续。

### B1–B4 条件式验收合同

这些不是批准直接执行的算法清单。B0 GO 后，按下表逐个编写独立细化执行计划，冻结协议和后端再开发；每项先测试、后实现、再回归与窄范围提交。

| 阶段 | 文件边界 | 必须交付与验收 |
|---|---|---|
| B1 领域/运行时 | 新 `app/services/continuity/{contracts,config,policy,client}.py`；`app/services/preference_schema.py`；外置 sidecar | 原设计六张 continuity 表；health/capabilities/evaluate/cancel；配置、模型、prompt、输入 hash；禁用不加载后端，enforce 无资格拒绝启动；对 UNKNOWN/缺证据 fail closed |
| B2 候选接入 | `rank_dedup.py`、`ranking.py`、`app/services/clip_dedup.py`、continuity facade | snap 后、MoE 前；非传递贪心分组且同分按 key；替补通过 supersede 兄弟 pending；确定性证据最多修正一次边界并重算 key/组/证据；VLM 不再触发换模型 |
| B3 复核续跑 | 新 `app/services/continuity/review.py`、`app/routers/continuity.py`；task_engine models/schema/worker/orchestrator；`stage_io.py`、`materialize.py`；现有 UI 接口 | review item 不依赖成品 GIF；HARD_REJECT 可申诉；MANUAL_PASS 生成 `continuity_resume` 专用 job 和单候选 manifest；跳过普通输出配额和重排，仅过 MoE 硬门；追加 PBF/result/库映射，原任务已完成仍可恢复 |
| B4 资格/启用 | continuity qualification 服务、配置加载器、`app/quality_moe/policy.py`、测试及实测报告 | 人工冻结源隔离 holdout；精确安全上界+40%/60%可用性+资源门；模型/config/prompt hash 绑定；正式 enforce 才退役旧 action_incomplete 硬门；其他 MoE 硬门不变 |

必须纳入 B 细化计划的故障测试：

- 同一 photometric_motion 两项异常不能伪装独立双证据；人体/人脸 appearance 同理。面部表情突跳不能导致拒绝。
- 媒体首尾缺上下文记录 `context_truncated_media_edge`；不能因不存在的文件外帧直接故障，但无法证明起止阶段仍 REVIEW。
- shadow 不改变旧正式导出集合；实验通过状态必须带 `qualified=false`。enforce 缺输入/模型/审计时不能 fallback_fixed 自动出片。
- 候选按视频批量确定性分析，Ollama embedding → 受管模型释放 → sidecar → VLM；侧车缓存/轨迹共享，资源锁覆盖其他 GifAgent GPU 任务，不卸载外部用户模型。
- VRAM 失败缩批重试一次，仍失败 REVIEW；视频 1,200 秒耗尽按视频位置五桶报告顺序偏差，不能跳过必需证据。
- 人工批准绑定 assessment hash/revision；配置升级只标旧项 stale 并显式排队重评，不覆盖人工历史。源变化或丢失 needs_attention，不使用 proxy 代替原片导出。
- 恢复幂等键包含 review item/revision/assessment；重复点击、进程崩溃、追加产物后提交失败、Quality MoE 拒绝均验证原视频已发布内容不丢失。
- direct 共用 stage，不修改 `_PREV_STAGE` 主链来假装一个新完整视频任务；resume 用独立计划复用编码 stage。

**B 启用资格补充：**同源多个候选不能当作独立二项样本。正式安全检验预注册源级抽样（每独立源一项）或先批准聚类统计方案；仅“train/holdout 源不交叉”不足以自动证明候选间独立。canary 只在完整资格取得后实施，不能以少量 canary 代替安全资格。

## 5. 子项目 C：曲线影子排序与自动学习

### Task C1：版本固定、两路排序与配置绑定

**Files:** 新建 `app/services/curve_online.py`、`tests/test_curve_online.py`；修改 `app/pipeline/ranking.py`、`app/services/reranker.py`、`app/pipeline/config.py`；保留 `curve_preference.py` 为模型/资格权威。

**Interfaces:** `curve_route(mode: str, qualified: bool, confidence: float, compatible: bool, quality_pass: bool) -> str` 返回 `BASELINE/SHADOW/ACTIVE`；`score_curve_candidate(conn, snapshot: dict, candidate: dict) -> dict` 返回 baseline/nominal/effective 及 inactive reasons。snapshot 固定 model、profile、qualification 和运行配置；mode OFF 始终基线，shadow 可用 DRAFT 做诊断，active 仅合格发布部署。

- [ ] 初始测试先锁失败模式：

```python
def test_unqualified_curve_never_changes_output():
    from app.services.curve_online import curve_route
    assert curve_route("active", False, 1.0, True, True) == "BASELINE"
    assert curve_route("active", True, 0.0, True, True) == "BASELINE"
    assert curve_route("active", True, 1.0, True, False) == "BASELINE"
```

- [ ] 统一在线与重放调用 `score_preference_export_candidate`，一次实际 embedding 供全部分支使用；保留整数/浮点评分双轨规则。无 caption 不嵌入残余情绪/场景文本。
- [ ] baseline 为相同候选/画像/配置的无曲线结果；nominal 用 1.0，effective 用冻结资格置信度。不得因为 confidence=0 就把 nominal 写到 effective。
- [ ] shadow 始终返回原生产 baseline 排序；DRAFT 的 shadow 结果不得用生产“已启用”标识。active 加载 `PUBLISHED` 且匹配 source 特征版本/extractor 的模型；禁止 SQL 取“最新时间的一行”作为部署模型。
- [ ] 每视频仅固定一次 profile/model 版本，途中 current 切换只影响下一任务。启动配置漂移拒绝 active；运行中证据缺失仅停曲线且留原因。连续性门禁失败不进入 active 偏好重排。
- [ ] 测试空向量、字段缺失、非有限分数、source/GIF 域混用、config hash 变化、mid-video 模型更新、关闭功能时完全相同的分数/顺序。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_curve_online.py tests/test_curve_preference_scoring.py tests/test_rank_pipeline_preference.py tests/test_direct_staged_parity.py -q`；提交 `feat: add source curve shadow ranking`。

### Task C2：参数化训练、不可变快照和留出集治理

**Files:** 新建 `app/services/curve_training.py`、`tests/test_curve_training_snapshot.py`；修改 `scripts/train_curve_preference_model.py`、`app/services/curve_preference_qualification.py`、`scripts/qualify_curve_preference_model.py`。

**Interfaces:** `validate_training_manifest(manifest: dict) -> list[str]`；`train_draft(snapshot_dir: Path, output_dir: Path, feature_version: str) -> dict`；响应只允许 DRAFT/NEEDS_ATTENTION，永不含自动 promotion。CLI 参数显式指定 feature version、extractor hash 和预先冻结的 split manifest。

- [ ] 最小失败测试：

```python
def test_source_leakage_blocks_training_snapshot():
    from app.services.curve_training import validate_training_manifest
    assert "source_overlap" in validate_training_manifest({
        "train_sources": ["a"], "development_sources": ["b"],
        "holdout_sources": ["a"],
    })
```

- [ ] 将脚本 NumPy L2 训练函数抽入服务，CLI 复用；不复制 GAIN_MAP、pairwise、NDCG 与资格门禁。标准化统计只在训练集拟合，缺失/非有限特征拒绝或明确剔除并报告，不静默填零。
- [ ] 使用 SQLite 一致性快照，并冻结实际有效事件 ID/created_at/内容 hash，而非仅 MAX 时间戳；correction/撤销/同一目标新标签都能改变快照。文件缺失保持失败原因，不撤销用户反馈来凑覆盖。
- [ ] 清单绑定候选、真实源组、GIF hash、分析输入及窗口、特征值/版本/extractor、真实无曲线基线、向量、文本、场景、画像、配置、标签事件、训练种子和代码身份。外部媒体 unavailable 不伪造源组为统一 unknown 后参与资格。
- [ ] 持久化源角色 train/development/final_holdout；按源以及已知同源重编码/近重复 lineage 分组。画像构建也排除评估源，避免反馈向量泄漏。
- [ ] 现有已反复观察的 2,510 项作为 regression/reference holdout；不得用于改模型后“调到通过”的最终证明。超参数/特征只用 train/development；最终晋升预注册新源 final_holdout、指标和评估次数。自动开发训练不自动反复窥视同一最终集。
- [ ] 冻结清单和资格产物后不允许自动更新标签；新事件产生新快照/新模型。资格发现输入漂移则记录 stale，不能覆盖旧 metrics。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_curve_training_snapshot.py tests/test_curve_preference_model.py tests/test_curve_preference_qualification.py tests/test_preference_holdout_split.py -q`；提交 `feat: train curve models from frozen source snapshots`。

### Task C3：持久化自动训练 worker

**Files:** 新建 `app/services/learning_jobs.py`、`scripts/run_learning_worker.py`、`tests/test_learning_jobs.py`；修改 `preference_schema.py`；在现有反馈服务成功事务之后只发送轻量调度信号，不同步训练。

**Interfaces:** `learning_eligibility(stats: dict, policy: dict) -> list[str]`；`enqueue_learning(conn, snapshot_sha256: str, policy_sha256: str) -> str`；`run_learning_once(conn, now_utc: str, owner: str) -> dict`。后台进程扫描变化并领取单个租约，不依赖 UI 保持打开。

- [ ] 先测试新增反馈不足不能训练：

```python
def test_no_feedback_is_not_a_training_trigger():
    from app.services.learning_jobs import learning_eligibility
    assert "insufficient_new_feedback" in learning_eligibility(
        {"new_effective_candidates": 0},
        {"min_new_effective_candidates": 100},
    )
```

- [ ] 实现 §2.3 调度门：上次成功训练后的有效候选变化≥100、新源≥20、正负各≥20、间隔≥24h，并满足特征/基线完整性。数字由配置控制并入 policy hash，不用 SQL 反馈事件总数计算新增。
- [ ] correction/撤销影响已训练样本时，即使新增不足也生成 lineage stale 警报；禁止忽略撤销而继续自动部署。评估是否重训进入 NEEDS_ATTENTION，不自动删除已发布模型。
- [ ] 任务在一致性快照完成后固定 input hash，事务领取租约；相同 input+policy 唯一。每完成阶段先冻结输出 hash 再推进状态；进程死后租约过期重试，成功阶段不重写。
- [ ] TRAIN 只产 DRAFT；QUALIFY 先做开发诊断。没有未使用最终评估资格批次时返回 NEEDS_ATTENTION；已有批准的最终集才调用原资格服务。NOT_QUALIFIED 是完成的科学结果，不是无限重试异常。
- [ ] IO/受管模型暂不可用采用最多 3 次、30/120/600 秒退避；输入 hash 不匹配、最终标签不足、非法配置进入 NEEDS_ATTENTION。取消停在安全检查点，保留可恢复产物。
- [ ] worker 不获取超过一个 GPU 租约；NumPy 训练用 CPU，资格所需 VLM/embedding 在新视频交互任务空闲后执行。不能通过 shell 后台无状态循环代替任务表。
- [ ] 测试同反馈两次入队、两个 worker 竞争、反馈事务回滚、训练中途取消、租约失效、重启续跑、NOT_QUALIFIED 不重试、训练绝不写 PUBLISHED。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_learning_jobs.py tests/test_curve_training_snapshot.py tests/test_curve_preference_promotion.py -q`；提交 `feat: schedule recoverable curve learning jobs`。

### Task C4：晋升、部署和回滚分离

**Files:** 修改 `app/services/curve_preference.py`、`app/services/curve_online.py`、`app/routers/preference.py`、`app/services/preference_schema.py`；新建 `tests/test_curve_deployment.py`。

**Interfaces:** `validate_deployment(model_status: str, qualified: bool, config_matches: bool) -> list[str]`；`activate_deployment(conn, qualification_id: str, expected_revision: int, operator: str) -> dict`；`rollback_deployment(conn, target_revision: int, expected_revision: int, operator: str) -> dict`。通过 compare-and-swap 避免并发覆盖，目标旧版本仍需匹配当前特征/配置。

- [ ] 先测试 DRAFT 不能激活：

```python
def test_draft_is_not_deployable():
    from app.services.curve_online import validate_deployment
    assert "model_not_published" in validate_deployment("DRAFT", True, True)
```

- [ ] 保留既有 promotion 的清单等集、源/标签绑定、八项 hash、统计重算和 confidence 校验。升级为部署入口时再从冻结向量/画像/特征重算三路分数；不能把人工复核时成功离线重算误称当前发布器已自动做过。
- [ ] 满足原资格全部门槛（≥30 可比 holdout 行、源隔离、有效配对、nominal/effective LCB>0、NDCG 非退化约束等）仍只生成待批准记录。资格阈值从原服务读取，不重新编一套宽松规则。
- [ ] 人工明确批准后 promotion 可写 PUBLISHED；另一部署事务才切 current revision。配置开关仍是独立受控操作，不能由训练 worker 修改 models.yaml。
- [ ] 每视频固定 deployment revision。新配置/模型/prompt/extractor 变更须新资格；回滚优先关闭曲线回无曲线基线，不能回到不兼容旧模型。连续性 enforce 的回退必须显式告知“强保证未启用”，不能静默恢复旧自动出片。
- [ ] 报告展示当前部署版本、最近训练结果、未通过原因、待修复候选、反馈/特征覆盖、配对源数、影子收益及回滚入口；不把样本数足够写成“学习已生效”。
- [ ] 测试 dry-run 无写入、资格包缺项/篡改、相同标签不同源、confidence=0、配置漂移、两个激活请求竞争、关闭曲线后旧分数逐项恢复。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest tests/test_curve_deployment.py tests/test_curve_online.py tests/test_curve_preference_promotion.py tests/test_curve_preference_qualification.py -q`；提交 `feat: separate curve qualification and deployment`。

## 6. 总体验证与发布清单

### 6.1 每次任务交付

- [ ] 先运行对应新增测试确认失败，再实现；文中最小测试只是起点，任务列出的所有失败场景均须具名测试。
- [ ] 运行任务列出的回归；新 app 模块纳入仓库现有 mypy/lint 范围，不以 scripts 不在门禁中为由跳过新增业务服务类型检查。
- [ ] 提交前检查 `git diff --check` 和窄路径 diff，保护其他任务已有改动。不提交生产库、模型权重、个人视频或大体积证据。
- [ ] 报告提交 ID、实际命令/退出码、测试数量、输入与配置 hash、真实未完成项。不要以“全量绿灯”代替模型收益与运行时测试。

### 6.2 综合回归命令

在对应测试文件已实现后运行；未创建文件不能计作通过：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_candidate_capture.py tests/test_candidate_capture_ingest.py tests/test_curve_source_features.py tests/test_curve_online.py tests/test_curve_training_snapshot.py tests/test_learning_jobs.py tests/test_curve_deployment.py tests/test_library_ingest.py tests/test_rank_pipeline_preference.py tests/test_direct_staged_parity.py tests/test_curve_preference_model.py tests/test_curve_preference_qualification.py tests/test_curve_preference_promotion.py -q
```

B 阶段追加 `tests/continuity/`、`tests/task_engine/`、`tests/quality_moe/`，并按仓库实际命令完成全量回归。PyInstaller 验证必须覆盖新服务 import、外置进程发现、取消/恢复和缺模型提示；不允许把开发环境通过当成 EXE 已验证。

### 6.3 真实媒体与启用

1. A/C 小批 shadow：先 dry-run 输出素材范围/预算；批准后执行，比较旧正式输出集合、排序和新证据。报告源窗口特征与历史 GIF 域差异，不能混合汇总宣称模型收益。
2. B0：人标开发样本、领域可用性和完整模型成本；批准后才做 B1–B4。
3. B4：预注册独立安全/可用性 holdout；不合格停在 shadow，可人工逐项审批导出。
4. C：新增反馈达到调度门后自动产生 DRAFT；独立最终评估及部署批准完成前只 shadow。没有合格曲线模型不阻止已合格连续性链工作。
5. canary 样本选择及数量在启用前冻结，持续人工抽查。任一确认的强门禁漏放立即暂停自动放行、保全证据；曲线资格/config 漂移立即停用曲线。不要用可疑样本数量减少来维持漂亮指标。
6. 初期 `promotion_policy=manual` 固定。全自动晋升不是本期交付范围，必须积累独立批次验证后另行批准，不能新增配置值就视为获得权限。

## 7. 完成定义与下一条执行指令

| 能力 | 完成代码不算完成；必须额外满足 |
|---|---|
| 按动作可靠截取 | B0 获准、B 生产链与复核恢复通过、完整安全/可用性资格、明确启用 |
| 曲线偏好影响排序 | source 同域训练/评估、配置一致、PUBLISHED 且部署 active、质量门通过、真实线上/离线同分 |
| 自动学习 | 有效反馈触发、快照训练/评估可恢复、不合格不部署、批准后版本切换及回滚已验证 |

推荐给实施 Agent 的首条任务：

> 阅读本计划和所列规格，只执行 A1。先核对工作树和当前身份生成链，添加失败测试，完成版本化 pre-export key 与配置快照；保留旧 manifest 恢复兼容。不要开启 capture/continuity/preference，不访问真实视频进行重推理，不修改生产数据库。提交窄范围 diff 和实际测试证据后等待 review。

后续按 A2 → A3 → C1 → C2 → C3 → C4 推进；B0 可以独立进行，但人工标签不足时明确停点。GLM Harness 可执行这些编码工单，不参与后台服务调用。

**执行方式：**可在当前任务逐项执行并在每项 review 后继续；也可经用户选择采用子代理逐项实现与独立复核。本文交付不自动启动任一种实现方式。
