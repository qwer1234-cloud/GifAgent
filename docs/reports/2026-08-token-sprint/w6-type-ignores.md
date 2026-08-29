# W6 type: ignore 登记（移交 W7 线索）

W6 分支 `sprint/w6-mypy` 在白名单范围内新增的 `# type: ignore[code]` 共 4 处。
W7 审查建议：抽查各处是否掩饰真实 `Any` 泄漏（1、2 为平台类型桩差异，3 为协议红线取舍，4 为三方桩缺口，均无 Any 流入调用方）。

| # | file:line | code | 原因 | W7 处置建议 |
|---|-----------|------|------|-------------|
| 1 | app/services/batch_queue.py:62 | attr-defined | typeshed 的 fcntl 桩仅存在于 POSIX 平台；该 `else` 分支由 `os.name == "nt"` 在运行时守卫（POSIX 才执行）。win32 平台检查时报 `flock` / `LOCK_EX` / `LOCK_NB` 不存在（一行 3 个错误共用一个 ignore） | 可改 `if sys.platform != "win32":` 结构或 per-module override；非 Any 泄漏 |
| 2 | app/services/batch_queue.py:112 | attr-defined | 同上（`flock` / `LOCK_UN`，release 路径） | 同上 |
| 3 | app/services/stage_timing.py:41 | exit-return | `_Span.__exit__` 恒返回 False（注释明确"never swallow the body's exception"）。mypy 2.3 认为恒 False 的 `__exit__` 应注解 `-> None`；但把 `return False` 改 `return None` 触碰"不改 if/return"红线，故保留 `-> bool` + ignore | 如红线放宽，可将返回类型改 `-> None` 并 `return None`（行为等价，with 语句仅做真值判断），即可删 ignore |
| 4 | app/services/temporal_evidence.py:101 | call-overload | `cv2.calcOpticalFlowPyrLK(previous.gray, current.gray, points, None)`：OpenCV 运行时接受 `prevPts=None`（首帧标准用法）；项目随附的 cv2 类型桩未把第 4 参数建模为可选 | 升级/修正 opencv 类型桩后可删；非 Any 泄漏 |

## 既有 ignore（非 W6 引入，未改动）

| file:line | code | 说明 |
|-----------|------|------|
| app/task_engine/stages.py:31 | return-value | 范围文件内的真实抑制（`StageOutcome` 收敛校验路径），保留 |
| app/quality_moe/evaluator.py:230, 418；app/routers/candidates.py:502, 780；app/services/preference_events.py:114；app/routers/workbench.py:376；app/ui/tabs/control.py:505 等 | misc | 白名单/冻结区外，不在 W6 范围 |

## W6 删除的失效 ignore（mypy 2.3 下 code 失配，改用注解修复）

| file:line | 旧 code | 处置 |
|-----------|---------|------|
| app/services/gif_encode.py:88 | arg-type | 参数注解 `fps: object` → `Any`，删除 ignore |
| app/services/stage_timing.py:81 | arg-type | 参数注解 `eval_count: object` → `Any`，删除 ignore |
