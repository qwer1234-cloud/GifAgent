# Stage Split 第八次 Review 修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复合法低分 zero-clip 的 Artifact 断链，并把重试耗尽、显式生命周期和确定性 LLM 验证落实为不可绕过的发布门禁。

**Architecture:** 保持现有八阶段生产流水线不变，只修正空 `synthesize` 分支的 Artifact 返回契约，并收紧 E2E 驱动器。失败链路通过 `TaskRepository` 的零延迟 `RetryPolicy` 在真实 Worker 中耗尽重试；生命周期使用函数级测试验证命令和 endpoint；成功链路必须证明 LLM stub 的响应进入 `synthesize_manifest`。

**Tech Stack:** Python 3.14、pytest、SQLite、TaskWorker、AdaptivePipelineAdapter、httpx、ffmpeg、PowerShell。

## Global Constraints

1. 不删除、重建、清空或覆盖 `data/task_state.db`、`data/quality_lab.db`、`data/library.db`、历史 GIF/PBF/result JSON、标签、Review 数据和 Preference Memory。
2. 测试只能写入 `tmp_path`；不得写入仓库 `data/`、正式导出目录或用户视频目录。
3. 测试不得访问、启动、停止或修改真实 Ollama、WSL、DeepSeek、云端 LLM/VLM 或 embedding 服务。
4. 完整链路必须使用真实 `TaskWorker + AdaptivePipelineAdapter + subprocess`；不得手工创建中间 Stage 或 Artifact。
5. 每个问题先写或收紧 RED 测试，确认失败原因正确后再修改生产代码。
6. 所有命令使用 `.\.venv\Scripts\python.exe`，不要使用系统 Python 3.9。
7. 本计划全部通过前，不构建 EXE，不重跑历史队列。

---

## 当前失败基线

```text
compileall: exit 0
pytest: 930 passed, 1 failed, 2 skipped, 3 warnings
git diff --check: exit 0
```

唯一失败：

```text
test_full_chain_valid_low_scores_materialize_zero_clip
rank_dedup=retry_wait
No artifact of kind 'synthesize_manifest'
```

根因：`_stage_synthesize()` 的空 clips 分支写出了文件，但返回结果没有 `_artifacts`，Worker 因而没有注册 `synthesize_manifest`。

---

## 文件职责

- Modify: `scripts/test_video_adaptive.py`
  - 修复空 synthesize 分支的 Artifact 返回契约。
- Modify: `tests/task_engine/test_full_production_stage_chain.py`
  - 让 outage/invalid payload 真正耗尽重试。
  - 强制验证 LLM stub 与 synthesize manifest。
  - 保留并通过合法低分 zero-clip 链路。
- Modify: `tests/task_engine/test_vlm_stage_runtime.py`
  - 补齐显式生命周期六项测试。
- Modify: `Agent.md`
  - 仅在四条 E2E 全部通过后记录门禁已关闭。
- Create: `STAGE_SPLIT_PRODUCTION_PATH_EIGHTH_FIX_REPORT_2026-07-18.md`
  - 保存本次修复与验证证据。

---

### Task 1: 修复 empty synthesize 的 Artifact 契约

**Files:**
- Modify: `scripts/test_video_adaptive.py:2320-2329`
- Modify: `tests/task_engine/test_vlm_stage_runtime.py`
- Test: `tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_chain_valid_low_scores_materialize_zero_clip`

**Interfaces:**
- Consumes: `_save_manifest(work_dir, "synthesize", manifest) -> str`。
- Produces: 空 clips 与非空 clips 分支均返回 `synthesize_manifest` Artifact。

- [ ] **Step 1: 增加空分支 Artifact RED 单元测试**

构造合法的空 `refine_manifest`，直接调用 `_stage_synthesize()`：

