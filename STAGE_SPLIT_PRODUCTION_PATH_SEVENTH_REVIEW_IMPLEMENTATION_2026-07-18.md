# Stage Split 第七次 Review 修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭第六次修复后仍存在的 2 个 P0 和生产门禁缺口，使合法 zero-clip、无效 VLM 响应、refine 提取失败、显式模型生命周期以及完整八阶段生产子进程链路都具有确定且可验证的行为。

**Architecture:** 保留现有八阶段 Task Engine、Artifact 协议和 `AdaptivePipelineAdapter` 子进程边界。VLM 粗筛与 refine 继续共用 `_score_vlm_frame()`，但成功响应必须携带有效评分；模型生命周期统一解析为显式 runtime 配置并传入启停函数；完整 E2E 继续使用真实 Worker、真实 Adapter、真实 ffmpeg 和本地 HTTP stub，不使用真实 Ollama、WSL 或云端服务。

**Tech Stack:** Python 3.14、pytest、SQLite、FastAPI Task Engine、httpx、ffmpeg/ffprobe、Pillow、PotPlayer PBF、PowerShell。

## Global Constraints

1. 不删除、重建、清空或覆盖 `data/task_state.db`、`data/quality_lab.db`、`data/library.db`、历史 GIF/PBF/result JSON、标签、Review 数据和 Preference Memory。
2. 测试只能读写 `tmp_path`；不得写入仓库 `data/`、正式导出目录或用户视频目录。
3. 测试不得访问、启动、停止或修改真实 Ollama、WSL、DeepSeek、云端 LLM/VLM 或 embedding 服务。
4. 不得以 Fake Adapter、手工插入中间 Stage/Artifact 或直接调用阶段函数替代完整生产门禁。
5. 所有新增行为先写 RED 测试，确认失败原因正确后再实现最小修复。
6. 使用项目解释器执行命令：`.\.venv\Scripts\python.exe`，不要使用系统 Python 3.9。
7. 在本计划全部通过前，不构建发布 EXE，不重跑历史队列。

---

## 文件职责与改动边界

- Modify: `scripts/test_video_adaptive.py`
  - 严格 VLM 评分成功条件。
  - 初始化并记录 refine 请求、提取、评分和失败计数。
  - 将 VLM 生命周期解析为显式 runtime，不再根据 URL 推断。
  - 让 `stop_model()`、`wait_model()` 使用冻结配置的 `launch_mode/base_url`。
- Modify: `tests/task_engine/test_vlm_stage_runtime.py`
  - 覆盖无效 payload、合法低分、refine 空集合、提取失败和生命周期命令选择。
- Modify: `tests/task_engine/test_full_production_stage_chain.py`
  - 收紧成功链路断言。
  - 增加 outage、invalid payload、valid-low-score 三条真实 Worker/Adapter/子进程链路。
- Modify: `configs/models.yaml`
  - 为新任务提供显式生命周期默认配置。
- Modify: `README.md`
  - 记录 `manage_lifecycle`、`launch_mode` 语义和发布前验证命令。
- Modify: `Agent.md`
  - 记录八阶段生产门禁、历史数据保护和禁止伪 zero-clip 的约束。
- Optional Modify: `app/ui/tabs/settings.py`
  - 仅当配置 UI 会丢弃或无法编辑生命周期字段时增加字段；不得顺带重构 Settings 页面。

---

### Task 1: 为两个 P0 建立 RED 单元测试

**Files:**
- Modify: `tests/task_engine/test_vlm_stage_runtime.py`
- Test: `tests/task_engine/test_vlm_stage_runtime.py`

**Interfaces:**
- Consumes: `_score_vlm_frame(...) -> tuple[dict | None, str | None]`、`_stage_refine(...) -> dict`。
- Produces: 严格评分与合法 zero-clip 修复必须满足的回归测试。

- [ ] **Step 1: 增加缺失评分字段的失败测试**

使用现有 `_StubServer`，增加返回 `{}` 的场景；不得把函数本身 monkeypatch 掉：

```python
def test_score_vlm_frame_rejects_missing_worthiness(tmp_path):
    mod = _load_stage_module()
    stub = _StubServer({"response": "{}"})
    stub.start()
    try:
        payload, error = mod._score_vlm_frame(
            base_url=stub.base_url,
            model="stub-vlm",
            image_bytes=b"jpeg",
            prompt="score",
            options={},
            threshold=0.55,
            timestamp=1.0,
            frame_path=str(tmp_path / "frame.jpg"),
        )
        assert payload is None
        assert "gif_worthiness" in error
    finally:
        stub.stop()
```

