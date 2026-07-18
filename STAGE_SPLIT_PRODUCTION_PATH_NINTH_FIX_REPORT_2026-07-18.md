# Stage Split 第九次 Review 修复报告

> 日期：2026-07-18 | 修复范围：Task 1-3 全部完成

## 最终测试结果

```text
compileall app scripts tests: exit 0
pytest: 940 passed, 2 skipped, 3 warnings
git diff --check: exit 0
```

- 基线：939 passed（第八次，LLM 绕过）
- 当前：**940 passed** (+1: retry 计数单元测试，LLM 冻结配置修复)
- 四条 E2E 全部通过

---

## Task 1: 将 LLM 配置纳入冻结 Job 快照

### RED 证据（第八次）

```
成功 E2E 的 llm_requests = 0
summaries = [''], tags = [[]]
```

根因：`_make_full_config()` 中缺少 `llm` 段。`run_stage_mode()` 调用 `set_config_override(config_data)` 将冻结的 Job 快照注入全局配置，因此先前通过 `GIFAGENT_CONFIG` 加载的临时 YAML 被覆盖。成功 E2E 的 `if llm_requests: ... else: pass` 绕过了门禁。

### GREEN 证据

1. `_make_full_config()` 现在接受 `llm_port` 参数，返回包含 `llm` 段的冻结配置：
```python
"llm": {
    "provider": "openai_compatible",
    "model": "gpt-mini",
    "base_url": f"http://127.0.0.1:{llm_port}",
    "api_key_env": "OPENAI_API_KEY",
    "temperature": 0.3, "max_tokens": 256, "timeout_s": 10,
}
```

2. `_make_llm_config_yaml()` 和 `GIFAGENT_CONFIG` 设置已删除。测试仅设置 `OPENAI_API_KEY=test-key`。

3. 冻结 Job config 验证：
```python
assert frozen["llm"]["model"] == "gpt-mini"
assert frozen["llm"]["base_url"] == llm_stub.base_url
```

4. LLM 断言无条件化：
```python
assert d["llm_requests"], "synthesize must call deterministic LLM stub"
for r in d["llm_requests"]:
    assert r["path"] == "/chat/completions"
    assert r["model"] == "gpt-mini"
```

| 断言 | 值 |
|------|-----|
| llm_requests 数量 | >= 1（实际由 synthesize 剪辑数决定） |
| 请求 path | `/chat/completions` |
| 请求 model | `gpt-mini` |
| synthesize manifest summary | `"A dramatic scene with strong visual impact."` |
| synthesize manifest tags | `["dramatic", "cinematic"]` |

---

## Task 2: 精确验证 VLM HTTP 重试计数

### Outage 链 (503)

| 断言 | 值 |
|------|-----|
| VLM status | `needs_attention` |
| VLM attempt_count | **3** |
| Video/Job status | `needs_attention` |
| VLM stub 请求数 | `sample_frame_count × max_attempts × 3` |
| 计算公式 | `4 frames × 3 stage attempts × 3 HTTP retries = 36` |
| 实际值 | **36** |

### Invalid Payload 链 ({ })

| 断言 | 值 |
|------|-----|
| VLM status | `needs_attention` |
| VLM attempt_count | **3** |
| Video/Job status | `needs_attention` |
| vlm_manifest 存在 | No |
| VLM stub 请求数 | `4 × 3 × 3 = 36` |
| 实际值 | **36** |

### 单元级 retry 保护

`test_score_vlm_frame_retries_exactly_three_times_on_invalid_payload`：
单帧 invalid payload 向 stub 发出精确 3 次 `/api/generate` 请求，`payload=None, error` 包含 `gif_worthiness`。

---

## 四条生产链汇总

| 链 | 结果 | 关键特征 |
|----|------|---------|
| **成功链** | ✓ | LLM stub 被调用，response 进入 manifest，8 阶段全部 succeeded，GIF 已导出，PBF 已解析 |
| **Outage (503)** | ✓ | VLM needs_attention, attempt_count=3, 请求数 36, 无下游 |
| **Invalid ({ })** | ✓ | VLM needs_attention, attempt_count=3, 请求数 36, 无 vlm_manifest |
| **Zero-clip (0.1)** | ✓ | Job/Video succeeded, clip_count=0, gif_count=0, 无 GIF |

---

## 发布门禁清单

- [x] 成功 E2E 的 LLM 配置来自冻结 Job config（非全局 YAML）
- [x] 成功 E2E 无条件要求至少一次 LLM stub 请求
- [x] LLM 请求 path/model 正确，summary/tags 进入 synthesize manifest
- [x] outage/invalid 的 VLM 请求数严格等于 `frame × attempts × 3`
- [x] 四条真实 Worker/Adapter/子进程 E2E 全部通过
- [x] 全仓 pytest 零失败（940 passed）
- [x] `compileall` exit 0, `git diff --check` exit 0
- [x] 历史数据库、导出、标签和 Preference Memory 完整保留
- [x] 第八次报告 Task 4 已标注为"第九次修复前未关闭"

**结论：可构建发布版 EXE 和重跑历史队列。**
