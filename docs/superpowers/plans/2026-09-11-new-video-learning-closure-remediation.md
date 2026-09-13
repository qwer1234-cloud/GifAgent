# 新视频曲线数据闭环与自动学习整改 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 A/C 接口骨架整改为可运行、可恢复、可复现的新视频数据采集、曲线影子排序、DRAFT 自动训练、资格评估和人工部署闭环，同时保持未获资格功能不影响正式 GIF 输出。

**Architecture:** 采用一条共享的 staged/direct `rank_dedup` 执行路径，在 snap 后冻结候选身份，在 Quality MoE 后为可评分候选批量提取原视频曲线特征，并通过唯一生产打分函数生成 baseline/nominal/effective 三路结果。所有训练以不可变快照驱动；学习 worker 每次只推进一个可验证阶段；模型晋升、部署激活和配置启用是三个相互独立的动作。

**Tech Stack:** Python 3、SQLite、NumPy、ffmpeg、现有 Preference Memory 与 task pipeline；重视觉推理运行在 `.venv-spike` 外置进程中，PyInstaller 主程序只持有协议和客户端。

**Spec:**

- `docs/superpowers/specs/2026-09-04-person-semantic-continuity-body-curve-design.md`
- `docs/superpowers/plans/2026-09-06-new-video-continuity-preference-learning.md`
- 本计划取代上一计划中 A1–A3、C1–C4 的“已完成”判断；B0–B4 的人标、连续性与统计资格停点保持不变。
- 代码复核基线：2026-09-11 当前工作树，HEAD `d08049a2`，综合测试 `133 passed`，但存在本文第 1 节列出的真实闭环缺陷。

## Global Constraints

- 本计划只实现 A/C 整改，不宣称人物动作连续性 B 已完成；B0 没有人工质量标签和 go/no-go 结论时不得启用 continuity enforce。
- `candidate_capture.enabled=false`、curve mode 非 active 时必须保持现有正式 GIF 集合、分数和排序不变。
- capture 开启时不得用全零、路径哈希或缺失值冒充源视频 SHA-256；无法读取源文件必须显式失败。
- 历史 GIF 域 `curve-v1` 与原视频窗口域 `curve-source-v1` 严格隔离；模型 feature version 和 extractor hash 必须双向非空且完全一致。
- 曲线评分只能复用 `app.services.reranker.score_preference_export_candidate`；不得再实现第二套线性混合公式。
- baseline 是同一候选、同一画像、同一权重下关闭 curve 分量的真实生产分数；不能用 `COALESCE(final_score, profile_score, base_rag_similarity)` 事后猜测。
- 缺失、NaN、Inf、布尔值和维度错误的曲线特征一律 `UNAVAILABLE`；不得补零参与训练或在线评分。
- 自动 worker 只能创建 DRAFT 和资格结果，不能写 PUBLISHED、不能激活部署、不能修改 `configs/models.yaml`。
- 晋升必须继续通过 `load_qualification_bundle` 和 `validate_promotion`；部署不得另开只检查状态字符串的旁路。
- 数据库迁移幂等；生产数据变更前创建 SQLite 一致性备份。本计划的测试只使用临时数据库和合成媒体。
- 工作树存在大量用户改动。每个任务只暂存任务列出的路径，不运行 `git add .`，不清理或覆盖无关文件。
- GLM-5.3-Flash 可以在 Harness 中执行本计划、生成测试或辅助 review，但不是 GifAgent 运行时 provider。

---

## 1. 已确认缺陷与完成定义

| 编号 | 当前事实 | 完成证据 |
|---|---|---|
| R1 | `run_learning_once()` 不执行阶段，直接 `COMPLETE/SUCCEEDED` | 不存在快照必须 NEEDS_ATTENTION；真实合成闭环生成 DRAFT 与资格结果 |
| R2 | `persist_capture`、`extract_source_features`、`score_curve_candidate` 没有生产调用方 | staged/direct 共享集成测试产生账本、特征、三路分数与 publication link |
| R3 | `extract_source_features()` 对合法请求固定返回 runtime unavailable | 外置 worker 对合成视频返回完整 `curve-source-v1`；模型不可用仍显式 UNAVAILABLE |
| R4 | 在线 curve 使用 `0.9*base+0.1*probability` 混合不同尺度 | 在线与资格重放逐行分数完全一致，80 与 1.0 不再得到 72.1 |
| R5 | deployment 将 profile hash 当 config hash，并硬编码 `config_matches=True` | 激活前完整重放资格校验，模型、配置、特征和资格包任一漂移均拒绝 |
| R6 | 训练只比较声明的 source 集合，实际 row 可来自 holdout | 角色集合从逐行事实重算；声明与实际不一致直接拒绝 |
| R7 | 租约使用不同时间格式且先查后改，活租约会被抢占 | epoch 时间 + 原子 CAS；两连接并发只有一个 owner |
| R8 | 同一候选不同 capture version 覆盖同一个 JSON 文件 | 内容寻址文件；旧 DB 行 hash 永远与文件一致；身份相同内容不同报冲突 |
| R9 | 缺特征补零、缺 extractor identity 仍兼容 | AVAILABLE 必须十项完整有限值；任一身份缺失不兼容 |

A/C 的最终完成定义不是“新接口存在”或“单元测试通过”，而是下面这条合成端到端链可复现：