- [ ] **Step 2: 增加非有限值和越界值测试**

```python
@pytest.mark.parametrize("value", [None, True, -0.1, 1.1, "nan"])
def test_score_vlm_frame_rejects_invalid_worthiness(tmp_path, value):
    # Stub 返回包含 value 的 JSON；期望 payload is None，error 指明评分无效。
```

测试必须确认这些响应不会被计为 0.5 分成功响应。

- [ ] **Step 3: 增加 refine 无高分帧的合法成功测试**

构造经过 `validate_manifest_json()` 的 `discover_manifest` 与空 `vlm_manifest`，调用真实 `_stage_refine()`：

```python
result = mod._stage_refine(
    str(video_path), str(frames_dir), str(work_dir), cfg, inputs, config_data,
)
manifest = json.loads((work_dir / "refine_manifest.json").read_text("utf-8"))
assert result["output_key"] == "refine"
assert manifest["refine_regions"] == 0
assert manifest["refine_requested"] == 0
assert manifest["refine_extracted"] == 0
assert manifest["refine_attempted"] == 0
assert manifest["refine_parsed"] == 0
assert manifest["frames"] == []
```

- [ ] **Step 4: 运行 RED 测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py -k "missing_worthiness or invalid_worthiness or no_high_score"
```

Expected: 缺失评分测试显示 payload 被错误转换为 0.5；无高分测试显示 `UnboundLocalError`。不得因为 fixture、路径或 JSON schema 构造错误而失败。

- [ ] **Step 5: 提交 RED 测试**

```powershell
git add tests/task_engine/test_vlm_stage_runtime.py
git commit -m "test: expose invalid vlm score and empty refine failures"
```

---

### Task 2: 收紧共享 VLM 评分协议

**Files:**
- Modify: `scripts/test_video_adaptive.py:145-199`
- Modify: `tests/task_engine/test_vlm_stage_runtime.py`

**Interfaces:**
- Consumes: Ollama-compatible `POST {base_url}/api/generate` 响应。
- Produces: `_score_vlm_frame(...)`；只有有效、有限、位于 `[0.0, 1.0]` 的评分才返回 `(payload, None)`。

- [ ] **Step 1: 在共享客户端中区分 JSON 成功与业务评分成功**

在 `_score_vlm_frame()` 中移除 `safe_worth(parsed.get("gif_worthiness", 0.5))` 的兜底。实现以下等价校验：

```python
worth = parsed.get("gif_worthiness")
if (
    isinstance(worth, bool)
    or not isinstance(worth, (int, float))
    or not math.isfinite(float(worth))
    or not 0.0 <= float(worth) <= 1.0
):
    last_error = (
        "invalid gif_worthiness: expected finite number in [0, 1], "
        f"got {worth!r}"
    )
    if attempt < 2:
        time.sleep(2)
        continue
    return None, last_error
parsed["gif_worthiness"] = float(worth)
```

允许 caption 等非关键质量字段继续记录 `_quality_errors`，但评分字段缺失或无效必须是致命响应错误。

- [ ] **Step 2: 让 parse error 真正完成配置的重试次数**

当前 parse error 第一次即返回，却报告“after 1 attempt”。改为保存 `last_error` 并继续循环，第三次才返回失败。HTTP、JSON、parse 和评分校验使用同一重试计数。

- [ ] **Step 3: 验证粗筛与 refine 共享相同失败语义**

增加测试分别通过 `_stage_vlm()` 和 `_stage_refine()` 触发无效评分，断言两者都抛出 `RuntimeError`，且不会写出表示成功的候选/clip manifest。

- [ ] **Step 4: 运行定向测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py
```

Expected: 全部通过；无效 payload 不再增加 `parsed_count`。

- [ ] **Step 5: 提交评分协议修复**

```powershell
git add scripts/test_video_adaptive.py tests/task_engine/test_vlm_stage_runtime.py
git commit -m "fix: reject invalid vlm scores in split stages"
```

---

### Task 3: 修复 refine 空集合与提取失败状态

**Files:**
- Modify: `scripts/test_video_adaptive.py:1965-2089`
- Modify: `tests/task_engine/test_vlm_stage_runtime.py`

**Interfaces:**
- Produces manifest fields: `refine_requested: int`、`refine_extracted: int`、`refine_extraction_failed: int`、`refine_attempted: int`、`refine_parsed: int`、`refine_failed: int`。
- Produces: 合法无高分输入成功输出空 refine manifest；需要 refine 但完全无法提取时明确失败。

