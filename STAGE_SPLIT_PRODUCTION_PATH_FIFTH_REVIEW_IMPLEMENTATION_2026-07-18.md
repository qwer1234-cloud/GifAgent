# Stage Split 生产链路第五次 Review 实施文档（2026-07-18）

## 1. Review 结论

第四次修复报告中的测试数量可复现：

```text
901 passed, 2 skipped, 15 warnings
```

P0-1、P0-2、P1-1、P1-2、P1-3 的主路径实现基本成立，但仍有 2 个 P0、2 个 P1 问题。尤其是完整八阶段生产子进程 E2E（原文 §9.2C）仍未完成，当前不得构建发布版 EXE，也不得正式重跑历史队列。

## 2. 安全边界

1. 不删除、重建或清空 `data/task_state.db`、`data/quality_lab.db`、历史 GIF、PBF、result JSON、标签和 Review 数据。
2. 所有新增测试使用 `tmp_path` 下的数据库、工作目录、导出目录和临时短视频。
3. 不访问或停止用户真实的 Ollama/WSL 模型；完整链路测试必须使用确定性本地 stub。
4. 不使用 `--ignore`、`-k` 排除、删除测试或降低断言来获得绿色结果。
5. 本轮只在完整八阶段 E2E 通过后，才允许构建 EXE 或重跑历史队列。

## 3. P0-1：崩溃恢复会丢失 `needs_attention` outcome

### 3.1 证据与根因

- `app/task_engine/worker.py::_save_result()` 只保存 output_key、artifacts 和 metrics，没有保存 `StageResult.outcome`。
- `app/task_engine/worker.py::_try_recover()` 调用 `complete_stage_with_artifacts()` 时没有传入 `needs_attention` 和 `attention_message`。
- 因此 materialize 已经产生不可恢复发布冲突、结果本应为 `needs_attention` 时，如果 Worker 在写入结果文件后、提交数据库前崩溃，下一次 claim 会把该 Stage 恢复成 `succeeded`，并可能进一步形成 video/job 伪成功。

### 3.2 先增加 RED 测试

新增真实恢复测试，例如：

```text
test_recovery_preserves_needs_attention_outcome
test_recovery_rejects_unknown_outcome
```

第一项必须模拟：

1. materialize 返回 `outcome="needs_attention"` 并产生有效 Artifact。
2. Worker 已写入 `stage_result.json`，但在数据库原子提交前崩溃。
3. Lease 过期后由新 Worker claim 同一 Stage，并走 `_try_recover()`。
4. 断言 Stage、video、job 最终均为 `needs_attention`；Artifact 仍只写入一次。

### 3.3 实施要求

1. `StageResult.outcome` 改为受约束类型或 Enum，只允许 `succeeded`、`needs_attention`。
2. `_save_result()` 持久化 outcome；必要时将 attention message 一并持久化，或由统一 helper 根据 metrics 生成。
3. `_try_recover()` 严格校验 outcome，并以与正常提交路径完全相同的参数调用 `complete_stage_with_artifacts()`。
4. 正常完成与恢复完成必须共用一个“提交 StageResult”helper，避免两套状态语义再次漂移。
5. 对旧版不含 outcome 的结果文件保持兼容：明确按 `succeeded` 处理，并增加兼容测试；未知值不得静默当作成功。

## 4. P0-2：VLM Stage 的生产子进程路径仍不可可靠执行

### 4.1 证据与根因

`scripts/test_video_adaptive.py::_stage_vlm()` 当前存在真实生产问题：

1. 使用 `LLM_MODEL`，但该名称只在 `run_pipeline()` 的局部作用域中定义；本地 LLM 配置下会触发 `NameError`。
2. VLM 模型仍硬编码为 `llava:13b`，没有消费 Job 的最终配置快照。
3. `stop_model()` 无条件执行 `wsl ollama stop ...`；Windows 原生 Ollama、无 WSL 环境和 deterministic stub 都会失败。
4. 模型生命周期、HTTP endpoint 和模型名没有形成可注入的 Stage 运行时配置，因此 §9.2C 无法在不触碰用户真实模型的前提下运行。

这些不是“仅测试环境问题”，而是完整生产 Stage 路径尚未闭环。

### 4.2 先增加 RED 测试

```text
test_vlm_stage_uses_frozen_job_model_config
test_vlm_stage_does_not_require_wsl_for_external_endpoint
test_vlm_stage_rejects_missing_model_config_cleanly
```

### 4.3 实施要求

1. 从传入的冻结 Job config 中解析 VLM model、base URL、provider 和模型生命周期策略，禁止在 Stage 内使用未定义全局变量或硬编码模型。
2. 抽取最小 `VlmRuntime`/`ModelLifecycle` 接口；生产可使用 Ollama 实现，E2E 使用本地 deterministic HTTP stub。
3. 仅在明确配置为本机 Ollama 且启用模型切换时执行 stop/start；外部 URL、云端 provider 和 stub 不得调用 `wsl` 或本机 `ollama`。
4. 如果仍需启动命令，平台选择必须显式且可配置；不要以 `wsl` 作为所有 Windows 环境的默认前提。
5. Stage 子进程必须只消费其 `config_json` 快照和显式环境变量，不读取会随进程变化的模块级配置缓存。

