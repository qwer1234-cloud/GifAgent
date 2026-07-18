# Stage Split 复核与修复要求（2026-07-18）

## 结论

本轮所谓“全阶段 Split”尚未达到可运行、可提交、可构建的标准。现有测试数量虽然达到 `806 passed, 2 skipped`，但关键测试存在空实现，且真实 Worker 链路无法可靠地把上一阶段产物交给下一阶段。

在下列 P0/P1 问题全部修复，并完成本文末尾的端到端验收前，不要提交、构建 EXE 或推送远程。

## P0：必须首先修复

### 1. 阶段隔离目录导致上游 Manifest 无法被下游读取

证据：

- `app/task_engine/worker.py:303-304` 为每个 Stage 创建独立目录：`<base>/<stage_name>/<stage_id>`。
- `scripts/test_video_adaptive.py:1269-1274` 的 `_load_input_manifest()` 却只在当前 Stage 的 `work_dir` 中查找上一阶段 Manifest。
- `scripts/test_video_adaptive.py:1366-1373` 在找不到 discover Manifest 时使用 `duration_s=0`，sample 阶段会静默产生 0 个采样点，而不是失败。

影响：discover 即使成功写出 Manifest，sample 也读不到；后续 vlm、refine、synthesize、rank_dedup 同样断链。Stage 可能显示 succeeded，但实际没有处理任何有效输入，属于静默数据损坏。

修复要求：

1. 建立明确的阶段 Artifact 依赖协议。下游 Stage 必须根据 `input_key` 或数据库中的 Artifact 记录解析上游 Manifest 的绝对路径和 SHA-256，不能猜测当前目录中的文件。
2. `StageContext` 应携带已验证的输入 Artifact，或提供统一的 Artifact resolver；不要让业务脚本直接跨目录猜路径。
3. 输入 Manifest 缺失、类型不符、文件不存在或哈希不一致时必须使 Stage 失败，禁止回退为 `{}`、`0` 或空列表后成功。
4. Manifest 应包含 schema/version 字段，并在读取时验证必需字段。

验收测试：使用不同的 `stage_id` 和不同工作目录依次执行 discover、sample，断言 sample 读取到 discover 的真实 `duration_s`，产生预期采样点；删除或篡改 discover Manifest 后，sample 必须明确失败。

### 2. Worker 没有把 `StageResult.artifacts` 提交到 `task_artifacts`

证据：

- `app/task_engine/worker.py:481-489` 仅执行 adapter、写 `_stage_result.json`、调用 `complete_stage()`。
- `app/task_engine/worker.py:353-374` 只把 Artifact 元数据写进恢复文件，没有调用 Artifact commit/repository 入库逻辑。
- `app/task_engine/worker.py:523-527` 和 `app/task_engine/orchestrator.py:292-296` 的 gif_clip fan-out 均依赖 `task_artifacts`。

影响：Stage 返回 ArtifactRef 也不会出现在数据库，rank_dedup fan-out 没有输入，崩溃恢复也无法证明数据库和文件系统状态一致。

修复要求：

1. Worker 在 Stage 标记 succeeded 前，逐个验证 Artifact 的路径、大小和 SHA-256，并写入 `task_artifacts`。
2. Artifact 入库与 `complete_stage()` 必须具备原子语义：全部成功才完成 Stage；任一 Artifact 校验或写入失败时，Stage 不得成功。
3. 恢复路径 `_try_recover()` 也必须补齐相同的 Artifact 校验与幂等入库，不能仅依据 `_stage_result.json` 完成 Stage。
4. 使用数据库唯一约束或幂等 upsert 防止 Worker 重试产生重复 Artifact。

验收测试：Fake Adapter 返回一个真实文件的 ArtifactRef，执行一次 Worker 后断言 `task_artifacts` 恰有一条正确记录；重复恢复/重试后仍只有一条；篡改文件后恢复必须拒绝成功。

### 3. rank_dedup fan-out 使用错误的 Manifest 识别规则，并制造 `clip_id=None` 占位任务

证据：

- `app/task_engine/worker.py:535` 和 `app/task_engine/orchestrator.py:303` 只读取文件名包含 `clip` 的 JSON。
- 实际输出名为 `rank_dedup_manifest.json`，文件名不含 `clip`。
- `app/task_engine/worker.py:557-565` 以及 `app/task_engine/orchestrator.py:321-324` 在找不到 clip 时创建无 `clip_id` 的 gif_clip 占位 Stage。

影响：即使 Artifact 正确入库，也可能无法识别真实 Manifest；占位 Stage 混淆“合法零 clip”与“Artifact 丢失/协议错误”，可能掩盖故障并产生不可执行任务。

修复要求：

