# Stage Split 生产链路二次复审实施文档（2026-07-18）

## 1. 目标与结论

本实施文档用于修复 `STAGE_SPLIT_PRODUCTION_PATH_FIX_REPORT_2026-07-18.md` 二次复审后仍存在的问题。

当前测试基线为：

```text
tests/task_engine + tests/quality_lab: 329 passed
全仓测试: 820 passed, 2 skipped
```

测试基线真实有效，但尚未覆盖完整生产 Adapter 链路。实施完成前不得构建发布版 EXE，也不得用现有 Stage Split 对历史队列执行正式重跑。

本轮必须解决：

- 4 个 P0：数据库二次启动失败、内部 JSON 被登记为业务 Artifact、Control 配置被忽略、materialize 未完成正式发布。
- 4 个 P1：Artifact stage_id 归属校验、lease 状态污染后续任务、Manifest Validator 未接入、真实生产路径 E2E 缺失。

## 2. 安全边界

1. 不删除 `data/task_state.db`、`data/quality_lab.db`、候选 GIF、标签、Preference Memory、导出目录或历史日志。
2. Schema 修复必须使用向前迁移，禁止通过删除数据库或重建空库规避问题。
3. 所有迁移先在数据库副本上验证，再使用现有数据库执行只增不删的结构升级。
4. materialize 发布正式 GIF 时保留 Stage 工作目录中的原始 Artifact，直到独立的保留策略明确清理。
5. 不通过放宽校验、吞掉异常或在测试中替换生产 Adapter 来获得绿灯。

## 3. Phase 0：先增加 RED 测试

在修改实现前先提交以下失败测试，测试名称可调整，但覆盖语义不得减少。

### 3.1 Schema 重启测试

新增：`tests/task_engine/test_schema_v4_reopen.py`

步骤：

1. 创建临时 task DB。
2. 创建一个 sample stage。
3. 插入两条不同路径的 `sample_frames`。
4. 关闭数据库连接。
5. 再次调用 `connect_task_db()`。
6. 断言连接成功、两条 Artifact 均保留、旧索引不存在、新索引存在。

该测试必须能复现当前错误：

```text
UNIQUE constraint failed: index 'uq_artifact_stage_kind_clip'
```

### 3.2 生产 Artifact 白名单测试

新增：`tests/task_engine/test_production_artifact_contract.py`

至少覆盖：

- discover 工作目录同时存在 `config_snapshot.json`、`input_manifest.json`、`discover_manifest.json` 时，只登记 `discover_manifest.json`。
- sample 只登记 `sample_manifest` 和真实 `sample_frames`，不登记配置、输入清单、日志和 result 临时文件。
- gif_clip 只登记一个 `gif_file` 和一个 `gif_clip_manifest`。
- materialize 只登记正式 result、PBF（若启用）和 `materialize_manifest`。
- Adapter 遇到缺失、未知或不属于当前 Stage 的 `artifact_kind` 时失败，不进行扩展名兜底猜测。

### 3.3 Control 配置冻结测试

新增：`tests/task_engine/test_control_config_snapshot.py`

通过真实 `/api/tasks/jobs` 创建任务，配置使用当前 `configs/models.yaml`，断言 Worker/Adapter/阶段脚本最终看到：

```yaml
adaptive:
  sample_interval: 8
  max_output: 60
preference_memory:
  enabled: true
```

同时增加旧任务兼容测试：历史 `config_json` 使用 `config_snapshot` 包装时，仍能解析出同样的业务配置。

### 3.4 Materialize 端到端测试

新增：`tests/task_engine/test_materialize_production.py`

覆盖四种情况：

1. 两个 gif_clip 全成功。
2. 一个成功、一个失败。
3. 全部失败。
4. rank_dedup 输出 zero-clip。

断言：

- materialize 收到 `gif_file`、`gif_clip_manifest` 和所有 clip 的终态摘要。
- 正式导出目录只包含校验成功的 GIF。
- PBF 时间和名称来自对应 clip manifest，不得回退为全零。
- result JSON 明确列出 `succeeded_clips`、`failed_clips`、`cancelled_clips`。
- 部分失败时 materialize 可成功发布有效 GIF，但 video/job 最终为 `needs_attention`。

### 3.5 Lease 隔离测试

新增测试：同一个 `TaskWorker` 先处理一个 lease 丢失的 Stage，再处理一个正常 Stage；第二个 Stage 必须成功提交，证明 lease 状态没有跨 Stage 泄漏。