```text
源视频窗口
  -> snap 后 preexport key
  -> 外置 curve-source-v1 特征
  -> Quality MoE 通过
  -> 无曲线 baseline + nominal/effective shadow
  -> 不改变正式排序
  -> GIF 入库及 publication link
  -> 有效 like/dislike 事件
  -> 冻结训练快照
  -> DRAFT 模型
  -> NOT_QUALIFIED 或 QUALIFIED 资格结果
  -> 人工晋升
  -> 显式部署 revision
```

最后两步在测试中可使用合成的合格资格包；真实生产仍必须单独授权。

## 2. 文件边界与稳定接口

### 2.1 保留并整改

| 文件 | 整改后职责 |
|---|---|
| `app/services/candidate_capture.py` | 候选身份、不可变 capture 文件、账本写入和正式 ID 映射 |
| `app/services/curve_source_features.py` | 严格请求/响应类型、批量外置进程客户端、兼容性校验 |
| `app/services/curve_online.py` | 读取一次部署快照、调用共享 scorer、生成三路分数、部署 revision |
| `app/services/curve_training.py` | 构建/验证不可变快照、训练 DRAFT、登记模型 |
| `app/services/learning_jobs.py` | eligibility、原子租约、阶段执行、重试与恢复 |
| `app/pipeline/stages/rank_dedup.py` | 唯一 staged/direct 集成点，不内嵌模型算法 |
| `app/services/library_ingest.py` | 成功 GIF 入库后以实际 candidate ID 建立 publication link |
| `app/services/curve_preference.py` | 模型完整性、资格 bundle 和唯一晋升权威 |

### 2.2 新增

| 文件 | 职责 |
|---|---|
| `app/services/curve_source_runtime.py` | 外置 feature worker 的进程、批次、超时和资源锁客户端 |
| `scripts/extract_curve_source_features.py` | `.venv-spike` 内运行的 JSON 请求/响应 worker |
| `tests/test_curve_source_runtime.py` | 协议、超时、hash、完整特征和批处理测试 |
| `tests/test_curve_pipeline_capture.py` | 共享 rank_dedup 的闭环集成测试 |
| `tests/test_learning_pipeline_e2e.py` | snapshot→train→qualify 的恢复与端到端测试 |

### 2.3 稳定类型

在对应模块定义冻结 dataclass，后续任务必须复用这些名字：

```python
@dataclass(frozen=True)
class CurveFeatureResult:
    preexport_key: str
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    feature_version: str
    extractor_sha256: str
    features: dict[str, float] | None
    actual_timestamps: tuple[float, ...]
    input_hash: str
    missing_reasons: tuple[str, ...]
    wall_clock_ms: float

@dataclass(frozen=True)
class CurveWindow:
    preexport_key: str
    start_us: int
    end_us: int

@dataclass(frozen=True)
class PersistedCapture:
    record_id: str
    payload_path: str
    payload_sha256: str

@dataclass(frozen=True)
class CaptureBatchResult:
    captures: tuple[PersistedCapture, ...]
    failed_preexport_keys: tuple[str, ...]

@dataclass(frozen=True)
class CurveRuntimeSnapshot:
    mode: Literal["off", "shadow", "active"]
    deployment_revision: int | None
    model_version: str | None
    qualification_id: str | None
    profile_version: str | None
    algorithm_config_hash: str
    feature_version: str
    extractor_sha256: str
    confidence: float

@dataclass(frozen=True)
class CurveCandidateScores:
    route: Literal["BASELINE", "SHADOW", "ACTIVE"]
    baseline_score: float
    nominal_score: float | None
    effective_score: float | None
    production_score: float
    inactive_reasons: tuple[str, ...]

@dataclass(frozen=True)
class TrainingSnapshot:
    snapshot_dir: str
    manifest_sha256: str
    train_row_count: int
    development_row_count: int
    final_holdout_row_count: int

@dataclass(frozen=True)
class DraftTrainingResult:
    status: Literal["DRAFT", "NEEDS_ATTENTION"]
    model_version: str | None
    model_path: str | None
    model_sha256: str | None
    error_code: str | None

@dataclass(frozen=True)
class LearningStats:
    new_effective_candidates: int
    new_source_groups: int
    positive_candidates: int
    negative_candidates: int
    hours_since_last_job: float | None

@dataclass(frozen=True)
class EnqueueResult:
    status: Literal["ENQUEUED", "EXISTING", "INELIGIBLE"]
    job_id: str | None
    reasons: tuple[str, ...]

@dataclass(frozen=True)
class LearningJob:
    job_id: str
    stage: Literal["SNAPSHOT", "TRAIN", "QUALIFY", "COMPLETE"]
    owner: str
    input_snapshot_sha256: str
    policy_sha256: str

@dataclass(frozen=True)
class LearningStageResult:
    outcome: Literal["ADVANCE", "RETRY", "NEEDS_ATTENTION", "COMPLETE"]
    next_stage: Literal["TRAIN", "QUALIFY", "COMPLETE"] | None
    artifact_path: str | None
    artifact_sha256: str | None
    error_code: str | None

@dataclass(frozen=True)
class DeploymentResult:
    status: Literal["ACTIVATED", "ROLLED_BACK", "REFUSED", "CONFLICT"]
    revision: int | None
    reasons: tuple[str, ...]
```

## 3. 实施顺序

### Task 0：先冻结九项失败行为

**Files:**

- Modify: `tests/test_candidate_capture_ingest.py`
- Modify: `tests/test_curve_source_features.py`
- Modify: `tests/test_curve_online.py`
- Modify: `tests/test_curve_training_snapshot.py`
- Modify: `tests/test_learning_jobs.py`
- Modify: `tests/test_curve_deployment.py`

