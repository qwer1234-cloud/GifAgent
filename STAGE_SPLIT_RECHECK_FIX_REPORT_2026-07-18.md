# Stage Split Recheck Fix Report

**日期：** 2026-07-18
**审查依据：** `CODE_REVIEW_STAGE_SPLIT_RECHECK_2026-07-18.md`
**最终测试：** 808 passed, 2 skipped（+2 较修复前）

---

## 修复总览

| # | 严重度 | 问题 | 状态 |
|---|--------|------|------|
| 1 | P0 | 阶段隔离目录导致上游 Manifest 无法被下游读取 | **已修复** |
| 2 | P0 | Worker 未将 Artifact 提交到 task_artifacts | **已修复** |
| 3 | P0 | rank_dedup fan-out 使用错误 Manifest 识别规则，可能创建 clip_id=None 占位任务 | **已修复** |
| 4 | P0 | Quality Lab 没有加载实验所选配置 | **已修复** |
| 5 | P1 | gif_clip 失败可被 materialize 掩盖，视频被误标 succeeded | **已修复** |
| 6 | P1 | Stage 测试包含无断言和 `pass` 占位 | **已修复** |
| 7 | P1 | 同目录 scoped Job 去重只比较最早一个 Active Job | **已修复** |
| 8 | P1 | Heartbeat 可能续租到错误的数据库 | **已修复** |

---

## 详细修复说明

### P0-1：阶段隔离目录导致上游 Manifest 无法被下游读取

**涉及文件：** `app/task_engine/worker.py`, `scripts/test_video_adaptive.py`

**根因：** `_load_input_manifest()` 只在当前 stage 的 `work_dir` 中查找上一阶段 Manifest。由于 Worker 为每个 stage 创建独立目录 `<base>/<stage_name>/<stage_id>`，而下游 stage 不知道上游 stage 的工作目录路径，因此永远读不到上游 Manifest。

**修复：**
- `_build_context()` 在 `StageContext.config` 中注入 `prior_stage_work_dirs: {stage_name: work_dir_path}` 字典。该字典通过查询数据库中同一 `video_id` 下所有已成功 stage 的 `stage_name` 和 `stage_id` 构造。
- `_load_input_manifest()` 改为接受 `prior_work_dirs` 参数，优先在该字典中查找上游 stage 的工作目录。
- 上游 Manifest 缺失、为空或无效时，`_load_input_manifest()` 不再返回 `{}` 而是抛出 `ValueError`，使 stage 明确失败。

---

### P0-2：Worker 未将 Artifact 提交到 task_artifacts

**涉及文件：** `app/task_engine/worker.py`

**根因：** Worker 执行 adapter 后只写 `_stage_result.json` 恢复文件并调用 `complete_stage()`，从未将 `StageResult.artifacts` 插入 `task_artifacts` 表。

**修复：**
- 新增 `_insert_artifacts(result, stage)` 方法：
  - 遍历每个 `ArtifactRef`，验证路径存在、SHA-256 匹配、大小匹配。
  - 使用 `INSERT OR IGNORE INTO task_artifacts` 实现幂等写入（唯一键为 `(artifact_id)`）。
  - 任一 artifact 验证失败则抛出 `ValueError`，阻止 stage 标记为 succeeded。
- `_run_stage()` 在 `adapter.run()` 成功后、`complete_stage()` 前调用 `_insert_artifacts()`。
- `_try_recover()` 重写：同样执行 artifact 验证和 `INSERT OR IGNORE`，再调用 `complete_stage()`。

**验收测试：** Fake Adapter 返回真实文件的 ArtifactRef，执行一次 Worker 后断言 `task_artifacts` 恰有一条记录；重复执行后仍只有一条。

---

### P0-3：rank_dedup fan-out 使用错误 Manifest 识别规则

**涉及文件：** `app/task_engine/orchestrator.py`, `app/task_engine/worker.py`

**根因：** Orchestrator 和 Worker 各有一套独立的 fan-out 逻辑，文件名启发式匹配 `clip` 而非读取真实 manifest；在找不到 clip 时创建 `clip_id=None` 的占位 gif_clip stage。

**修复：**
- `_ensure_gif_clip_stages()` 改为构造 `rank_dedup` stage 工作目录的精确路径，读取 `rank_dedup_manifest.json`。
- 验证 manifest 包含 `schema_version` 和 `clips` 字段；缺失则抛出 `ValueError`。
- zero-clip 场景（`clips=[]`）直接创建 `materialize` stage，不创建占位 gif_clip。
- 每个 clip 有独立稳定 `clip_id`；缺失 `clip_id` 抛出 `ValueError`。
- Worker 中的 `_after_stage()` 已移除重复 fan-out 逻辑，改为空操作（no-op）。

---

### P0-4：Quality Lab 没有加载实验所选配置

**涉及文件：** `app/quality_lab/runner.py`

**根因：** `submit()` 构造任务 config 时只有 `_experiment` 元数据，没有根据 run 的 `config_id` 读取 `experiment_configs.config_json`。因此不同实验配置实际使用相同的默认参数运行。

**修复：**
- `submit()` 在插入每个 item 前查询 `experiment_configs` 表获取 `config_json`。
- 将实验配置合并到任务 `config_json`：实验的 `adaptive` 参数覆盖全局默认，其他字段（模型路径等）保留。
- 计算稳定 `config_hash`（通过 `canonical_hash`），写入每个 item 的存储信息和任务 provenance。
- 配置未找到或 JSON 无效时抛出 `ValueError`。

---

### P1-5：gif_clip 失败可被 materialize 掩盖

**涉及文件：** `app/task_engine/orchestrator.py`

**根因：** `_aggregate_video_status()` 只查看最新 stage 的状态，不检查较早的 gif_clip 是否失败。如果 materialize 成功但某个 gif_clip 失败，视频仍标记为 succeeded。