### 3.6 真实 Adapter 链路测试

新增最小生产链路：

```text
TaskWorker
  -> AdaptivePipelineAdapter
  -> Python 子进程
  -> scripts/test_video_adaptive.py --task-stage
  -> result JSON
  -> Artifact 原子入库
  -> resolver
  -> 下一 Stage
```

第一批至少覆盖 `discover -> sample`，第二批覆盖 `gif_clip -> materialize`。不得使用 Fake StageAdapter 替代 `AdaptivePipelineAdapter`。

## 4. Phase 1：修复 Schema 迁移执行器

涉及文件：

- `app/task_engine/schema.py`
- `tests/task_engine/test_schema*.py`

### 4.1 根因

`_migrate_task_schema()` 每次连接都无条件执行 v3，然后执行 v4。v4 数据库已经允许同一 Stage 存在多条 `sample_frames`；再次创建 v3 旧唯一索引时会立即失败，因此应用重启后无法打开数据库。

### 4.2 实施要求

1. 从 `task_migrations` 读取已应用版本集合，不以常量值猜测当前状态。
2. 每个迁移只执行一次：

   ```text
   未应用 v3 -> 执行 v3 -> 记录 v3
   未应用 v4 -> 执行 v4 -> 记录 v4
   已应用 v4 -> 不得再次执行 v3/v4 DDL
   ```

3. 兼容以下数据库：

   - 全新数据库。
   - 有旧 task 表但没有 migration 记录的数据库。
   - v3 数据库。
   - v4 且已有多帧 Artifact 的数据库。

4. v4 迁移先 `DROP INDEX IF EXISTS uq_artifact_stage_kind_clip`，再创建新索引。
5. migration 记录和 DDL 在同一事务内提交；失败时不得留下“已记录但未完成”的版本。
6. 不覆盖或删除现有 Artifact 数据。

### 4.3 验收

- 连续打开同一数据库三次均成功。
- 插入多帧后关闭并重启仍成功。
- `PRAGMA integrity_check` 返回 `ok`。
- `task_migrations` 每个版本最多一条记录。

## 5. Phase 2：改为显式 Artifact 输出协议

涉及文件：

- `scripts/test_video_adaptive.py`
- `app/task_engine/adaptive_adapter.py`
- `app/task_engine/artifacts.py`
- `app/task_engine/repository.py`

### 5.1 删除目录扫描推断

删除 Stage 模式中“遍历 work_dir/exports 并按扩展名收集 Artifact”的做法。每个 Stage handler 必须显式返回 Artifact 描述：

```json
{
  "path": "absolute-path",
  "artifact_kind": "discover_manifest",
  "clip_id": null
}
```

禁止登记：

- `config_snapshot.json`
- `input_manifest.json`
- `stage.log`
- `result_<stage>.json` 及其 `.tmp`
- 其他上游复制品或内部控制文件

### 5.2 Stage 输出白名单

以 `STAGE_ARTIFACT_KINDS` 为唯一白名单：

| Stage | 允许输出 |
|---|---|
| discover | `discover_manifest` |
| sample | `sample_manifest`, `sample_frames` |
| vlm | `vlm_manifest` |
| refine | `refine_manifest` |
| synthesize | `synthesize_manifest` |
| rank_dedup | `rank_dedup_manifest` |
| gif_clip | `gif_file`, `gif_clip_manifest` |
| materialize | `result`, `materialize_manifest`, 可选 `pbf_file` |

若保留 PBF，增加明确的 `pbf_file` kind，不得把二进制 PBF 标记为要求 JSON 字段的 `result`。

### 5.3 Adapter 校验

`AdaptivePipelineAdapter` 必须：

1. 要求脚本显式提供 `artifact_kind`。
2. 校验 kind 属于当前 Stage 白名单。
3. 校验 path 是本次 Stage 允许产生的文件；不得把输入 Artifact 重新登记为输出。
4. 计算真实 size 和 SHA-256，不信任脚本传入值。
5. 使用标准化绝对路径生成 artifact_id；Windows 下统一分隔符和大小写策略。
6. 不再使用扩展名或文件名包含关系推断 kind。

### 5.4 Repository 所有权校验

`complete_stage_with_artifacts()` 增加：

```python
if ref.stage_id != stage_id:
    raise ValueError(...)
```

同时校验：

- `artifact_kind` 属于 `STAGE_ARTIFACT_KINDS[stage_name]`。
- `clip_id` 与当前 Stage 完全一致。
- artifact path/ID/内容发生冲突时整批事务回滚。

