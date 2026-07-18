# GifAgent 第二轮 Code Review 报告

日期：2026-07-18
审查对象：`master@ccbab52c` 上的当前未提交工作区
前置报告：`CODE_REVIEW_RECOMMENDATIONS_2026-07-18.md`
结论：仍不建议提交、构建 EXE 或推送

## 1. 本轮结论

上一轮问题只完成了部分修复：

- 已增加任务初始化、视频发现和 Stage 推进框架。
- 已修复重复点击收藏时重复写入 `favorite` 事件的问题。
- 已增加收藏与普通评分纠正时的 `favorite_gifs` 同步。
- 已恢复部分 `candidate_review.py` 兼容导出。

但核心生产链路仍未闭环：

- 桌面 Launcher 没有启动 Task Worker。
- 每个 Stage 仍然执行完整视频 Pipeline。
- 失败 Job 无法进入终态。
- 长任务没有续租，可能被多个 Worker 重复执行。
- Quality Lab CLI 仍未实现真实任务客户端。
- 完整测试仍然无法通过，并且部分测试会无限等待。

## 2. 修复状态总览

| 上一轮问题 | 当前状态 | 说明 |
|---|---|---|
| Control 新建任务不执行 | 部分完成 | 已有 Orchestrator，但 Launcher 未启动 Worker |
| Stage 重复完整处理 | 未完成 | `run_stage_mode()` 仍调用完整 `run_pipeline()` |
| Quality Lab CLI 不可用 | 未完成 | `_build_runner()` 仍抛出 `NotImplementedError` |
| 收藏重复写事件 | 已完成 | 只在真正插入收藏时记录事件 |
| 收藏纠正一致性 | 部分完成 | 表同步已增加，但同一事件仍可重复纠正 |
| UI 兼容和测试 | 部分完成 | 收集错误已消除，但仍有 11 项 UI 失败 |
| 运行产物隔离 | 未完成 | 日志、数据库、备份和结果文件仍在工作区 |

## 3. P0：桌面程序没有启动 Task Worker

### 证据

`app/ui/launcher.py:180-183` 只启动 FastAPI：

```python
api_thread = threading.Thread(target=start_api_server, daemon=True)
api_thread.start()
```

生产代码中 `TaskWorker` 仍然只在 `scripts/task_worker.py:107` 被实例化。Control 创建 Job 后，除非用户手工运行 Worker 脚本，否则任务不会开始。

### 推荐修改

1. 在 Launcher 初始化数据库和 FastAPI 后启动受控 Worker。
2. 为 Worker 增加 `stop_event`，关闭桌面窗口时请求干净退出。
3. Worker 启动失败必须输出到 Status/Event 日志，不能只留下 `pending` Job。
4. 多个 EXE 同时运行时依靠 Lease 保证 Stage 不会重复执行。
5. 增加 Launcher 集成测试，验证启动后无需手工脚本即可处理 Job。

### 验收标准

- 打开 EXE 后创建一个测试目录任务，Job 自动从 `pending` 进入 `running`。
- 不运行 `scripts/task_worker.py` 也能完成任务。
- 关闭 EXE 后 Worker 不再继续领取新 Stage。

## 4. P0：Stage 模式仍执行完整 Pipeline

### 证据

`scripts/test_video_adaptive.py:1159-1160`：

```python
try:
    output = run_pipeline(video_path, FRAMES_DIR, EXPORT_DIR, cfg)
```

`stage` 和 `clip_id` 只写入结果或参数，没有决定实际执行范围。Orchestrator 通过 `_NEXT_STAGE` 每次只创建一个 Stage，并且创建 `gif_clip` 时没有传递具体 `clip_id`。

当前行为会导致：

- 一个视频最多完整处理 8 次。
- 每个 Stage 使用独立工作目录，却不读取上一个 Stage 的 Artifact。
- 单个 GIF 失败时不能只重建该 GIF。
- 重试可能覆盖或重复生成已经成功的输出。

### 推荐修改

1. 将完整 Pipeline 拆成真实 Stage Handler。
2. 每个 Handler 只读取上游 manifest/Artifact 并生成自己的结果。
3. `synthesize` 或 `rank_dedup` 完成后，为每个 Clip 创建独立 `gif_clip` Stage。
4. 每个 `gif_clip` 必须带稳定 `clip_id`。
5. 所有 Clip 成功后才创建单一 `materialize` Stage。
6. 增加调用计数测试，证明每个 Handler 只调用一次。

### 验收标准

