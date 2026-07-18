# Stage Split 复核问题实施计划（2026-07-18）

## 1. 目标与完成定义

本计划用于修复 `STAGE_SPLIT_RECHECK_FIX_REPORT_2026-07-18.md` 二次 Review 后仍存在的问题。目标不是让现有测试继续通过，而是让真实 Worker 能够可靠执行以下链路：

```text
discover → sample → vlm → refine → synthesize → rank_dedup
                                                     ├─ gif_clip(clip-1)
                                                     ├─ gif_clip(clip-2)
                                                     └─ gif_clip(clip-N)
                                                               ↓
                                                          materialize
```

完成必须同时满足：

- 每个 Stage 只执行自己的职责。
- 下游输入通过数据库中已验证的 Artifact 解析，不通过目录猜测传递。
- 0、1、N 个 clip 都能进入正确终态。
- 每个 GIF 可以独立失败、独立重试，成功 GIF 不重新生成。
- materialize 能聚合所有成功 GIF，并写入正式导出目录。
- Artifact 入库与 Stage 完成具有原子语义。
- Heartbeat 使用同一个数据库和同一套 lease 配置，不允许重复领取窗口。
- Quality Lab 使用完整、可复现的实验配置。
- 视频、Job 和实验状态不会把 cancelled/partial failure 误报为 succeeded。

在本计划全部验收前，不提交、不构建 EXE、不推送远程。

## 2. 已确认的当前故障

以下结论已经用当前代码和临时数据库复现，不需要再次讨论是否存在：

1. Worker 写入 `prev_stage_work_dir`，stage-mode 读取 `prior_stage_work_dirs`，真实 Manifest 传递仍然断开。
2. 默认 Artifact ID 为 `discover_0`、`sample_0` 等，在全局主键下会跨视频碰撞，`INSERT OR IGNORE` 静默丢弃后续记录。
3. materialize 只扫描自己的隔离目录，已有 sibling gif_clip GIF 时仍得到 `gif_count=0`。
4. zero-clip 分支调用会拒绝无 gif_clip 的 `_check_create_materialize()`，链停在 rank_dedup。
5. 默认 Stage lease 为 90 秒，但 Heartbeat 使用 `max_delay_seconds=300` 后首次等待 100 秒，存在重复领取窗口。
6. Artifact 逐条 commit；第二个 Artifact 失败时，第一个已永久写入。
7. Quality Lab 只保留实验配置中的 `adaptive`，丢弃 `vlm` 和其他字段。
8. 所有 Stage 终态时，cancelled 可能被聚合为 succeeded。
9. 当前 fan-out、zero-clip、并发和“完整链”测试存在条件断言或绕过 Worker/Adapter 的情况。

## 3. 实施原则

### 3.1 数据库是依赖关系的唯一事实来源

Stage 不得通过以下方式查找输入：

- 猜测其他 Stage 的工作目录。
- 从当前 `work_dir` 查找上一阶段文件。
- 根据文件名是否包含 `clip` 判断 Artifact 类型。
- 依赖“最近一个 succeeded Stage”推断输入。

所有输入必须由 `task_stages + task_artifacts` 解析，并验证文件存在、大小和 SHA-256。

### 3.2 配置和输入依赖不得混在一起

不要继续向 `StageContext.config` 注入 `prev_stage_work_dir` 或 `prior_stage_work_dirs`。配置快照应保持不可变；输入 Artifact 应使用独立字段传递。

建议扩展 `StageContext`：

```python
@dataclass(frozen=True)
class StageContext:
    job_id: str
    video_id: str
    stage_id: str
    video_path: Path
    clip_id: str | None
    input_key: str
    work_dir: Path
    config: Mapping[str, object]
    inputs: Mapping[str, tuple[ArtifactRef, ...]]
```

其中 `inputs` 的键使用明确的逻辑类型，例如：

```text
discover_manifest
sample_manifest
vlm_manifest
refine_manifest
synthesize_manifest
rank_dedup_manifest
gif_file
gif_clip_manifest
```

