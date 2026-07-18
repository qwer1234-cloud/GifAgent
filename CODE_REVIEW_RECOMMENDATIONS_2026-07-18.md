# GifAgent 未提交实现 Code Review 改进建议

日期：2026-07-18
适用范围：当前项目根目录中的未提交修改
目标读者：接手修复、验证和整理提交的下一个 Agent

## 1. 结论

当前实现不应直接提交、构建 EXE 或推送。主要阻断点不是界面细节，而是新任务引擎尚未形成完整生产链路：Control 可以创建 Job，但不会发现视频、创建 Stage 或启动 Worker；即使补上调度，现有 Stage Adapter 也会在每个逻辑阶段重复执行完整视频流水线。

建议严格按本文顺序处理：

1. 修通任务创建、阶段推进和 Worker 生命周期。
2. 让每个 Stage 只执行自己的工作，并复用上游产物。
3. 补完 Quality Lab 的真实任务客户端和单视频任务语义。
4. 修复收藏与偏好事件的一致性。
5. 恢复测试兼容并更新已过时的结构断言。
6. 隔离运行数据，完成全量测试后再构建和提交。

## 2. 修改边界与数据保护

实施过程中必须遵守以下约束：

- 不删除用户已有 GIF、标签、收藏、偏好画像、任务历史或数据库备份。
- 不通过删除 `data/`、重建数据库或清空表来让测试通过。
- 数据库迁移必须可重复执行，并在旧数据副本上验证行数和关键字段。
- 修改打包代码后，同时验证 `dist/GifAgentUI/GifAgentUI.exe` 和 `dist/GifAgentUI/_internal/`，不能只替换 EXE。
- 不要使用 `git add .` 或 `git add -A`。最终必须逐文件暂存，避免把日志、数据库和实验输出提交进去。
- 当前工作区已有大量用户修改；不要回退或覆盖与本任务无关的内容。

## 3. P0：修通任务引擎生产链路

### 3.1 当前问题

涉及文件：

- `app/routers/tasks.py`
- `app/task_engine/repository.py`
- `app/task_engine/worker.py`
- `app/task_engine/schema.py`
- `app/task_engine/fingerprints.py`
- `app/ui/tabs/control.py`
- `app/ui/launcher.py`
- `scripts/task_worker.py`

`POST /api/tasks/jobs` 当前只创建状态为 `pending` 的 `task_jobs` 记录。生产代码没有调用 `TaskRepository.add_video()` 和 `ensure_stage()`，也没有启动任务 Worker。因此：

- Job 永远停留在 `pending`。
- Job 的视频数和 Stage 数始终为零。
- 同目录活动任务唯一约束一直生效，导致该目录以后也不能再次加入。
- Cancel/Retry 命令只被写入数据库，没有持续运行的 Worker 消费它们。

### 3.2 推荐实现

新增一个生产级编排服务，例如 `app/task_engine/orchestrator.py`，负责：

1. 解析 Job 的目录、扩展名和数量限制。
2. 使用稳定排序发现目录中的视频。
3. 为每个视频计算指纹，并调用 `add_video()`。
4. 为每个视频创建首个可执行 Stage。
5. 在 Stage 成功后创建或解锁下一个 Stage。
6. 根据 Stage 状态聚合并更新 `task_videos.status` 和 `task_jobs.status`。
7. 在取消、失败、需要人工处理和全部成功时写入明确事件。

建议的状态推进：

```text
Job pending
  -> discovering
  -> running
  -> completed | failed | needs_attention | cancelled

Video pending
  -> running
  -> completed | failed | needs_attention | cancelled
```

不要让路由函数执行耗时视频扫描。路由只应创建 Job 并通知常驻 Worker，扫描本身作为 `discover` 阶段执行。

### 3.3 Worker 生命周期

当前 `TaskWorker.run_forever()` 实际是“处理到队列为空就退出”，`poll_seconds` 没有使用。需要二选一：

- 推荐：由 `app/ui/launcher.py` 启动一个常驻 Worker 线程或子进程，队列为空时按 `poll_seconds` 轮询，并提供干净退出机制。
- 或者：每次创建 Job 后显式启动一个受控 Worker 子进程，并使用数据库 Lease 保证同一 Stage 不会被多个 Worker 重复执行。

无论采用哪一种，都必须避免：

- 启动多个 EXE 后重复处理同一 Stage。
- Worker 崩溃后 Stage 永久保持 `running`。
- UI 关闭时强杀仍在写数据库的线程。
- API 返回成功但 Worker 实际没有启动。