- `sample` Stage 不执行 VLM 和 GIF 导出。
- `vlm` Stage 不重新采样视频。
- 单个 `gif_clip` 失败只重试对应 `clip_id`。
- 上游已成功 Artifact 的文件哈希和修改时间保持不变。

## 5. P1：失败 Job 永远保持 running

### 证据

`app/task_engine/orchestrator.py:165-190` 遇到失败视频时执行：

```python
if vstatus in _TERMINAL_STATES:
    if vstatus == "failed" or vstatus == "needs_attention":
        all_done = False
```

因此，只要存在失败视频，`all_done` 永远为 `False`，后面的失败终态聚合永远不会运行。

最小数据库复现结果：

```text
advance_first=running
advance_second=running
job_status=running
video_status=needs_attention
```

这会持续占用 `uq_active_job_directory`，用户无法重新添加该目录。

### 推荐修改

- `all_done` 只表示所有视频是否到达终态，不应因为终态是失败而变成 `False`。
- 单独维护 `has_failures` 和 `has_attention`。
- 建议终态优先级：`needs_attention` > `failed` > `succeeded`。
- 增加单视频失败、全部失败、部分成功和部分失败测试。

## 6. P1：Orchestrator 事务边界损坏

### 6.1 `_write_event()` 被重复定义

`app/task_engine/orchestrator.py:352-359` 已有带 `commit()` 的实现，但 `368-373` 又定义了同名函数并覆盖前者，后一个版本没有提交事务。

实测：

```text
event_visible_same_conn=1
event_committed_other_conn=0
```

删除重复定义，并统一决定事件是由调用者事务提交，还是由 `_write_event()` 自行提交。不要同时采用两种模式。

### 6.2 Cancel/Retry 不是单一事务

`_cancel_job()` 和 `_retry_job()` 在 `BEGIN IMMEDIATE` 后调用 `_set_job_status()`，而 `_set_job_status()` 会自行 `commit()`。这会提前提交部分更新，后续命令状态更新失败时无法整体回滚。

推荐增加不提交版本的内部更新函数，最终只由 Cancel/Retry 的外层事务提交一次。

## 7. P1：90 秒 Lease 无法覆盖一小时任务

### 证据

- `TaskRepository.claim_stage()` 默认 `lease_seconds=90`。
- `AdaptivePipelineAdapter` 子进程允许运行 `timeout=3600`。
- `TaskWorker._run_stage()` 同步等待 Adapter 完成，期间没有调用 `heartbeat()`。

第二个 Worker 可以在 90 秒后重新领取相同 Stage，造成重复 VLM 请求、重复导出和数据库竞争。

### 推荐修改

1. Adapter 执行期间启动后台心跳线程。
2. 心跳间隔应小于 Lease 的三分之一，例如 20-30 秒。
3. Adapter 结束后无论成功或异常都停止并等待心跳线程退出。
4. `complete_stage()` 前再次验证 Lease Owner。
5. 增加两个 Worker 的并发测试：第一个持续心跳时，第二个不能领取 Stage。

## 8. P1：Quality Lab 生产入口仍不可用

### 证据

`scripts/run_quality_experiment.py:24-39` 仍直接抛出：

```python
raise NotImplementedError(
    "A concrete TaskClient is not yet wired. ..."
)
```

`app/quality_lab/runner.py:140-162` 仍使用 Benchmark 视频的父目录创建任务，无法保证一个 Experiment Item 只处理一个视频。同目录多个 Item 还会触发活动目录冲突。

### 推荐修改

- 实现真实 `HttpTaskClient`。
- 扩展任务 API 支持明确的 `source_path` 或 `video_paths`。
- Quality Lab 必须以视频指纹或 Item ID 作为实验任务身份的一部分。
- CLI 连接失败时返回非零退出码，并保持 Item 为可重试状态。

### 测试盲区

Quality Lab 当前测试为 `153 passed`，但测试使用 Fake/Stub Client，没有执行真实 `_build_runner()`、HTTP API 或 CLI，因此不能证明生产入口可用。

## 9. P1：任务配置快照和视频指纹不完整

### 配置快照

`app/routers/tasks.py:154-156` 只保存：

```python
{"limit": body.limit, "extensions": body.extensions}
```

Stage 因此拿不到当前 Adaptive、VLM、Preference Memory 和模型配置。队列处理结果可能与 Config 页显示的配置不同。

推荐在创建 Job 时保存经过验证的完整配置快照，并保证整个 Job 生命周期使用同一快照。

### 视频指纹

`app/task_engine/orchestrator.py:115` 当前调用：

```python
repo.add_video(job_id, path, "")
```