- [ ] **Step 1: 在任何条件分支之前初始化计数器**

```python
refine_requested = 0
refine_extracted = 0
refine_extraction_failed = 0
refine_attempted = 0
refine_responded = 0
refine_parsed = 0
refine_failed = 0
```

- [ ] **Step 2: 检查每次 ffmpeg 提取结果**

```python
refine_requested = len(refine_ts)
completed = subprocess.run(..., capture_output=True, timeout=15)
if completed.returncode != 0:
    refine_extraction_failed += 1
    print(f"  refine extract FAILED ts={ts}: ffmpeg exit={completed.returncode}")
    continue
```

文件不存在、文件过小、图片解码失败或亮度过滤也必须增加 `refine_extraction_failed`；成功加入 `refine_frames` 时增加 `refine_extracted`。

- [ ] **Step 3: 定义完全提取失败的状态**

当 `refine_requested > 0 and refine_extracted == 0` 时抛出：

```python
raise RuntimeError(
    f"Refine extraction failed: requested={refine_requested}, "
    f"extraction_failed={refine_extraction_failed}"
)
```

不要把该场景写成 `attempted=0, failed=0` 的成功 manifest。部分提取成功时继续评分，并在 manifest 中保留失败计数。

- [ ] **Step 4: 增加提取失败测试**

让 `subprocess.run()` 返回 `returncode=1`，构造至少一个 `refine_ts`，断言 `_stage_refine()` 抛出包含 `Refine extraction failed` 的 `RuntimeError`。

- [ ] **Step 5: 运行定向测试并提交**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py -k refine
git add scripts/test_video_adaptive.py tests/task_engine/test_vlm_stage_runtime.py
git commit -m "fix: make refine zero and extraction outcomes explicit"
```

Expected: 无高分场景成功；完全提取失败场景失败；正常 refine 场景计数一致。

---

### Task 4: 完整恢复显式生命周期兼容层

**Files:**
- Modify: `scripts/test_video_adaptive.py:202-253`
- Modify: `scripts/test_video_adaptive.py:313-370`
- Modify: `scripts/test_video_adaptive.py:1868-1879`
- Modify: `configs/models.yaml`
- Modify: `tests/task_engine/test_vlm_stage_runtime.py`

**Interfaces:**
- Produces: `VlmRuntimeConfig(provider, model, base_url, manage_lifecycle, launch_mode)`。
- Produces: `_resolve_vlm_runtime(config_data: dict | None) -> VlmRuntimeConfig`。
- Consumes: `launch_mode` 仅允许 `none | native | wsl`。

- [ ] **Step 1: 先写生命周期 RED 测试**

必须包含以下精确场景：

```python
def test_vlm_lifecycle_disabled_never_spawns_model_command(...): ...
def test_vlm_lifecycle_none_wins_over_manage_true(...): ...
def test_vlm_lifecycle_native_uses_native_ollama_command(...): ...
def test_vlm_lifecycle_wsl_uses_wsl_command_only_when_explicit(...): ...
def test_vlm_lifecycle_does_not_infer_mode_from_base_url(...): ...
def test_wait_model_uses_frozen_base_url(...): ...
```

其中 URL 推断测试应使用 `http://127.0.0.1:11434` 且不提供生命周期字段，期望安全默认 `manage_lifecycle=False, launch_mode="none"`。

- [ ] **Step 2: 定义不可变 runtime 配置**

```python
@dataclass(frozen=True)
class VlmRuntimeConfig:
    provider: str
    model: str
    base_url: str
    manage_lifecycle: bool
    launch_mode: Literal["none", "native", "wsl"]
```

解析规则：

- `provider` 只能为 `ollama`。
- `model`、`base_url` 必填。
- `manage_lifecycle` 缺省为 `False`，不得根据 URL 推断。
- `launch_mode` 缺省为 `none`。
- `manage_lifecycle=False` 或 `launch_mode=none` 均关闭生命周期。
- 未知 `launch_mode` 立即抛出 `ValueError`。

- [ ] **Step 3: 将启停函数改为消费 runtime**

