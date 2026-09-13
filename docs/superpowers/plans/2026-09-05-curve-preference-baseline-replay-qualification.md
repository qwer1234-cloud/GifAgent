# Curve Preference Baseline Replay and Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` for every code task and `superpowers:verification-before-completion` before reporting completion. Execute tasks in order. Do not mutate the production database until Task 7's explicit promotion command, and do not enable production curve reranking in this plan.

**Goal:** 对最新的 `curve-v1` DRAFT 模型重放与当前生产排序同语义、且与 holdout 源视频完全隔离的基线，完成配对 source-grouped 资格评估；只有输入冻结、覆盖率、无泄漏、pairwise LCB 和 NDCG 门禁全部通过后，才允许通过独立命令把指定模型从 `DRAFT` 提升为 `PUBLISHED`。

**Architecture:** 生产库只作为只读源，通过 SQLite Online Backup 得到一致性工作快照。资格运行在快照中构建排除 holdout 源视频的 Preference Profile，复用当前 `PreferenceReranker` 和导出打分函数分别计算 `baseline`、名义曲线方案和最终置信度曲线方案。所有候选输入、模型、事件、向量、视觉评分、配置和输出均冻结为带 SHA-256 的运行产物。资格记录与模型发布分离；训练器只能写 `DRAFT`，发布器必须验证一个匹配当前模型与清单哈希的 `QUALIFIED` 记录。

**Tech Stack:** Python 3.11、SQLite、NumPy、现有 Preference Memory 服务、现有 `export_ranking`/`reranker` 纯函数、pytest、mypy；缺失历史视觉评分沿用当前 Ollama VLM 富化路径，但 GLM-5.3-Flash 不成为运行时或资格门禁依赖。

**Spec:** [人物语义连续性与身体曲线设计 §5.8](../specs/2026-09-04-person-semantic-continuity-body-curve-design.md)、[里程碑 0B 结论 §8–10](../../reports/2026-09-continuity-spike/milestone-0b-findings.md)

## Global Constraints

1. 生产库固定为 `dist/GifAgentUI/data/library.db`；开发库 `data/library.db` 不得被误用为生产资格依据。
2. 生产库在 Task 1–6 始终只读。读取快照必须使用 `sqlite3.Connection.backup()`，禁止直接复制仍有 WAL 的 `.db` 文件，禁止删除或 checkpoint 覆盖 `-wal`/`-shm`。
3. 不修改历史 `preference_events`、`candidate_gifs`、`candidate_vectors`、`curve_features`，不覆盖三条现有 DRAFT 模型记录。
4. 不把 `COALESCE(final_score, profile_score, base_rag_similarity)` 当成基线。门禁比较的是同一冻结候选集上“当前生产算法、curve 关闭”与“同一算法、curve 开启”的配对结果。
5. holdout 来源以目标 DRAFT 模型的 `holdout_manifest_json` 为唯一人口定义。运行前必须重算并验证 `holdout_manifest_sha256`，且逐行核对候选 ID、源视频、标签事件 ID、事件时间、曲线特征和 GIF 文件哈希。
6. 构建评估用 Preference Profile 时，排除所有 holdout `source_video_sha256`。不得使用当前已发布画像直接评估，因为它可能含有 holdout 来源反馈。
7. 候选评分以源视频内排序为单位；禁止跨源视频配对。bootstrap 以源视频组重采样，固定 `BOOTSTRAP_SEED=20260905`、`BOOTSTRAP_REPS=2000`。
8. 不做权重搜索。曲线名义权重固定为 0.10；只有 curve 可用时 `base_rag` 才从 0.55 调整为 0.45。curve 不可用时结果必须与当前实现逐字段兼容。
9. 曲线模型对特征输出的 logit 先经 sigmoid 映射到 `[0,1]`，再参与偏好评分；训练/评估中的排序指标仍可使用 logit，因为 sigmoid 单调，但混合打分只能使用概率。
10. 缺失视觉富化只能在资格快照或运行目录中补齐，不回写生产库。视觉模型不可用、JSON 无效、GIF 哈希不符或文件缺失均为资格失败，不得用默认中位分补造证据。
11. 当前 cinematic 证据未在历史 `candidate_gifs` 中冻结时，基线必须调用 `adult_moe_export_score()` 的生产回退语义（cinematic=0.5），并在报告中单列 `cinematic_fallback_count`；不得在脚本中复制另一套公式。
12. `PreferenceReranker` 的 curve 参数默认关闭。Task 1–7 不修改 `configs/models.yaml` 中的 `preference_memory.enabled`，也不在 `app/pipeline/ranking.py` 接入 curve。
13. 资格通过不等于上线。`PUBLISHED` 模型在本计划结束时仍无生产消费方；生产 shadow/接入需另开实施计划和验收。
14. 当前工作树已有与 Preference Memory 重叠的未提交变更。执行前先记录 `git status --short` 和相关文件 diff；每个提交只暂存本 Task 的文件，禁止 `git add -A`，不得覆盖用户改动。

## 0. 当前事实与本计划决策

截至 2026-09-05，生产库最新模型为 `curve-pref-v1-20260905120400`，状态 `DRAFT`，holdout 2,510 行、117 个源视频，`curve_model_confidence=NULL`。三条模型记录均为 DRAFT。