### 5.5 Sample Frame 协议

恢复 `vlm` 对 `sample_frames` 的显式依赖。每张保留帧登记为独立 Artifact；sample manifest 只保存 frame artifact_id、timestamp 等索引信息，不把未经登记的裸路径当成数据库外依赖。

VLM 应通过 resolver 返回的 `sample_frames` 与 manifest 中的 artifact_id 对应，不直接信任 manifest 内任意文件路径。

## 6. Phase 3：统一任务配置快照

涉及文件：

- `app/routers/tasks.py`
- `app/task_engine/worker.py`
- `app/task_engine/adaptive_adapter.py`
- `scripts/test_video_adaptive.py`
- `app/quality_lab/config_builder.py`

### 6.1 新任务格式

新建任务时，将完整业务配置冻结为顶层结构，不再只放入 `config_snapshot`：

```json
{
  "adaptive": {},
  "preference_memory": {},
  "vlm": {},
  "models": {},
  "video_paths": [],
  "_task": {
    "limit": 0,
    "extensions": ""
  }
}
```

请求中的实验/任务 override 对完整基础配置执行递归深度合并，禁止浅层覆盖丢失模型和路径字段。

### 6.2 历史任务兼容

新增唯一的 `normalize_task_config(raw_config)`：

1. 如果存在历史 `config_snapshot`，先将其作为 base。
2. 将 wrapper 顶层业务字段作为 override 深度合并。
3. 提取 limit/extensions/video_paths 到保留元数据。
4. 返回统一的顶层业务配置。

Worker、Quality Lab 和 stage 脚本全部使用该函数，禁止各自实现不同解包逻辑。

### 6.3 验收

- Control 新任务读取用户当前配置，而不是脚本默认值。
- 历史任务仍能按创建时配置恢复。
- Quality Lab 的单视频路径、实验 override、模型配置和 config hash 一致。
- provenance 基于实际执行的标准化配置计算。

## 7. Phase 4：完整实现 Materialize 发布

涉及文件：

- `app/task_engine/artifacts.py`
- `app/task_engine/worker.py`
- `app/task_engine/orchestrator.py`
- `scripts/test_video_adaptive.py`

### 7.1 Materialize 输入信封

将 `input_manifest.json` 升级为版本化信封：

```json
{
  "schema_version": 1,
  "stage": "materialize",
  "artifacts": {
    "gif_file": [],
    "gif_clip_manifest": []
  },
  "stage_statuses": [
    {"stage_id": "...", "clip_id": "...", "status": "succeeded"}
  ]
}
```

普通 Stage 同样使用 `artifacts` 字段，避免将协议元数据与 artifact kind 混在同一层。

materialize resolver 必须返回：

- 所有 succeeded gif_clip 的 `gif_file`。
- 对应的 `gif_clip_manifest`。
- 所有 gif_clip 的 succeeded/failed/cancelled/needs_attention 终态摘要。

### 7.2 正式输出目录

建立共享的输出目录解析函数，由 Control、直接模式和 Stage 模式共同调用。优先使用冻结任务配置中的明确输出目录；若未配置，使用项目既有默认导出目录，并按视频创建子目录。

不得继续把 `work_dir/exports/<video>` 当成最终用户输出目录。

### 7.3 原子发布

1. 对源 GIF 重新校验 size、SHA-256、clip_id。
2. 先复制到正式目录内的临时文件。
3. 完成复制后再次校验。
4. 使用同卷原子 rename 发布正式文件。
5. 文件名冲突时按 artifact_id/sha256 判断幂等或冲突，不静默覆盖不同内容。
6. result JSON 中的 path 必须指向正式文件。
7. 生成 PBF、result JSON 和 materialize manifest 后再完成 Stage。
8. 不删除 task work 中的源 Artifact。

### 7.4 状态语义

- 全成功：materialize succeeded，video/job succeeded。
- 部分成功：发布成功 GIF，materialize succeeded，但 video/job needs_attention。
- 全失败：不得伪造空成功；video/job needs_attention。
- zero-clip：显式空结果，materialize succeeded。

## 8. Phase 5：Lease、Manifest 与错误传播

### 8.1 Lease 状态改为单次执行局部状态

不要使用会跨 Stage 保留的 `self._lease_lost`。每次 `_run_stage()` 创建独立 `threading.Event` 或局部状态，由 heartbeat 线程和主线程共享。

增加测试：