### 3.3 任何缺失或不一致都必须显式失败

Manifest 缺失、schema 不支持、Artifact 哈希不符、clip_id 不存在、配置无效时，Stage 必须失败或进入 needs_attention。禁止返回 `{}`、0、空列表后继续成功。

## 4. Phase A：建立 Artifact 数据协议

### 4.1 修改数据模型

涉及文件：

- `app/task_engine/models.py`
- `app/task_engine/schema.py`
- `app/task_engine/artifacts.py`
- `app/task_engine/repository.py`

为 Artifact 增加明确的生产 Stage 和逻辑类型：

```text
stage_id
artifact_kind
```

建议把 `task_artifacts` 扩展为：

```sql
stage_id TEXT REFERENCES task_stages(stage_id)
artifact_kind TEXT NOT NULL DEFAULT 'generic'
```

要求：

1. 使用增量迁移升级 schema，不删除、重建或清空现有 `task_state.db`。
2. 旧 Artifact 可保留 `stage_id=NULL`，但新 Worker 产生的 Artifact 必须具有 stage_id 和 artifact_kind。
3. 新增索引支持按 `video_id + stage_name + artifact_kind + clip_id` 查询。
4. 新增唯一约束，至少保证同一 Stage 的同一逻辑 Artifact 不重复。

建议 Artifact ID 使用完整 identity 的稳定哈希：

```python
artifact_id = canonical_hash({
    "stage_id": stage_id,
    "artifact_kind": artifact_kind,
    "clip_id": clip_id,
    "path": normalized_absolute_path,
})
```

禁止继续使用 `f"{stage_name}_{index}"`。

### 4.2 Artifact 冲突规则

不再使用无条件 `INSERT OR IGNORE`。插入时：

1. 如果 artifact_id 不存在，正常插入。
2. 如果 artifact_id 已存在，必须逐项验证 job_id、video_id、stage_id、kind、clip_id、path、sha256 和 size 完全一致。
3. 任一字段不一致时抛出明确的 Artifact identity collision 错误。
4. 相同记录才视为幂等成功。

### 4.3 Artifact resolver

在 repository/artifacts 层增加统一解析方法，例如：

```python
resolve_stage_inputs(video_id, stage_name, clip_id=None) -> dict[str, tuple[ArtifactRef, ...]]
```

依赖规则应显式编码：

| 当前 Stage | 必需输入 |
|---|---|
| discover | 无 |
| sample | discover_manifest |
| vlm | sample_manifest + sample frames |
| refine | vlm_manifest + 必需帧 |
| synthesize | refine_manifest |
| rank_dedup | synthesize_manifest |
| gif_clip | rank_dedup_manifest，且包含当前 clip_id |
| materialize | 当前视频全部 succeeded gif_clip_manifest + gif_file |

Resolver 返回前必须重新验证文件、大小和 SHA-256。

### 4.4 Phase A 测试

新增或改写：

- 两个视频产生 discover Artifact，断言 Artifact ID 不同且数据库有两条记录。
- 同一个 Artifact 重试提交，断言幂等且只有一条记录。
- 伪造相同 ID、不同 SHA/path，断言明确失败。
- Resolver 能从不同 Stage 隔离目录读取上游 Manifest。
- 删除或篡改上游文件后，Resolver 拒绝返回输入。
- 旧 schema 数据升级后仍可读取，现有行不丢失。

## 5. Phase B：实现原子 Stage 完成与恢复

涉及文件：

- `app/task_engine/worker.py`
- `app/task_engine/repository.py`
- `app/task_engine/artifacts.py`

### 5.1 新增事务方法

建议实现：

```python
complete_stage_with_artifacts(
    stage_id,
    worker_id,
    output_key,
    artifacts,
)
```

处理顺序：

