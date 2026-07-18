# Stage Split 生产链路第四次 Review 实施文档（2026-07-18）

## 1. 目标与当前结论

本实施文档用于修复第三次修复后仍未闭环的问题。

当前测试基线已经核验：

```text
python -m pytest -q
882 passed, 2 skipped
```

测试结果真实，但生产行为仍存在：

- P0：2 项。
- P1：4 项。

完成本文 P0、真实 Worker 重试测试和完整八阶段生产 E2E 前，不应构建发布版 EXE，也不应对历史队列执行正式重跑。

## 2. 安全边界

1. 不删除或重建 `data/task_state.db`、`data/quality_lab.db`。
2. 不覆盖、删除或重命名已有正式 GIF、PBF、result JSON、标签和 Review 历史。
3. 所有测试使用 `tmp_path` 中的 DB、work_dir 和 export_dir。
4. 不通过 `--ignore`、删除失败测试或降低断言规避问题。
5. 不允许以 Fake Adapter 测试代替生产 `AdaptivePipelineAdapter` 发布门槛。

## 3. Phase 0：先增加 RED 测试

在修改实现前，先增加以下失败测试。

### 3.1 成功 gif_clip 缺失 Artifact

新增测试：

```text
test_materialize_rejects_succeeded_clip_without_artifacts
test_materialize_rejects_succeeded_clip_missing_gif_file
test_materialize_rejects_succeeded_clip_missing_manifest
```

测试步骤：

1. 创建一个状态为 `succeeded` 的 gif_clip Stage。
2. 分别不插入 Artifact、只插入 manifest、只插入 gif_file。
3. 调用 `resolve_materialize_inputs()`。
4. 断言抛出结构化错误，不能返回空集合。

当前错误行为的复现基线：

```text
RESOLVED: {'gif_file': 0, 'gif_clip_manifest': 0}
STATUSES: []
```

### 3.2 发布冲突必须改变最终状态

新增真实 Worker 测试：

```text
test_materialize_conflict_marks_video_needs_attention
```

测试必须：

1. 创建一个 succeeded gif_clip 和有效 Artifact。
2. 在正式输出目录预放同名、不同 SHA-256 的历史 GIF。
3. 让 Worker 真实运行 materialize。
4. 确认历史 GIF 内容不变。
5. 确认新输出使用稳定冲突名称，或者 materialize/video/job 为 `needs_attention`。
6. 禁止只检查 result JSON 中存在 `overwrite_prevented`。

### 3.3 完整终态信封

新增测试：一个 succeeded、一个 needs_attention、一个 cancelled 的 gif_clip，构建 materialize 输入信封后断言三条状态全部存在。

### 3.4 Manifest 版本测试

新增：

```text
test_manifest_rejects_schema_version_zero
test_manifest_rejects_future_schema_version
test_materialize_envelope_rejects_unknown_version
```

### 3.5 配置 Hash API 测试

通过真实 `/api/tasks/jobs` 创建任务：请求配置携带旧 hash，服务器深度合并新的基础字段后，断言持久化 hash 等于最终执行配置，而不是请求中的旧值。

### 3.6 真实重试测试

新增测试必须真实执行：

1. 两个 gif_clip 中一个成功、一个失败。
2. 发出 retry command 或调用正式 retry API。
3. Worker 重新 claim 失败 clip。
4. 成功 clip 的 attempt_count、stage_id、Artifact SHA 不变。
5. 失败 clip attempt_count 增加且最终成功。

## 4. P0-1：Materialize 必须从 Stage 集合验证 Artifact 完整性

涉及文件：

- `app/task_engine/artifacts.py`
- `app/task_engine/worker.py`
- `tests/task_engine/test_stage_inputs.py`
- `tests/task_engine/test_production_e2e.py`

### 4.1 根因

当前 `resolve_materialize_inputs()` 从 `task_artifacts` 开始查询。没有 Artifact 的 succeeded Stage 不会出现在 JOIN 结果中，因此会被误认为不存在。

### 4.2 实施要求

1. 查询必须从 succeeded gif_clip Stage 开始：

   ```sql
   SELECT stage_id, clip_id
   FROM task_stages
   WHERE video_id=?
     AND stage_name='gif_clip'
     AND status='succeeded'
   ```

2. 对每个 succeeded Stage 单独查询 Artifact，并严格要求：

   - 恰好一个 `gif_file`。
   - 恰好一个 `gif_clip_manifest`。
   - 两者 stage_id 等于当前 Stage。
   - 两者 clip_id 等于当前 clip。
   - 文件存在、size 和 SHA-256 正确。
   - manifest 的 clip_id、gif_path、sha256 与 gif_file 一致。

