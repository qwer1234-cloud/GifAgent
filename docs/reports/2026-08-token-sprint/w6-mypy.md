# Token Sprint W6 — mypy strict 白名单清零报告

- 分支：`sprint/w6-mypy`（worktree `.worktrees/sprint-w6-mypy`，基于 master `b31eb794`）
- executor：w6-executor；配置 `[tool.mypy]` 由 coordinator 落盘，本分支 `git diff pyproject.toml uv.lock` 为空（已验证）。
- 执行日期：2026-08-29
- 工具版本：mypy 2.3.1（`uv run mypy`，strict=true，files=app/quality_lab + app/task_engine + app/services）

## 1. 基线与终态

| 指标 | 基线（master b31eb794） | 终态（本分支 b4fbf316） |
|------|------|------|
| `uv run mypy` 总错误 | **297** errors / 66 files | **101** errors / 18 files |
| 白名单范围内错误 | 196 | **0** |

### 按模块清零明细（范围内 → 0）

| 模块 | 基线 | 终态 | commit |
|------|------|------|--------|
| ① `app/quality_lab/`（全部） | 24 | 0 | `14206d06` types: strict mypy for app/quality_lab (24 errors -> 0) |
| ② `app/services/`（配置排除名单之外，33 文件） | 136 | 0 | `a458545c` types: strict mypy for app/services whitelist (136 errors -> 0) |
| ③ `app/task_engine/`（排除 artifacts*，9 文件） | 36 | 0 | `b4fbf316` types: strict mypy for app/task_engine whitelist (36 errors -> 0, artifacts excluded) |
| `app/ui/` | — | 跳过（按计划） | — |

### 剩余 101 条的归属（全部在白名单/冻结区之外）

`exclude` 只阻止文件作为源被枚举，**不阻止 import-following**；以下文件因被白名单内模块导入而被 mypy 跟随检查，其错误不属于 W6 修改范围（冻结区/非白名单包，一律未改动）：

| 来源 | 错误数 | 说明 |
|------|--------|------|
| `app/task_engine/artifacts/*` | 39（quality_schema 25, manifests 10, resolve 4） | 重构冻结 / 包拆分中（exclude） |
| `app/services/embedding.py` 等 exclude 名单 | 21（embedding 11, library_search 5, reranker 2, preference_events 2, preference_memory 1） | coordinator exclude 名单，冻结 |
| `app/quality_moe/*` | 32（evaluator 9, config 9, repair 8, policy 2, 其余各 1） | 不在 `files` 白名单，import-following 带出 |
| `app/db.py` / `app/config.py` | 6 / 3 | 不在 `files` 白名单，import-following 带出 |

移交建议（W7/coordinator 裁定）：若要求 `uv run mypy` 全仓 clean，需要 coordinator 在 `[tool.mypy]` 追加 per-module `follow_imports = "skip"`/`"silent"` override 或扩 `files`，W6 无权修改配置。

## 2. 只加注解不改行为 — 方法与等价性说明

全部改动为类型注解/注解性中间变量，无 if/return/逻辑变更。三处需说明的最小非注解文本变更（语义逐位等价）：

1. `app/services/temporal_evidence.py:107`：`np.float32([[1,0,0],[0,1,0]])` → `np.array([[1,0,0],[0,1,0]], dtype=np.float32)`。两者产生完全相同的 float32 2x3 ndarray（形状/数值/dtype 一致），仅类型桩可推断。供 W7 复核。
2. `app/task_engine/adaptive_adapter.py:262-264`：循环内局部变量 `key` 更名 `pair_key`——原 `key` 由 L257 海象表达式绑定在函数作用域（tuple[tuple[str,int,int],float,int,int]），与后续 5 元组键冲突；纯局部重命名，零运行时差异。
3. `app/task_engine/worker.py:869`：`_after_stage(result)` 参数注解为 `StageResult | None`——调用点 `result` 在异常路径确为 None（L743/L757 初始化），注解忠实于运行时真实类型；方法体为 no-op。

另有两处 `object` → `Any` 参数注解放宽（duck-typed 转换器，接收任意 JSON 标量，运行时不变）：`transition_guard.py` 的 `value()` 返回类型与 `_finite_or_none()`、`gif_windows.py:_finite_number()`、`gif_encode.py:nearest_divisible_gif_fps()`、`stage_timing.py:observe_vlm(eval_count)`。

一处消除潜在误导注解（对齐真实行为）：`quality.py` 两个 `validate_*` 返回 `tuple[dict | None, ...]` → `tuple[dict[str, Any], ...]`（函数无条件返回 dict，调用方均不判 None）。

## 3. `# type: ignore[code]` 登记表（移交 W7）

W6 新增 4 处（详见 `w6-type-ignores.md`）：

| # | file:line | code | 原因 |
|---|-----------|------|------|
| 1 | app/services/batch_queue.py:62 | attr-defined | typeshed 仅提供 POSIX fcntl 桩；该分支由 `os.name == "nt"` 运行时守卫，win32 平台检查报 `flock/LOCK_EX/LOCK_NB` 不存在 |
| 2 | app/services/batch_queue.py:112 | attr-defined | 同上（`flock/LOCK_UN`） |
| 3 | app/services/stage_timing.py:41 | exit-return | `__exit__` 恒返回 False（从不吞异常）；mypy 2.3 要求改注解为 `-> None` 并把 `return False` 改 `return None`——涉及改 return 语句（红线），保留 `-> bool` + ignore |
| 4 | app/services/temporal_evidence.py:101 | call-overload | `cv2.calcOpticalFlowPyrLK(..., None)`：OpenCV 运行时接受 prevPts=None（标准用法），随附 cv2 类型桩未建模该可选参数 |

同时删除 2 处既有失效 ignore（mypy 2.3 下 error code 失配）：`gif_encode.py:88 [arg-type]`、`stage_timing.py:81 [arg-type]`——分别以 `Any` 参数注解替代，行为不变。

既有（非 W6 引入）保留：`app/task_engine/stages.py:31 [return-value]`（范围文件内，真实抑制，未动）。

## 4. pytest 原文数字（每模块清零后均跑全量 `uv run pytest -q tests/ --no-header`）

- master 基线（主工作区全量）：`4 failed, 2155 passed, 2 skipped in 251.37s`
- quality_lab 清零后：`4 failed, 2155 passed, 2 skipped in 319.20s`（另一次 268.17s）
- services 清零后：`4 failed, 2155 passed, 2 skipped in 241.72s`
- task_engine 清零后：`4 failed, 2155 passed, 2 skipped in 228.31s`

4 个失败与 master 完全一致，均为**预存失败**（与 W6 改动无关）：
- `tests/test_ollama_loopback_config.py::test_ollama_defaults_use_ipv4_loopback`
- `tests/test_preference_preflight.py::test_preference_memory_status_reports_safe_defaults`
- `tests/test_preference_preflight.py::test_status_handles_missing_manifest`
- `tests/test_preference_preflight.py::test_status_handles_malformed_manifest`

## 5. 验收门核对

| 门 | 结果 |
|----|------|
| 配置所有权（pyproject.toml/uv.lock 无 diff） | 通过 |
| 冻结区（artifacts*、preference_*、embedding、reranker、library_search、workbench_schema、app/pipeline、app/ui） | 无 diff |
| 无行为变化 | pytest 数字与 master 一致 |
| ignore 全部登记 | 4 处新增全部登记 W7 文件 |
| 每模块单独 commit | 3 个 commit（14206d06 / a458545c / b4fbf316），另报告 commit |