最新 holdout 的现状：

| 项目 | 数量 | 处理 |
|---|---:|---|
| 冻结 holdout 行 | 2,510 | 必须全部进入资格清单 |
| 已有 `nomic-embed-text:latest/768` clip 向量 | 2,386 | 复用并绑定 blob SHA-256 |
| 无文本且无向量 | 124 | 在 work.db 中完成 VLM 富化与文本向量生成；任一步失败均阻断资格 |
| 有 `gif_worthiness` 与 `sex_act` 的 VLM JSON | 2,386 | 复用并绑定 JSON 哈希 |
| 无 VLM 评分但有 artifact path | 124 | 在工作快照中调用现有富化路径补齐，失败即阻断资格 |
| `base_rag_similarity/profile_score/final_score` 非空 | 0 | 不再等待自然交集，也不直接 UPDATE 补列 |

### 资格数据流

```text
生产库（只读）
  └─ SQLite online backup ─> qualification/work.db
       ├─ 验证 DRAFT 模型 + train/holdout manifest 哈希
       ├─ 排除 holdout sources 构建隔离 Preference Profile
       ├─ 冻结/补齐 baseline evidence（仅 work.db / artifacts）
       └─ 同一批 holdout 候选配对打分
            ├─ baseline: 当前算法，curve off
            ├─ nominal:  curve weight 0.10, confidence 1.0
            └─ effective: curve weight 0.10 × 派生 confidence
                 └─ source-grouped metrics + bootstrap gates
                      ├─ NOT_QUALIFIED：模型保持 DRAFT
                      └─ QUALIFIED：生成不可变资格记录，等待显式 promote
```

### 门禁定义

资格状态只有 `QUALIFIED` 与 `NOT_QUALIFIED`，不使用模糊的 warning 通过：

- 模型状态必须为 `DRAFT`，模型与 train/holdout manifest 哈希全部自洽；
- holdout 行数至少 30，训练/holdout 源视频交集为 0，训练与 holdout 均同时有正负类；
- 2,510 个冻结候选必须全部能核对数据库行、源视频、有效事件身份、曲线特征和 artifact SHA；
- baseline base/profile/export score 覆盖率必须为 100%；124 个无文本候选须先在 work.db 中补齐与当前新候选相同的 VLM 文本和向量，不允许通过默认值制造可比较性；
- baseline 与 proposed 必须在完全相同的候选和源内正负 pair 上比较；
- 名义方案 `pairwise_delta_lcb95 > 0`；
- 按名义方案 LCB 派生 `confidence=min(data_confidence, clamp(lcb95/0.05,0,1))` 后重新打分，最终方案仍须 `pairwise_delta_lcb95 > 0`；
- 最终方案 `mean_source_ndcg_at_20_delta >= 0`；
- 报告必须包含 tie rate、缺失/回退计数、按标签与源位置的覆盖分桶。任一非有限指标直接失败。

### 精确打分契约

当前 `app/pipeline/ranking.py::_rank_clips_with_preference()` 并不直接使用 `PreferenceReranker.final_score`。资格工具必须复用这条真实路径，不得只比较曲线 logit 与 `candidate_gifs` 的某个历史列：

```python
baseline_breakdown = reranker.score(
    candidate_vector=vector,
    base_rag_similarity=gif_worthiness,
    scenario_keys=scenario_keys,
    profile_version=isolated_profile_version,
    enabled=True,
)
baseline_export_score = blend_export_scores(
    base_export_score,
    baseline_breakdown["profile_score"],
    0.50,
    0.50,
)

proposed_breakdown = reranker.score(
    candidate_vector=vector,
    base_rag_similarity=gif_worthiness,
    scenario_keys=scenario_keys,
    profile_version=isolated_profile_version,
    enabled=True,
    curve_probability=curve_score,
    curve_model_version=model.model_version,
    curve_model_confidence=qualification_confidence,
)
proposed_export_score = blend_export_scores(
    base_export_score,
    proposed_breakdown["profile_score"],
    0.50,
    0.50,
)
```

其中 `base_export_score` 必须来自 `_clip_base_export_payload()` 所依赖的同一底层纯函数；adult 模式调用 `adult_moe_export_score()`，并使用资格运行冻结的 `quality_ranking_adult_weight=0.80`、`quality_ranking_cinematic_weight=0.20`。外层 0.50/0.50 从当前 `preference_memory.base_score_weight/preference_score_weight` 冻结，不在 holdout 上调参。若运行配置不是这些值，输入 manifest 必须记录实际值，并以当前实际值重放。

为避免资格公式与生产入口再次分叉，Task 5 应新增公共纯函数 `score_preference_export_candidate()`，让资格工具使用它；后续 shadow 集成也必须使用同一函数。此计划不在 `app/pipeline/ranking.py` 切换调用方，但测试会证明公共函数与当前入口的输出一致。

## File Map

### Create