所有视频指纹为空，无法可靠恢复、实验归因或判断源文件是否变化。应在初始化阶段计算并写入稳定指纹；如果完整 SHA-256 成本过高，可先保存快速指纹，再由 Discover Stage 补充完整哈希。

## 10. P1：偏好事件仍可被重复纠正

### 已完成部分

`FavoriteService.favorite()` 现在只在真正插入新收藏时写入 `favorite` 事件，重复点击不再重复计权。

`correct_feedback()` 也增加了 `favorite_gifs` 的增删同步。

### 剩余问题

`app/services/preference_events.py:105-109` 只按 `event_id` 查询原事件，没有检查：

- 原事件是否已经撤销。
- 原事件是否已经被另一个 correction supersede。
- correction 是否针对当前有效事件。

同一原事件被纠正两次后会产生多个同时有效的 correction。纠正旧收藏还可能删除用户后来重新建立的收藏。

推荐在单一写事务中验证原事件仍有效，并通过唯一约束或原子条件更新防止并发重复纠正。

## 11. P1：测试门禁仍未恢复

### 本轮测试结果

| 测试范围 | 结果 |
|---|---|
| Python compileall | 通过 |
| Orchestrator | 14 passed |
| 收藏与偏好相关 | 34 passed |
| Quality Lab | 153 passed，1 warning |
| UI 兼容与布局 | 17 passed，11 failed |
| `git diff --check` | 通过 |
| 完整 pytest | 无法完成，Worker 测试挂起 |

### UI 剩余失败

`candidate_review.py` 已恢复以下导出：

- `is_batch_command_line`
- `summarize_checkpoint_status`
- Config Help/CSS/JS 常量
- Review CSS/快捷键常量

但仍缺少合理兼容项：

- `rate_and_advance`
- `profile_publish_choices`
- `undo_last_action`
- `load_candidate_page`

其余只检查源码字符串必须出现在 `candidate_review.py` 的测试，应改为验证 `build_workbench()` 的最终行为和模块归属。

### Worker 测试挂起

`TaskWorker.run_forever()` 已改为无限轮询，但现有测试仍直接调用并期待返回计数。需要提供：

- `run_forever(stop_event=...)` 用于常驻服务。
- `run_until_idle()` 或 `drain()` 用于测试和一次性 CLI。

不能通过给测试增加超长等待来掩盖问题。

## 12. P2：提交范围仍未清理

当前工作区仍包含不适合直接提交的内容：

- 数据库 WAL/SHM 删除。
- `data/quality_lab.db`。
- 多个批处理、Resize、Pipeline 和 VLM 日志。
- `data/backups/*.bak`。
- 大幅变化的运行结果 JSON。
- `.superpowers/brainstorm/.last-*`。

处理原则：加入 `.gitignore` 或从暂存范围排除，但不要删除用户本地历史数据。最终必须逐文件暂存并审查 `git diff --cached`。

## 13. 推荐修复顺序

1. 删除重复 `_write_event()`，修复 Job 失败终态和 Cancel/Retry 事务。
2. 为 Worker 增加 Stop Event、Drain 模式和自动心跳。
3. 在 Launcher 中启动 Worker，并添加冷启动集成测试。
4. 拆分真实 Stage Handler，建立 Artifact 输入输出协议。
5. 为每个 Clip 创建独立 `gif_clip` Stage。
6. 保存完整配置快照和视频指纹。
7. 实现 Quality Lab `HttpTaskClient` 和单视频任务请求。
8. 阻止同一偏好事件被重复纠正。
9. 恢复必要 UI 兼容导出，更新过时的源码结构测试。
10. 运行完整测试，清理提交范围，最后再构建 EXE。

## 14. 完成定义

- [ ] Launcher 自动启动 Worker。
- [ ] Job 可以从创建处理到成功、失败、取消或待处理终态。
- [ ] 失败 Job 不再永久占用目录唯一约束。
- [ ] 每个 Stage 只执行自己的工作。
- [ ] 每个 GIF 有独立 `clip_id` 和可重试 Stage。
- [ ] Worker 处理长任务时持续续租。
- [ ] Quality Lab CLI 使用真实 HTTP Task Client。
- [ ] Job 保存完整配置快照和视频指纹。
- [ ] 同一偏好事件不能产生多个有效 correction。
- [ ] UI 兼容测试和完整 pytest 全部通过。
- [ ] 运行数据和用户历史未被删除或误提交。
- [ ] EXE 和 `_internal` 来自同一份已验证源码。

## 15. 复审说明

本轮未修改任何生产代码。外部 Codex 对抗审查运行 5 分钟后超时，未返回额外结果；本文结论来自源码审读、针对性测试和临时数据库复现。
