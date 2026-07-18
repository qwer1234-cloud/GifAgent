# Stage Split 生产链路复审与实施计划（2026-07-18）

## 1. 复审结论

截图中的 `820 passed, 2 skipped` 证明新增测试本身通过，但**尚不能证明真实生产 Stage Split 链路可用**。当前实现仍存在 4 个阻断级问题：

1. 生产 `AdaptivePipelineAdapter` 没有生成新协议要求的 `stage_id`、`artifact_kind` 和稳定 `artifact_id`。
2. Worker 已停止传递 `prior_stage_work_dirs`，但生产阶段脚本仍只会通过该旧字段寻找上游输入，并不会消费 `StageContext.inputs`。
3. Artifact 唯一索引不允许 sample 阶段登记多张采样帧。
4. 生产 materialize 仍只扫描自己的隔离工作目录，无法看到 sibling `gif_clip` 的产物。

因此本次复审结论为：**不建议合并、构建 EXE 或重跑真实队列；需先完成本文 P0 项并通过真实适配器端到端测试。**

## 2. 已核验证据

### 2.1 测试结果

- `python -m pytest -q tests/task_engine tests/quality_lab`：`329 passed in 23.24s`。
- `python -m pytest -q`：无法复现截图中的全仓通过。pytest 会收集 `scripts/test_video_rag.py` 和 `scripts/test_video_rag_v2.py`，在收集阶段真实调用 Ollama/WSL，分别出现连接拒绝和 30 秒超时。
- 新增 `tests/task_engine/test_e2e.py` 使用自定义 Fake Adapter 构造正确的 artifact，并未运行 `AdaptivePipelineAdapter`、`scripts/test_video_adaptive.py --stage`、真实输入序列化或真实 materialize。

### 2.2 最小复现

- 同一 `sample` stage 插入两条不同路径、相同 `artifact_kind=sample_frames` 的 artifact，第二条触发：

  ```text
  UNIQUE constraint failed: index 'uq_artifact_stage_kind_clip'
  ```

- 按生产适配器当前构造方式创建 `ArtifactRef`，结果为：

  ```text
  stage_id = ''
  artifact_kind = 'generic'
  ```

## 3. Review Findings

### P0-1 生产 Adapter 未实现 Artifact 数据协议

涉及文件：

- `app/task_engine/adaptive_adapter.py:94-118`
- `scripts/test_video_adaptive.py:1186-1219`
- `app/task_engine/repository.py:382-407`

现状：阶段脚本只回传 `path` 和 `size_bytes`。Adapter 使用 `discover_0` 这类位置型默认 ID，且未设置 `stage_id`、`artifact_kind`。Repository 只验证 job/video/stage_name/clip，没有验证 `ref.stage_id == stage_id`，所以错误数据会成功入库。后续 resolver 查询 `discover_manifest`、`sample_manifest` 等类型时必然找不到生产产物。

实施要求：

1. 为每个阶段定义明确的输出 kind 映射；不得靠文件扩展名模糊猜测。
2. 阶段脚本在 result JSON 中输出 `artifact_kind`、`clip_id` 和相对/绝对路径。
3. Adapter 使用 `context.stage_id` 和 `make_artifact_id(...)` 构造 ArtifactRef。
4. `complete_stage_with_artifacts()` 强制校验 `stage_id`、允许的 `artifact_kind`、stage/clip 归属；空 `stage_id` 和 `generic` 必须拒绝用于新 Stage Split 产物。
5. 增加真实 `AdaptivePipelineAdapter` 合同测试，断言数据库中不存在空 `stage_id` 或 `generic` 新记录。

### P0-2 新输入链路没有接入生产阶段脚本

涉及文件：

- `app/task_engine/worker.py:287-350`
- `app/task_engine/adaptive_adapter.py:142-157`
- `scripts/test_video_adaptive.py:1152-1178`
- `scripts/test_video_adaptive.py:1281-1311`

现状：Worker 注释明确要求下游读取 `StageContext.inputs`，但 Adapter 只写 config snapshot，没有序列化 inputs。脚本仍读取 `config_data["prior_stage_work_dirs"]` 并通过目录猜测 manifest。Worker 又把 resolver 的 `FileNotFoundError`/`ValueError` 吞掉并设为 `inputs=None`，使缺失或损坏依赖被伪装成“resolver 不适用”。

实施要求：

