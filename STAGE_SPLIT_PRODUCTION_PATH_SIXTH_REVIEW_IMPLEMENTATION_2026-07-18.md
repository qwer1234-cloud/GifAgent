# Stage Split 生产链路第六次 Review 实施文档（2026-07-18）

## 1. 当前结论

第五次修复报告中的基线已经复核：

```text
916 passed, 2 skipped, 3 warnings
compileall exit 0
git diff --check exit 0
```

以下项目已经有效落入生产代码：

- `StageResult.outcome` 使用受约束契约；Adapter、正常提交和恢复提交都会校验。
- `.stage_result.json` 保存 outcome，Worker 恢复不会再把 `needs_attention` 改成 `succeeded`。
- materialize resolver 会扫描全部 gif_clip Stage，拒绝 pending、leased、running 和 retry_wait。
- pytest 已注册 `slow` marker。

但当前仍有 1 个 P0 和 3 个 P1 问题，完整八阶段生产子进程 E2E（§9.2C）也尚未实现。修复这些问题并让 §9.2C 通过之前，不得构建发布版 EXE，也不得正式重跑历史队列。

## 2. 安全边界

1. 不删除、重建、清空或覆盖 `data/task_state.db`、`data/quality_lab.db`、历史 GIF、PBF、result JSON、标签、Review 和 Preference Memory 数据。
2. 所有测试使用 `tmp_path` 下的数据库、工作目录、导出目录和临时短视频。
3. 测试不得访问、启动、停止或修改用户真实的 Ollama、WSL、云端 LLM/VLM 服务。
4. 不使用 `--ignore`、`-k` 排除、删除测试、修改测试发现规则或降低断言来制造绿色结果。
5. 不以函数级 stub 测试代替真实 `TaskWorker + AdaptivePipelineAdapter + subprocess` 发布门禁。

## 3. Phase 0：先增加 RED 测试

修复实现前先增加以下失败测试。修复报告必须给出 RED 原因和 GREEN 结果。

### 3.1 VLM Provider 与失败语义

建议新增到 `tests/task_engine/test_vlm_stage_runtime.py`：

```text
test_vlm_rejects_unsupported_provider_before_sending_requests
test_vlm_all_transport_failures_fail_stage_instead_of_zero_clip
test_vlm_invalid_payload_never_becomes_default_score
test_vlm_valid_low_scores_are_a_legitimate_zero_result
test_vlm_partial_failures_are_counted_in_manifest_metrics
```

必须区分以下情况：

- HTTP/协议/解析全部失败：Stage 失败并进入 Worker 重试，不能输出合法 zero-clip。
- VLM 成功返回且所有有效分数低于阈值：允许输出 zero-clip。
- 部分帧成功、部分帧失败：Manifest 明确记录失败数；不得把失败帧按默认 `0.5` 分处理。
- `provider=openai_compatible` 但没有 OpenAI Vision 协议实现：在发送请求前明确拒绝，不能继续调用 Ollama `/api/generate`。

### 3.2 Refine Endpoint

```text
test_refine_uses_frozen_job_vlm_base_url
test_refine_never_falls_back_to_global_ollama_base_in_stage_mode
```

测试必须真的生成至少一个 refine frame，并断言所有 refine 请求到达 Job config 指定的 stub；仅测试 `refine_ts=[]` 不算覆盖。

### 3.3 Zero-clip Artifact 完整性

建议新增到 `tests/task_engine/test_materialize_resolver.py`：

```text
test_zero_clip_rejects_rank_manifest_sha_mismatch
test_zero_clip_rejects_rank_manifest_size_mismatch
test_zero_clip_rejects_unknown_rank_manifest_schema_version
test_zero_clip_rejects_rank_manifest_stage_mismatch
```

测试流程：先插入合法 succeeded rank_dedup Stage 与 Artifact，再修改磁盘 Manifest 内容但不更新数据库 SHA/size，断言 resolver 拒绝 zero-clip。

### 3.4 显式模型生命周期

```text
test_vlm_lifecycle_disabled_never_spawns_model_command
test_vlm_lifecycle_native_uses_native_ollama_command
test_vlm_lifecycle_wsl_uses_wsl_command_only_when_explicit
test_vlm_lifecycle_does_not_infer_mode_from_base_url
```