```python
def test_synthesize_empty_frames_returns_manifest_artifact(tmp_path):
    mod = _load_stage_module()
    work_dir = tmp_path / "synthesize"
    work_dir.mkdir()
    refine_path = tmp_path / "refine_manifest.json"
    refine_path.write_text(json.dumps({
        "schema_version": 1,
        "stage": "refine",
        "scored_count": 0,
        "frames": [],
    }), encoding="utf-8")
    result = mod._stage_synthesize(
        str(work_dir),
        mod.extract_config({"adaptive": {}, "preference_memory": {}}),
        {"refine_manifest": [{"artifact_id": "refine-1", "path": str(refine_path)}]},
    )
    assert result["clip_count"] == 0
    assert len(result["_artifacts"]) == 1
    assert result["_artifacts"][0]["artifact_kind"] == "synthesize_manifest"
    assert Path(result["_artifacts"][0]["path"]).exists()
```

- [ ] **Step 2: 运行 RED 测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py::test_synthesize_empty_frames_returns_manifest_artifact
```

Expected: FAIL，错误为返回字典缺少 `_artifacts`，而不是输入 manifest 构造错误。

- [ ] **Step 3: 修正生产代码**

把空分支改为与正常分支相同的返回契约：

```python
if not clips_data:
    manifest = {
        "schema_version": 1,
        "stage": "synthesize",
        "clip_count": 0,
        "clips": [],
        "output_key": "synthesize",
    }
    manifest_path = _save_manifest(work_dir, "synthesize", manifest)
    return {
        "output_key": "synthesize",
        "clip_count": 0,
        "_artifacts": [
            _make_artifact(manifest_path, "synthesize_manifest")
        ],
    }
```

- [ ] **Step 4: 运行单元测试和完整 zero-clip E2E**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py::test_synthesize_empty_frames_returns_manifest_artifact
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_chain_valid_low_scores_materialize_zero_clip -s
```

Expected：

- synthesize、rank_dedup、materialize 均 succeeded。
- `rank_dedup_manifest.clip_count == 0`。
- `materialize_manifest.gif_count == 0`。
- Job 与 Video succeeded。
- 没有 gif_clip Stage、gif_file Artifact 或正式 GIF。

- [ ] **Step 5: 提交修复**

```powershell
git add scripts/test_video_adaptive.py tests/task_engine/test_vlm_stage_runtime.py
git commit -m "fix: preserve synthesize artifact on zero clip path"
```

---

### Task 2: 让失败 E2E 真正耗尽重试

**Files:**
- Modify: `tests/task_engine/test_full_production_stage_chain.py:217-309`
- Modify: `tests/task_engine/test_full_production_stage_chain.py:457-523`

**Interfaces:**
- Consumes: `RetryPolicy(max_attempts, base_delay_seconds, max_delay_seconds)`。
- Produces: `_drive_full_chain()` 返回最终状态，而不是第一次 `retry_wait` 快照。

- [ ] **Step 1: 写出失败链路的最终状态断言**

将 outage 和 invalid-payload 测试从宽松断言：

```python
assert by_name.get("vlm") in ("retry_wait", "needs_attention", "failed")
assert job_status != "succeeded"
```

收紧为：

```python
vlm_stage = by_name["vlm"]
assert vlm_stage["status"] == "needs_attention"
assert vlm_stage["attempt_count"] == 3
assert job_status == "needs_attention"
assert video_status == "needs_attention"
```

如果当前聚合规则对 Job/Video 使用其他明确终态，应以真实 schema 允许的最终注意状态为准，但不得接受 `running`、`pending` 或 `retry_wait`。

- [ ] **Step 2: 将同一零延迟策略传给 Repository 和 Worker**

`TaskRepository.fail_stage()` 使用 Repository 自己的策略决定 `retry_at`，因此只传给 Worker 不生效。修改 helper：