**Interfaces:** 只消费现有接口，不创建临时生产 API。测试必须先在当前代码上失败，证明不是给现有实现补宽松断言。

- [ ] **Step 1: 写 learning worker 空成功回归。**

```python
def test_missing_snapshot_never_completes_learning_job(tmp_path):
    conn = sqlite3.connect(tmp_path / "learning.db")
    apply_preference_schema(conn)
    enqueue_learning(conn, "f" * 64, "e" * 64)
    result = run_learning_once(conn, now_epoch=1_800_000_000, owner="w1")
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["error_code"] == "snapshot_not_found"
    assert conn.execute("SELECT COUNT(*) FROM curve_preference_models").fetchone()[0] == 0
```

- [ ] **Step 2: 写实际行 source 泄漏、缺字段补零和 extractor 缺失回归。**

```python
def test_manifest_rows_must_match_declared_source_roles():
    manifest = {
        "feature_version": "curve-source-v1",
        "train_sources": ["train"],
        "development_sources": ["dev"],
        "holdout_sources": ["holdout"],
        "train_rows": [{"source_video_sha256": "holdout"}],
    }
    assert "source_role_mismatch" in validate_training_manifest(manifest)

def test_available_features_cannot_be_synthesized_from_missing_values():
    with pytest.raises(ValueError, match="feature_set_incomplete"):
        format_source_feature_response(status="AVAILABLE", features={})

def test_extractor_identity_is_required_on_both_sides():
    reasons = validate_feature_compatibility(
        {"feature_version": "curve-source-v1", "extractor_sha256": "a" * 64},
        {"feature_version": "curve-source-v1"},
    )
    assert "extractor_sha256_missing" in reasons
```

- [ ] **Step 3: 写共享 scorer 与 capture 不可变性回归。**

```python
def test_curve_online_uses_shared_export_scorer(monkeypatch):
    calls = []
    def shared_score(**kwargs):
        calls.append(kwargs)
        return {"final_score": 81.25, "profile_score": 0.73}
    monkeypatch.setattr(
        "app.services.curve_online.score_preference_export_candidate",
        shared_score,
        raising=False,
    )
    result = score_curve_candidate(
        None,
        {"mode": "active", "qualified": True, "confidence": 1.0,
         "compatible": True, "nominal_weight": 0.10},
        {"base_score": 80.0, "curve_probability": 1.0},
    )
    assert calls
    assert result["final_score"] == pytest.approx(81.25)

def test_capture_versions_do_not_overwrite_old_payload(conn, tmp_path, capture_payload):
    payload = dict(capture_payload)
    first = persist_capture(conn, payload, tmp_path)
    first_bytes = Path(first.payload_path).read_bytes()
    second = persist_capture(conn, {**payload, "capture_version": "capture-v2"}, tmp_path)
    assert first.payload_path != second.payload_path
    assert Path(first.payload_path).read_bytes() == first_bytes
```

- [ ] **Step 4: 写租约竞争和部署漂移回归。** 使用两个连接打开同一个临时 SQLite 文件；同一到期前 job 只有一个连接能领取。将 algorithm config hash、extractor hash、资格 bundle 任一字段篡改后，`activate_deployment` 必须返回 REFUSED，且 active revision 不变。
- [ ] **Step 5: 运行定向测试并保存失败摘要。**

Run: `.venv/Scripts/python.exe -m pytest tests/test_candidate_capture_ingest.py tests/test_curve_source_features.py tests/test_curve_online.py tests/test_curve_training_snapshot.py tests/test_learning_jobs.py tests/test_curve_deployment.py -q`

Expected: 新增测试至少分别暴露 R1、R4、R5、R6、R7、R8、R9；既有测试保持通过。

- [ ] **Step 6: 提交测试。**

```powershell
git add tests/test_candidate_capture_ingest.py tests/test_curve_source_features.py tests/test_curve_online.py tests/test_curve_training_snapshot.py tests/test_learning_jobs.py tests/test_curve_deployment.py
git commit -m "test: expose curve learning closure gaps"
```

### Task 1：修复候选身份和不可变 capture 存储

**Files:**

- Modify: `app/services/candidate_capture.py`
- Modify: `app/services/preference_schema.py`
- Modify: `tests/test_candidate_capture.py`
- Modify: `tests/test_candidate_capture_ingest.py`

**Interfaces:** `assign_candidate_capture_identities(...) -> None` 保持调用签名；`persist_capture(...) -> PersistedCapture` 返回 `record_id/payload_path/payload_sha256`；`link_publication(...) -> None` 保持签名。

- [ ] **Step 1: 移除伪源身份。** capture 开启且源文件不可读或传入 SHA 非 64 位小写 hex 时抛出 `SourceIdentityUnavailable`。删除 `"0" * 64` 降级。已有 candidate ID 只有在 manifest 明确 `candidate_identity_version=legacy-v1` 时保留；新 capture 必须计算 full preexport key。
- [ ] **Step 2: 使用内容寻址路径。** payload 目标固定为 `evidence_root/run_id/capture_version/preexport_key/payload_sha256.json`。临时文件写在同一目录，flush 后 `os.fsync()`，再 `replace()`。
- [ ] **Step 3: 使数据库身份不可变。** `UNIQUE(run_id, preexport_key, capture_version)` 已存在同 hash 时幂等返回；同身份不同 hash 时抛 `CaptureConflict`，不得 UPDATE 旧路径/hash。失败状态也保存自己的版本化 failure payload。
- [ ] **Step 4: 增加追加式候选事件。** schema 新增 `candidate_analysis_events(event_id, record_id, event_kind, payload_path, payload_sha256, created_at)`；`event_kind` 只接受 `scored/selected/not_selected/quality_rejected/publication_failed`。同 record/event payload hash 幂等，内容不同产生新事件，不能 UPDATE 历史事件。
- [ ] **Step 5: 校验完整窗口。** `anchor_us` 必须位于 `[start_us,end_us]`；baseline 接受有限 `int/float` 但拒绝 bool；source/preexport/payload hashes 必须严格 hex；capture status 只接受 schema 枚举。
- [ ] **Step 6: 保持文件/DB 故障可恢复。** 文件成功而 INSERT 失败可留下内容寻址孤儿；重试复用同一文件。DB 行存在但文件缺失或 hash 不符返回 `NEEDS_REPAIR`，不覆盖原证据。
- [ ] **Step 7: 验证。**