1. 在事务外计算并验证所有文件的 size/SHA-256，避免长时间持有 SQLite 写锁。
2. `BEGIN IMMEDIATE`。
3. 重新检查 Stage 存在、状态允许完成、lease_owner 等于当前 Worker、lease 未被其他 Worker 接管。
4. 校验所有 Artifact 的 job/video/stage/clip ownership 与当前 Context 一致。
5. 幂等插入全部 Artifact。
6. 更新 Stage 为 succeeded、写 output_key、清空 lease。
7. 写 Stage completed event。
8. 一次 commit。
9. 任一步失败则一次 rollback，不允许留下部分 Artifact。

删除 `_insert_artifacts()` 中逐条 commit 的实现。

### 5.2 恢复路径

`_try_recover()` 必须复用同一个 `complete_stage_with_artifacts()`，不得维护第二套插入逻辑。

恢复要求：

- `_stage_result.json` 必须有 schema_version、stage_id、stage_name 和完整 Artifact identity。
- 文件存在且哈希一致才可恢复。
- 结果属于其他 job/video/stage 时拒绝恢复。
- 已完成 Stage 的重复恢复保持幂等。

### 5.3 Phase B 测试

- 两个 Artifact 中第二个无效，断言数据库 Artifact 数量仍为 0，Stage 不成功。
- Artifact 全部有效但 Stage lease_owner 错误，断言 Artifact 数量为 0。
- 在 Artifact 插入后注入 Stage UPDATE 失败，断言事务完全回滚。
- 崩溃恢复成功后，Artifact 和 Stage 在同一次事务中可见。
- 重复恢复不增加记录。

## 6. Phase C：重接真实 Stage 输入链

涉及文件：

- `app/task_engine/worker.py`
- `app/task_engine/stages.py`
- `app/task_engine/adaptive_adapter.py`
- `scripts/test_video_adaptive.py`

### 6.1 Worker 构造 Context

`_build_context()` 应：

1. 加载不可变 Job 配置快照。
2. 调用 Artifact resolver 获取当前 Stage 的精确输入。
3. 将输入放入 `StageContext.inputs`。
4. 不再写入 `prev_stage_work_dir` 或任何目录映射配置。

### 6.2 Adapter 与 stage-mode 输入

Adapter 应把输入 Artifact 列表写成单独的 `input_manifest.json`，或通过明确 CLI 参数传给子进程，例如：

```text
--task-input-manifest <path>
```

该文件至少包含：

```json
{
  "schema_version": 1,
  "stage_id": "...",
  "inputs": {
    "rank_dedup_manifest": [
      {"path": "...", "sha256": "...", "size_bytes": 123}
    ]
  }
}
```

`scripts/test_video_adaptive.py` 的每个 handler 只能从该输入表读取上游内容。删除 `_load_input_manifest()` 中基于 work_dir/prior_work_dirs 的查找逻辑。

### 6.3 Manifest schema 校验

每类 Manifest 定义必需字段和 schema_version。至少验证：

- stage 与预期类型一致。
- video_id/video_path 与当前任务一致。
- clip_count 与 clips 长度一致。
- gif_clip Manifest 的 clip_id 与当前 Stage clip_id 一致。
- rank_dedup clips 中 clip_id 非空且在同一视频内唯一。

### 6.4 Phase C 测试

- discover 和 sample 使用不同 stage_id/目录，sample 读取真实 duration 并产生预期采样点。
- 上游 Manifest 缺失、类型错误、schema 不支持或哈希不匹配时，下游明确失败。
- gif_clip 只读取自己的 clip_id，不处理其他 clip。
- 多个 gif_clip 并行完成后，彼此输出不覆盖。

## 7. Phase D：修复 fan-out、zero-clip 与 materialize

涉及文件：

- `app/task_engine/orchestrator.py`
- `app/task_engine/repository.py`
- `scripts/test_video_adaptive.py`

### 7.1 rank_dedup fan-out

只保留一个 fan-out 入口。建议由 Orchestrator 在事务内执行：

