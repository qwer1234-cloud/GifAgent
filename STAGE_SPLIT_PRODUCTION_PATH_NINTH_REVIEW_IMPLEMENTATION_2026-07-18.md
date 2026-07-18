# Stage Split 第九次 Review 修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭第八次修复后剩余的 LLM E2E 绕过和 VLM HTTP 重试计数缺口，使四条生产链路的发布门禁与报告完全一致。

**Architecture:** E2E 的 VLM、LLM、adaptive 和 runtime 配置全部进入同一个 Job 冻结快照，stage subprocess 只消费该快照，不再依赖随后会被覆盖的临时 YAML。成功链无条件验证 LLM 请求及 manifest 内容；失败链根据实际 sample frame 数、Stage attempt 数和每帧 HTTP retry 数精确验证请求次数。

**Tech Stack:** Python 3.14、pytest、TaskWorker、AdaptivePipelineAdapter、SQLite、httpx、本地 HTTP Stub、ffmpeg、PowerShell。

## Global Constraints

1. 不删除、重建、清空或覆盖 `data/task_state.db`、`data/quality_lab.db`、`data/library.db`、历史 GIF/PBF/result JSON、标签、Review 数据和 Preference Memory。
2. 测试只能写入 `tmp_path`，不得访问正式导出目录或仓库 `data/`。
3. 测试不得访问真实 Ollama、WSL、DeepSeek、云端 LLM/VLM 或 embedding 服务。
4. 完整链路必须使用真实 `TaskWorker + AdaptivePipelineAdapter + subprocess`，不得手工插入 Stage 或 Artifact。
5. 使用 `.\.venv\Scripts\python.exe`，不要使用系统 Python 3.9。
6. 本计划全部通过前，不构建 EXE，不重跑历史队列。

---

## 当前证据

全仓测试虽然通过：

```text
939 passed, 2 skipped, 3 warnings
compileall exit 0
git diff --check exit 0
```

但成功 E2E helper 的真实输出为：

```text
vlm_requests = 9
llm_requests = 0
summaries = ['']
tags = [[]]
```

根因：`_make_full_config()` 没有 `llm` 段；`run_stage_mode()` 使用 Job 快照调用 `set_config_override(config_data)`，因此先前通过 `GIFAGENT_CONFIG` 加载的临时 YAML 被覆盖。当前测试又在 `if llm_requests:` 为空时执行 `pass`，所以门禁被绕过。

---

## 文件职责

- Modify: `tests/task_engine/test_full_production_stage_chain.py`
  - 将 LLM 配置放入 Job 冻结快照。
  - 无条件验证 LLM stub 请求与 synthesize manifest。
  - 精确验证 outage/invalid-payload 的 HTTP 请求次数。
- Modify: `Agent.md`
  - 删除“diagnostic assertion”例外，记录强制 LLM 门禁。
- Modify: `STAGE_SPLIT_PRODUCTION_PATH_EIGHTH_FIX_REPORT_2026-07-18.md`
  - 标明第八次报告的 Task 4 未完成，不得继续声称门禁关闭。
- Create: `STAGE_SPLIT_PRODUCTION_PATH_NINTH_FIX_REPORT_2026-07-18.md`
  - 保存最终验证证据。

---

### Task 1: 将 LLM 配置纳入冻结 Job 快照

**Files:**
- Modify: `tests/task_engine/test_full_production_stage_chain.py:138-187`
- Modify: `tests/task_engine/test_full_production_stage_chain.py:217-270`

**Interfaces:**
- Consumes: `_StubServer.base_url`、`CreateJob.config_json`、stage mode 的 `set_config_override()`。
- Produces: `_make_full_config(..., llm_port, **kw)`，其返回值同时包含 VLM 与 LLM 的确定性配置。

- [ ] **Step 1: 先把成功 E2E 改成无条件 RED 断言**

删除：

```python
if d["llm_requests"]:
    ...
else:
    pass
```

改为：

```python
assert d["llm_requests"], "synthesize must call deterministic LLM stub"
for request in d["llm_requests"]:
    assert request["path"] == "/chat/completions"
    assert request["model"] == "gpt-mini"

synth = json.loads(
    Path(d["art_by_kind"]["synthesize_manifest"][0]["path"])
    .read_text(encoding="utf-8")
)
assert synth["clips"]
assert any(
    clip.get("summary") == "A dramatic scene with strong visual impact."
    and clip.get("tags") == ["dramatic", "cinematic"]
    for clip in synth["clips"]
), synth
```