## 4. P0：修复 VLM Provider 协议和伪 zero-clip

### 4.1 根因

`scripts/test_video_adaptive.py::_stage_vlm()` 当前无论 `vlm.provider` 是什么，都执行：

```text
POST <base_url>/api/generate
读取 JSON.response
```

这是 Ollama 协议，不是 OpenAI-compatible Vision 协议。现有测试把 `provider=openai_compatible` 指向一个模拟 Ollama `/api/generate` 的 stub，因此测试证明的是“跳过 WSL”，没有证明 provider 协议正确。

同时，单帧请求连续失败三次后只打印 `FAILED`，Stage 仍写入 `scored_count=0` 的成功 Manifest。后续流水线会把服务中断误认为“视频没有值得输出的片段”，最终可能把 job 标成 succeeded。

此外，`parse_vlm_response()` 返回 `_parse_error` 时，当前逻辑仍通过 `safe_worth(..., default=0.5)` 生成默认分数。无效响应可能被当作真实 0.5 分并进入后续流水线。

### 4.2 推荐实施方案

本轮优先采用窄而明确的协议边界：

1. Stage Split 生产链路只支持 `vlm.provider=ollama`。
2. deterministic stub 也声明为 `provider=ollama`，但通过显式配置关闭模型生命周期。
3. 对 `openai`、`openai_compatible` 或未知 provider，在发出任何 HTTP 请求前抛出清晰的配置错误。
4. 未来若真正支持 OpenAI Vision，单独实现 provider client，不得复用 Ollama payload/response parser 假装兼容。

建议抽取共享边界：

```python
@dataclass(frozen=True)
class VlmRuntimeConfig:
    provider: Literal["ollama"]
    model: str
    base_url: str
    manage_lifecycle: bool
    launch_mode: Literal["none", "native", "wsl"]

@dataclass(frozen=True)
class VlmEvaluation:
    payload: dict | None
    success: bool
    error: str | None
```

再由一个共享 client 完成：

```text
score_frame(runtime, image_bytes, prompt, options)
```

`_stage_vlm()` 与 `_stage_refine()` 都必须使用该入口，避免 endpoint、payload、parser 和错误语义漂移。

### 4.3 计数和失败规则

VLM Manifest/metrics 至少记录：

```text
attempted_count
response_count
parsed_count
kept_count
failed_count
```

规则：

1. `validated_frames` 非空且 `parsed_count == 0`：抛出可重试错误，Stage 不得成功。
2. HTTP 成功但 JSON/质量门校验失败：计入 failed，不得生成默认 0.5 分。
3. `parsed_count > 0` 且 `kept_count == 0`：这是合法的“有效低分 zero result”。
4. 部分失败允许继续时，Manifest 必须保留失败计数和原因摘要；不得静默丢失。
5. 日志不得包含 API key、Authorization header 或完整敏感响应。

## 5. P1-1：Refine 必须使用冻结配置 endpoint

### 5.1 根因

`_stage_refine()` 已调用 `_resolve_vlm_config(config_data)` 得到 `vlm_model` 和 `vlm_base_url`，但真正请求仍使用模块级 `OLLAMA_BASE`。

这会导致：

- Job config 指向 deterministic stub 时，refine 仍访问用户真实 Ollama。
- 自定义端口或远端 Ollama 的 refine 阶段访问错误地址。
- 函数级 VLM 测试通过，但完整生产链路在 refine 阶段漂移。

### 5.2 实施要求

1. 不要只把 `OLLAMA_BASE` 文本替换成 `vlm_base_url` 后结束；优先让 VLM 和 refine 共用同一个 provider client。
2. Stage mode 禁止从模块级环境默认值回退；模型和 endpoint 必须来自冻结 Job config。
3. direct/legacy mode 如需保留默认值，必须与 Stage mode 分支明确隔离并有兼容测试。
4. refine 测试必须产生真实 refine frame，并验证请求 URL、model 和返回结果进入 refine manifest。

## 6. P1-2：Zero-clip 证明必须验证 Artifact 完整性

### 6.1 根因

`_assert_zero_clip_proven()` 当前从数据库读取 Artifact path 后直接打开 JSON，只检查 `clip_count` 和 `clips`。它没有验证：