### 3.4 验收标准

- 创建包含 2 个视频的目录任务后，数据库出现 1 个 Job、2 个 Video 和可执行 Stage。
- 不需要手工运行 `scripts/task_worker.py`，任务也能自动开始。
- 同一个目录在活动任务期间返回 409，并带回已有 Job ID。
- Job 完成、取消或明确终止后，该目录可以重新加入。
- Worker 被异常终止后，Lease 到期可恢复任务，不重复已完成 Stage。
- Control 可以显示当前视频、当前 Stage、进度、失败原因和最终状态。

## 4. P0：实现真正的阶段化执行

### 4.1 当前问题

涉及文件：

- `scripts/test_video_adaptive.py`
- `app/task_engine/adaptive_adapter.py`
- `scripts/task_worker.py`

`run_stage_mode(stage=..., clip_id=...)` 当前忽略阶段边界，始终调用完整 `run_pipeline()`。Worker 又为每个视频配置了以下 8 个 Adapter：

```text
discover -> sample -> vlm -> refine -> synthesize -> rank_dedup -> gif_clip -> materialize
```

如果直接接通调度，同一视频会重复运行完整流水线最多 8 次。单个 GIF 失败重试也会重新做采样和 VLM，成本高且容易产生重复输出。

### 4.2 推荐实现

为每个 Stage 定义明确的输入、输出和恢复条件：

| Stage | 输入 | 输出 | 恢复条件 |
|---|---|---|---|
| discover | 视频路径、Job 配置 | 视频元数据、指纹 | 指纹和元数据文件有效 |
| sample | 视频元数据 | 采样帧清单 | 帧文件及 manifest 完整 |
| vlm | 采样 manifest | VLM 原始响应、结构化标注 | 响应可解析且数量匹配 |
| refine | 粗候选 | 精修候选 | 候选边界合法 |
| synthesize | 精修候选 | Clip 计划 | Clip ID 稳定且可重复生成 |
| rank_dedup | Clip 计划、偏好画像 | 排序及去重结果 | 结果包含 provenance |
| gif_clip | 单个 `clip_id` | 临时 GIF Artifact | 文件存在且可解码 |
| materialize | 临时 Artifact | 最终 GIF、数据库记录 | 导出文件和数据库一致 |

实施要求：

- `stage` 必须真正决定执行哪个处理函数。
- `clip_id` 必须限制 `gif_clip` 只生成指定 GIF。
- 下游 Stage 通过 Artifact/manifest 读取上游结果，不能重新计算上游步骤。
- 每个输出先写临时文件，验证成功后再原子替换。
- Stage 重试必须具有幂等性，即重复执行不会产生重复数据库记录或重复最终文件。
- `clear_output_dir` 不能在 Stage 重试时清除其他已成功 GIF。

### 4.3 验收标准

- 单个视频完成后，每个 Stage 只执行一次。
- 故意让一个 `gif_clip` 失败，只重跑对应 `clip_id`。
- 重启 Worker 后从最后一个未完成 Stage 继续。
- 已完成 GIF 的修改时间和数据库记录在失败重试期间保持不变。
- 相同输入和配置重复执行时，Artifact ID 和 provenance 可追踪且不会重复入库。

## 5. P1：补完 Quality Lab 的真实执行链路

### 5.1 当前问题

涉及文件：

- `scripts/run_quality_experiment.py`
- `app/quality_lab/runner.py`
- `app/routers/quality_lab.py`
- `app/routers/tasks.py`

`scripts/run_quality_experiment.py::_build_runner()` 对所有命令直接抛出 `NotImplementedError`。另外，`ExperimentRunner.submit()` 使用 Benchmark 视频的父目录创建任务。这会导致：

- 同一目录中的多个 Benchmark 视频发生活动目录冲突。
- 一个实验 Item 实际处理整个目录，而不是指定视频。
- Item 与 Job 的一对一关系不成立，实验指标不可归因。

### 5.2 推荐实现

1. 实现 `HttpTaskClient`，至少覆盖：
   - 创建任务。
   - 查询单个任务。
   - 取消任务。
   - 重试任务或失败 Stage。
2. 扩展任务请求，使其支持明确的 `video_paths` 或单个 `source_path`。
3. 任务去重键应同时考虑任务类型和视频指纹，不能仅使用父目录。
4. `experiment_items` 应保存独立 `task_job_id`、源视频指纹、配置 ID 和最终 Artifact 引用。
5. CLI 应使用非零退出码报告失败，并输出可供脚本解析的错误信息。