- [ ] **Step 2: 运行成功链并确认 RED 原因**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_eight_stage_chain -s
```

Expected：FAIL 于 `synthesize must call deterministic LLM stub`；不得因 VLM、GIF、PBF 或 materialize 失败。

- [ ] **Step 3: 把 LLM 配置加入 `_make_full_config()`**

修改签名：

```python
def _make_full_config(
    work_base: Path,
    export_base: Path,
    vlm_port: int,
    llm_port: int,
    **kw,
) -> dict:
```

在返回字典中加入：

```python
"llm": {
    "provider": "openai_compatible",
    "model": "gpt-mini",
    "base_url": f"http://127.0.0.1:{llm_port}",
    "api_key_env": "OPENAI_API_KEY",
    "temperature": 0.3,
    "max_tokens": 256,
    "timeout_s": 10,
    **kw.get("llm", {}),
},
```

- [ ] **Step 4: 让 E2E driver 使用同一个冻结快照**

把配置创建改为：

```python
config = _make_full_config(
    work_base,
    export_base,
    vlm_stub.port,
    llm_stub.port,
    **(config_overrides or {}),
)
```

保留：

```python
monkeypatch.setenv("OPENAI_API_KEY", "test-key")
```

删除 `_make_llm_config_yaml()`、`llm_yaml` 和 `GIFAGENT_CONFIG` 设置。测试不得再通过全局 YAML 注入 LLM 配置，因为 stage mode 的真实契约是冻结 Job config。

- [ ] **Step 5: 验证 Job 数据库中的冻结配置**

在 helper 创建 Job 后增加：

```python
frozen = json.loads(conn.execute(
    "SELECT config_json FROM task_jobs WHERE job_id=?",
    (job.job_id,),
).fetchone()["config_json"])
assert frozen["llm"]["model"] == "gpt-mini"
assert frozen["llm"]["base_url"] == llm_stub.base_url
```

- [ ] **Step 6: 运行成功 E2E 并确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_eight_stage_chain -s
```

Expected：

- `llm_requests >= 1`。
- 所有请求 path 为 `/chat/completions`、model 为 `gpt-mini`。
- synthesize manifest 中至少一个 clip 包含 stub summary 与完整 tags。
- 八阶段、GIF、PBF、result SHA 继续通过。

- [ ] **Step 7: 提交 LLM 门禁修复**

```powershell
git add tests/task_engine/test_full_production_stage_chain.py
git commit -m "test: freeze llm config in production stage chain"
```

---

### Task 2: 精确验证 VLM 内部 HTTP 重试次数

**Files:**
- Modify: `tests/task_engine/test_full_production_stage_chain.py:300-330`
- Modify: `tests/task_engine/test_full_production_stage_chain.py:487-550`

**Interfaces:**
- Consumes: `sample_manifest.frame_count`、`max_attempts`、`_score_vlm_frame()` 固定的每帧 3 次 HTTP attempt。
- Produces: helper 返回 `sample_frame_count` 与 `expected_failed_vlm_requests`。

- [ ] **Step 1: 从 Artifact 读取真实 coarse frame 数**

在 `_drive_full_chain()` 收集 Artifact 后增加：

```python
sample_manifest = json.loads(
    Path(art_by_kind["sample_manifest"][0]["path"])
    .read_text(encoding="utf-8")
)
sample_frame_count = int(sample_manifest["frame_count"])
assert sample_frame_count > 0
```

返回字典加入：

```python
"sample_frame_count": sample_frame_count,
"max_attempts": max_attempts,
```

- [ ] **Step 2: 在 outage 链精确断言请求数**

```python
expected = d["sample_frame_count"] * d["max_attempts"] * 3
assert len(d["vlm_requests"]) == expected, (
    f"outage VLM requests={len(d['vlm_requests'])}, expected={expected} "
    f"({d['sample_frame_count']} frames × {d['max_attempts']} stage attempts × 3 HTTP retries)"
)
```

- [ ] **Step 3: 在 invalid-payload 链使用同一精确断言**

```python
expected = d["sample_frame_count"] * d["max_attempts"] * 3
assert len(d["vlm_requests"]) == expected, (
    f"invalid-payload VLM requests={len(d['vlm_requests'])}, expected={expected}"
)
```

不要只保留 `len(...) > 0`。

- [ ] **Step 4: 运行两条失败 E2E**

```powershell
.\.venv\Scripts\python.exe -m pytest -q \
  tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_chain_vlm_outage_never_zero_succeeds \
  tests/task_engine/test_full_production_stage_chain.py::TestFullProductionStageChain::test_full_chain_invalid_vlm_payload_never_exports_default_score_clip -s
```

Expected：两条链均为 VLM `attempt_count=3`、Job/Video `needs_attention`，并且请求数严格等于 `frame_count × 3 × 3`。

- [ ] **Step 5: 增加单元级 retry 保护**