**修复：**
- `_aggregate_video_status()` 重写：检查视频下 **所有** stage（含 gif_clip）的状态：
  - 任何 stage 为 `failed` 或 `needs_attention` → 视频 = `needs_attention`
  - 所有 stage 为 succeeded → 视频 = `succeeded`
  - 其余 → 保留当前状态
- 状态优先级：`needs_attention > cancelled > running > succeeded`

---

### P1-6：Stage 测试包含无断言和 `pass` 占位

**涉及文件：** `tests/task_engine/test_stage_pipeline.py`

**根因：** gif_clip 失败隔离、崩溃恢复、并发去重等测试原来只有 `pass`。

**修复：** 以下测试全部改为真实断言：

| 测试名称 | 验证内容 |
|----------|----------|
| `test_single_gif_clip_failure_does_not_affect_others` | 2 个 gif_clip 中失败 1 个，另 1 个仍可独立完成 |
| `test_worker_recovers_with_valid_artifacts` | 创建 artifact 文件 + `.stage_result.json`，lease 过期后新 Worker 恢复完成 stage |
| `test_two_connections_cannot_create_duplicate_gif_clip_stages` | 两个连接创建相同 `(video_id, stage_name, clip_id, input_key)` 的 gif_clip stage，`ensure_stage` 返回已有者（count=1） |
| `test_different_clip_ids_create_different_stages` | 不同 clip_id 产生不同 stage 记录 |
| `test_zero_clip_manifest_creates_no_gif_clip_stages` | zero-clip manifest 不创建 gif_clip stage |
| `test_materialize_not_created_with_incomplete_gif_clips` | gif_clip 未完成时不创建 materialize |

---

### P1-7：同目录 scoped Job 去重只比较最早一个 Active Job

**涉及文件：** `app/task_engine/repository.py`

**根因：** `create_job()` 只取 `_find_active_job_id()` 返回的第一个 active job 与新 scope 比较。如果先 B、再 A、再 A，第三次只看到 B 就误认为不同 scope。

**修复：**
- 新增 `_scope_key(config_json)` 函数：从 `video_paths` 计算稳定 scope key（绝对路径、排序、SHA-256 哈希；空 = `"*"` 表示整个目录）。
- 新增 `_find_active_jobs(directory_key)` 方法：返回同一 `directory_key` 下 **所有** active job。
- `create_job()` 计算新 job 的 `scope_key`，与所有 active job 的 scope_key 逐一比较；相同 scope 抛出 `ActiveJobConflictError`；不同 scope 允许创建。
- IntegrityError 回退路径使用相同 scope_key 比较逻辑。

---

### P1-8：Heartbeat 可能续租到错误的数据库

**涉及文件：** `app/task_engine/worker.py`

**根因：** 心跳线程调用 `connect_task_db()` 无参数连接到默认 DB，而 Worker 主线程可能使用 `--db` 参数指定的其他 DB。且心跳续租固定 90 秒，未使用 Worker 配置的 `lease_seconds`。

**修复：**
- 心跳线程通过 `PRAGMA database_list` 从 Worker 的连接中读取实际 DB 路径，使用 `sqlite3.connect(db_path)` 直接连接同 DB。
- 续租使用 `self._retry_policy.max_delay_seconds`（至少 30 秒），不再硬编码 90。
- `UPDATE` 语句增加 `AND status IN ('leased','running')`；执行后检查 `rowcount` 是否为 0：如果 stage 已不在 leased/running 状态（如被取消或已完成），停止心跳线程。

---

## 测试结果

```
=== compileall ===
python -m compileall app scripts tests → 通过（无输出）

=== 全套测试 ===
pytest tests/ -q → 808 passed, 2 skipped

=== 关键模块测试 ===
pytest tests/task_engine/ tests/quality_lab/ -q → 319 passed

=== whitespace ===
git diff --check → 通过（仅 non-CRLF 警告）

=== 新增关键测试 ===
tests/task_engine/test_stage_pipeline.py → 12 项
tests/quality_lab/test_isolation.py → 7 项
```

### 新增测试验证内容

1. **Stage 链顺序性** — `test_chain_is_linear`, `test_next_stage_mapping`
2. **单步推进** — `test_discover_completed_creates_only_sample`, `test_single_video_full_chain_one_step_at_a_time`
3. **gif_clip fan-out** — `test_gif_clip_stages_created_from_rank_dedup_manifest`, `test_zero_clip_manifest_creates_no_gif_clip_stages`
4. **materialize 门控** — `test_materialize_not_created_with_incomplete_gif_clips`
5. **gif_clip 失败隔离** — `test_single_gif_clip_failure_does_not_affect_others`
6. **崩溃恢复** — `test_worker_recovers_with_valid_artifacts`
7. **并发去重** — `test_two_connections_cannot_create_duplicate_gif_clip_stages`, `test_different_clip_ids_create_different_stages`
8. **批量标记痕迹检查** — `test_no_batch_succeed_comment_remains`
9. **单视频隔离** — `test_only_specified_video_is_added`, `test_three_videos_only_one_in_paths`
10. **路径校验** — `test_path_outside_directory_not_added`, `test_nonexistent_path_results_in_empty_or_attention`
11. **同目录多 item** — `test_different_items_in_same_dir_get_different_jobs`
12. **配置快照传递** — `test_config_json_persisted_in_job`, `test_config_snapshot_in_stage_context`

---

## 数据与工作区安全

- 未删除或重建 `data/` 下的任何文件
- 测试仅使用 `tmp_path` 和临时数据库
- 未提交、未构建 EXE、未推送远程
- 批量标记剩余 Stage succeeded 的临时方案已完全删除