Run: `.venv/Scripts/python.exe -m pytest tests/test_candidate_capture.py tests/test_candidate_capture_ingest.py -q`

Expected: 旧版本 hash 在写入新版本后仍匹配；重复写幂等；不同内容冲突；不可读源不会生成伪 key。

- [ ] **Step 8: 提交。**

```powershell
git add app/services/candidate_capture.py app/services/preference_schema.py tests/test_candidate_capture.py tests/test_candidate_capture_ingest.py
git commit -m "fix: make candidate capture evidence immutable"
```

### Task 2：实现真实的原视频曲线特征外置运行时

**Files:**

- Create: `app/services/curve_source_runtime.py`
- Create: `scripts/extract_curve_source_features.py`
- Create: `tests/test_curve_source_runtime.py`
- Modify: `app/services/curve_source_features.py`
- Modify: `tests/test_curve_source_features.py`

**Interfaces:** `CurveSourceRuntime.evaluate_video(source_path: Path, source_sha256: str, windows: tuple[CurveWindow, ...], timeout_s: float) -> tuple[CurveFeatureResult, ...]`；worker 从 UTF-8 JSON 请求文件读取一个视频及多个窗口，向 UTF-8 JSON 响应文件原子写入同序结果。

- [ ] **Step 1: 写协议测试。** 请求包含 `protocol_version=curve-source-runtime-v1`、源 SHA、窗口 preexport key/start_us/end_us、feature version、decode profile、extractor manifest/hash。响应必须回显 request SHA、模型包 hash 和每项 input hash。
- [ ] **Step 2: 严格化结果格式。** AVAILABLE 要求 `CURVE_SOURCE_FEATURE_NAMES` 十项键集合完全相等，所有值为有限非 bool 数字，至少三个实际时间戳且位于窗口内；否则整体 UNAVAILABLE 并保留原因。缺任一 extractor identity 返回 `extractor_sha256_missing`。
- [ ] **Step 3: 提取纯 curve 算法。** 将 `scripts/continuity_spike_backend_real.py` 中现有曲线计算抽为不依赖 CLI 的轻量模块或由 worker 在 spike 环境内复用；主 app 不 import onnxruntime、cv2 推理后端或 spike venv。
- [ ] **Step 4: 实现每视频批处理。** worker 一次解码一个源并处理所有 post-dedup 窗口，复用 pose 输出；特征只聚合 snap 后候选窗口，不把前后 continuity context 混入偏好特征。身体裁切用分析帧尺寸，真实源尺寸另存元数据。
- [ ] **Step 5: 实现客户端故障契约。** 可执行文件不存在、timeout、非零退出、响应缺失、request hash 不匹配、模型 hash 漂移、单窗口推理失败分别返回结构化 UNAVAILABLE；不把失败当全零特征。日志不得包含视频帧或完整环境变量。
- [ ] **Step 6: 增加跨进程资源锁。** 锁按应用拥有的 GPU runtime 建立，包含 owner、PID、创建/到期 epoch；只有持有者能终止自己启动的 worker，不关闭用户其他 Ollama/WSL 任务。初版等待超时后返回 `inference_runtime_busy`。
- [ ] **Step 7: 验证协议和真实后端。** 默认单元测试使用确定性 fake backend 对测试生成的小型 MP4 返回 AVAILABLE，断言十项值有限、重放一致；另以 `.venv-spike/Scripts/python.exe scripts/extract_curve_source_features.py --self-test-real` 运行真实模型 smoke，命令读取仓库内内容寻址模型包和脚本内合成姿态夹具，不访问用户视频。真实 smoke 可以得到 AVAILABLE 或明确的 `pose_coverage_low`，但不能得到协议/hash/runtime 错误。用 fake worker 覆盖超时与畸形响应。

Run: `.venv/Scripts/python.exe -m pytest tests/test_curve_source_features.py tests/test_curve_source_runtime.py -q`

- [ ] **Step 8: 提交。**

```powershell
git add app/services/curve_source_features.py app/services/curve_source_runtime.py scripts/extract_curve_source_features.py tests/test_curve_source_features.py tests/test_curve_source_runtime.py
git commit -m "feat: execute source curve features out of process"
```

### Task 3：把 capture 和原视频特征接入共享 rank_dedup

**Files:**

- Modify: `app/pipeline/stages/rank_dedup.py`
- Modify: `app/pipeline/ranking.py`
- Modify: `app/pipeline/config.py`
- Modify: `app/pipeline/stages/materialize.py`
- Modify: `app/services/library_ingest.py`
- Create: `tests/test_curve_pipeline_capture.py`
- Modify: `tests/test_direct_staged_parity.py`