- Stage A 丢失 lease 后不提交。
- 同一 Worker 随后处理 Stage B 并成功提交。
- heartbeat DB 异常持续到 lease 可能失效时，主线程不得盲目提交。
- `0 < heartbeat_seconds < lease_seconds` 在 CLI 和直接构造 TaskWorker 时都成立。

### 8.2 接入 Manifest Validator

`_read_upstream_manifest()` 必须调用共享 `validate_manifest_json()`，并传入：

- expected artifact_kind
- expected producer stage
- expected clip_id（适用时）
-支持的 schema version

不得只检查 `schema_version` 是否存在。未知版本、字段缺失、stage 错误、clip_id 错误都必须形成结构化 StageError。

### 8.3 Resolver 数据来源

继续要求 resolver 只读取通过 `task_artifacts.stage_id = task_stages.stage_id` 关联且生产 Stage 为 succeeded 的 Artifact。增加错误 stage_id 绑定和历史失败 attempt 的回归测试。

## 9. Phase 6：真实生产路径 E2E

Fake Adapter E2E 保留为调度器单元测试，但不能作为发布门槛的唯一证据。

### 9.1 测试基础设施

在测试进程中提供可控的外部边界：

- 临时 PATH 中的 fake `ffprobe`/`ffmpeg` 可执行程序。
- 本地临时 HTTP 服务模拟 VLM/Embedding 响应，或通过正式依赖注入接口提供 deterministic backend。
- 所有文件、DB、work_dir、正式 export_dir 位于 `tmp_path`。
- 测试不得访问真实 Ollama、WSL、用户视频或仓库 data 目录。

### 9.2 必须覆盖

1. 完整 8 阶段全成功。
2. 多 sample frame。
3. 两个 gif_clip fan-out。
4. 单个 gif_clip 失败后只重试失败 clip。
5. 部分失败 materialize。
6. zero-clip。
7. Worker 中途重启并从 DB/Artifact 恢复。
8. 完成后关闭并重新打开 v4 DB。

### 9.3 端到端断言

- 每个 Stage 只执行自身职责一次。
- task_artifacts 不含 config/input/log/result 临时控制文件。
- 所有 Artifact stage_id、kind、clip_id 正确。
- 每个消费者只读取 resolver 指定的 Artifact。
- 正式输出 GIF 与 DB SHA-256 一致。
- 状态聚合和事件日志符合成功/失败语义。

## 10. 实施顺序与提交边界

建议拆分为以下独立提交，便于 Review 和回滚：

1. `test: add red coverage for stage split production gaps`
2. `fix: make task schema migrations version-aware`
3. `fix: enforce explicit stage artifact protocol`
4. `fix: normalize frozen task configuration`
5. `fix: materialize verified gifs to final output`
6. `fix: isolate lease state and validate manifests`
7. `test: add production adapter end-to-end coverage`
8. `docs: update stage split architecture and operations`

不要把所有修复压在一个提交中，也不要夹带无关 UI、Preference Memory 或历史数据修改。

## 11. 验证矩阵

每个 Phase 完成后运行相关测试；最终必须运行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

数据库验证：

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
SELECT version, COUNT(*) FROM task_migrations GROUP BY version;
SELECT artifact_kind, COUNT(*) FROM task_artifacts GROUP BY artifact_kind;
SELECT COUNT(*) FROM task_artifacts WHERE stage_id IS NULL OR stage_id='';
```

对新生产任务，最后一条查询必须为 0；历史兼容数据应单独统计，不得在迁移中删除。

## 12. 最终完成标准

只有同时满足以下条件，才能再次声明 Production Path 已修复：

1. 含多帧 Artifact 的 v4 DB 可重复关闭和启动。
2. 生产 Artifact 中不存在内部控制 JSON 或错误 kind。
3. Control、Quality Lab 和历史任务配置均执行预期参数。
4. materialize 将校验成功的 GIF 发布到正式目录，并输出完整 clip 状态。
5. Repository 拒绝错误 stage_id 和非法 kind。
6. Lease 丢失不污染同一 Worker 的后续 Stage。
7. 所有 Manifest 在消费前通过共享 Validator。
8. 真实 Adapter/子进程端到端测试覆盖完整 8 阶段。
9. 全仓测试通过且无真实外部服务副作用。
10. 使用一个临时短视频完成 smoke test，并核对 DB、日志、正式 GIF、PBF、result JSON。

修复报告必须逐项提供代码位置、RED/GREEN 测试名称、实际命令输出和生产 smoke test 证据，不能仅以测试总数代替生产链路验证。