## 5. P1-1：Materialize zero-clip 判定仍有状态空洞

### 5.1 证据与根因

`resolve_materialize_inputs()` 的 `stage_rows` 查询只选择终态 gif_clip。随后以 `if not stage_rows` 判定“完全不存在 gif_clip”。当数据库中存在 pending、leased、running 或 retry_wait 的 gif_clip 时，该函数仍会错误返回 `zero_clip=True`。

正常 Orchestrator 理论上不会提前创建 materialize，但 resolver 自身文档声称它能证明“没有任何 gif_clip Stage”，当前查询并不能满足该不变量；并发、迁移数据或手工恢复时可产生伪 zero-clip 成功。

### 5.2 实施要求

1. 先查询该 video 的全部 gif_clip Stage。
2. 数量为 0 时，才允许进入 zero-clip 分支，并进一步验证 materialize 来自声明 `clip_count=0` 的有效 rank_dedup manifest/artifact。
3. 只要存在非终态 gif_clip，resolver 必须拒绝执行并给出包含 stage_id/status 的结构化错误。
4. 增加以下测试：

```text
test_materialize_rejects_pending_gif_clip_as_zero_clip
test_materialize_rejects_retry_wait_gif_clip_as_zero_clip
test_zero_clip_requires_rank_manifest_declaring_zero
```

## 6. P1-2：Outcome 契约允许未知字符串静默成功

当前 `StageResult.outcome` 是普通 `str`，Adapter 直接接受子进程 JSON 中的任意值，而 Worker 只对精确字符串 `needs_attention` 特判。拼写错误或未来未支持值会被当成 succeeded。

实施要求：

1. 在 Adapter 边界解析为 Enum/Literal，并拒绝未知值。
2. `stage_result.json` 恢复边界执行相同校验。
3. Repository 接收显式 outcome，而不是布尔猜测；若暂不调整 Repository，至少用一个共享转换函数保证正常和恢复路径一致。
4. 新增 Adapter 与恢复路径的非法 outcome 测试。

## 7. P1-3：注册 pytest 的 `slow` marker

完整测试虽通过，但产生 13 条 `PytestUnknownMarkWarning`（其中生产 E2E 大量使用 `@pytest.mark.slow`）。在 `pyproject.toml` 注册 marker，并在 CI 中保留一个“包含 slow”的发布门禁命令，避免未来 runner 将未知 marker 升级为错误或误排除生产测试。

## 8. 必须补齐的 §9.2C 完整生产 E2E

新增一个不手工预置中间 Stage/Artifact 的测试：

```text
POST/create Job
-> initialize
-> real TaskWorker
-> real AdaptivePipelineAdapter subprocess
-> discover
-> sample
-> vlm (deterministic local HTTP stub)
-> refine
-> synthesize (deterministic dependency injection)
-> rank_dedup
-> gif_clip fan-out
-> materialize
```

验收断言：

1. 八类 Stage 均实际被 claim，stage_id、attempt_count 和事件记录完整。
2. 每一阶段只消费数据库解析出的前序 Artifact，不猜测工作目录。
3. 至少产生一个真实 ffmpeg GIF，并发布到 `tmp_path` 正式目录。
4. PBF 经过解析验证 start/end；result JSON 只引用已发布文件。
5. 最终 Stage、video、job 均为 succeeded。
6. 测试期间没有访问用户真实 Ollama、没有执行 WSL 命令、没有修改仓库 `data/`。
7. 再补一条 zero-clip 完整链路；单 clip retry、部分失败和重启恢复可保留为独立生产 E2E。

## 9. 推荐实施顺序

1. 增加 outcome 恢复 RED 测试，统一正常/恢复提交语义。
2. 严格化 outcome 契约。
3. 修复 zero-clip 全量 Stage 判定。
4. 抽取 VLM runtime/model lifecycle，消除未定义变量和硬编码 WSL。
5. 完成 §9.2C 全链路 deterministic stub。
6. 注册 slow marker，运行全量验证。

## 10. 验证命令与发布门槛

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

最终修复报告必须提供：

- 新增 RED 测试修复前的失败原因与修复后的 GREEN 结果。
- 完整八阶段测试中每个 Stage 的数据库状态和 Artifact 类型摘要。
- deterministic VLM/LLM stub 的隔离证据，证明未访问真实 Ollama/WSL。
- Worker 崩溃恢复后 `needs_attention` 未丢失的数据库断言。
- 无任何测试排除项的全仓结果。

只有上述问题全部关闭并且 §9.2C 通过后，才允许构建发布版 EXE 或执行历史队列正式重跑。