```python
from app.task_engine.models import RetryPolicy

policy = RetryPolicy(
    max_attempts=max_attempts,
    base_delay_seconds=0,
    max_delay_seconds=0,
)
repo = TaskRepository(conn, retry_policy=policy)
worker = TaskWorker(
    repo,
    "worker-1",
    adapters,
    retry_policy=policy,
    lease_seconds=120,
    heartbeat_seconds=40,
    db_path=str(db_path),
)
```

保留 `max_attempts` helper 参数，并在返回字典中提供 Video/Job 状态和 `last_error_json`，避免每个测试重复查询。

- [ ] **Step 3: 驱动流水线直到没有可领取工作**

零延迟策略下，一次 `worker.drain()` 应能连续领取 retry_wait Stage。使用最多 10 轮的有界循环防止测试死循环：

```python
for _ in range(10):
    processed = worker.drain()
    advance_job(repo, job.job_id)
    if processed == 0:
        break
else:
    pytest.fail("worker did not reach a terminal state within 10 drains")
```

循环结束后查询 Stage；outage/invalid 的 VLM 必须 `attempt_count == max_attempts` 且终态为 `needs_attention`。

- [ ] **Step 4: 验证每次重试都访问了 stub**

粗采样帧数记为 `coarse_frames`，断言：

```python
assert len(d["vlm_requests"]) == coarse_frames * max_attempts * 3
```

其中末尾 `3` 是 `_score_vlm_frame()` 每个 Stage attempt 内部的 HTTP 重试次数。若采用更稳定的计数方式，可从 `sample_manifest.frame_count` 计算，但不得只断言请求数大于零。

- [ ] **Step 5: 运行两条失败链路**

```powershell
.\.venv\Scripts\python.exe -m pytest -q \
  tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_chain_vlm_outage_never_zero_succeeds \
  tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_chain_invalid_vlm_payload_never_exports_default_score_clip -s
```

Expected：两个用例均耗尽三次 Stage attempt；VLM 为 `needs_attention`；后续 rank_dedup/materialize 不存在；无 result/GIF。

- [ ] **Step 6: 提交失败门禁修复**

```powershell
git add tests/task_engine/test_full_production_stage_chain.py
git commit -m "test: exhaust vlm retries in production failure gates"
```

---

### Task 3: 补齐显式生命周期六项测试

**Files:**
- Modify: `tests/task_engine/test_vlm_stage_runtime.py`
- Test: `scripts/test_video_adaptive.py:269-410`

**Interfaces:**
- Consumes: `VlmRuntimeConfig`、`_resolve_vlm_runtime()`、`_ollama_command()`、`stop_model()`、`wait_model()`。
- Produces: URL 不推断、none/native/wsl 命令及冻结 endpoint 的回归证据。

- [ ] **Step 1: 测试缺省配置不从 URL 推断**

```python
def test_vlm_lifecycle_does_not_infer_mode_from_base_url():
    mod = _load_stage_module()
    runtime = mod._resolve_vlm_runtime({"vlm": {
        "provider": "ollama",
        "model": "m",
        "base_url": "http://127.0.0.1:11434",
    }})
    assert runtime.manage_lifecycle is False
    assert runtime.launch_mode == "none"
```

- [ ] **Step 2: 测试 disabled 与 none 不执行命令**

```python
@pytest.mark.parametrize("manage,mode", [(False, "wsl"), (True, "none")])
def test_vlm_lifecycle_disabled_never_spawns_model_command(
    tmp_path, monkeypatch, manage, mode,
):
    # 构造一个真实 _stage_vlm 输入；subprocess.run 被替换为抛 AssertionError。
    # VLM HTTP 使用本地 StubServer。
    # 期望阶段成功且 subprocess.run 从未被调用。
```

- [ ] **Step 3: 测试 native 命令**

```python
def test_vlm_lifecycle_native_uses_native_ollama_command(monkeypatch):
    mod = _load_stage_module()
    runtime = mod.VlmRuntimeConfig(
        provider="ollama", model="m", base_url="http://stub",
        manage_lifecycle=True, launch_mode="native", retry_delay_s=0,
    )
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw:
                        calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **kw: _PsResponse([]))
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    assert mod.stop_model("m", runtime) is True
    assert calls == [["ollama", "stop", "m"]]
```