```python
def _ollama_command(runtime: VlmRuntimeConfig, *args: str) -> list[str]:
    if runtime.launch_mode == "native":
        return ["ollama", *args]
    if runtime.launch_mode == "wsl":
        return ["wsl", "ollama", *args]
    raise ValueError("launch_mode=none cannot execute ollama commands")

def stop_model(name: str, runtime: VlmRuntimeConfig) -> bool:
    subprocess.run(_ollama_command(runtime, "stop", name), ...)
    httpx.get(f"{runtime.base_url}/api/ps", ...)

def wait_model(name: str, runtime: VlmRuntimeConfig, timeout_s: int = 120) -> bool:
    httpx.post(f"{runtime.base_url}/api/generate", ...)
```

删除 `_should_manage_vlm_lifecycle()` 中的 URL 比较逻辑。阶段代码只读取一次 runtime，并将同一对象传给评分、停止和等待函数。

- [ ] **Step 4: 明确新任务的项目默认值**

在 `configs/models.yaml` 中加入：

```yaml
vlm:
  provider: "ollama"
  model: "llava:13b"
  base_url: "http://127.0.0.1:11434"
  manage_lifecycle: true
  launch_mode: "wsl"
```

这是当前项目的显式默认行为；历史冻结快照缺少字段时采用安全默认 `none`，不得偷偷恢复 URL 推断。

- [ ] **Step 5: 验证 Settings 保存不会删除新增键**

运行或补充测试：加载带 `manage_lifecycle/launch_mode` 的配置，调用 `save_config()` 修改 model/base URL，再读取 YAML，断言两个生命周期键保持不变。仅在失败时修改 `app/ui/tabs/settings.py`。

- [ ] **Step 6: 运行生命周期测试并提交**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py -k lifecycle
git add scripts/test_video_adaptive.py tests/task_engine/test_vlm_stage_runtime.py configs/models.yaml app/ui/tabs/settings.py
git commit -m "fix: make vlm lifecycle explicit across runtimes"
```

只添加实际修改过的文件；不得为了匹配命令而无意义修改 Settings。

---

### Task 5: 收紧完整成功链路的发布断言

**Files:**
- Modify: `tests/task_engine/test_full_production_stage_chain.py:336-482`

**Interfaces:**
- Consumes: Task 2 至 Task 4 新增的计数字段与 runtime 行为。
- Produces: 一个无法通过空 refine、未调用 LLM 或宽松 PBF 检查绕过的完整成功 E2E。

- [ ] **Step 1: 强制要求 refine Artifact**

将 `refine_manifest` 加入必需 Artifact kind 列表，删除 `if refine_artifacts:` 宽松分支：

```python
required_kinds = {
    "discover_manifest", "sample_manifest", "vlm_manifest",
    "refine_manifest", "synthesize_manifest", "rank_dedup_manifest",
    "gif_file", "gif_clip_manifest", "result", "materialize_manifest",
}
assert required_kinds <= set(art_by_kind)
```

- [ ] **Step 2: 断言真实 refine 请求与解析**

```python
assert rm["refine_requested"] > 0
assert rm["refine_extracted"] > 0
assert rm["refine_attempted"] > 0
assert rm["refine_parsed"] > 0
assert rm["refine_failed"] == 0
assert len(vlm_requests) > vm["parsed_count"]
```

最后一个断言证明 stub 同时收到 coarse 和 refine 请求，而不是仅有 coarse 高分区域。

- [ ] **Step 3: 删除恒真 LLM 断言**

```python
assert llm_requests, "synthesize must call deterministic LLM stub"
assert all(r["path"] == "/chat/completions" for r in llm_requests)
assert all(r["model"] == "gpt-mini" for r in llm_requests)
```

同时读取 `synthesize_manifest`，断言至少一个 clip 的 `summary` 和 `tags` 等于 stub 响应，证明响应被生产代码实际消费。

- [ ] **Step 4: 用可观测方式证明没有真实服务泄漏**

删除在 stub request path 中搜索 `127.0.0.1:11434` 的无效断言。改为：

- Job 冻结配置中的 VLM/LLM URL 都指向随机本地端口。
- lifecycle 为 `false/none`。
- 子进程日志中不存在 `wsl ollama`、`ollama stop`。
- Stub 请求数与 manifest 计数匹配。
- 测试结束后检查仓库 `data/` 关键文件的 mtime/size 未变化；若该检查不稳定，则使用网络 transport 审计点，禁止除两个 stub 端口外的 HTTP 目的地。

- [ ] **Step 5: 真正解析 PBF 的 start/end**

PBF 的 start 位于毫秒字段，end 位于标题的 `HH:MM:SS-HH:MM:SS`。增加测试辅助函数：

```python
def _parse_pbf_interval(path: Path) -> tuple[int, int]:
    text = path.read_bytes()[2:].decode("utf-16-le").replace("\r", "")
    match = re.search(
        r"^\d+=(\d+)\*#\d+\s+([0-9:]+)-([0-9:]+)\s+",
        text,
        re.MULTILINE,
    )
    assert match, f"PBF interval not found: {text!r}"
    start_ms = int(match.group(1))
    title_start_ms = _timestamp_to_ms(match.group(2))
    end_ms = _timestamp_to_ms(match.group(3))
    assert abs(start_ms - title_start_ms) < 1000
    assert end_ms > title_start_ms
    return title_start_ms, end_ms