**Interfaces:** `_assign_candidate_identities` 在 snap 后、dedup 前调用；`capture_rank_candidates(context, clips, runtime_snapshot) -> CaptureBatchResult` 在 Quality MoE 后、输出截断前持久化全部 post-dedup 可评分候选。

- [ ] **Step 1: 写共享路径集成测试。** staged 和 direct 输入同一合成 manifest，fake feature runtime 返回确定结果；断言两路 preexport key、capture payload、三路分数、选中状态完全一致。
- [ ] **Step 2: 调整身份时序。** `_apply_boundary_snaps` 后立即为 `clean_clips` 生成身份；dedup 保留该身份。不得在 dedup 后重新按列表 index 生成 ID。
- [ ] **Step 3: 批量请求特征。** 对 post-dedup 候选每视频调用一次 runtime；结果按 preexport key 严格一一匹配。遗漏、重复或额外结果将对应 capture 标记 NEEDS_REPAIR。
- [ ] **Step 4: 持久化截断前候选。** Quality MoE 之后、排序之前建立 payload；排序和 output cap 之后向新增的 `candidate_analysis_events` 表追加 selection 事件，事件 payload 同样使用内容寻址文件。不能覆盖初始证据。`not_selected` 不是 dislike，`quality_rejected` 不是审美负样本。
- [ ] **Step 5: 关联正式 GIF。** rank manifest 携带 `analysis_record_id`；gif_clip/materialize 原样传播。`CandidateService.materialize_run_candidate()` 返回的实际 candidate ID 与 GIF 字节 hash 在同一显式事务内写 publication link。缺 record ID 时保持旧入库；有 record ID 而链接失败时记录 NEEDS_REPAIR，不能报告完整闭环成功。
- [ ] **Step 6: 执行故障策略。** disabled 完全不调用 runtime/DB；shadow 的 capture/feature 失败保留正式旧输出并写原因；curve active 或未来 continuity enforce 要求完整 capture，否则候选不得走受保护输出。
- [ ] **Step 7: 验证。**

Run: `.venv/Scripts/python.exe -m pytest tests/test_curve_pipeline_capture.py tests/test_direct_staged_parity.py tests/test_library_ingest.py tests/test_rank_pipeline_preference.py -q`

Expected: disabled 输出逐字段等于旧基线；shadow 正式顺序相同且新增证据完整；失败不会产生虚假 AVAILABLE。

- [ ] **Step 8: 提交。**

```powershell
git add app/pipeline/stages/rank_dedup.py app/pipeline/ranking.py app/pipeline/config.py app/pipeline/stages/materialize.py app/services/library_ingest.py tests/test_curve_pipeline_capture.py tests/test_direct_staged_parity.py
git commit -m "feat: capture source curve evidence in shared pipeline"
```

### Task 4：统一在线、影子和资格重放打分

**Files:**

- Modify: `app/services/curve_online.py`
- Modify: `app/pipeline/ranking.py`
- Modify: `app/services/curve_preference_qualification.py`
- Modify: `tests/test_curve_online.py`
- Modify: `tests/test_rank_pipeline_preference.py`
- Modify: `tests/test_curve_preference_qualification.py`

**Interfaces:** `load_curve_runtime_snapshot(conn: sqlite3.Connection, config: Mapping[str, Any]) -> CurveRuntimeSnapshot` 每视频调用一次；`score_curve_candidate(reranker, runtime, candidate) -> CurveCandidateScores` 三路都调用共享 scorer。

- [ ] **Step 1: 删除自造线性公式。** baseline 调用 `score_preference_export_candidate(..., curve_probability=None)`；nominal 使用相同参数加 curve probability/model version/confidence=1.0；effective 使用资格置信度。所有分数保持生产 final score 的同一尺度。
- [ ] **Step 2: 固定实际画像和模型。** 每视频解析一次 `preference_profile_current` 和 active deployment revision；同一视频中途的 current 变化不影响该批候选。没有 active deployment 时 active 请求降为 BASELINE 并记录 `deployment_unavailable`。
- [ ] **Step 3: 兼容性 fail closed。** active 要求 model=PUBLISHED、qualification=QUALIFIED、confidence∈(0,1]、算法配置 hash、feature version、extractor hash 和部署 revision 全部匹配；shadow 可显式指定 DRAFT 做诊断，但标记 `qualified=false`。
- [ ] **Step 4: 保持 caption 门禁。** candidate vector 只由 `preference_embedding_text` 生成；空 caption 不使用遗留 emotion/scene 泛化 embedding。实际 float32 向量、文本 hash、embedding model 和 scenario keys 写入 capture payload。
- [ ] **Step 5: 资格重放共用同一函数。** qualification 不再维护独立评分分支；冻结参数构造相同 runtime/candidate 输入。对同一 100 行 fixture，在线 baseline/nominal/effective 与资格文件最大绝对差 ≤1e-12。
- [ ] **Step 6: 验证。**

Run: `.venv/Scripts/python.exe -m pytest tests/test_curve_online.py tests/test_rank_pipeline_preference.py tests/test_curve_preference_qualification.py tests/test_curve_pipeline_capture.py -q`

- [ ] **Step 7: 提交。**

```powershell
git add app/services/curve_online.py app/pipeline/ranking.py app/services/curve_preference_qualification.py tests/test_curve_online.py tests/test_rank_pipeline_preference.py tests/test_curve_preference_qualification.py tests/test_curve_pipeline_capture.py
git commit -m "fix: unify production and curve replay scoring"
```