- [ ] **Step 4: 测试 wsl 命令仅在显式配置时出现**

复用上一步结构，runtime 使用 `launch_mode="wsl"`，断言：

```python
assert calls == [["wsl", "ollama", "stop", "m"]]
```

同时检查 native 用例中不存在 `wsl`，缺省/none 用例中不存在任何命令。

- [ ] **Step 5: 测试 wait_model 使用冻结 base URL**

```python
def test_wait_model_uses_frozen_base_url(monkeypatch):
    mod = _load_stage_module()
    runtime = mod.VlmRuntimeConfig(
        provider="ollama", model="m",
        base_url="http://127.0.0.1:45678",
        manage_lifecycle=True, launch_mode="native", retry_delay_s=0,
    )
    urls = []
    monkeypatch.setattr(mod.httpx, "post", lambda url, **kw:
                        urls.append(url) or _StatusResponse(200))
    assert mod.wait_model("m", runtime, timeout_s=1) is True
    assert urls == ["http://127.0.0.1:45678/api/generate"]
```

- [ ] **Step 6: 测试未知 launch mode 明确失败**

```python
def test_vlm_lifecycle_rejects_unknown_launch_mode():
    mod = _load_stage_module()
    with pytest.raises(ValueError, match="launch_mode"):
        mod._resolve_vlm_runtime({"vlm": {
            "provider": "ollama", "model": "m",
            "base_url": "http://stub", "launch_mode": "auto",
        }})
```

- [ ] **Step 7: 运行生命周期测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py -k lifecycle
```

Expected：至少六个生命周期场景通过，无真实 sleep、WSL、Ollama 或 HTTP 外联。

- [ ] **Step 8: 仅在测试暴露真实缺陷时修改生产代码并提交**

```powershell
git add tests/task_engine/test_vlm_stage_runtime.py scripts/test_video_adaptive.py
git commit -m "test: lock explicit vlm lifecycle behavior"
```

如果生产代码无需修改，只暂存测试文件。

---

### Task 4: 强制成功 E2E 使用确定性 LLM Stub

**Files:**
- Modify: `tests/task_engine/test_full_production_stage_chain.py:437-443`

**Interfaces:**
- Consumes: `_LLM_RESP`、`llm_requests`、`synthesize_manifest`。
- Produces: LLM 请求与 manifest 内容的双向证据。

- [ ] **Step 1: 删除条件式断言**

把：

```python
if d["llm_requests"]:
    ...
```

替换为：

```python
assert d["llm_requests"], "synthesize must call deterministic LLM stub"
for request in d["llm_requests"]:
    assert request["path"] == "/chat/completions"
    assert request["model"] == "gpt-mini"
```

- [ ] **Step 2: 验证响应进入 synthesize manifest**

```python
synth = json.loads(
    Path(art_by_kind["synthesize_manifest"][0]["path"])
    .read_text(encoding="utf-8")
)
assert synth["clips"]
assert any(
    clip.get("summary") == "A dramatic scene with strong visual impact."
    and clip.get("tags") == ["dramatic", "cinematic"]
    for clip in synth["clips"]
), synth
```

该断言失败时应修复冻结 LLM config 的传递或 `_synthesize_clips_with_llm()` 的异常吞噬问题；不得恢复条件式断言。

- [ ] **Step 3: 运行完整成功链路**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_eight_stage_chain -s
```

Expected：LLM stub 至少收到一次请求，model 为 `gpt-mini`，summary/tags 进入 synthesize manifest。

- [ ] **Step 4: 提交 LLM 门禁**

```powershell
git add tests/task_engine/test_full_production_stage_chain.py scripts/test_video_adaptive.py
git commit -m "test: require deterministic llm synthesis in production gate"
```