1. 按 `stage_name + artifact kind/schema` 识别 rank_dedup Manifest，不要使用文件名子串启发式判断。
2. 对 Manifest 做结构校验，并从唯一、已验证的 rank_dedup Manifest 中读取 clip_id。
3. 若 `clips=[]` 是合法结果，应记录明确的 zero-clips 结果，并直接创建 materialize 或进入定义清晰的终态。
4. 若 Manifest 缺失、无效或不可读，应令 rank_dedup/fan-out 失败，不得创建 `clip_id=None` 占位 Stage。
5. Worker 和 Orchestrator 不应各自维护一套不同的 fan-out 逻辑；保留一个事务性、幂等的唯一入口。

验收测试：覆盖 0、1、N 个 clips；覆盖 Manifest 缺失/损坏；断言 N 个 clip 只创建 N 个具有非空 clip_id 的 gif_clip Stage，重复推进不会增加数量。

### 4. Quality Lab 没有加载实验所选配置

证据：

- `app/quality_lab/runner.py:257-264` 为任务构造的 config 只有 `_experiment` 元数据，没有根据 run 的 `config_id` 读取 `experiment_configs.config_json`。
- `app/routers/tasks.py:161-168` 把全局配置放在 `config_snapshot` 下。
- `scripts/test_video_adaptive.py:159-202` 的 `extract_config()` 从顶层 `adaptive` 读取参数，因此嵌套的全局配置不会被使用。

影响：不同实验配置可能实际使用相同默认参数运行，Quality Lab 的 A/B 结果和 champion promotion 没有可信度。

修复要求：

1. `ExperimentRunner.submit()` 必须通过 `experiment_runs.config_id` 加载对应 `experiment_configs.config_json`。
2. 对配置进行 JSON/schema 校验后，生成不可变的最终配置快照；在顶层保留 pipeline 期望的 `adaptive`、模型配置等字段。
3. `_experiment` 元数据和单视频 `video_paths` 可合并到快照，但不得覆盖实验配置中的业务参数。
4. Task API 应定义唯一配置结构；避免一部分参数在顶层、一部分参数藏在 `config_snapshot`。
5. 将最终配置快照的哈希写入任务和 Artifact provenance，确保实验可复现。

验收测试：创建两套 `adaptive.max_output` 明显不同的实验配置，分别提交同一 benchmark item，断言两个 Job 的最终 config_json 和 config hash 不同，Stage adapter 读取到各自的参数；每个 Job 仍只包含指定的单个视频。

## P1：提交前必须修复

### 5. gif_clip 失败可被 materialize 成功掩盖，最终视频被误标为 succeeded

证据：

- `app/task_engine/orchestrator.py:327-367` 把 failed、cancelled、needs_attention 都视为可创建 materialize 的 terminal 状态。
- `app/task_engine/orchestrator.py:374-396` 只查看最新 Stage；只要最新 materialize succeeded 且没有非终态 Stage，就把视频标记 succeeded，没有检查更早的 clip 失败。

影响：部分 GIF 失败的视频会显示完全成功，失败视频重跑、状态栏统计和 Quality Lab 指标都会失真。

修复要求：

1. 明确定义视频聚合状态优先级，例如：`needs_attention/failed > cancelled > running > succeeded`。
2. materialize 可以在部分成功时执行，但最终视频必须保留 `partial_failed` 或 `needs_attention` 语义；若当前 schema 不支持 `partial_failed`，至少不能标记为 succeeded。
3. materialize 的输入必须包含成功 clip 列表和失败 clip 列表，输出结果也要记录二者。
4. Job 聚合和 Quality Lab 指标必须使用相同状态语义。

验收测试：两个 gif_clip 中一个 succeeded、一个 needs_attention/failed，materialize 即使成功，视频和 Job 也不得 succeeded；单独重试失败 clip 成功后，才允许聚合为 succeeded，且已成功 GIF 不重新生成。

### 6. 新增 Stage 测试包含无断言和 `pass`，806 passed 不能证明功能完成

证据：

- `tests/task_engine/test_stage_pipeline.py:197-205` 明确说明没有断言 gif_clip fan-out。
- `tests/task_engine/test_stage_pipeline.py:216-229` materialize 测试没有任何结果断言。
- `tests/task_engine/test_stage_pipeline.py:239-266` 的失败隔离、崩溃恢复、并发去重测试均为 `pass`。

修复要求：

1. 删除所有占位说明和空测试，改成真实数据库、真实临时文件、真实哈希和多连接并发断言。
2. 测试必须先能在旧错误实现上失败，再在修复后转绿；禁止只断言“到达过 rank_dedup”。
3. 至少增加一个完整 Worker 链集成测试：每个 Stage 使用不同目录，外部 ffmpeg/ffprobe/VLM 用可控 fake，验证准确的 Stage 序列、Artifact 数量、clip fan-out、最终状态和重试行为。
4. 增加“成功 clip 不重跑、仅重试失败 clip”的哈希/mtime 断言。