- `app/services/curve_preference.py` — 模型加载、哈希校验、sigmoid 概率评分、curve-aware 纯打分契约。
- `app/services/curve_preference_qualification.py` — 快照、输入冻结、配对 source-grouped 指标、门禁与资格产物。
- `app/services/preference_profile_snapshot.py` — 在工作快照中构建 source-excluded 画像并导出不可变画像快照。
- `scripts/qualify_curve_preference_model.py` — dry-run/正式资格 CLI；生产库保持只读。
- `scripts/promote_curve_preference_model.py` — 验证资格记录后执行唯一允许的 DRAFT→PUBLISHED 转换。
- `tests/test_curve_preference_scoring.py`
- `tests/test_curve_preference_qualification.py`
- `tests/test_curve_preference_promotion.py`
- `docs/reports/2026-09-curve-preference-qualification/README.md` — 实际运行证据模板和最终 go/no-go 结论。

### Modify

- `app/services/preference_schema.py` — 将 curve 模型 schema 纳入幂等主迁移；新增资格运行表。
- `app/services/preference_types.py` — 增加 curve 打分与资格运行 TypedDict/dataclass。
- `app/services/reranker.py` — 增加默认关闭的 curve 分量，保持 curve 缺失路径兼容。
- `app/services/preference_evaluation.py` — 抽取可对内存评分记录工作的 source-grouped 指标，现有 API 复用。
- `scripts/train_curve_preference_model.py` — 复用模型服务；训练阶段只能写 DRAFT，不再直接 PUBLISHED。
- `scripts/enrich_library_candidates.py` — 抽出可复用的 VLM 证据解析，不改变原 CLI 行为。
- `tests/test_curve_preference_model.py`
- `tests/test_preference_reranker.py`
- `tests/test_preference_evaluation.py`
- `tests/test_startup_schema.py`

### Explicitly Not Modified

- `app/pipeline/ranking.py`
- `app/pipeline/stages/rank_dedup.py`
- `configs/models.yaml` 及任何运行配置
- 生产 `candidate_gifs`/`preference_events` 历史数据

## Task 1: Centralize the Curve Model and Qualification Schema

**Files:**

- Modify: `app/services/preference_schema.py`
- Modify: `app/services/preference_types.py`
- Create: `app/services/curve_preference.py`
- Modify: `scripts/train_curve_preference_model.py`
- Modify: `tests/test_startup_schema.py`
- Create: `tests/test_curve_preference_scoring.py`

### Step 1: Write failing schema and model-integrity tests

Add tests requiring `apply_preference_schema()` to create/migrate:

```sql
CREATE TABLE IF NOT EXISTS curve_preference_models (
    model_version TEXT PRIMARY KEY,
    feature_version TEXT NOT NULL,
    extractor_sha256 TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK(status IN ('DRAFT','PUBLISHED','REJECTED','SUPERSEDED')),
    coefficients_json TEXT NOT NULL,
    intercept REAL NOT NULL,
    feature_mean_json TEXT NOT NULL,
    feature_std_json TEXT NOT NULL,
    feature_names_json TEXT NOT NULL,
    l2_lambda REAL NOT NULL,
    training_event_watermark TEXT NOT NULL,
    training_source_set_sha256 TEXT NOT NULL,
    holdout_source_set_sha256 TEXT NOT NULL DEFAULT '',
    training_manifest_sha256 TEXT NOT NULL DEFAULT '',
    holdout_manifest_sha256 TEXT NOT NULL DEFAULT '',
    training_manifest_json TEXT NOT NULL DEFAULT '{}',
    holdout_manifest_json TEXT NOT NULL DEFAULT '{}',
    holdout_row_count INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    curve_model_confidence REAL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS curve_preference_qualification_runs (
    qualification_id TEXT PRIMARY KEY,
    model_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('QUALIFIED','NOT_QUALIFIED')),
    input_manifest_sha256 TEXT NOT NULL,
    profile_snapshot_sha256 TEXT NOT NULL,
    baseline_scores_sha256 TEXT NOT NULL,
    proposed_scores_sha256 TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    report_path TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    source_db_snapshot_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    qualified_at TEXT,
    FOREIGN KEY(model_version) REFERENCES curve_preference_models(model_version)
);
```

Tests must prove the migration is idempotent and preserves pre-existing model rows.

Add model-loader tests that reject:

- non-DRAFT target during qualification;
- malformed feature name/mean/std/coefficient key sets;
- zero or non-finite std/coefficients/intercept;
- recomputed train or holdout manifest hash mismatch;
- requested `feature_version` not equal to the model row.

Run:

```powershell
python -m pytest tests/test_startup_schema.py tests/test_curve_preference_scoring.py -q
```

Expected: failures because the centralized schema and loader do not exist.

### Step 2: Implement model types and loader

In `app/services/curve_preference.py`, implement these public functions:

```python
@dataclass(frozen=True)
class CurvePreferenceModel:
    model_version: str
    feature_version: str
    extractor_sha256: str
    feature_names: tuple[str, ...]
    mean: np.ndarray
    std: np.ndarray
    coefficients: np.ndarray
    intercept: float
    training_manifest_sha256: str
    holdout_manifest_sha256: str
    training_manifest: dict[str, object]
    holdout_manifest: dict[str, object]

def load_curve_model(
    conn: sqlite3.Connection,
    model_version: str,
    *,
    allowed_statuses: frozenset[str],
) -> CurvePreferenceModel:
    """Load and validate one exact version; raise ValueError on mismatch."""

def curve_logit(
    model: CurvePreferenceModel,
    features: Mapping[str, object],
) -> float:
    """Return the standardized linear score or raise EvidenceUnavailable."""

def curve_probability(
    model: CurvePreferenceModel,
    features: Mapping[str, object],
) -> float:
    """Return a stable sigmoid of curve_logit in the closed unit interval."""
```

`curve_probability()` 必须稳定实现 sigmoid，极端 logit 不能 overflow；缺少任一模型特征、非数值或非有限值时返回显式 unavailable 结果，不填 0。

把 `_ensure_model_columns()` 从训练脚本移到 `apply_preference_schema()`。训练脚本调用公共 schema/loader，不再维护第二份 DDL。

### Step 3: Close the direct-publish bypass

修改 `scripts/train_curve_preference_model.py`：

- 保留 `--publish` 仅用于兼容旧命令，但其含义改为“持久化训练产物”；写入状态始终为 `DRAFT`；
- 输出明确提示：`training completed; qualification required before publication`；
- 删除训练器中把其他 PUBLISHED 模型改为 SUPERSEDED 的代码路径；
- 保留诊断指标，但它们不能改变 DRAFT 状态；
- 后续唯一发布入口为 Task 7 的 promotion service。

增加回归测试：即使伪造一个正的 paired delta，训练脚本也不能直接写 PUBLISHED。

### Step 4: Verify and commit narrowly

```powershell
python -m pytest tests/test_startup_schema.py tests/test_curve_preference_model.py tests/test_curve_preference_scoring.py -q
python -m mypy app/services/curve_preference.py
git add app/services/preference_schema.py app/services/preference_types.py app/services/curve_preference.py scripts/train_curve_preference_model.py tests/test_startup_schema.py tests/test_curve_preference_model.py tests/test_curve_preference_scoring.py
git commit -m "refactor: centralize curve preference model lifecycle"
```

## Task 2: Add a Curve-Aware Reranker Contract Without Enabling It

**Files:**

- Modify: `app/services/reranker.py`
- Modify: `app/services/preference_types.py`
- Modify: `tests/test_preference_reranker.py`
- Modify: `tests/test_curve_preference_scoring.py`

### Step 1: Freeze current no-curve behavior

Before changing implementation, add characterization tests over these cases:

- `enabled=False`;
- no current profile;
- global like only;
- like + dislike + scenario;
- missing caption/vector path as represented by caller fallback.

For fixed input vectors and profile rows, snapshot the full result dict. The test must prove no-curve output remains byte-equivalent after the signature expands.

### Step 2: Specify the new API

Extend `PreferenceReranker.score()` with keyword-only optional inputs:

```python
curve_probability: float | None = None,
curve_model_version: str | None = None,
curve_model_confidence: float | None = None,
```

Rules:

- all three are `None` → current code path exactly;
- partial inputs, non-finite values, or values outside `[0,1]` → curve inactive with an explicit reason;
- valid curve input → nominal positive weights become `base_rag=0.45`, `curve_preference=0.10*confidence`, `global_like=0.25`, `scenario_like=0.15`；
- valid curve unavailable on one candidate → restore that candidate's current no-curve weight set, not a zero curve score;
- positive weights continue availability-aware renormalization；negative dislike remains an independently subtracted component；
- `profile_score` must include curve using the effective curve weight, not nominal 0.10；
- result adds `curve_preference_score` and `curve_model_version` for auditability.

### Step 3: Test exact edge behavior

Required tests:

- confidence 0 produces no ranking influence;
- confidence 0.5 produces exactly half the pre-normalization curve weight of confidence 1；
- unavailable curve does not inject 0.5；
- curve probability near 0 lowers the proposed score, near 1 raises it；
- no-curve result dict matches the characterization fixture；
- all final scores remain finite and clamped to `[0,1]`。

### Step 4: Verify and commit

```powershell
python -m pytest tests/test_preference_reranker.py tests/test_curve_preference_scoring.py -q
git add app/services/reranker.py app/services/preference_types.py tests/test_preference_reranker.py tests/test_curve_preference_scoring.py
git commit -m "feat: add inactive-by-default curve reranker component"
```

## Task 3: Build a Source-Isolated Preference Profile Snapshot

**Files:**

- Create: `app/services/preference_profile_snapshot.py`
- Create: `tests/test_curve_preference_qualification.py`
- Modify: `app/services/preference_memory.py` only if a pure helper must be extracted

### Step 1: Write leakage tests first

Construct an in-memory database with at least four source videos and feedback corrections. Tests must prove:

- every holdout source is absent from global centroids, scenario centroids, tag priors and profile counts;
- a holdout candidate whose vector is identical to a train candidate still cannot enter the profile by ID or source；
- current published profile is not read by the snapshot builder；
- build inputs and outputs are deterministic for the same database snapshot；
- changing one training vector, effective event, tag or ProfileBuildConfig changes the snapshot SHA。

### Step 2: Implement snapshot construction in a work database

Public API:

```python
@dataclass(frozen=True)
class PreferenceProfileSnapshot:
    profile_version: str
    event_watermark: str
    excluded_source_videos: tuple[str, ...]
    config_json: str
    manifest: dict[str, object]
    manifest_sha256: str

def build_source_isolated_profile_snapshot(
    conn: sqlite3.Connection,
    *,
    excluded_source_videos: set[str],
    config: ProfileBuildConfig,
) -> PreferenceProfileSnapshot:
    """Build in work.db and freeze every event/vector/profile input hash."""
```

Implementation can call `PreferenceMemoryService.build_profile(dry_run=False, config=config, exclude_source_videos=excluded_source_videos)` because it runs only on `work.db`。Then export every generated profile row into canonical JSON：scope、scenario key、counts、confidence、tag weights、centroid blob SHA-256 and base64 blob。Manifest must also bind every effective training event ID/time/rating/source and every consumed vector blob hash。

Do not add an “as-of historical watermark” mode in this task. Qualification compares the frozen curve model against the current production algorithm at qualification time；clean evaluation is obtained by excluding the entire holdout source set。Record the qualification-time event watermark separately from the curve model's training watermark。

### Step 3: Verify and commit

```powershell
python -m pytest tests/test_curve_preference_qualification.py tests/test_preference_profile_v2.py tests/test_preference_holdout_split.py -q
git add app/services/preference_profile_snapshot.py app/services/preference_memory.py tests/test_curve_preference_qualification.py
git commit -m "feat: build source-isolated preference profile snapshots"
```

If `app/services/preference_memory.py` required no edit, do not stage it.

## Task 4: Freeze and Complete Baseline Evidence in the Work Snapshot

**Files:**

- Modify: `scripts/enrich_library_candidates.py`
- Modify: `app/services/curve_preference_qualification.py`
- Modify: `tests/test_curve_preference_qualification.py`

### Step 1: Extract reusable enrichment parsing

Move the existing VLM response parsing and normalized evidence mapping into import-safe functions; keep CLI defaults and prompt behavior unchanged:

```python
def parse_enrichment_response(
    raw: str,
    *,
    model: str,
    enriched_at: str,
) -> dict[str, object]:
    """Validate one VLM result and return the normalized evidence mapping."""

def score_evidence_from_summary(
    summary: Mapping[str, object],
) -> dict[str, object] | None:
    """Return normalized worthiness/sex-act evidence when both are present."""
```

Tests must cover fenced JSON、invalid JSON、missing caption、0–100 score normalization and model identity fields。

### Step 2: Implement qualification input validation

For each holdout manifest row:

1. load `candidate_gifs` and `curve_features` from `work.db`；
2. verify candidate ID/source and serialized curve features equal the frozen holdout manifest；
3. verify the manifest's effective event still exists with the same ID/time/rating；a later correction does not rewrite the frozen label, but is reported as `post_manifest_event_change` and blocks qualification until a new model is trained；
4. hash the GIF file and compare to `artifact_sha256`；
5. load `gif_worthiness`/`sex_act` from `vlm_summary_json`；
6. if absent, run the existing local enrichment model against that GIF and write the result only to `qualification/evidence.jsonl` and `work.db`；
7. call `adult_moe_export_score()` to get the base export score；do not copy its formula；
8. load the text vector when present and bind `embedding_model`、dimension、`source_text_hash` and vector blob SHA-256；富化后仍缺向量时，在 `work.db` 中调用现有 `upsert_candidate_vector()`，使用固定 `nomic-embed-text:latest/768`；生成失败则阻断资格，不回写生产库。

The canonical input record must include：

```json
{
  "candidate_id": "cand_example",
  "source_video_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "rating": "like",
  "event_id": "prefevt_example",
  "event_created_at": "2026-09-05T12:00:00+00:00",
  "artifact_path": "C:/example/candidate.gif",
  "artifact_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
  "curve_features": {"pose_extension": 0.75},
  "curve_features_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
  "vlm_evidence": {"gif_worthiness": 74, "sex_act": 63},
  "vlm_evidence_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
  "base_export_score": 0.0,
  "candidate_vector_sha256": "4444444444444444444444444444444444444444444444444444444444444444",
  "profile_fallback_reason": null
}
```

### Step 3: Enforce coverage and failure semantics

Add tests proving:

- a missing/mismatched artifact blocks qualification before model scoring；
- one failed VLM enrichment blocks 100% base score coverage；
- missing vector is generated only in work.db after VLM enrichment；embedding failure yields `base_score_coverage_incomplete` and cannot qualify；
- `cinematic_score` absence increments `cinematic_fallback_count` and uses the existing production fallback；
- rerunning against identical work.db and evidence cache produces identical input manifest SHA；
- no code path issues UPDATE/INSERT against the source production connection。

### Step 4: Verify and commit

```powershell
python -m pytest tests/test_curve_preference_qualification.py -q
git add scripts/enrich_library_candidates.py app/services/curve_preference_qualification.py tests/test_curve_preference_qualification.py
git commit -m "feat: freeze reconstructable curve qualification evidence"
```

## Task 5: Implement Paired Source-Grouped Metrics and Gates