### 5.3 验收标准

- `create`、`submit`、`refresh`、`cancel` 命令均不再抛出 `NotImplementedError`。
- 同一目录下两个视频可以作为两个独立实验 Item 提交。
- 每个 Item 只处理自己的视频。
- 重复执行 `submit` 不会创建重复 Job。
- API 不可用时，CLI 给出连接错误并返回非零退出码，不留下虚假的 `running` 状态。

## 6. P1：修复收藏与 Preference Memory 一致性

### 6.1 当前问题

涉及文件：

- `app/services/favorites.py`
- `app/services/preference_events.py`
- `app/routers/candidates.py`
- `app/services/preference_memory.py`

`FavoriteService.favorite()` 使用 `INSERT OR IGNORE` 防止重复收藏，但即使收藏记录没有新建，仍会追加新的 `favorite` 偏好事件。用户重复点击会让一个候选在 Preference Memory 中被重复计权。

此外，把一个事件从 `favorite` 纠正为其他评分，或从其他评分纠正为 `favorite` 时，`favorite_gifs` 没有同步更新。

### 6.2 推荐实现

- 只有 `cursor.rowcount == 1` 时才写入新的 `favorite` 事件。
- 或为收藏事件增加唯一业务键，确保同一候选只有一个有效收藏事件。
- `correct_feedback()` 应在单一事务中同步：
  - `preference_events`
  - `candidate_gifs.status`
  - `favorite_gifs`
- 纠正链应验证原事件仍然有效，避免同一事件被多次并行纠正。
- 撤销收藏只撤销与该收藏动作对应的记录，不能误删后续重新创建的收藏。

### 6.3 必须补充的测试

- 连续收藏同一候选两次，只产生一条有效收藏事件。
- 收藏后撤销，收藏表和有效事件流都恢复。
- `favorite -> like` 纠正后从收藏列表移除。
- `like -> favorite` 纠正后加入收藏列表。
- 同一事件第二次纠正被拒绝或形成明确、唯一的纠正链。
- 任一步骤数据库异常时，三个相关表全部回滚。

## 7. P1：恢复兼容接口并修正测试边界

### 7.1 当前问题

`app/ui/candidate_review.py` 已重构为 Workbench 薄入口，但旧测试和可能存在的外部调用仍从这里导入：

- `is_batch_command_line`
- `summarize_checkpoint_status`
- `CONFIG_FIELD_HELP`
- `CONFIG_TOOLTIP_CSS`
- Review/Profile 相关辅助函数和 UI 常量

完整测试在收集阶段出现 4 个 ImportError；聚焦测试结果为：

```text
681 passed, 1 skipped, 13 failed
```

### 7.2 推荐实现

先区分两类失败：

1. **公共兼容契约**：仍被生产代码、脚本或合理测试使用的名称，应在 `candidate_review.py` 临时重新导出，并注明弃用计划。
2. **过时的源码结构断言**：只检查某段源码必须存在于单一文件的测试，应改为验证 Workbench 最终行为或新的模块归属。

不要为了通过测试把所有 UI 逻辑重新复制回 `candidate_review.py`。推荐保持薄入口，只增加有限的兼容导出。

### 7.3 验收标准

- `pytest` 可以完成收集。
- Workbench 重构后的行为测试覆盖 Control、Review、Profile、Settings 和 Lab。
- 旧入口的必要公共导入仍可使用。
- 不再用大段源码字符串断言代替 UI 行为测试。
- `candidate_review.py` 不出现与 Tab 模块重复的业务实现。

## 8. P1：整理提交范围和运行产物

当前工作区包含大量不适合直接提交的运行产物，例如：

- `data/quality_lab.db`
- `data/vlm_loop.log`
- `data/lapkalu_batch*.log`
- `data/resize_gifs*.log`
- `data/pipeline_stage2.log`
- `data/backups/*.bak`
- `data/library.db-wal`
- `data/library.db-shm`
- `.superpowers/brainstorm/.last-*`
- 大幅变化的运行结果 JSON

处理原则：

1. 先用 `git status --short` 保存当前清单。
2. 判断每个文件是源码、测试夹具、必要文档，还是纯运行数据。
3. 运行数据加入 `.gitignore`，但不要删除用户磁盘上的原文件。
4. 已被 Git 跟踪的运行结果若不应更新，只从本次提交中排除，不要擅自回退用户修改。
5. 测试确实需要的数据库或 JSON 应缩减为脱敏、稳定、最小化 Fixture。
6. 最终逐文件执行 `git add <file>`，再检查 `git diff --cached --stat` 和 `git diff --cached`。