1. 从 task_artifacts 解析唯一的 rank_dedup_manifest。
2. 校验 Manifest。
3. `clips=[]`：直接幂等创建 materialize，input_key 指向 rank_dedup Artifact identity。
4. `clips=N`：为每个非空、唯一 clip_id 创建一个 gif_clip Stage。
5. 重复 advance 不增加 Stage。
6. Manifest 缺失或损坏时，把视频/Job 置为 needs_attention，并记录可见错误事件；禁止 `except ValueError: pass` 后永久空闲。

### 7.2 materialize 输入

materialize 必须通过 Artifact resolver 接收：

- 所有 succeeded gif_clip 的 gif_file。
- 对应 gif_clip_manifest。
- failed、cancelled、needs_attention clip 的状态摘要。

不得扫描 materialize 自己的 `work_dir` 寻找其他 Stage 的 Manifest。

### 7.3 正式导出目录

materialize 应把成功 GIF 汇总到项目现有正式导出结构：

```text
data/exports/adaptive_test/<input_folder>/<video_name>/
```

具体路径以最终配置快照为准。要求：

1. 先写 job 专属临时目录。
2. 校验 GIF 哈希后再原子移动/替换本次 Job 所属文件。
3. 不删除其他 Job、历史标签、候选记录或人工反馈关联的文件。
4. 已成功 clip 重试时，如果目标文件哈希一致，不重新编码、不改变 mtime。
5. materialize 输出 result JSON，记录 succeeded/failed/cancelled clip 列表和最终文件路径。
6. PBF 只引用最终导出目录中的成功 GIF。

### 7.4 0、1、N clip 语义

| 情况 | 预期行为 |
|---|---|
| 0 clips | 不创建 gif_clip；创建 materialize；输出 gif_count=0；视频可成功 |
| 1 clip 成功 | 创建 1 个 gif_clip；materialize 输出 1 个 GIF；视频成功 |
| N clips 全成功 | materialize 输出 N 个 GIF；视频成功 |
| N clips 部分失败 | materialize 可输出成功部分，但视频/Job 为 needs_attention |
| 失败 clip 重试成功 | 只执行失败 clip；最终重新聚合后视频/Job 才成功 |

### 7.5 Phase D 测试

- zero-clip 必须断言 materialize 存在、完成且视频/Job succeeded。
- 3 clips 必须无条件断言恰好 3 个 gif_clip，clip_id 集合完全一致。
- materialize 从三个独立 gif_clip 目录聚合三个 GIF 到正式导出目录。
- 一个 clip 失败时输出其他成功 GIF，但视频/Job 不得 succeeded。
- 重试失败 clip 时，成功 GIF 的 SHA-256 和 mtime 不变。
- 重复 fan-out/materialize 不产生重复 Stage、Artifact 或候选记录。

## 8. Phase E：修复 Lease 与 Heartbeat

涉及文件：

- `app/task_engine/models.py`
- `app/task_engine/worker.py`
- `app/task_engine/repository.py`
- `scripts/task_worker.py`
- `configs/models.yaml`

### 8.1 独立 lease 配置

不要使用 RetryPolicy 的 `max_delay_seconds`。为 TaskWorker 增加独立配置：

```python
lease_seconds: int = 90
heartbeat_seconds: int | None = None
```

规则：

- claim_stage 使用同一个 lease_seconds。
- 默认 heartbeat interval 可取 `max(1, lease_seconds // 3)`。
- 必须保证 `heartbeat_seconds < lease_seconds`。
- CLI 增加 `--lease-seconds`，并从 `task_engine.lease_seconds` 读取默认值。

### 8.2 同数据库连接工厂

TaskWorker 初始化时接收明确的 db_path 或 connection factory。Heartbeat 在线程内部使用该 factory 创建同一 DB 的短连接。

不要依赖默认 DB，也不要把内存数据库或空 `PRAGMA database_list` 路径当作普通文件路径。

### 8.3 Heartbeat 失败处理