### 7. 同目录 scoped Job 去重只比较最早一个 Active Job

证据：

- `app/task_engine/repository.py:88-106` 只取 `_find_active_job_id()` 返回的一条 Job 与新 scope 比较。
- `app/task_engine/repository.py:408-417` 查询带 `LIMIT 1`。

影响：同目录先存在 scope B，再存在 scope A 后，再次添加 scope A 时只要最早记录是 B，就可能被误认为不同 scope 而允许重复 Job。

修复要求：

1. 规范化 `video_paths`（绝对路径、大小写、分隔符、排序、去重），计算稳定的 `scope_key`。
2. 重复定义应至少包含 `directory_key + scope_key + active status`；不要逐条只比较第一条记录。
3. 最好把 scope_key 持久化并用数据库约束/事务防止并发重复；如果 SQLite 无法直接表达 active-only 唯一约束，则在 `BEGIN IMMEDIATE` 中查询所有 active scopes 并精确比较。
4. 空 video_paths 表示整个目录时，应与任何同目录 active scoped Job 的冲突规则写清楚并测试。

验收测试：按 B、A、A 的顺序创建同目录 scoped Job，第三次必须返回第二个 A 的 existing_job_id；并发创建两个相同 scope 只能成功一个。

### 8. Heartbeat 可能续租到错误的数据库

证据：

- `scripts/task_worker.py:98` 支持 `connect_task_db(args.db)` 使用自定义 DB。
- `app/task_engine/worker.py:456-469` 的心跳线程调用无参数 `connect_task_db()`，会连接默认 DB，并把 lease 固定延长 90 秒。

影响：使用 `--db` 的 Worker 主线程和心跳线程操作不同数据库，真实 lease 不续期，Stage 可能被另一个 Worker 重复领取；同时 heartbeat 没有沿用 CLI/config 的 lease_seconds。

修复要求：

1. TaskWorker 初始化时接收 connection factory 或明确的 DB path，心跳使用完全相同的数据库。
2. 心跳续租时使用 Worker 配置的 lease_seconds，不要硬编码 90 秒。
3. 续租 UPDATE 必须检查受影响行数；lease_owner 不匹配或 Stage 已终态时停止心跳并令当前执行路径安全退出。

验收测试：使用非默认临时 DB 和较短 lease，运行耗时超过一个 lease 周期的 fake Stage；断言自定义 DB 中 lease 持续前移、默认 DB 未被创建/修改，第二个 Worker 无法重复领取。

## 建议实施顺序

1. 先定义 Artifact/Manifest schema、输入解析协议与最终配置快照结构。
2. 实现 Worker 的 Artifact 校验、原子入库、恢复幂等和同 DB heartbeat。
3. 改造所有 Stage 通过 Artifact resolver 读取上游输入，移除当前目录猜测和静默空值回退。
4. 合并 rank_dedup fan-out 为单一事务入口，正确处理 0/1/N clips。
5. 修复 gif_clip/materialize/video/job 状态聚合和失败 clip 单独重试。
6. 修复 Quality Lab 配置加载、单视频 scope 与配置哈希。
7. 修复 scope_key 去重及并发约束。
8. 最后把所有 RED/占位测试改成真实断言，并增加完整 Worker 链集成测试。

## 最终验收门槛

Agent 完成后必须同时提供以下证据：

1. `python -m compileall app scripts tests` 通过。
2. `pytest tests -q` 全部通过，且 `tests/task_engine/test_stage_pipeline.py` 不再包含 `pass`、空测试或“以后再断言”的注释。
3. 一次真实的独立目录 Stage 链集成测试通过，数据库中每个 Stage 都有预期 Artifact，所有 SHA-256 可重新计算验证。
4. 0、1、N clips，以及单 clip 失败后重试，均有自动化测试。
5. Quality Lab 两套不同配置确实传到 Stage，并具有不同 config hash。
6. 同目录 B、A、A scope 重复测试和双连接并发测试通过。
7. 自定义 DB heartbeat 测试通过。
8. `git diff --check` 通过。
9. 输出本次改动文件清单和测试结果；不要仅报告测试数量，必须列出新增关键测试名称及其验证内容。

## 数据与发布约束

- 不删除或重建现有 `data/`、历史任务数据库、Quality Lab 数据、导出 GIF、标签或偏好记忆。
- 测试只使用临时目录和临时数据库。
- 未收到明确指令前，不提交、不构建 EXE、不推送远程。