## 9. 推荐实施顺序

### 阶段 A：建立基线

1. 保存 `git status --short` 和当前测试结果。
2. 备份将用于迁移测试的数据库，不能覆盖原库。
3. 为任务 API、Worker、Stage Adapter 和收藏一致性新增失败测试。

### 阶段 B：任务引擎

1. 实现 Job 初始化与视频发现。
2. 实现状态聚合和 Stage 推进。
3. 实现常驻 Worker 生命周期。
4. 验证取消、Lease 恢复、重试和同目录去重。

### 阶段 C：阶段化 Pipeline

1. 拆分真实 Stage Handler。
2. 定义 Artifact manifest 和稳定 ID。
3. 实现单 Clip 重试。
4. 验证已成功输出不会被重写。

### 阶段 D：Quality Lab 与偏好事件

1. 实现 `HttpTaskClient`。
2. 支持单视频实验任务。
3. 修复收藏幂等和纠正事务。
4. 验证 Preference Memory 不会重复计权。

### 阶段 E：兼容、文档和提交清理

1. 恢复必要兼容导出。
2. 更新过时测试。
3. 更新 README、Agent.md 和使用手册中的真实行为，不能描述尚未实现的功能。
4. 隔离运行产物并检查暂存区。

### 阶段 F：构建前验证

按顺序执行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

如果 `uv` 因全局缓存权限失败，继续使用项目 `.venv`，不要修改或清空用户全局缓存。

只有全部阻断测试通过后，才执行项目既有 EXE 构建流程。构建完成后必须验证：

- EXE 能启动 FastAPI 和 Gradio。
- Control 创建任务后 Worker 自动开始。
- 同目录重复添加被正确拒绝。
- 单个失败 GIF 可单独重试。
- 历史任务、收藏和 Preference Memory 仍存在。
- `dist/GifAgentUI/_internal/` 包含最新代码和依赖。
- 构建产物版本清单与当前提交一致。

## 10. 建议测试矩阵

| 层级 | 场景 | 预期结果 |
|---|---|---|
| Repository | 并发创建同目录 Job | 仅一个成功，另一个返回已有 Job ID |
| API | 创建合法目录任务 | 返回 201，并最终出现视频与 Stage |
| API | 创建空目录任务 | 明确完成或失败，不永久 pending |
| Worker | 队列暂时为空 | Worker 保持等待，不退出 |
| Worker | Lease 中途过期 | 任务可恢复且不重复已提交结果 |
| Pipeline | 单 Stage 重试 | 只执行失败 Stage |
| Pipeline | 单 Clip 失败 | 只重建对应 GIF |
| Quality Lab | 同目录两个 Benchmark 视频 | 创建两个独立 Item/Job |
| Preference | 重复收藏 | 只有一个有效收藏事件 |
| Preference | 收藏纠正与撤销 | 收藏表、候选状态、事件流一致 |
| UI | 旧入口兼容导入 | 不发生 ImportError |
| Packaging | 冷启动 EXE | API、UI、Worker 都能启动 |
| Packaging | 使用旧历史数据库启动 | 自动迁移且历史数据不丢失 |

## 11. 完成定义

只有同时满足以下条件，才能认为本轮改进完成：

- [ ] Control 创建的任务可以自动处理到终态。
- [ ] 每个 Stage 只执行自身职责。
- [ ] 单个失败 GIF 可以独立重试。
- [ ] Quality Lab CLI 使用真实任务 API。
- [ ] Benchmark Item 与具体视频一一对应。
- [ ] 收藏操作幂等，纠正和撤销保持数据库一致。
- [ ] 完整测试可以收集并全部通过。
- [ ] `git diff --check` 通过。
- [ ] 用户历史数据未被删除或重置。
- [ ] 运行日志、数据库和备份未混入提交。
- [ ] EXE 与 `_internal` 均来自同一份最新代码。
- [ ] README、Agent.md 和使用手册只描述已验证行为。

## 12. 接手 Agent 的最终交付格式

完成后请报告：

1. 修改了哪些生产链路，以及为何这样设计。
2. 数据库是否迁移，迁移前后行数如何验证。
3. 新增和更新了哪些测试。
4. 完整测试的准确通过/失败数量。
5. EXE 输出路径、构建时间和版本信息。
6. 使用真实目录验证了哪些 UI 和队列行为。
7. 哪些运行产物被排除在提交之外。
8. 最终 Commit ID 和远程 Push 结果；未被明确授权时不要自行提交或推送。