**Files:**

- Modify: `app/services/reranker.py`
- Modify: `app/services/preference_evaluation.py`
- Modify: `app/services/curve_preference_qualification.py`
- Modify: `tests/test_preference_reranker.py`
- Modify: `tests/test_preference_evaluation.py`
- Modify: `tests/test_curve_preference_qualification.py`

### Step 1: Extract an in-memory grouped evaluator

Add a pure function used by both qualification and the existing service：

```python
def evaluate_source_grouped_scores(
    rows: Sequence[GroupedScoreRow],
    *,
    baseline_field: str,
    proposed_field: str,
    bootstrap_seed: int,
    bootstrap_reps: int,
) -> SourceGroupedComparison:
    """Return deterministic paired metrics grouped only by source video."""
```

`GroupedScoreRow` carries candidate ID、source、rating、baseline、proposed。Metrics：

- positive-vs-negative pair count per source；
- strict pairwise accuracy using the current `score_pos > score_neg` contract；
- tie rate reported separately；
- mean per-source NDCG@20 using `GAIN_MAP`；
- paired source bootstrap distributions and 5th-percentile LCB for pairwise delta；
- comparable row/source counts and non-finite count。

Do not silently change existing API response fields；have `PreferenceEvaluationService.evaluate_source_grouped()` adapt its SQL rows into this helper and preserve current keys。

### Step 2: Implement the two-pass confidence protocol

First add the shared pure adapter in `app/services/reranker.py`：

```python
def score_preference_export_candidate(
    *,
    reranker: PreferenceReranker,
    candidate_vector: np.ndarray,
    gif_worthiness: float,
    scenario_keys: list[str],
    profile_version: str,
    base_export_score: float,
    base_score_weight: float,
    preference_score_weight: float,
    curve_probability: float | None = None,
    curve_model_version: str | None = None,
    curve_model_confidence: float | None = None,
) -> dict[str, object]:
    """Mirror the current export blend and optionally add the curve component."""
```

When `curve_probability` is `None`, its result must equal the current `_rank_clips_with_preference()` scoring body for the same inputs。If `profile_score` is unavailable, it returns the unmodified `base_export_score` and a stable fallback reason。

For the exact same input records：

1. compute baseline with curve absent；
2. compute nominal proposed with `curve_model_confidence=1.0`；
3. evaluate nominal paired delta LCB；
4. derive confidence from the frozen formula；
5. recompute proposed scores with that confidence；
6. evaluate the effective paired delta LCB and NDCG delta。

No weight, seed, split or threshold may be selected from holdout results。Both nominal and effective results must be persisted。

### Step 3: Implement a single fail-closed decision function

```python
def decide_curve_qualification(
    inputs: QualificationInputs,
) -> QualificationDecision:
    """Collect all fail-closed reasons and return the immutable decision."""
```

The function collects all precondition failures before evaluating outcome。Any reason means `NOT_QUALIFIED`。Required stable reason codes：

- `model_not_draft`
- `model_manifest_hash_mismatch`
- `source_overlap`
- `holdout_too_small`
- `missing_class`
- `candidate_identity_mismatch`
- `effective_event_changed`
- `artifact_missing`
- `artifact_hash_mismatch`
- `base_score_coverage_incomplete`
- `no_within_source_pairs`
- `non_finite_metric`
- `nominal_pairwise_lcb_not_positive`
- `effective_pairwise_lcb_not_positive`
- `effective_ndcg_regression`

### Step 4: Add adversarial tests

At minimum：

- good curve vs bad baseline qualifies；
- bad curve vs good baseline fails；
- source overlap fails even if delta is excellent；
- a high point estimate with LCB≤0 fails；
- nominal pass but confidence-scaled effective score fails；
- effective pairwise pass with NDCG regression fails；
- tied baseline pairs are counted consistently and reported；
- candidate ordering changes do not change metrics or manifest hashes；
- changing one source assignment changes the manifest and can trigger overlap。

### Step 5: Verify and commit

```powershell
python -m pytest tests/test_preference_reranker.py tests/test_preference_evaluation.py tests/test_curve_preference_qualification.py -q
python -m mypy app/services/curve_preference_qualification.py
git add app/services/reranker.py app/services/preference_evaluation.py app/services/curve_preference_qualification.py tests/test_preference_reranker.py tests/test_preference_evaluation.py tests/test_curve_preference_qualification.py
git commit -m "feat: gate curve models with paired source grouped replay"
```

## Task 6: Build the Qualification CLI and Immutable Run Bundle

**Files:**

- Create: `scripts/qualify_curve_preference_model.py`
- Modify: `app/services/curve_preference_qualification.py`
- Modify: `tests/test_curve_preference_qualification.py`
- Create: `docs/reports/2026-09-curve-preference-qualification/README.md`

### Step 1: Define the CLI contract

```powershell
python scripts/qualify_curve_preference_model.py `
  --db dist/GifAgentUI/data/library.db `
  --model-version curve-pref-v1-20260905120400 `
  --work-dir data/curve_preference_qualification `
  --dry-run
```

Dry-run must：

