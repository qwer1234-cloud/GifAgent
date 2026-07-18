# Stage Split 生产链路第三次 Review 实施报告（2026-07-18）

## 1. Review 结论

基于 `STAGE_SPLIT_PRODUCTION_PATH_SECOND_FIX_REPORT_2026-07-18.md` 和当前代码复审，现有测试全部通过，但生产链路仍有未解决问题：

- P0：3 项。
- P1：4 项。
- 当前验证基线：`357 passed`（task_engine + quality_lab），全仓 `848 passed, 2 skipped`。

在完成本文 P0 和真实 Worker 链路测试前，不应构建发布版 EXE，也不应重跑历史队列。

## 2. 复审证据

### 2.1 历史 v4 数据库仍可能启动失败

上一版 Schema 逻辑只记录 `SCHEMA_VERSION=4`，未必单独记录 v3。新代码看到缺少 migration 3 后会重新执行 v3，创建旧索引 `uq_artifact_stage_kind_clip`。

模拟历史数据库仅保留 migration 4，并插入两条 sample frame 后重启，结果：

```text
CONFIRMED LEGACY-V4 REOPEN FAILURE:
UNIQUE constraint failed: index 'uq_artifact_stage_kind_clip'
```

### 2.2 真实 Worker 的 materialize 输入不完整

实际 `_build_context()` 输出：

```text
MATERIALIZE INPUT KINDS: ['gif_file']
TERMINAL STATUSES: [{'clip_id': 'clip1', 'status': 'succeeded'}]
```

生产 Worker 没有传递 `gif_clip_manifest`。当前 E2E 测试是手工构造 `input_manifest.json`，因此没有覆盖真实 resolver。

### 2.3 正式输出会覆盖历史 GIF

创建同名历史 GIF 后执行 materialize，现有 `os.replace()` 会直接替换历史内容：

```text
原内容: GIF89a-HISTORY
发布后: GIF89a-NEW
```

### 2.4 配置合并仍会丢字段

基础配置：

```yaml
adaptive:
  sample_interval: 8
  max_output: 60
```

请求只覆盖 `sample_interval: 3` 后，最终 `max_output` 丢失；同时 `_experiment` 和 `config_hash` 被标准化逻辑丢弃。

## 3. 必须修复的问题

### P0-1：兼容旧版仅记录 v4 的数据库

涉及文件：

- `app/task_engine/schema.py`
- `tests/task_engine/test_schema_v4_reopen.py`

#### 根因

当前迁移器只根据 `task_migrations` 中是否存在版本号决定是否执行 DDL，没有识别“Schema 已经达到 v4，但缺少 v3 记录”的历史状态。

#### 实施要求

1. 新增 Schema 能力检测函数，至少检查：

   - `task_artifacts.stage_id` 是否存在。
   - `task_artifacts.artifact_kind` 是否存在。
   - `uq_artifact_stage_identity` 是否存在。
   - `uq_artifact_stage_kind_clip` 是否存在。

2. 迁移判定使用“migration 记录 + 实际 Schema”双重证据：

   - 有 v4 索引及 v3 列、但只有 migration 4：补记 migration 3，不执行 v3 DDL。
   - 有 v3 列和旧索引、没有 v4 索引：补记/确认 v3 后只执行 v4。
   - 全新数据库：按 v3 → v4 顺序执行。
   - Schema 与 migration 记录矛盾且无法安全推断：停止启动并输出结构化错误，不尝试破坏性修复。

3. 每个迁移在显式事务中完成：DDL 成功后才写 migration 记录；失败时回滚。
4. 禁止删除 task_artifacts 数据或通过重建空数据库解决。
5. 添加以下测试：

   - migration 表只有 4，Schema 实际为 v4，且有多帧 Artifact。
   - migration 表只有 3，Schema 实际为 v3。
   - migration 表为空但 v3 列已存在。
   - 连续重启三次保持幂等。
   - `PRAGMA integrity_check` 和 `foreign_key_check` 通过。

#### 验收标准

历史 v4 数据库包含多个 `sample_frames` 时，可以反复关闭和打开；Artifact 数量、路径、SHA-256 不变。

### P0-2：让真实 Worker 为 materialize 解析完整输入

涉及文件：

- `app/task_engine/artifacts.py`
- `app/task_engine/worker.py`
- `scripts/test_video_adaptive.py`
- `tests/task_engine/test_production_e2e.py`