在 `tests/task_engine/test_vlm_stage_runtime.py` 增加一个单帧 invalid payload 测试，直接断言 `_score_vlm_frame()` 向 stub 发出 3 次请求：

```python
payload, error = mod._score_vlm_frame(
    base_url=stub.base_url,
    model="stub-vlm",
    image_bytes=b"jpeg",
    prompt="score",
    options={},
    threshold=0.5,
    timestamp=0.0,
    frame_path="frame.jpg",
    retry_delay_s=0.0,
)
assert payload is None
assert error is not None
assert len([r for r in stub.requests if r["path"] == "/api/generate"]) == 3
```

- [ ] **Step 6: 提交重试计数门禁**

```powershell
git add tests/task_engine/test_full_production_stage_chain.py tests/task_engine/test_vlm_stage_runtime.py
git commit -m "test: verify every vlm retry reaches the frozen endpoint"
```

---

### Task 3: 校准报告并执行最终发布验证

**Files:**
- Modify: `Agent.md`
- Modify: `STAGE_SPLIT_PRODUCTION_PATH_EIGHTH_FIX_REPORT_2026-07-18.md`
- Create: `STAGE_SPLIT_PRODUCTION_PATH_NINTH_FIX_REPORT_2026-07-18.md`

**Interfaces:**
- Consumes: Tasks 1-2 的测试输出与 stub 请求证据。
- Produces: 不含条件式例外的最终门禁报告。

- [ ] **Step 1: 修正第八次报告的历史结论**

在第八次报告 Task 4 和发布门禁处明确注明：当时仅实现 diagnostic assertion，实际 `llm_requests=0`，因此 Task 4 在第九次修复前并未关闭。不要删除原报告或重写历史测试数字。

- [ ] **Step 2: 更新 Agent.md 不变量**

记录：

- LLM/VLM 配置必须来自同一个冻结 Job 快照。
- 成功生产 E2E 中 `llm_requests == 0` 必须失败。
- stub summary/tags 必须进入 `synthesize_manifest`。
- outage/invalid 请求数必须等于 `sample frames × stage attempts × per-frame retries`。

- [ ] **Step 3: 运行定向验证**

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_vlm_stage_runtime.py
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py -s
```

Expected：四条生产链全部通过，成功链 LLM 请求非零，失败链请求数精确匹配。

- [ ] **Step 4: 运行完整回归**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Expected：零失败、无新增 skip；`git diff --check` exit 0。

- [ ] **Step 5: 检查历史数据未变更**

```powershell
Get-Item data\task_state.db,data\quality_lab.db,data\library.db |
  Select-Object FullName,Length,LastWriteTime
```

同时确认正式导出、标签、Review 和 Preference Memory 未被测试修改。

- [ ] **Step 6: 编写第九次修复报告**

报告必须包含：

1. 修复前 `llm_requests=0`、空 summary/tags 的 RED 证据。
2. Job 数据库中冻结 `llm.model/base_url` 的证据。
3. 修复后 LLM 请求 path/model/count 以及 manifest summary/tags。
4. outage 与 invalid 的 sample frame 数、Stage attempt_count、HTTP request count 计算式和实值。
5. 四条 E2E、完整 pytest、compileall、`git diff --check` 汇总。
6. 历史数据、导出、标签和 Preference Memory 未变化证据。

- [ ] **Step 7: 提交文档**

```powershell
git add Agent.md STAGE_SPLIT_PRODUCTION_PATH_EIGHTH_FIX_REPORT_2026-07-18.md STAGE_SPLIT_PRODUCTION_PATH_NINTH_FIX_REPORT_2026-07-18.md
git commit -m "docs: close ninth stage split production gate"
```

---

## 发布门禁

只有全部满足后，后续 Agent 才能建议构建 EXE；构建与历史队列重跑仍为独立任务：

- [ ] 成功 E2E 的 LLM 配置来自冻结 Job config，而不是临时全局 YAML。
- [ ] 成功 E2E 无条件要求至少一次 LLM stub 请求。
- [ ] 请求 path/model 正确，summary/tags 进入 synthesize manifest。
- [ ] outage/invalid 的 VLM 请求数严格匹配 frame、Stage attempt 和 HTTP retry 数。
- [ ] 四条真实 Worker/Adapter/子进程 E2E 全部通过。
- [ ] 全仓 pytest、compileall、`git diff --check` 全部通过且无新增 skip。
- [ ] 历史数据库、导出、标签和 Preference Memory 完整保留。

## 建议执行方式

使用 `superpowers:subagent-driven-development` 串行执行 Task 1-3。Task 1 与 Task 2 都修改同一个 E2E 文件，不要并行实施；每个 Task 完成后分别进行规格符合性 Review 和代码质量 Review。