- open source DB read-only；
- validate model/manifests and count required artifacts；
- report how many VLM enrichments are needed；
- report expected disk use and current model/provider identity；
- perform no VLM calls、no source DB writes、no qualification DB insert。

正式命令去掉 `--dry-run`。每次运行生成唯一 `qualification_id`，不得覆盖旧目录。

### Step 2: Implement SQLite snapshot safety

Sequence：

1. create `{work_dir}/{qualification_id}/`；
2. open source connection URI with `mode=ro`；
3. call `source_conn.backup(work_conn)`；
4. run `PRAGMA integrity_check` on `work.db`；
5. hash the consistent `work.db` snapshot；
6. all subsequent builds/writes target `work.db` only；
7. before finalizing, reopen the production source read-only and verify target model/manifests have not changed。

Never place temp DB in the repository root。Interrupted runs retain `run_state.json` with `INCOMPLETE` and may resume only when source/model/input hashes match。

### Step 3: Write the immutable bundle

Required files：

```text
data/curve_preference_qualification/{qualification_id}/
  run_state.json
  input_manifest.json
  input_manifest.sha256
  profile_snapshot.json
  profile_snapshot.sha256
  evidence.jsonl
  baseline_scores.jsonl
  nominal_scores.jsonl
  effective_scores.jsonl
  metrics.json
  report.md
  work.db
```

Every JSON uses sorted keys and rejects NaN/Infinity。JSONL rows sort by `(source_video_sha256, candidate_id)`。`report.md` includes the exact command、Git commit/worktree diff hash、source DB snapshot hash、model/manifests、profile snapshot、provider/model identity、coverage、fallbacks、metrics、gate reasons and final status。

### Step 4: Test interruption and determinism

Use tmp databases and fake VLM functions。Tests：

- dry-run writes no DB rows；
- interruption after 10 rows resumes without rescoring completed evidence；
- changed source DB/model hash refuses resume；
- two complete runs with the same frozen inputs produce equal content hashes except qualification ID/timestamps；
- NOT_QUALIFIED run still preserves full evidence and report；
- no source DB mutation by comparing `Connection.total_changes` and pre/post logical table hashes。

### Step 5: Verify and commit

```powershell
python -m pytest tests/test_curve_preference_qualification.py -q
python scripts/qualify_curve_preference_model.py --help
git add scripts/qualify_curve_preference_model.py app/services/curve_preference_qualification.py tests/test_curve_preference_qualification.py docs/reports/2026-09-curve-preference-qualification/README.md
git commit -m "feat: add reproducible curve qualification replay"
```

## Task 7: Add Explicit, Atomic Promotion

**Files:**

- Create: `scripts/promote_curve_preference_model.py`
- Modify: `app/services/curve_preference.py`
- Create: `tests/test_curve_preference_promotion.py`

### Step 1: Write promotion refusal tests

Promotion must refuse when：

- qualification status is not QUALIFIED；
- model is not DRAFT；
- report/input/profile/score file hash differs；
- qualification points to another model version or manifest hash；
- model row changed after qualification；
- qualification ID already exists in production with a different content hash；
- more than one PUBLISHED row would remain after transaction。

### Step 2: Implement the promotion transaction

Command：

```powershell
$qualificationId = 'replace-with-reviewed-qualification-id'
python scripts/promote_curve_preference_model.py `
  --db dist/GifAgentUI/data/library.db `
  --model-version curve-pref-v1-20260905120400 `
  --qualification-dir "data/curve_preference_qualification/$qualificationId"