### Task 5：建立逐行事实驱动的训练快照和 DRAFT 登记

**Files:**

- Modify: `app/services/curve_training.py`
- Modify: `scripts/train_curve_preference_model.py`
- Modify: `app/services/curve_preference.py`
- Modify: `tests/test_curve_training_snapshot.py`
- Modify: `tests/test_curve_preference_model.py`

**Interfaces:** `build_training_snapshot(conn, policy, output_dir) -> TrainingSnapshot`；`validate_training_manifest(manifest) -> list[str]`；`train_draft(snapshot_dir, output_dir, feature_version) -> DraftTrainingResult`；`register_draft(conn, result) -> str`。

- [ ] **Step 1: 从权威事件解析样本。** 复用 `load_latest_scoring_events`；like/favorite→1，dislike→0，neutral/skip/quality_reject 不拟合。冻结 event ID、created_at、rating、correction/supersede 身份和事件内容 hash。
- [ ] **Step 2: 只选择完整同域记录。** 样本必须拥有 publication link、真实 GIF hash、`curve-source-v1` 特征、精确无曲线 baseline、向量/text/scenario/profile/config 身份。缺一项进入 exclusion report，不撤销用户反馈、不填零。
- [ ] **Step 3: 从逐行事实重算角色。** manifest 每行包含唯一 candidate ID、source SHA、preexport key、窗口、label、features、extractor、artifact、baseline 和事件身份。验证器从 rows 重算 train/development/final_holdout source 集合并与声明集合完全比较；unknown/空 source 不能进入资格。
- [ ] **Step 4: 冻结数据角色。** 已反复查看的 2,510 行只作为 reference regression set。新 source 先按冻结 split seed 分为 train/development；final_holdout 清单在评估前一次性冻结，训练器绝不读取其标签。已知同源重编码和 lineage 必须落同一组。
- [ ] **Step 5: 严格训练输入。** feature names 必须精确等于版本定义，不从第一行动态发现；标签只能 0/1 且两类均存在；所有值有限；标准化均值/方差仅用 train。拒绝 `row_feats.get(name, 0.0)`。
- [ ] **Step 6: 完整保存 DRAFT。** model row 冻结训练/开发/holdout 清单及 hashes、source set hashes、extractor、代码版本、L2、迭代收敛状态和事件水位。训练函数只返回 DRAFT/NEEDS_ATTENTION，不写 PUBLISHED。
- [ ] **Step 7: 验证篡改。** 修改任一 feature、baseline、event、source、artifact hash 或 split role 都必须改变 manifest hash并使旧结果失效。实际 row 从 holdout 混入 train 必须被拒绝。

Run: `.venv/Scripts/python.exe -m pytest tests/test_curve_training_snapshot.py tests/test_curve_preference_model.py tests/test_preference_holdout_split.py -q`

- [ ] **Step 8: 提交。**

```powershell
git add app/services/curve_training.py scripts/train_curve_preference_model.py app/services/curve_preference.py tests/test_curve_training_snapshot.py tests/test_curve_preference_model.py
git commit -m "fix: train drafts from row-bound source snapshots"
```

### Task 6：把 learning worker 改为真实、可恢复的阶段机

**Files:**

- Modify: `app/services/learning_jobs.py`
- Modify: `app/services/preference_schema.py`
- Modify: `scripts/run_learning_worker.py`
- Modify: `app/services/preference_events.py`
- Modify: `app/routers/candidates.py`
- Modify: `tests/test_learning_jobs.py`
- Create: `tests/test_learning_pipeline_e2e.py`

**Interfaces:** `collect_learning_stats(conn, policy) -> LearningStats`；`maybe_enqueue_learning(conn, policy) -> EnqueueResult`；`claim_learning_job(conn, owner, now_epoch, lease_seconds) -> LearningJob | None`；`execute_learning_stage(conn, job, services) -> LearningStageResult`。

- [ ] **Step 1: 迁移时间字段。** 新增 `lease_expires_epoch INTEGER` 和 `next_retry_epoch INTEGER`；旧文本列仅兼容读取一次，不再比较 ISO 字符串。所有测试注入整数 epoch，避免本地时区和 SQLite datetime 格式差异。
- [ ] **Step 2: 原子领取。** 使用 `BEGIN IMMEDIATE`，在事务内选择可领取 job，再以 job ID、原 status、原 owner/lease 条件 UPDATE；`rowcount==1` 才获得租约。领取后提交，耗时阶段不持有 DB 锁。`max_concurrent_jobs=1` 在同一事务中检查。
- [ ] **Step 3: 每次只推进一个阶段。** SNAPSHOT 调 `build_training_snapshot`；TRAIN 调 `train_draft/register_draft`；QUALIFY 调现有资格服务；只有阶段产物存在、hash 匹配且记录成功，才写下一个 stage。删除无条件 `next_stage="COMPLETE"`。
- [ ] **Step 4: 区分科学结果和执行错误。** NOT_QUALIFIED 写 COMPLETE/SUCCEEDED，并保留原因；证据不足写 NEEDS_ATTENTION；暂时 IO/runtime 错误按 30/120/600 秒最多三次 RETRY_WAIT；输入或产物 hash 漂移直接 NEEDS_ATTENTION。
- [ ] **Step 5: 支持恢复。** 重新领取时先校验已完成阶段的产物 hash；一致则从当前 stage 继续，不一致停下。owner 只能续租/完成自己持有的 job；取消后任何 worker 都不能推进。
- [ ] **Step 6: 计算真实 eligibility。** 从上次成功训练 snapshot 的事件集合计算新增有效候选、新 source、正负数量和时间间隔，不接受生产调用方随意传入 stats。correction/undo 导致旧训练 lineage stale 时创建 NEEDS_ATTENTION 审计事件，不自动删除或替换线上模型。
- [ ] **Step 7: 接入反馈提交之后。** feedback/correction/undo 的权威事务先提交；随后 best-effort 调 `maybe_enqueue_learning`。排队失败不回滚用户反馈，但写结构化 warning。不得在 API/UI 请求中训练或运行 VLM。
- [ ] **Step 8: 测试真实阶段链。** 合成 snapshot 运行四次 `run_learning_once`，依次观察 SNAPSHOT→TRAIN→QUALIFY→COMPLETE，并确认生成一个 DRAFT；不存在 snapshot 不能成功；两个 SQLite 连接并发只有一个 owner；活租约不会被误判过期。