- 文件 size 与数据库记录是否一致；
- 文件 SHA-256 与数据库记录是否一致；
- Artifact 的 stage_id、stage_name、kind 是否一致；
- Manifest schema_version 和 stage 是否受支持。

因此 succeeded 后被修改、损坏或错误迁移的 rank manifest 仍可能被当成可信 zero-clip 证据。

### 6.2 实施要求

1. 查询 rank Artifact 的完整字段并构造 `ArtifactRef`。
2. 调用现有 `validate_artifact_strict()` 验证存在性、size 和 SHA-256。
3. 调用 `validate_manifest_json(..., "rank_dedup_manifest", expected_stage="rank_dedup")` 验证 schema 和 stage。
4. 验证 Artifact 所属 succeeded Stage 的 stage_id、video_id 和 stage_name。
5. 最后才允许检查 `clip_count == 0` 且 `clips == []`。
6. 任一验证失败都阻止 materialize；不得回退成空成功。

## 7. P1-3：模型生命周期必须显式配置

### 7.1 根因

当前 `_should_manage_vlm_lifecycle()` 通过 provider 和 `http://127.0.0.1:11434` 推断是否管理模型，`stop_model()` 仍固定执行：

```text
wsl ollama stop <model>
```

这会把 endpoint 地址、部署位置和进程启动方式混为一体：Windows 原生 Ollama、自定义端口、`localhost`、容器和远端转发都无法准确表达。

### 7.2 推荐配置

```yaml
vlm:
  provider: ollama
  model: llava:13b
  base_url: http://127.0.0.1:11434
  manage_lifecycle: true
  launch_mode: wsl  # none | native | wsl
```

规则：

- `manage_lifecycle: false` 或 `launch_mode: none`：不得启动任何命令，不得 sleep 等待本机模型切换。
- `launch_mode: native`：执行本机 `ollama`，不得调用 WSL。
- `launch_mode: wsl`：只有显式配置时才执行 `wsl ollama`。
- 不根据 URL 猜测 launch mode。
- `wait_model()`、`stop_model()` 和 `/api/ps` 都接收解析后的 runtime/base_url，不得继续读取全局 `OLLAMA_BASE`。

## 8. §9.2C：完整八阶段生产子进程 E2E

以上 P0/P1 修复后，新增真正的发布门禁测试，建议放在：

```text
tests/task_engine/test_full_production_stage_chain.py
```

### 8.1 禁止的测试捷径

- 不得手工插入任何中间 Stage 或 Artifact。
- 不得使用 Fake Adapter。
- 不得直接调用 `_stage_vlm()`、`_stage_refine()` 代替 Worker 子进程。
- 不得让 VLM 全部返回低分，以 zero-clip 绕过 gif_clip/materialize。
- 不得因为短视频没有 refine frame 就声称覆盖 refine。
- 不得访问用户真实 Ollama、WSL、云端 LLM、仓库 `data/` 或正式导出目录。

### 8.2 必须运行的链路

```text
create Job
-> initialize_job
-> TaskWorker
-> AdaptivePipelineAdapter subprocess
-> discover
-> sample
-> vlm
-> refine
-> synthesize
-> rank_dedup
-> gif_clip fan-out
-> materialize
```

所有 8 类 Stage 必须由 Orchestrator 正常创建和推进。

### 8.3 确定性依赖

1. 使用 ffmpeg 生成足够长的临时短视频。
2. 启动本地 `_StubServer`，实现 Ollama `/api/generate` 协议。
3. Job config 使用：

   ```yaml
   vlm:
     provider: ollama
     model: deterministic-vlm
     base_url: http://127.0.0.1:<random-port>
     manage_lifecycle: false
     launch_mode: none
   ```

4. Stub 对 coarse frame 返回高分，确保进入 refine；对 refine frame 返回可预测结果。
5. LLM synthesis 必须使用明确的确定性注入或显式禁用模式，不能访问真实云端 API。仅依靠“缺少 API key 后失败且非致命”不算隔离证明。
6. Embedding dedup 测试配置应显式关闭，或提供确定性 embedding stub；不得访问真实 embedding 服务。

### 8.4 测试参数建议

为了保证 coarse sample 与 refine 都发生，临时视频和 adaptive 配置应满足：