1. 定义版本化 `stage_inputs.json`，至少包含 artifact_id、stage_id、artifact_kind、clip_id、path、sha256、size_bytes。
2. Adapter 将 `context.inputs` 原子写入 stage 工作目录，并通过明确 CLI 参数传给脚本。
3. 每个生产 stage 只从输入清单读取上游 artifact，彻底删除 `prior_stage_work_dirs` 和 `_load_input_manifest()` 的目录猜测路径。
4. 除 discover 和合法 zero-clip materialize 外，resolver 缺失/校验失败必须让 stage 失败或进入 `needs_attention`，不得回退到 `None`。
5. 输入清单中的每个文件在使用前校验 size 和 SHA-256。

### P0-3 Artifact 唯一索引阻止多帧产物

涉及文件：

- `app/task_engine/schema.py:136-166`
- `app/task_engine/artifacts.py:178-186`

现状：唯一索引是 `(stage_id, artifact_kind, COALESCE(clip_id, ''))`。sample 正常会生成多张 `sample_frames`，因此第二张帧必然冲突。当前 Fake E2E 每个 kind 只生成一项，未覆盖该情况。

实施要求：

1. 重新定义逻辑身份，例如 `(stage_id, artifact_kind, clip_id, normalized_path)`，或增加显式 `artifact_key/item_key`。
2. 新迁移必须先删除旧索引，再创建新索引，并兼容已有数据库。
3. 增加“同一 sample stage 至少 3 张 frame 可原子提交”的迁移测试和 repository 测试。
4. 保留同一逻辑 artifact 重放幂等、内容变化发生 collision 的语义。

### P0-4 真实 materialize 无法聚合 sibling GIF

涉及文件：

- `scripts/test_video_adaptive.py:2001-2065`
- `app/task_engine/artifacts.py:296-328`

现状：materialize 只执行 `os.listdir(work_dir)` 查找 `gif_clip_*.json`。每个 gif_clip 有独立 work_dir，所以 materialize 自己的目录通常为空，最终会错误输出 `gif_count=0`。它也没有按正式 export 配置复制/链接 GIF。

实施要求：

1. materialize 只消费 resolver 提供的所有成功 `gif_file`/`gif_clip_manifest`，不扫描 sibling 目录。
2. 对每个 clip 校验 manifest 与 GIF 的 clip_id、SHA-256、文件大小一致。
3. 将成功 GIF 复制或原子移动到正式输出目录，再生成 PBF、result JSON、materialize manifest。
4. 明确定义部分失败策略：成功 clip 可发布，但 video/job 必须聚合为 `needs_attention`，结果 JSON 要列出失败 clip。
5. zero-clip 使用显式空输入语义，不得靠 Worker 吞掉 resolver 错误实现。

### P1-1 Resolver 会读取失败或历史 Stage 的陈旧 Artifact

涉及文件：

- `app/task_engine/artifacts.py:202-228`
- `app/task_engine/artifacts.py:296-328`

现状：查询只过滤 video/stage_name/kind/clip，没有 join `task_stages`，也不要求生产 stage 为 `succeeded`。重试、历史失败或错误归属的 artifact 都可能被返回；materialize 的批量查询同样如此。

实施要求：查询必须通过 `task_artifacts.stage_id = task_stages.stage_id` 关联，并限制生产 stage 状态为 `succeeded`；明确重试 attempt 的 artifact 选择规则。materialize 只能聚合终态成功的 gif_clip。

### P1-2 Manifest 校验器与真实 Manifest 字段不一致

涉及文件：

- `app/task_engine/artifacts.py:335-360`
- `scripts/test_video_adaptive.py:1436-1444`
- `scripts/test_video_adaptive.py:1522-1537`
- `scripts/test_video_adaptive.py:1639-1646`

现状：校验器要求 sample 的 `sample_points`、vlm 的 `scores`、refine 的 `refined_regions`；真实脚本分别输出 `frame_paths/timestamps`、`frames`、`frames/refine_regions`。一旦把 manifest 校验真正接入 resolver，合法生产产物会被拒绝。

实施要求：先确定唯一 schema，再让 producer、validator、consumer 共享同一版本；为每种 manifest 加正例、缺字段、错误 stage、错误 clip_id 和 schema 版本测试。

### P1-3 rank_dedup 无效 Manifest 被吞掉后可永久 idle

涉及文件：

- `app/task_engine/orchestrator.py:253-269`

现状：rank_dedup 已 succeeded 后，`_ensure_gif_clip_stages()` 的 `ValueError` 被直接 `pass`。若 artifact 永久缺失或损坏，后续不会产生 gif_clip/materialize，也不会可靠进入 `needs_attention`。