```

- [ ] **Step 6: 运行成功链路**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_eight_stage_chain -s
```

Expected: 8 类 Stage 全部 succeeded；coarse/refine/LLM stub 均收到请求；至少一个真实 GIF；PBF start/end 有效；result SHA 正确。

- [ ] **Step 7: 提交成功门禁修复**

```powershell
git add tests/task_engine/test_full_production_stage_chain.py
git commit -m "test: enforce complete eight stage production gate"
```

---

### Task 6: 增加三条完整失败/zero-clip 子进程链路

**Files:**
- Modify: `tests/task_engine/test_full_production_stage_chain.py`
- Optional Modify: `scripts/test_video_adaptive.py`（仅用于提供不影响生产默认值的可测试 retry delay 配置）

**Interfaces:**
- Produces: `test_full_chain_vlm_outage_never_zero_succeeds`。
- Produces: `test_full_chain_invalid_vlm_payload_never_exports_default_score_clip`。
- Produces: `test_full_chain_valid_low_scores_materialize_zero_clip`。

- [ ] **Step 1: 提取共享 E2E 驱动器**

将创建视频、数据库、Job、Worker 和结果查询提取为测试文件内 helper，但不得隐藏以下生产调用：

```python
job = repo.create_job(CreateJob(...))
initialize_job(repo, job.job_id)
worker = TaskWorker(repo, worker_id, real_adapters, ...)
worker.drain()
advance_job(repo, job.job_id)
```

Helper 返回 `job_id`、Stage 行、Artifact 行、视频状态、Job 状态和 stub 请求记录。

- [ ] **Step 2: 实现 outage 完整链路**

Stub 对 `/api/generate` 返回 HTTP 503。断言：

```python
assert job_status != "succeeded"
assert video_status != "succeeded"
assert stage_status["vlm"] in {"failed", "pending"}
assert "rank_dedup" not in stage_status
assert "materialize" not in stage_status
assert not result_artifacts
assert not published_gifs
```

如果 Worker 重试策略把 Stage 留在 pending，应继续 drain 到达到测试配置的 `max_attempts`，不要手工修改状态。

- [ ] **Step 3: 实现 invalid payload 完整链路**

Stub 返回 `{"response": "{}"}`。断言与 outage 相同，并额外确认错误日志包含 `gif_worthiness`，不存在分数为 0.5 的 VLM manifest 成功 Artifact。

- [ ] **Step 4: 实现合法低分 zero-clip 完整链路**

Stub 返回结构完整且 `gif_worthiness=0.1` 的响应。断言：

```python
assert job_status == "succeeded"
assert video_status == "succeeded"
assert all_required_non_gif_stages_succeeded
assert rank_manifest["clip_count"] == 0
assert rank_manifest["clips"] == []
assert materialize_manifest["gif_count"] == 0
assert not gif_clip_stages
assert not gif_file_artifacts
assert not published_gifs
```

该测试必须经过 `_assert_zero_clip_proven()` 的 SHA、size、schema 和 stage 校验，不得直接创建 materialize Stage。

- [ ] **Step 5: 控制失败测试耗时但不改变生产语义**

优先让测试 Job 使用一个 coarse frame 和较小 `max_attempts`。若仍过慢，可给共享客户端增加冻结配置字段 `vlm.retry_delay_s`，生产缺省仍为 `2.0`，测试显式设置 `0.0`；不得通过 parent-process monkeypatch 绕过子进程重试。