3. 任意 succeeded clip 缺失或重复 Artifact 时，resolver 失败；不得跳过后继续 materialize。
4. zero-clip 必须由明确的 `input_key`/rank manifest 语义识别，不能通过“没有查到 succeeded Artifact”推断。
5. 查询 failed/cancelled/needs_attention Stage，加入完整终态摘要，但不要求它们存在成功 Artifact。
6. 返回类型建议使用显式数据对象，而不是松散字典：

   ```python
   @dataclass(frozen=True)
   class MaterializeInputs:
       artifacts: dict[str, tuple[ArtifactRef, ...]]
       stage_statuses: tuple[GifClipStatus, ...]
       zero_clip: bool
   ```

### 4.3 验收标准

- succeeded clip 缺任意 Artifact 时 materialize 不运行。
- succeeded clip 数量等于信封中 succeeded 状态数量。
- 每个 succeeded 状态都能找到一对匹配 Artifact。
- zero-clip 仍能成功产生显式空结果。

## 5. P0-2：发布失败必须进入确定的状态语义

涉及文件：

- `scripts/test_video_adaptive.py`
- `app/task_engine/worker.py`
- `app/task_engine/orchestrator.py`
- 可新增 `app/task_engine/materialize_result.py`

### 5.1 当前问题

`overwrite_prevented`、copy failure、SHA mismatch 等只计入 `failed_count`。Stage 仍返回正常 `output_key`，Worker 随后将 materialize 标为 succeeded。

### 5.2 推荐方案

优先实现“稳定冲突命名并成功发布”：

```text
原名称: video_001.gif
冲突名称: video_001.<clip-id-8>.<new-sha-12>.gif
```

规则：

1. 目标不存在：发布到原名称。
2. 同名同 SHA：幂等复用原文件。
3. 同名不同 SHA：发布到由 clip_id + 新内容 SHA 生成的稳定名称。
4. 稳定冲突名称已存在且 SHA 相同：幂等复用。
5. 稳定冲突名称已存在但 SHA 不同：抛出不可恢复冲突并进入 needs_attention。

如果产品决定不创建冲突名称，则 materialize 必须显式进入 `needs_attention`，不能报告 succeeded。

### 5.3 StageResult 状态契约

不要让 Worker 根据任意 metrics 猜测失败。为 StageResult 增加明确结果语义之一：

```python
outcome: Literal['succeeded', 'needs_attention']
```

或让 materialize 在存在未发布成功的“本应成功 clip”时抛出专用非瞬时异常，由 Worker 记录 needs_attention。

必须区分：

- 上游 gif_clip 本身失败：允许发布成功 clip，但 video/job needs_attention。
- succeeded gif_clip 的 GIF 发布失败：materialize needs_attention。
- 所有 succeeded gif_clip 发布成功：materialize succeeded。

### 5.4 文件系统事务

1. GIF 临时文件继续使用唯一名称并与目标同卷。
2. PBF 和 result JSON 也必须使用 Stage 唯一临时文件；禁止共享 `<name>.tmp`。
3. result/PBF 写入失败时，不能删除历史正式文件。
4. result JSON 只能引用已成功发布或幂等复用的文件。
5. 重试不得产生无限递增的重复冲突文件。

## 6. P1-1：Materialize 信封必须包含所有终态

涉及文件：

- `app/task_engine/artifacts.py`
- `app/task_engine/adaptive_adapter.py`

### 实施要求

1. `build_materialize_input_envelope()` 不得从 gif_file 推导状态。
2. 使用 resolver 返回的完整 `stage_statuses`，包括：

   - succeeded
   - needs_attention
   - cancelled
   - failed（若状态模型仍支持）

3. 每条状态至少包含：

   ```json
   {
     "stage_id": "...",
     "clip_id": "...",
     "status": "...",
     "attempt_count": 1,
     "last_error": null
   }
   ```

4. 对 status 做固定排序，确保输入 Hash 和测试可重复。
5. 删除不再使用的 `get_gif_clip_terminal_statuses()` 或让专用 resolver 成为唯一调用入口，避免两套状态来源漂移。

## 7. P1-2：严格验证 Manifest Schema 版本

涉及文件：

- `app/task_engine/artifacts.py`
- `scripts/test_video_adaptive.py`
- `tests/task_engine/test_manifest_validation.py`

### 实施要求

1. 为每种 Manifest 定义支持版本，例如：

   ```python
   MANIFEST_SCHEMAS = {
       'discover_manifest': {1: validator_v1},
       'sample_manifest': {1: validator_v1},
       # ...
   }
   ```

2. `schema_version` 必须是整数；布尔值、字符串、0、负数和未知未来版本均拒绝。
3. `validate_manifest_json()` 根据 kind + version 选择对应 Validator。
4. materialize 输入信封也执行版本验证。
5. 错误消息包含 artifact kind、实际版本和支持版本列表。