实施要求：区分“事务尚未可见”的短暂情况和“已成功 Stage 的产物无效”。后者写结构化错误事件并把 video/job 聚合为 `needs_attention`；增加损坏 rank manifest 的终态测试。

### P1-4 Lease/Heartbeat 尚未完整接入生产入口

涉及文件：

- `app/task_engine/worker.py:594-652`
- `scripts/task_worker.py:54-107`

现状：后台 heartbeat 检测 `rowcount == 0` 后只停止线程，主线程不知道 lease 已丢失，仍可能继续保存结果并尝试完成 Stage。生产 CLI 也没有 lease/heartbeat 参数或配置文件读取，截图所称的独立配置没有从实际入口接通。

实施要求：

1. 增加线程安全的 `lease_lost` 状态并在提交前检查。
2. 丢失 lease 后不得写入/完成/fail 不再归本 Worker 所有的 Stage。
3. 校验 `0 < heartbeat_seconds < lease_seconds`。
4. CLI 增加配置与覆盖参数，并将 db_path、lease、heartbeat 显式传入 TaskWorker。
5. 增加执行中被另一 Worker 接管、heartbeat DB 异常、非法配置三类测试。

### P1-5 当前“端到端测试”绕过了生产路径

涉及文件：

- `tests/task_engine/test_e2e.py:47-195`
- `tests/task_engine/test_e2e.py:309-328`

现状：测试 Adapter 自己生成符合期望的 artifact，并以 Fake materialize 完成流程，因此无法发现 P0-1 至 P0-4。

实施要求：保留 Fake E2E 作为调度器单元测试，另增一组 production-path E2E：使用真实 `AdaptivePipelineAdapter` 和 stage CLI；外部 ffprobe/ffmpeg/VLM 可在进程边界替换为确定性 fixture，但 result JSON、输入清单、Artifact 入库、resolver、真实 materialize 必须走生产实现。

### P1-6 全仓 pytest 会在收集期执行外部副作用

涉及文件：

- `scripts/test_video_rag.py`
- `scripts/test_video_rag_v2.py`
- pytest 配置文件（需新增或调整）

现状：脚本文件名满足 pytest 默认收集规则，导入即抽帧、调用 Ollama/WSL，并修改数据文件。它使“全量测试通过”依赖本机服务状态且不可复现。

实施要求：将可执行逻辑放进 `main()` 并使用 `if __name__ == "__main__"`；配置 pytest 仅收集 `tests/`；需要保留的外部集成测试使用显式 marker 并默认跳过。

## 4. 推荐实施顺序

### Phase 0：先补 RED 测试

1. 真实 Adapter 输出协议测试。
2. sample 多帧原子提交测试。
3. discover → sample 的真实输入清单测试。
4. 两个 sibling gif_clip → 真实 materialize 测试。
5. 陈旧/失败 Stage artifact 不可解析测试。

### Phase 1：修复 Artifact 身份与数据库约束

完成 P0-1、P0-3、P1-1；先迁移数据库，再修改 repository 和 adapter，确保新旧数据库都能启动。

### Phase 2：接通生产输入协议

完成 P0-2、P1-2；逐阶段替换目录猜测，并用 discover → rank_dedup 的真实链路测试验证。

### Phase 3：修复 fan-out 与 materialize

完成 P0-4、P1-3；覆盖 zero-clip、全成功、部分失败、全部失败和单 clip 重试。

### Phase 4：完善租约和测试入口

完成 P1-4、P1-5、P1-6；最后再执行完整验证矩阵。

## 5. 完成标准

只有同时满足以下条件，才能把 Stage Split 标记为完成：

1. 生产数据库新增 artifact 均有非空 stage_id 和明确 artifact_kind。
2. sample 阶段可登记任意数量采样帧。
3. 生产代码中不再使用 `prior_stage_work_dirs` 或上游目录猜测。
4. 真实 materialize 能从隔离的 sibling gif_clip 聚合 GIF，并写入正式输出目录。
5. resolver 不读取失败、取消、旧 attempt 的 artifact。
6. 缺失/损坏依赖会形成确定的失败或 `needs_attention`，不会 idle。
7. Fake 调度 E2E 与 production-path E2E 均通过。
8. `python -m pytest -q` 不触发外部服务和数据修改，并得到可重复的测试总数。
9. 在临时目录用一个短视频跑完真实 8 阶段，核对 task_stages、task_artifacts、最终 GIF/PBF/result JSON 和状态聚合。

## 6. 建议验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app scripts
```

最后一项真实短视频 smoke test 应使用全新临时 task DB 和 export 目录，避免历史数据掩盖结果；不得用 Fake Adapter 代替生产 Adapter。