- [ ] **Step 6: 运行四条完整链路**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py -s
```

Expected: 高分成功、outage 失败、invalid payload 失败、合法低分 zero-clip 成功，四种状态均符合断言。

- [ ] **Step 7: 提交失败场景门禁**

```powershell
git add tests/task_engine/test_full_production_stage_chain.py scripts/test_video_adaptive.py
git commit -m "test: cover vlm failure and valid zero clip production paths"
```

只添加实际修改过的文件。

---

### Task 7: 文档、全量验证与发布门禁报告

**Files:**
- Modify: `README.md`
- Modify: `Agent.md`
- Create: `STAGE_SPLIT_PRODUCTION_PATH_SEVENTH_FIX_REPORT_2026-07-18.md`

**Interfaces:**
- Consumes: Task 1 至 Task 6 的最终代码和测试结果。
- Produces: 可供下一轮 Review 使用的证据报告。

- [ ] **Step 1: 更新用户与 Agent 文档**

README 配置示例必须包含：

```yaml
vlm:
  provider: ollama
  model: llava:13b
  base_url: http://127.0.0.1:11434
  manage_lifecycle: true
  launch_mode: wsl
```

解释 `none/native/wsl`，明确 URL 不决定启动方式。`Agent.md` 记录 invalid payload 不得产生 0.5 默认分、合法低分必须走严格 zero-clip、发布前必须运行完整四场景 E2E。

- [ ] **Step 2: 运行语法与定向测试**

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py -s
```

Expected: 全部 exit 0；完整链路输出能区分 coarse/refine 请求并展示四种预期状态。

- [ ] **Step 3: 运行 Task Engine 与 Quality Lab 回归**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
```

Expected: 无失败、无新跳过。

- [ ] **Step 4: 运行完整仓库测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Expected: pytest 无失败；已有 skip 必须逐项说明，不能新增排除项；`git diff --check` exit 0。

- [ ] **Step 5: 检查历史数据没有被触碰**

对比实施前记录的文件大小、mtime 或 SHA：

```powershell
Get-Item data\task_state.db,data\quality_lab.db,data\library.db |
  Select-Object FullName,Length,LastWriteTime
```

同时确认正式导出、标签和 Preference Memory 未被测试修改。不得通过还原或删除数据来伪造检查结果。

- [ ] **Step 6: 编写修复报告**

`STAGE_SPLIT_PRODUCTION_PATH_SEVENTH_FIX_REPORT_2026-07-18.md` 必须包含：

1. 两个 P0 的 RED 失败证据与 GREEN 结果。
2. 生命周期六个测试的命令选择与 base URL 证据。
3. 高分成功、outage、invalid payload、valid-low-score 四条完整链路的 Job/Video/Stage 最终状态。
4. 成功链路的 coarse/refine/LLM 请求数、Artifact kind/stage_id/size/SHA、GIF、PBF start/end 和 result SHA。
5. 完整 pytest、compileall、`git diff --check` 输出。
6. 历史数据库、导出、标签和 Preference Memory 未变更的证据。

- [ ] **Step 7: 提交文档与最终验证结果**

```powershell
git add README.md Agent.md STAGE_SPLIT_PRODUCTION_PATH_SEVENTH_FIX_REPORT_2026-07-18.md
git commit -m "docs: record seventh stage split production gate"
```

---

## 发布门禁

只有同时满足以下条件，后续 Agent 才能建议构建 EXE；构建和重跑历史队列仍需作为单独任务执行：

- [ ] 无高分 refine 不再抛出未初始化变量错误。
- [ ] 缺失、非有限、布尔或越界 `gif_worthiness` 不会成为成功评分。
- [ ] VLM outage 和 invalid payload 不会形成 Job 伪成功。
- [ ] 合法低分结果通过严格 rank Artifact 证明后完成 zero-clip。
- [ ] refine 请求、提取、解析和失败计数可审计，完全提取失败不会静默成功。
- [ ] lifecycle 不根据 URL 推断；`none/native/wsl` 命令和 endpoint 均正确。
- [ ] 成功 E2E 确实经过真实 Worker、真实 Adapter、真实子进程和全部八阶段。
- [ ] 成功 E2E 确实产生 refine frame、调用确定性 LLM stub、导出真实 GIF，并验证 PBF start/end 与 result SHA。
- [ ] 四条完整生产链路全部通过。
- [ ] 全仓 pytest、compileall、`git diff --check` 全部通过且无新增跳过。
- [ ] 历史数据、导出、标签和 Preference Memory 完整保留。

## 建议执行方式

使用 `superpowers:subagent-driven-development`，每个 Task 由新的实现 Agent 完成，并在进入下一 Task 前分别进行规格符合性 Review 和代码质量 Review。不要并行修改 `scripts/test_video_adaptive.py`，避免多个 Agent 覆盖同一核心文件。