只添加实际修改过的文件。

---

### Task 5: 全量验证、文档校准与修复报告

**Files:**
- Modify: `Agent.md`
- Create: `STAGE_SPLIT_PRODUCTION_PATH_EIGHTH_FIX_REPORT_2026-07-18.md`

**Interfaces:**
- Consumes: Tasks 1-4 的最终代码和测试输出。
- Produces: 下一轮 Review 可以复验的准确报告。

- [ ] **Step 1: 运行语法和定向测试**

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py -s
```

Expected：四条生产 E2E 全部通过；zero-clip 不再 retry_wait；outage/invalid 均耗尽三次 Stage attempt。

- [ ] **Step 2: 运行 Task Engine 与 Quality Lab 回归**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
```

Expected：无失败、无新增 skip。

- [ ] **Step 3: 运行完整仓库验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Expected：pytest 零失败；`git diff --check` exit 0。报告必须写完整汇总，例如 `N passed, 2 skipped, 3 warnings`，不得省略 failed 数量。

- [ ] **Step 4: 校准 Agent.md**

只有 Step 1-3 全部通过后，才保留“四条 E2E 必须通过且当前已通过”的表述。记录以下不变量：

- empty synthesize 也必须注册 manifest Artifact。
- failure E2E 必须达到 `attempt_count == max_attempts` 的终态。
- 生命周期测试不允许真实命令或网络访问。
- LLM stub 必须被调用且响应必须进入 manifest。

- [ ] **Step 5: 检查历史数据未被触碰**

```powershell
Get-Item data\task_state.db,data\quality_lab.db,data\library.db |
  Select-Object FullName,Length,LastWriteTime
```

确认正式导出、标签、Review 和 Preference Memory 未被测试修改。不得通过删除、还原或重建历史数据完成检查。

- [ ] **Step 6: 编写第八次修复报告**

报告必须包含：

1. zero-clip 修复前 `No artifact of kind 'synthesize_manifest'` 和修复后完整链路证据。
2. outage/invalid 的 Stage attempt_count、最终 Stage/Video/Job 状态和 stub 请求数。
3. 六项生命周期测试及实际命令数组/endpoint。
4. LLM 请求路径、model、summary/tags manifest 内容。
5. 四条完整 E2E、完整 pytest、compileall、`git diff --check` 原始汇总。
6. 历史数据库、导出、标签和 Preference Memory 未变更证据。

- [ ] **Step 7: 提交文档**

```powershell
git add Agent.md STAGE_SPLIT_PRODUCTION_PATH_EIGHTH_FIX_REPORT_2026-07-18.md
git commit -m "docs: record eighth stage split production gate"
```

---

## 发布门禁

只有同时满足以下条件，后续 Agent 才能建议构建 EXE；构建和历史队列重跑仍是独立任务：

- [ ] 空 synthesize 返回并注册 `synthesize_manifest` Artifact。
- [ ] 合法低分链路经过 rank_dedup 与 materialize，Job/Video succeeded 且没有 GIF。
- [ ] outage 与 invalid payload 均耗尽 `max_attempts`，最终进入明确注意状态。
- [ ] 失败链路没有 rank_dedup、materialize、result 或 GIF。
- [ ] 六项生命周期测试全部存在并通过。
- [ ] 成功链路无条件调用 LLM stub，响应进入 synthesize manifest。
- [ ] 四条真实 Worker/Adapter/子进程 E2E 全部通过。
- [ ] 全仓 pytest、compileall、`git diff --check` 全部通过且无新增 skip。
- [ ] 历史数据、导出、标签和 Preference Memory 保持完整。

## 建议执行方式

使用 `superpowers:subagent-driven-development` 串行执行 Task 1-5。Tasks 1、2、4 都修改 `tests/task_engine/test_full_production_stage_chain.py` 或核心阶段脚本，不要并行实施；每个 Task 完成后先做规格符合性 Review，再做代码质量 Review。