#### 根因

`STAGE_INPUT_KINDS["materialize"]` 仍只有 `gif_file`。`resolve_all_gif_clip_artifacts()` 虽能查询两类 Artifact，但 Worker 没有调用它。

#### 实施要求

1. 不要简单给普通 resolver 增加 clip 过滤；materialize 需要聚合所有 clip，建立专用函数：

   ```python
   resolve_materialize_inputs(conn, video_id)
   ```

2. 返回内容必须包括：

   - 每个 succeeded gif_clip 的一个 `gif_file`。
   - 对应的一个 `gif_clip_manifest`。
   - 所有 gif_clip 的终态摘要。

3. 对每个 succeeded clip 校验：

   - 两种 Artifact 都存在且唯一。
   - stage_id 和 clip_id 一致。
   - size、SHA-256 校验通过。
   - manifest 中的 clip_id、gif_path、sha256 与 gif_file 一致。

4. materialize 的 Worker 分支调用专用 resolver；zero-clip 使用显式空输入。
5. 删除通过 config `_gif_clip_terminal_statuses` 私下传递状态的做法。使用版本化输入信封：

   ```json
   {
     "schema_version": 1,
     "stage": "materialize",
     "artifacts": {
       "gif_file": [],
       "gif_clip_manifest": []
     },
     "stage_statuses": []
   }
   ```

6. 修改 E2E：由 `TaskWorker._build_context()` 生成输入，禁止测试手工写 `input_manifest.json`。

#### 验收标准

真实 Worker 驱动的 `gif_clip -> materialize` 测试中，PBF 的 start/end、GIF 名称与 clip manifest 完全一致，不得出现全零时间。

### P0-3：正式输出不得覆盖历史文件

涉及文件：

- `scripts/test_video_adaptive.py`
- 可新增 `app/services/artifact_publish.py`
- `tests/task_engine/test_materialize_production.py`

#### 实施要求

1. 发布前检查正式目标是否存在：

   - 不存在：正常发布。
   - 已存在且 SHA-256 相同：幂等复用，不重复写入。
   - 已存在且 SHA-256 不同：不得覆盖；使用包含 clip_id/短 SHA 的稳定冲突名称，或将 Stage 标记 needs_attention。

2. 临时文件使用任务/Stage 唯一名称，例如：

   ```text
   .<filename>.<stage_id>.<uuid>.tmp
   ```

   禁止多个任务共享 `<filename>.tmp`。

3. 临时文件必须创建在正式目录同一卷，以保证 `os.replace()` 的原子语义。
4. materialize 失败时清理本次创建的临时文件，但不删除历史正式文件。
5. 正式 GIF 发布成功后，再生成 result JSON、PBF 和 materialize manifest。
6. 增加测试：

   - 同名同 SHA 幂等复用。
   - 同名不同 SHA 不覆盖。
   - 两个线程并发发布同名 GIF。
   - PBF/result 写入失败时历史文件保持不变。

#### 验收标准

现有导出目录中的任意不同内容文件都不会被静默替换；重复重试得到相同正式路径和内容。

### P1-1：配置必须先深度合并再标准化

涉及文件：

- `app/routers/tasks.py`
- `app/quality_lab/config_builder.py`
- `app/quality_lab/runner.py`
- `tests/task_engine/test_control_config_snapshot.py`
- `tests/quality_lab/test_isolation.py`

#### 根因

Router 先通过 `{**full_config, **body.config_json}` 浅层合并，局部 `adaptive` 会整体替换基础配置；`normalize_task_config()` 随后无法恢复被删除的字段。

#### 实施要求

1. 使用共享 `deep_merge(full_config, body.config_json or {})`。
2. 深度合并完成后再增加 `video_paths` 和 `_task` 元数据。
3. `normalize_task_config()` 必须保留：

   - `_experiment`
   - `config_hash`
   - `task_work_dir`
   - `export_base_dir`

4. 明确 `None` 的语义：若表示删除，必须由 deep_merge 一致处理；不得在 normalize 中静默忽略。
5. config_hash 由最终实际执行的业务配置计算，不能基于合并前配置。
6. 增加 API 级测试，不能只直接测试 normalize helper。

#### 验收标准

局部覆盖 `sample_interval` 后，`max_output`、模型配置和 Preference Memory 仍保持基础值；Quality Lab 的 run_id/item_id/config_hash 可从 Worker StageContext 中读取。