```text
duration >= 8s
sample_interval = 2
max_duration = 1
refine_threshold = 0.6
refine_radius = 1
refine_interval = 1
worthiness_threshold = 0.5
merge_gap >= 2
max_output >= 1
embedding_dedup_enabled = false
```

Agent 应根据实际采样公式校验时间点，不能只复制这些值而不确认 `refine_frames > 0`。

### 8.5 必须断言

1. discover、sample、vlm、refine、synthesize、rank_dedup、gif_clip、materialize 均至少存在一条 Stage 记录。
2. 所有 Stage 均由真实 `AdaptivePipelineAdapter` 子进程运行并到达预期终态。
3. 每阶段输入来自数据库解析的前序 Artifact，不通过目录猜测。
4. `sample_frames > 0`、`vlm.parsed_count > 0`、`refine_frames > 0`。
5. Stub 同时收到 coarse VLM 和 refine VLM 请求；所有请求使用冻结 config 中的 model/base_url。
6. 至少产生一个真实 ffmpeg GIF，并成功发布到 `tmp_path` 正式目录。
7. result JSON 只引用已发布且 SHA 正确的 GIF。
8. PBF 经过 parser 验证 start/end，不只检查文件存在或非空。
9. materialize、video、job 最终均为 succeeded。
10. 每个 Artifact 的 stage_id、kind、size、SHA 与磁盘文件一致。
11. 没有 WSL/ollama 启停命令，没有真实网络服务访问，没有写入仓库 `data/`。

### 8.6 失败场景门禁

完整成功链路通过后，再补以下生产子进程场景：

```text
test_full_chain_vlm_outage_retries_instead_of_zero_success
test_full_chain_invalid_vlm_payload_never_exports_default_score_clip
test_full_chain_refine_uses_frozen_endpoint
test_full_chain_zero_clip_from_valid_low_scores
```

其中 outage 与 invalid payload 必须证明 job 不会伪成功。

## 9. 推荐实施顺序

1. 增加第 3 节 RED 测试。
2. 收紧 provider 支持范围，修复全失败和 parse error 的伪分数/伪成功。
3. 抽取共享 VLM client，让 VLM/refine 使用同一 endpoint 和错误语义。
4. 严格验证 zero-clip rank Artifact 与 Manifest。
5. 将生命周期改为显式配置，移除 URL 推断和默认 WSL 假设。
6. 实现 §9.2C 完整成功链路。
7. 增加 outage、invalid payload、valid-low-score 的完整链路场景。
8. 最后运行全量验证并提交修复报告。

## 10. 验证命令

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_materialize_resolver.py
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py -s
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

不得使用任何测试排除参数。完整链路测试必须包含在普通 `pytest -q` 的收集结果中。

## 11. 修复报告必须提供的证据

1. 每个新增 RED 测试修复前的失败原因和修复后的 GREEN 结果。
2. §9.2C 中 8 类 Stage 的 stage_id、status、attempt_count 摘要。
3. 每阶段 Artifact kind、stage_id、size、SHA 验证摘要。
4. Stub 收到的 coarse/refine 请求数量、model 和 endpoint 摘要。
5. `refine_frames > 0` 的明确断言输出。
6. 至少一个真实 GIF、解析后的 PBF start/end、result JSON 引用验证。
7. VLM outage/invalid payload 不会形成 zero-clip succeeded 的数据库证据。
8. 没有 WSL 命令、真实 Ollama/云端访问和仓库 `data/` 写入的隔离证据。
9. 无排除项的完整 pytest 输出、compileall 输出和 `git diff --check` 输出。

## 12. 发布门槛

只有同时满足以下条件，才允许构建发布版 EXE 或重跑历史队列：

1. 第 4 至第 7 节问题全部关闭。
2. §9.2C 使用真实 Worker、真实 Adapter 和真实子进程通过。
3. 测试明确覆盖 refine frame，而不是以空 refine 绕过。
4. VLM 服务中断和无效响应不能形成 job 伪成功。
5. zero-clip 的 rank Artifact 经过 SHA、size 和 schema 严格验证。
6. 全仓测试无排除项通过。
7. 构建和后续运行继续保留全部历史数据、导出、标签和 Preference Memory。