Run: `.venv/Scripts/python.exe -m pytest tests/test_learning_jobs.py tests/test_learning_pipeline_e2e.py tests/test_candidates_api.py -q`

- [ ] **Step 9: 提交。**

```powershell
git add app/services/learning_jobs.py app/services/preference_schema.py scripts/run_learning_worker.py app/services/preference_events.py app/routers/candidates.py tests/test_learning_jobs.py tests/test_learning_pipeline_e2e.py
git commit -m "feat: execute recoverable curve learning stages"
```

### Task 7：封死部署旁路并实现可验证回滚

**Files:**

- Modify: `app/services/curve_online.py`
- Modify: `app/services/curve_preference.py`
- Modify: `app/services/preference_schema.py`
- Modify: `tests/test_curve_deployment.py`
- Modify: `tests/test_curve_preference_promotion.py`

**Interfaces:** `activate_deployment(conn, qualification_dir: Path, expected_revision: int, operator: str, runtime_config: Mapping[str, Any]) -> DeploymentResult`；`rollback_deployment(conn, target_revision: int, expected_revision: int, operator: str, runtime_config: Mapping[str, Any]) -> DeploymentResult`。

- [ ] **Step 1: 抽取可复用的资格证据校验。** 将 `validate_promotion` 中与 DRAFT 状态无关的 bundle、清单、向量、三路分数和统计检查提取为 `validate_qualification_evidence(..., allowed_model_statuses)`。promotion 调用时只允许 DRAFT；deployment 调用时只允许 PUBLISHED。激活以 qualification bundle 目录为输入并执行相同证据校验，部署函数不代替 promotion。
- [ ] **Step 2: 绑定正确配置。** 从 qualification input manifest 读取真实 algorithm config hash、base/preference 权重、feature/extractor/profile/model identities；与规范化 runtime config 比较。不得以 `profile_snapshot_sha256` 充当 config hash，不得传常量 True。
- [ ] **Step 3: 校验实际重放。** 在激活事务前从冻结 vectors、features、profile 和模型重新计算 baseline/nominal/effective，逐行比较资格分数文件；统计门禁继续由 `validate_promotion`/资格服务重算。任一差异拒绝。
- [ ] **Step 4: 原子 CAS 部署。** 不使用 `MAX(revision)` 代表当前 active。为 revision 冻结 `qualification_bundle_path` 和 bundle manifest hash，并建立单行 `curve_model_deployment_current(slot='current', revision)`；`BEGIN IMMEDIATE` 内比较 expected revision、插入追加 revision、更新 current 指针并提交。数据库约束保证最多一个 current 指针。
- [ ] **Step 5: 回滚重新验证。** 回滚到历史 revision 前重新检查目标模型仍 PUBLISHED、资格包可读、特征/extractor/config 兼容；不兼容时只允许显式回到 baseline。baseline 回滚也追加 revision，保留审计链。
- [ ] **Step 6: 禁止隐式启用。** PUBLISHED、deployed 和配置 mode=active 分别记录。训练 worker 不能调用 promotion/deployment；激活部署不能写配置文件；仅有 active 配置但无匹配 current revision 时运行 BASELINE。
- [ ] **Step 7: 验证。** 篡改配置、资格包、模型、向量、标签、分数或 extractor 任一项都 REFUSED；两个连接使用相同 expected revision 只有一个成功；失败事务不留下 `is_active=0` 的半切状态。

Run: `.venv/Scripts/python.exe -m pytest tests/test_curve_deployment.py tests/test_curve_preference_promotion.py tests/test_curve_preference_qualification.py tests/test_curve_online.py -q`

- [ ] **Step 8: 提交。**

```powershell
git add app/services/curve_online.py app/services/curve_preference.py app/services/preference_schema.py tests/test_curve_deployment.py tests/test_curve_preference_promotion.py
git commit -m "fix: bind curve deployment to verified qualification"
```

### Task 8：端到端验收、默认关闭与交付报告

**Files:**

- Modify: `tests/test_learning_pipeline_e2e.py`
- Modify: `tests/test_curve_pipeline_capture.py`
- Modify: `tests/test_direct_staged_parity.py`
- Modify: `build_exe.spec`
- Create: `docs/reports/2026-09-new-video-learning-closure/README.md`

**Interfaces:** 不新增业务接口；验证 Task 1–7 的真实组合。