### P1-2：接入 Manifest Validator

涉及文件：

- `scripts/test_video_adaptive.py`
- `app/task_engine/artifacts.py`
- `tests/task_engine/test_manifest_validation.py`

#### 实施要求

`_read_upstream_manifest()` 必须调用 `validate_manifest_json()`，传入：

- artifact_kind。
- expected producer stage。
- expected clip_id（适用时）。
- 支持的 schema version。

增加错误用例：缺字段、错误 stage、错误 clip_id、未知版本、空 JSON、错误编码和 manifest/GIF SHA 不一致。异常必须形成结构化 StageError，不得吞掉后继续运行。

### P1-3：恢复 sample_frames 的显式输入依赖

涉及文件：

- `app/task_engine/artifacts.py`
- `scripts/test_video_adaptive.py`
- `tests/task_engine/test_stage_inputs.py`

#### 实施要求

1. 修改依赖：

   ```python
   "vlm": ("sample_manifest", "sample_frames")
   ```

2. sample manifest 保存 `artifact_id + timestamp`，不以裸路径作为唯一引用。
3. VLM 根据 artifact_id 将 manifest 记录与 resolver 返回的 frame 对应。
4. frame 缺失、SHA 错误、重复 artifact_id 或 manifest 引用未知 frame 时，VLM Stage 必须失败。

### P1-4：增加真正完整的生产 E2E

涉及文件：

- `tests/task_engine/test_production_e2e.py`
- 测试 fixture/本地假服务

#### 当前覆盖缺口

现有测试只覆盖 discover、Worker discover，以及手工输入的 materialize，不是完整链路。

#### 实施要求

至少增加：

1. Worker 驱动 `discover -> sample`，验证实际数据库 resolver。
2. Worker 驱动 `gif_clip -> materialize`，禁止手工构造 materialize 输入。
3. 完整八阶段 deterministic E2E：

   ```text
   discover -> sample -> vlm -> refine -> synthesize
   -> rank_dedup -> gif_clip fan-out -> materialize
   ```

4. 覆盖单个 gif_clip 失败并只重试该 clip、部分成功、zero-clip、Worker 重启恢复。
5. 所有测试使用 tmp_path DB/work/export，不访问用户视频、真实队列数据库或历史导出目录。

## 4. 推荐实施顺序

1. 先增加所有 RED 测试。
2. 修复 P0-1 Schema 历史兼容并单独提交。
3. 修复 P0-2 materialize resolver。
4. 修复 P0-3 防覆盖发布。
5. 修复配置深度合并与 Quality Lab provenance。
6. 接入 Manifest Validator 和 sample_frames 显式依赖。
7. 最后补齐完整生产 E2E。

建议提交边界：

```text
test: cover legacy v4 and materialize resolver gaps
fix: make task migration infer legacy schema safely
fix: resolve complete materialize input envelope
fix: prevent formal export overwrite and publish races
fix: deep merge task config and preserve experiment metadata
fix: validate manifests and resolve sample frame artifacts
test: exercise full production stage chain
```

## 5. 验证命令

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

正式输出验证：

1. 预放一个同名不同内容 GIF。
2. 执行 materialize。
3. 确认历史文件 SHA 未变化。
4. 确认新文件使用稳定冲突名称或任务进入 needs_attention。
5. 重试后不产生额外重复文件。

## 6. 完成标准

只有满足以下全部条件，才可声明本轮修复完成：

1. 仅记录 migration 4 的历史 v4 多帧数据库可以安全启动。
2. 真实 Worker materialize 输入同时包含 gif_file、gif_clip_manifest 和终态摘要。
3. 正式发布不覆盖任何同名不同内容历史文件。
4. Control 和 Quality Lab 局部覆盖不会删除基础配置字段。
5. `_experiment`、config_hash 和 provenance 在 StageContext 中保留。
6. 所有 Manifest 在消费前经过共享 Validator。
7. VLM 通过 resolver 消费并校验 sample_frames。
8. 完整八阶段生产 E2E 通过。
9. 全仓测试通过，且不访问真实外部服务或用户数据。
10. 在临时短视频 smoke test 中核对 DB、事件、状态、正式 GIF、PBF 和 result JSON。

修复报告必须逐项列出 RED/GREEN 测试名称、实际命令输出和生产链路证据；不能仅以测试总数证明问题已经解决。