- lease_owner/status 不匹配时停止 Heartbeat，并通知执行主流程失去 lease。
- 连续数据库锁失败不能无限吞掉；在 lease 到期前无法成功续租时，Stage 结果不得提交。
- Stage 完成、失败、取消后必须停止并 join Heartbeat。
- Adapter 抛异常时同样 join，不能遗留后台线程。

### 8.4 Phase E 测试

- 自定义临时 DB，默认 DB 不得创建或修改。
- lease=6 秒、heartbeat=2 秒，Fake Stage 运行超过 12 秒，第二连接始终无法领取。
- max_delay_seconds 改成任意值都不影响 lease。
- Heartbeat 失去 ownership 后，原 Worker 不得提交 Artifact 或完成 Stage。
- 数据库持续锁定至 lease 过期时，结果不提交并产生明确错误。

## 9. Phase F：修复 Quality Lab 最终配置

涉及文件：

- `app/quality_lab/runner.py`
- `app/routers/tasks.py`
- 可新增共享配置构建模块
- `tests/quality_lab/test_runner.py`
- `tests/quality_lab/test_isolation.py`

### 9.1 单一配置构建函数

新增共享函数，例如：

```python
build_task_config(
    base_config,
    experiment_overrides,
    video_paths,
    experiment_metadata,
) -> FrozenTaskConfig
```

规则：

1. base_config 从正确的应用配置源加载，不从 Quality Lab DB 查询不存在的 `app_config` 表。
2. 对 dict 递归 deep merge；标量和列表由实验配置整体替换。
3. 完整保留实验配置中的 adaptive、vlm、模型、路径及扩展字段。
4. 最终配置在顶层使用 pipeline 约定结构，不再一部分顶层、一部分放入 `config_snapshot`。
5. 合并 video_paths 和 `_experiment` 元数据后冻结配置。
6. 对最终业务配置计算 canonical config_hash；明确是否排除 run_id/item_id 等运行时元数据。
7. Job、StageContext、Artifact provenance 使用同一个 hash。

### 9.2 修正测试 Fake

`tests/quality_lab/test_runner.py` 的 FakeTaskClient 必须按 directory + normalized video scope 区分 Job，不能继续仅按目录复用 Job。

同目录两个不同 benchmark item 应获得不同 Job ID。

### 9.3 Phase F 测试

- 实验配置包含 adaptive、vlm、custom_flag，提交后全部保留。
- 两套不同配置产生不同 config_hash。
- 相同配置重复提交产生相同业务 config_hash。
- 同目录两个不同视频得到不同 Job ID，且每个 Job 只发现自己的视频。
- 配置快照传入真实 Worker StageContext，而不是直接调用 repository 写入后自证。
- Artifact provenance 中的 config_hash 与 Job 最终配置一致。

## 10. Phase G：修复状态聚合与 scoped Job 去重测试

涉及文件：

- `app/task_engine/orchestrator.py`
- `app/task_engine/repository.py`
- `tests/task_engine/test_orchestrator.py`
- `tests/task_engine/test_repository.py`

### 10.1 状态优先级

视频和 Job 使用同一聚合函数，优先级固定为：

```text
needs_attention/failed > cancelled > running/leased/retry_wait/pending > succeeded
```

只有所有必需 Stage succeeded，或合法 zero-clip materialize succeeded 时，才能标记 succeeded。

部分 GIF 成功、部分失败时使用 needs_attention；不要让 materialize succeeded 覆盖 clip 失败。

### 10.2 scoped Job

保留“检查全部 active Job”的方向，但补齐：

- video_paths 转绝对路径、normcase、规范分隔符、去重、排序。
- B、A、A 顺序下第三次必须返回 A 的 existing_job_id。
- 两个真实 SQLite 连接并发创建同一 scope，只能成功一个。
- 同目录不同 scope 可并存。
- 明确定义全目录 scope `*` 与单视频 scope 并存规则并写测试。

### 10.3 Phase G 测试