- [ ] **Step 1: 建立 disabled 基线。** 保存旧配置下合成视频 rank manifest、正式选中 IDs、排序分数和输出数；整改后逐字段比较相同。新增诊断字段允许存在，但不得改变业务字段。
- [ ] **Step 2: 建立 shadow 闭环。** fake 外置 runtime 加临时 DB 跑 staged/direct；断言 capture 完整、source 特征 AVAILABLE、baseline/nominal/effective 可重放、正式排序等于 baseline、materialize link 指向实际 GIF hash。
- [ ] **Step 3: 建立学习闭环。** 写入源隔离的合成 like/dislike；worker 从 eligibility 到 DRAFT 和资格结果完整运行。NOT_QUALIFIED 不部署；构造严格合格 fixture 后，只有显式 promotion + deployment + active config 才改变排序。
- [ ] **Step 4: 建立故障矩阵。** 外置 runtime 缺失、capture DB 锁、payload hash 损坏、worker 中断、资格包篡改、deployment CAS 冲突分别验证 fail-closed/旧行为保留契约。
- [ ] **Step 5: 验证打包边界。** PyInstaller 分析中不得引入 onnxruntime、torch 或重模型；打包程序在 feature worker 未安装时正常启动并显示 `inference_runtime_unavailable`，disabled 模式不调用它。
- [ ] **Step 6: 运行综合回归。**

```powershell
.venv/Scripts/python.exe -m pytest tests/test_candidate_capture.py tests/test_candidate_capture_ingest.py tests/test_curve_source_features.py tests/test_curve_source_runtime.py tests/test_curve_pipeline_capture.py tests/test_curve_online.py tests/test_curve_training_snapshot.py tests/test_learning_jobs.py tests/test_learning_pipeline_e2e.py tests/test_curve_deployment.py tests/test_library_ingest.py tests/test_rank_pipeline_preference.py tests/test_direct_staged_parity.py tests/test_curve_preference_model.py tests/test_curve_preference_qualification.py tests/test_curve_preference_promotion.py tests/test_candidates_api.py -q
```

- [ ] **Step 7: 运行静态和差异检查。** 使用仓库现有 mypy files 配置覆盖新增 `app/services`；运行 `git diff --check`。测试 warnings 单列报告，不把 warning 当失败隐藏，也不把现有 warning 算作本功能成功证据。
- [ ] **Step 8: 写交付报告。** 报告包含提交列表、实际测试命令/退出码、disabled 与 shadow 对比、每个闭环产物 hash、模型状态、部署状态和仍未执行的真实媒体/B0 项。状态只能写 `A/C CODE_ACCEPTED_SHADOW_DISABLED`，不能写连续性已上线。
- [ ] **Step 9: 提交。**

```powershell
git add tests/test_learning_pipeline_e2e.py tests/test_curve_pipeline_capture.py tests/test_direct_staged_parity.py build_exe.spec docs/reports/2026-09-new-video-learning-closure/README.md
git commit -m "test: verify new video learning closure end to end"
```

## 4. Review 检查点

每个检查点完成后停下，只提交 diff 与测试证据，不提前开启下一阶段：

1. **Checkpoint 1 — 数据可信：** Task 0–3。证明真实 pipeline 能留下不可变候选和 source 特征，旧 payload 不被覆盖。
2. **Checkpoint 2 — 数学一致：** Task 4–5。证明在线、资格和训练使用同一尺度、同一数据身份，source 角色无泄漏。
3. **Checkpoint 3 — 自动化真实：** Task 6。证明 worker 执行真实阶段、可恢复且永不自动发布。
4. **Checkpoint 4 — 发布安全：** Task 7–8。证明 promotion/deployment/config 三段隔离，端到端 shadow 完成。

任一检查点失败时，保持以下默认值：

```yaml
candidate_capture:
  enabled: false
continuity_gate:
  enabled: false
  mode: shadow
  curve_preference:
    enabled: false
    mode: shadow
curve_learning:
  enabled: false
  promotion_policy: manual
```

不要为了通过验收修改统计阈值、随机种子或把缺失证据补成中性值。

## 5. 与人物动作连续性 B 的衔接

本计划完成后具备的是：新视频曲线数据、偏好影子排序和自动 DRAFT 学习。它仍不等于“基于动作自然连贯性自动截取”。B 继续按原计划执行：

1. 人工标注代表性原视频候选的 ACCEPTABLE/INVALID 和失效类型；like/dislike 不可替代。
2. 修正幅度噪声底、主体占满画面时相机补偿、阶段窗口和 burst 采样。
3. 补 ReID、本地 VLM 及完整 sidecar 证据，重新测量 REVIEW 比例和资源。
4. B0 go/no-go 经复核后，才实现 continuity 领域表、门禁、复核队列和 MANUAL_PASS 续跑。
5. 安全资格与可用性资格同时通过后，才能将 `continuity_gate.mode` 从 shadow 改为 enforce。

A/C 的 capture、外置资源锁和 source 特征可以被 B 复用，但 B 不得把偏好模型输出当硬门证据。

## 6. 交给实施 Agent 的第一条工单

> 阅读 `docs/superpowers/plans/2026-09-11-new-video-learning-closure-remediation.md`、原设计和当前代码。只执行 Task 0：新增能够在当前实现上稳定失败的九项回归测试，不修改 app 代码、配置、数据库或真实媒体。运行定向测试，逐项记录失败与 R1–R9 的对应关系；窄范围提交测试后停下等待 review。

Plan complete. 推荐采用 subagent-driven-development：每个 Task 使用新的实施上下文，Checkpoint 由独立 reviewer 核对 diff 和测试。也可以在当前任务按 executing-plans 顺序执行，但仍必须在四个 Checkpoint 停下。