```

Behavior：

1. make a timestamped SQLite online backup before mutation；
2. open `BEGIN IMMEDIATE`；
3. revalidate every qualification and artifact hash；
4. insert the immutable qualification row from the bundle；if the same ID and hashes already exist, treat it as an idempotent retry；
5. set previous PUBLISHED model to SUPERSEDED；
6. set only target DRAFT model to PUBLISHED，write `curve_model_confidence`、`published_at`；
7. verify exactly one PUBLISHED model；
8. commit；any failure rolls back。

Add `--dry-run` that performs every validation but no backup or transaction write。Promotion does not modify reranking config and does not make pipeline consume the model。

### Step 3: Verify and commit

```powershell
python -m pytest tests/test_curve_preference_promotion.py tests/test_curve_preference_model.py -q
git add scripts/promote_curve_preference_model.py app/services/curve_preference.py tests/test_curve_preference_promotion.py
git commit -m "feat: require qualified replay for curve model promotion"
```

## Task 8: Production Dry-Run, Full Replay, and Acceptance Report

**Files:**

- Modify: `docs/reports/2026-09-curve-preference-qualification/README.md`
- Runtime artifacts only: `data/curve_preference_qualification/{qualification_id}/`

### Step 1: Record preflight evidence

```powershell
git status --short
git rev-parse HEAD
Get-Process GifAgentCore,python -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,Path
& 'C:\Users\sunhao\anaconda3\Library\bin\sqlite3.exe' -readonly 'dist/GifAgentUI/data/library.db' "PRAGMA integrity_check;"
python scripts/qualify_curve_preference_model.py --db dist/GifAgentUI/data/library.db --model-version curve-pref-v1-20260905120400 --work-dir data/curve_preference_qualification --dry-run
```

Dry-run acceptance：target remains DRAFT；2,510 holdout rows recognized；117 holdout sources；expected enrichment count is reported；production table hashes unchanged。

### Step 2: Run a 30-candidate rehearsal on a disposable snapshot

Add/use `--limit 30` only as a diagnostic mode that can never emit QUALIFIED。Verify model/provider availability、artifact decoding、JSON parse rate、per-row latency and resume。Record results in the report。

```powershell
python scripts/qualify_curve_preference_model.py --db dist/GifAgentUI/data/library.db --model-version curve-pref-v1-20260905120400 --work-dir data/curve_preference_qualification --limit 30
```

### Step 3: Run the full 2,510-row qualification

```powershell
python scripts/qualify_curve_preference_model.py --db dist/GifAgentUI/data/library.db --model-version curve-pref-v1-20260905120400 --work-dir data/curve_preference_qualification
```

Do not promote in the same command or automatically after success。

### Step 4: Independently verify the bundle

```powershell
python -m pytest tests/test_curve_preference_model.py tests/test_curve_preference_scoring.py tests/test_curve_preference_qualification.py tests/test_curve_preference_promotion.py tests/test_preference_reranker.py tests/test_preference_evaluation.py tests/test_rank_pipeline_preference.py -q
python -m pytest -q
```

Use a separate read-only verifier invocation to recompute every bundle SHA and metric from JSONL。Compare production DB logical counts and event hashes before/after；they must be unchanged。

### Step 5: Stop for user review

Update the report with one of：

- `GO_FOR_EXPLICIT_PROMOTION`：all gates pass，include the exact dry-run promotion command；
- `NO_GO_RETRAIN_OR_REDESIGN`：list stable reason codes and the smallest next experiment；
- `BLOCKED_EVIDENCE_INCOMPLETE`：list candidate IDs/reasons，model remains DRAFT。

Even for GO, stop before Task 7's non-dry-run promotion unless the user explicitly confirms。

## Task 9: Optional Promotion After Explicit Approval

This task is deliberately conditional and is not part of automatic execution。

### Step 1: Validate only

```powershell
$qualificationId = 'replace-with-reviewed-qualification-id'
python scripts/promote_curve_preference_model.py --db dist/GifAgentUI/data/library.db --model-version curve-pref-v1-20260905120400 --qualification-dir "data/curve_preference_qualification/$qualificationId" --dry-run
```

### Step 2: Show the user

Provide：qualification ID、model version、effective confidence、pairwise delta LCB、NDCG delta、backup destination and the fact that pipeline consumption remains disabled。

### Step 3: Promote only after explicit confirmation

```powershell
$qualificationId = 'replace-with-reviewed-qualification-id'
python scripts/promote_curve_preference_model.py --db dist/GifAgentUI/data/library.db --model-version curve-pref-v1-20260905120400 --qualification-dir "data/curve_preference_qualification/$qualificationId"
```

### Step 4: Post-promotion checks

```powershell
& 'C:\Users\sunhao\anaconda3\Library\bin\sqlite3.exe' -readonly -header -column 'dist/GifAgentUI/data/library.db' "SELECT model_version,status,curve_model_confidence,published_at FROM curve_preference_models ORDER BY created_at DESC;"
python -m pytest tests/test_curve_preference_promotion.py tests/test_curve_preference_model.py -q
```

Expected：exactly one PUBLISHED；target confidence matches qualification；旧模型为 DRAFT 或 SUPERSEDED；candidate/event/feature counts unchanged。

## Completion Criteria

本计划完成必须同时满足：

1. 训练器无法直接发布 curve 模型，只有验证过的 qualification record 可解锁 promotion。
2. 生产库在资格重放期间保持逻辑只读，且有前后哈希/计数证据。
3. baseline 与 proposed 使用同一候选、同一 source-group pair、同一 source-excluded Profile 和同一 base export evidence。
4. 2,510 个 holdout 输入全部绑定模型清单、事件身份、特征、artifact、视觉评分和向量身份；无隐式默认分。
5. curve 不可用时 `PreferenceReranker` 保持当前行为；curve 有效时只按固定 0.10×confidence 影响评分。
6. qualification bundle 可独立重算，所有关键文件均有 SHA-256，重复运行结果可复现。
7. focused tests、全量 pytest 与新服务 mypy 通过；报告包含命令、实际数字和失败原因。
8. 不修改生产排序入口或配置；后续 shadow 集成另立计划。

## Follow-up Plan Boundary

只有模型经本计划显式 promotion 后，才编写下一份“curve preference production shadow integration”实施计划。下一阶段应覆盖：

- 在 `rank_dedup` 的 continuity/Quality MoE 通过候选上读取同版本 `curve_features`；
- 把 curve 分量接到 `app/pipeline/ranking.py`，先 `report_only/shadow`；
- 记录 baseline/proposed 双分数但只按 baseline 导出；
- staged/direct 一致性、缺特征回退、模型热加载和 EXE schema 迁移；
- canary 累积真实新候选反馈后，再决定是否让 proposed 排序生效。

本计划不提前实现这些生产行为，避免“资格评估”和“上线影响”在同一变更中互相污染。