- 单个 cancelled Stage 且无 active Stage，视频/Job 必须 cancelled。
- materialize succeeded + gif_clip cancelled，不能 succeeded。
- materialize succeeded + gif_clip failed，必须 needs_attention。
- 全部成功才 succeeded。
- B、A、A 和双连接并发 scope 测试。

## 11. Phase H：真实端到端测试

当前 `test_single_video_full_chain_one_step_at_a_time` 直接调用 `complete_stage()`，不属于端到端测试。新增一个真正执行以下组件的测试：

```text
TaskWorker.run_once
→ AdaptivePipelineAdapter 或协议等价 Fake Adapter
→ StageResult
→ Artifact 原子入库
→ Orchestrator 推进
→ gif_clip fan-out
→ materialize
→ 视频/Job 聚合
```

外部依赖 ffprobe、ffmpeg、VLM、LLM、Embedding 使用可控 Fake，但不得绕过 Worker、Context、Artifact resolver 和 repository 事务。

### 必须覆盖的端到端场景

1. 1 个视频、2 个 clips，全链成功。
2. 每个 Stage 的 work_dir/stage_id 都不同。
3. 数据库中每个 Stage 的 Artifact 数量和 kind 正确。
4. 所有 Artifact SHA-256 可重新计算。
5. 正式导出目录有 2 个 GIF 和正确 result JSON/PBF。
6. 第 2 个 clip 首次失败，第一次聚合为 needs_attention。
7. 重试后只运行第 2 个 clip；第 1 个 GIF 的 hash/mtime 不变。
8. 最终视频和 Job succeeded。
9. Worker 崩溃恢复不会重复导出或重复入库。
10. 两个 Worker 同时运行不会重复执行同一 Stage。

## 12. 测试质量要求

禁止以下测试模式：

- `if len(rows) > 0: assert ...`。
- 只断言 Stage 名称“出现过”，不检查数量、输入、输出和状态。
- 测试名写 two connections，实际只使用一个 repository/connection。
- 直接向 task_jobs 写配置，再断言读取到同一配置，用来代替 Runner/API 传递测试。
- zero-clip 只断言没有 gif_clip，不断言 materialize 和最终状态。
- 通过手工 `complete_stage()` 冒充 Worker 全链测试。

每个关键测试必须在修复前能够失败，并在修复后转绿。

## 13. 推荐提交前检查顺序

Agent 修改完成后按顺序执行：

```powershell
.venv\Scripts\python.exe -m compileall -q app scripts tests
.venv\Scripts\python.exe -m pytest tests/task_engine tests/quality_lab -q
.venv\Scripts\python.exe -m pytest tests -q
git diff --check
```

还必须运行一个独立的临时目录 smoke test，使用非默认 task DB，证明：

- 默认 `data/task_state.db` 未被修改。
- 正式导出目录得到预期 GIF。
- task_artifacts 没有跨视频 ID 冲突。
- zero-clip 能完成。
- 长耗时 Stage 的 lease 持续续租。

## 14. Agent 交付报告格式

Agent 完成后提交新的修复报告，必须包含：

1. 每个 Phase 的修改文件和关键函数。
2. schema migration 版本和数据兼容说明。
3. Artifact identity、输入解析、事务边界和状态优先级的最终设计。
4. 新增/修改测试名称及每项验证内容。
5. 端到端测试的 Stage、Artifact、GIF、状态和重试结果。
6. compileall、目标测试、全套测试、diff-check 的真实输出。
7. 当前仍未处理的问题；禁止把“测试通过”作为问题已解决的唯一证据。

## 15. 数据与发布约束

- 不删除、清空或重建现有 `data/`、`dist/GifAgentUI/data/`、task_state.db、quality_lab.db、library.db、历史导出、标签、候选记录或偏好记忆。
- schema 变更只允许可回滚的增量迁移，并先在临时数据库验证。
- 测试只使用 `tmp_path`、临时数据库和临时导出目录。
- 不处理或覆盖现有未提交的无关修改。
- 未收到用户明确指令前，不提交、不构建 EXE、不推送远程。