## 8. P1-3：最终配置合并后重新计算 config_hash

涉及文件：

- `app/routers/tasks.py`
- `app/quality_lab/config_builder.py`
- `app/quality_lab/runner.py`
- `tests/task_engine/test_control_config_snapshot.py`
- `tests/quality_lab/test_isolation.py`

### 实施要求

1. Router 完成以下步骤后再计算 hash：

   ```text
   load full_config
   -> deep_merge request overrides
   -> add video_paths/_task/_experiment
   -> normalize
   -> extract business config
   -> calculate config_hash
   -> persist
   ```

2. 不信任请求携带的 config_hash；它只能作为预期值用于比对，不能直接作为最终值。
3. `_experiment` 不参与业务 hash，但必须保留在持久化配置中。
4. `_task`、run_id、item_id、工作目录等运行元数据是否参与 hash 必须统一定义，并由一个共享 helper 执行。
5. Quality Lab 的 ExperimentConfig hash 与最终 task job hash 应明确区分；若语义相同，必须完全一致。

## 9. P1-4：建立真实生产 E2E 发布门槛

涉及文件：

- `tests/task_engine/test_production_e2e.py`
- `tests/task_engine/test_e2e.py`
- pytest 配置

### 9.1 修正误导性测试

1. `test_single_gif_clip_failure_only_that_clip_retried` 必须真实执行 retry；否则重命名为状态查询测试。
2. 同名不同 SHA 测试必须实际调用 `_stage_materialize()` 或 Worker，不能只读取历史文件。
3. PBF 测试解析书签并验证 start/end，不只断言文件非空。
4. zero-clip 必须断言 `succeeded`，不能接受 `needs_attention`。

### 9.2 必须新增的生产链路

至少包含以下三层：

#### A. Worker 跨阶段 resolver

```text
discover -> sample
```

断言 sample 真实消费 discover Artifact。

#### B. Fan-out 与发布

```text
rank_dedup -> gif_clip A/B -> materialize
```

禁止手工创建 materialize 输入信封。

#### C. 完整八阶段

```text
discover -> sample -> vlm -> refine -> synthesize
-> rank_dedup -> gif_clip fan-out -> materialize
```

使用真实 `AdaptivePipelineAdapter` 和子进程。ffprobe/ffmpeg 使用临时短视频；VLM/LLM 使用正式依赖注入或本地 deterministic 服务，不访问用户的真实 Ollama/WSL 状态。

### 9.3 覆盖场景

- 全成功。
- zero-clip。
- 单 clip 失败后只重试失败 clip。
- 部分失败仍发布成功 GIF，但 video/job needs_attention。
- materialize 同名冲突。
- Worker 进程重启恢复。
- DB 关闭重开后继续执行。

## 10. 推荐实施顺序

1. 新增 Phase 0 RED 测试。
2. 修复 materialize resolver 的 Stage 驱动完整性检查。
3. 修复 publish conflict 与 Stage 状态语义。
4. 补齐完整终态信封。
5. 增加 Manifest schema 版本检查。
6. 修复最终 config_hash。
7. 最后补齐真实 retry 和完整八阶段 E2E。

建议提交边界：

```text
test: cover missing succeeded artifacts and publish conflicts
fix: resolve materialize inputs from succeeded stages
fix: propagate materialize publish failures to task status
fix: include all gif clip terminal states in input envelope
fix: validate manifest schema versions
fix: recompute task config hash after final merge
test: exercise retry and full production stage chain
```

## 11. 验证命令

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

不得使用：

```text
--ignore=tests/task_engine/test_e2e.py
```

最终报告必须提供未排除测试的完整输出。

## 12. 最终完成标准

只有满足以下全部条件，才能声明本轮完成：

1. succeeded gif_clip 缺失任意 Artifact 时 materialize 被阻止。
2. materialize 输入信封包含所有成功、失败、取消终态。
3. 同名不同 SHA 不覆盖历史文件，且新 GIF 成功发布或任务进入 needs_attention。
4. 发布失败不会形成 materialize/video/job 伪成功。
5. 未知 Manifest 和输入信封版本被拒绝。
6. 最终持久化 config_hash 与实际执行配置一致。
7. 单 clip retry 测试真实执行 Worker retry，成功 clip 不被重跑。
8. PBF 时间经过解析验证。
9. 完整八阶段生产 E2E 使用真实 Adapter/子进程并通过。
10. `python -m pytest -q` 无排除项通过。

修复报告必须逐项列出 RED/GREEN 测试名称、实际命令输出、数据库状态和正式输出证据；不能只用测试总数证明生产链路完成。
