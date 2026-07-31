# DeepSeek Coop Agent 通信故障报告 — 2026-07-31

## 1. 报告用途

本文面向负责修复 DeepSeek 协作通信代码的 Agent。目标不是继续本次前端重构，而是提供足够完整的故障证据、根因、修复约束和验收测试，使修复者不必重新推断现场。

需要重点检查的实现：

- `C:\Users\sunhao\.codex\deepseek-coop\coop.ps1`
- `C:\Users\sunhao\.codex\deepseek-coop\DeepSeekCoop.psm1`
- `C:\Users\sunhao\.agents\skills\deepseek-coop\SKILL.md`

本报告位于 GifAgent 仓库，仅作为可版本化的故障交接文档。通信实现本身位于用户目录，不在本仓库内。

## 2. 结论摘要

这不是单一的 DeepSeek API 不可用事件。三次尝试暴露了一个组合故障：

1. 协调器把完整 Markdown Prompt 作为命令行参数传递，经过嵌套 PowerShell 时可能被再次解析和拆分。
2. 协调器同步等待子进程结束并把全部 stdout 缓存在内存中，因此运行期间没有真正的流式状态、日志、`thread_id` 持久化或纠偏通道。
3. 即使输出已经包含 `thread.started`，只要最终退出码非零，调用方也会在保存 `thread_id` 前直接标记失败，丢失可恢复会话。
4. Worker 默认继承完整的 Codex 协调者人格、技能和上下文。缺少明确角色边界时，DeepSeek 会把自己当成协调者并再次调用协作流程。
5. 没有“首次有效编辑”期限、阶段预算、重复探索检测或运行中止后的恢复协议。
6. 任务契约没有在派发前通过真实依赖 API 预检。第三次运行因此在发现 Gradio API 与计划冲突后陷入长时间依赖分析。
7. 第二次运行还出现了 WebSocket 认证失败和 HTTPS 流中断，但它是一次具体传输故障，不是全部三次失败的共同根因。

首要根因是协调器属于“同步批处理 CLI 包装器”，而不是“可观察、可恢复、可纠偏的 Agent 通信协议”。

## 3. 影响

| 影响项 | 结果 |
| --- | --- |
| 用户请求 | 前端重构未实施 |
| 主仓库 | 保持干净，未合并错误代码 |
| 原分支 | `feat/adult-adaptive-clip-tuning` |
| 原始 HEAD | `b89917fc8ce98b233a30dc64fbbd6611ed73dab8` |
| DeepSeek 业务 diff | 三次均为零 |
| 已验证测试 | 第三次 Worker 在隔离工作树中跑通修改前的 78 项目标测试 |
| 协调器最终状态 | 第三次残留为 `implementing`，`thread_id`、日志、结果和测试路径均未登记 |
| 数据安全 | 没有删除运行时数据，没有提交或合并隔离工作树 |

## 4. 当前通信路径

```text
GPT Desktop coordinator
        |
        | powershell coop.ps1 implement
        v
Invoke-CoopWorker
        |
        | codex exec -p deepseek ... FULL_MARKDOWN_PROMPT
        v
DeepSeek worker process
        |
        | stdout JSONL and stderr
        v
$rawLines in coordinator memory
        |
        | only after worker exits
        +--> write log
        +--> search thread.started
        +--> save state
        +--> run tests
```

关键问题是下半段只有在 Worker 完整退出后才发生。运行期间磁盘状态不包含真实进展。

## 5. 故障时间线

### 5.1 尝试一：Prompt 参数被 PowerShell 拆分

| 项目 | 值 |
| --- | --- |
| Run ID | `responsive-ui-layout-20260731-224618` |
| 结果 | `blocked` |
| Worker 退出码 | `2` |
| Thread ID | `null` |
| 业务 diff | 无 |

直接错误：

```text
error: unexpected argument 'tests statement ...' found
Usage: codex exec [OPTIONS] [PROMPT]
```

完整计划经嵌套 PowerShell 传递后，其中的引号和换行影响了 CLI 参数边界。第一次规避方式是把计划改写为不含单双引号，但这只是绕过症状，不是可靠修复。

证据：

- `C:\Users\sunhao\AppData\Local\Codex\deepseek-coop\runs\responsive-ui-layout-20260731-224618\state.json`
- `C:\Users\sunhao\AppData\Local\Codex\deepseek-coop\runs\responsive-ui-layout-20260731-224618\logs\deepseek-01.jsonl`

### 5.2 尝试二：角色递归、认证失败和流中断

| 项目 | 值 |
| --- | --- |
| Run ID | `responsive-ui-layout-r2-20260731-224853` |
| 结果 | `blocked` |
| Worker 退出码 | `1` |
| 协调器最终 Thread ID | `null` |
| JSONL 实际 Thread ID | `019fb8b6-65a8-7231-9616-c795f7acc4a2` |
| 业务 diff | 无 |

实际 JSONL 第一条业务事件已经是：

```json
{"type":"thread.started","thread_id":"019fb8b6-65a8-7231-9616-c795f7acc4a2"}
```

但协调器最终把 `thread_id` 记录为 `null`。原因有两个：

1. `Invoke-CoopWorker` 要等 Worker 退出后才扫描 `$rawLines`。
2. `Invoke-Implementation` 在退出码非零时先进入失败分支；将 `$worker.ThreadId` 赋给状态的语句位于失败分支之后，永远不会执行。

因此即使会话已经建立，瞬时传输故障也无法通过同一 Thread 恢复。

本次还观察到：

```text
401 Unauthorized: Missing bearer or basic authentication in header
url: wss://api.openai.com/v1/responses
```

CLI 随后从 WebSocket 回退 HTTPS，但连续五次出现：

```text
stream disconnected before completion
```

同时，Worker 在已有上下文中把自己理解为协调者或 Reviewer，并尝试再次启动 `deepseek-coop`，产生递归协调倾向。说明模型连接曾成功建立，但 Worker 角色隔离失败。

证据：

- `C:\Users\sunhao\AppData\Local\Codex\deepseek-coop\runs\responsive-ui-layout-r2-20260731-224853\state.json`
- `C:\Users\sunhao\AppData\Local\Codex\deepseek-coop\runs\responsive-ui-layout-r2-20260731-224853\logs\deepseek-01.jsonl`
- `C:\Users\sunhao\.codex\sessions\2026\07\31\rollout-2026-07-31T22-49-40-019fb8a7-0024-7073-b5d3-e2f716adff48.jsonl`

### 5.3 尝试三：通信正常，但无进展治理

| 项目 | 值 |
| --- | --- |
| Run ID | `responsive-ui-layout-r3-20260731-230849` |
| 停止前持续时间 | 约 20 分钟 |
| 协调器状态 | `implementing` |
| 协调器 Thread ID | `null` |
| Worker Session ID | `019fb8b9-6010-78e0-9705-908cb01b23ac` |
| 基线测试 | `78 passed in 9.25s` |
| 业务 diff | 无 |

第三次 Prompt 明确规定：

- Worker 只负责实现；
- 禁止调用 `deepseek-coop`、Codex、其他 Agent 或委派；
- 禁止检查协调器状态；
- 必须直接编辑当前工作树并运行测试。

该约束成功消除了递归协调。Worker 能够读取源码、执行 PowerShell、安装依赖并运行测试，证明 DeepSeek 模型、工具调用和文件读取链路可用。

随后 Worker 实测发现：

- Gradio 版本为 `6.18.0`；
- `gr.Blocks.__init__` 没有计划声称的 `css_paths` 参数；
- `gr.Row.__init__` 没有计划声称的 `wrap` 参数；
- `gr.Gallery.columns` 的公开类型为 `int | None`，不是字符串 `auto`。

这些发现是有价值的，但 Worker 此后不断搜索和反编译 Gradio Gallery 前端，约 20 分钟仍未产生第一次业务编辑。由于协议没有阶段预算、无 diff 告警或运行中纠偏入口，只能在操作系统层终止进程。

终止后：

- 协调器状态仍为 `implementing`；
- `thread_id`、`result_path`、`log_path`、`test_path` 和退出码仍为空；
- 协调器运行目录没有本次完整 JSONL；
- 只有 Codex 全局 Session 日志保留了真实执行过程；
- 隔离工作树无业务改动。

证据：

- `C:\Users\sunhao\AppData\Local\Codex\deepseek-coop\runs\responsive-ui-layout-r3-20260731-230849\state.json`
- `C:\Users\sunhao\.codex\sessions\2026\07\31\rollout-2026-07-31T23-09-44-019fb8b9-6010-78e0-9705-908cb01b23ac.jsonl`

## 6. 代码级根因

### RC-1：Prompt 使用命令行参数传输

严重级别：**P0**

`coop.ps1` 当前把完整 Prompt 直接追加到参数数组：

```powershell
$arguments += $Prompt
```

该设计对直接调用可能有效，但无法保证在嵌套 PowerShell、`.ps1` 包装器、换行、引号、反引号、中文、Unicode 和超长 Prompt 下保持字节级参数边界。

### RC-2：stdout 全量缓冲，缺少实时事件循环

严重级别：**P0**

当前实现：

```powershell
$rawLines = @(& $CodexExecutable @arguments 2>&1 |
    ForEach-Object { [string]$_ })
```

这会阻塞到进程结束。之后才：

- 写日志；
- 解析 `thread.started`；
- 更新运行状态；
- 返回控制权。

因此 `status` 命令无法报告真实进展，也无法支持运行中 Review、纠偏、取消或恢复。

### RC-3：非零退出时丢弃已经收到的 Thread ID

严重级别：**P0**

调用方先判断：

```powershell
if ($worker.ExitCode -ne 0 -or
    [string]::IsNullOrWhiteSpace($worker.ThreadId)) {
    # mark blocked and return
}
```

之后才执行：

```powershell
$state.thread_id = $worker.ThreadId
```

因此任何“已经建立 Thread，但后来传输中断”的运行都会丢失恢复句柄。

### RC-4：Worker 角色和上下文未最小化

严重级别：**P1**

`codex exec -p deepseek` 继承了完整 Codex 基础说明和可用技能。若任务 Prompt 只描述实施计划而没有明确 Worker 角色，DeepSeek 可能把自己理解成主协调者，执行：

- 重新检查协作技能；
- 再次初始化 Worktree；
- 再次启动 DeepSeek；
- 代替实现进行 Review。

### RC-5：缺少进展协议和失速检测

严重级别：**P1**

当前没有：

- `phase`；
- `last_event_at`；
- `last_tool_call_at`；
- `first_edit_at`；
- 当前 diff 统计；
- 重复命令检测；
- 阶段预算；
- 运行中告警或纠偏。

所以“Agent 活着但没有完成任务”无法与“Agent 正常编码”区分。

### RC-6：传输错误分类不足

严重级别：**P1**

当前把非零退出统一标记为 `worker_failed`。至少需要区分：

- `authentication_failed`：401、缺少认证头；
- `transport_disconnected`：流中断，可尝试恢复；
- `rate_limited`；
- `prompt_rejected`；
- `worker_process_crashed`；
- `worker_cancelled`；
- `worker_stalled`；
- `thread_id_missing`。

认证失败不应盲目重试；已取得 Thread ID 的流中断应优先恢复同一 Thread。

### RC-7：派发前未验证任务契约

严重级别：**P1**

实现计划要求了当前 Gradio 版本不存在的参数。通信系统本身不需要理解 Gradio，但应支持派发前的项目预检阶段，并把预检结果作为 Worker 的确定输入，避免 Worker 在实现阶段重新研究依赖契约。

## 7. 修复设计要求

### 7.1 安全传递 Prompt

推荐顺序：

1. 使用 stdin，将 Codex CLI Prompt 参数设为 `-`；
2. 或使用明确支持的 `--prompt-file`，如果当前 CLI 提供；
3. 不得把完整 Prompt 拼成 PowerShell 命令字符串；
4. 不得要求调用方删除引号来规避问题。

必须支持：

- CRLF 和 LF；
- 单引号和双引号；
- PowerShell 反引号；
- Markdown 代码块；
- 中文和 Emoji；
- 至少 256 KiB Prompt；
- 末尾无换行的文件。

### 7.2 实时 JSONL 事件循环

使用 `.NET System.Diagnostics.Process` 或等价机制：

- `UseShellExecute = false`；
- 重定向 stdin、stdout、stderr；
- stdout 到达一行就立即追加到运行日志；
- 每一行独立解析 JSON；
- 收到 `thread.started` 立即原子持久化 `thread_id`；
- stderr 单独记录，不得混入 JSONL 解析流；
- 定期刷新文件，进程被杀时仍保留已接收事件；
- 保留最近一个无法解析的原始事件用于诊断。

### 7.3 可恢复状态机

建议状态：

```text
initialized
  -> starting
  -> running
  -> awaiting_review
  -> revising
  -> approved
  -> merged

running
  -> reconnecting
  -> interrupted
  -> stalled
  -> blocked
  -> cancelled
```

状态至少包含：

```text
process_id
process_started_at
thread_id
phase
last_event_at
last_progress_at
first_edit_at
worker_exit_code
transport
retry_count
result_path
log_path
test_path
last_error.code
last_error.retryable
```

若进程异常退出但 `thread_id` 已知，状态应进入 `interrupted` 或 `reconnecting`，而不是丢弃 Thread 后直接 `blocked`。

### 7.4 最小化 Worker 契约

Worker Prompt 前置固定角色头，至少包含：

```text
You are the implementation worker in an existing isolated worktree.
Do not invoke coordinators, agents, skills, worktrees, or delegation.
Do not commit, merge, push, stash, reset, or switch branches.
Directly inspect the scoped files, edit them, run the exact tests, and finish.
```

更可靠的方案是为 Worker 提供独立、最小的 system prompt 或 profile，不注入：

- 主协调者的人格；
- `deepseek-coop` 技能；
- Thread 管理能力；
- 与实现无关的插件和技能目录；
- 原始主 Agent 的 review 职责。

### 7.5 进展与失速治理

建议默认阈值，可配置：

| 条件 | 动作 |
| --- | --- |
| 2 分钟无事件 | 状态标记 `quiet`，检查进程存活 |
| 5 分钟无第一次业务编辑 | 发送一次 Worker 纠偏消息 |
| 连续 10 次只读工具调用且无 diff | 标记 `analysis_loop` |
| 10 分钟无业务 diff | 暂停并请求协调者决定继续或终止 |
| 同一命令或等价搜索重复 3 次 | 记录重复探索告警 |
| 进程退出但 Thread 已知 | 自动尝试同 Thread 恢复一次 |

进展不能仅以“仍有 token 或工具调用”判断。至少要同时观察：

- Git diff；
- 测试状态；
- Worker 明确阶段；
- 新增或修改文件；
- 是否产生可审查结果。

### 7.6 传输恢复策略

- 401 和缺少认证头：立即失败，错误码 `authentication_failed`，不要重复五次相同请求。
- WebSocket 失败但 HTTPS 可用：记录 transport 降级，继续同一 Thread。
- HTTPS stream disconnect 且 Thread 已知：使用原 Thread 恢复。
- 重试使用指数退避和总预算。
- 每次重试保留原始错误、transport、次数和 Thread ID。
- 不得打印或保存 API Key。

### 7.7 中断和清理

提供正式的 `cancel` 或 `interrupt` 命令：

1. 验证 PID 和进程启动时间属于当前 Run；
2. 终止 Worker 及其子进程；
3. 刷新已有日志；
4. 保存最后已知 Thread ID；
5. 记录 `cancelled_at` 和操作者原因；
6. 将状态置为 `cancelled` 或 `interrupted`；
7. 保留 Worktree，不自动删除；
8. 再次调用具有幂等性。

不得依赖人工枚举并强制终止进程。

### 7.8 派发前预检

协调流程应允许主 Agent 在启动 Worker 前提供一份机器生成的 preflight：

- 当前依赖版本；
- 关键 API 签名；
- 基线测试结果；
- Git clean 状态；
- 可写目录；
- 精确测试命令；
- 已确认和待确认的实现约束。

Preflight 失败时不要消耗 Worker 尝试次数。

## 8. 建议实现结构

```text
coop.ps1
  parse command and emit final envelope only

DeepSeekCoop.psm1
  RunStateStore
  WorkerProcess
  JsonlEventReader
  ProgressMonitor
  RetryPolicy
  Cancellation
  Redaction

WorkerProcess
  start process
  write prompt to stdin
  stream stdout JSONL
  stream stderr text
  persist thread immediately
  expose cancel handle

ProgressMonitor
  observe heartbeat
  observe git diff
  detect analysis loop
  publish status snapshots
```

业务状态与进程状态应分开。`running` 不等于“有业务进展”，`process exited` 也不等于“Thread 不可恢复”。

## 9. 必须增加的测试

### 9.1 Prompt 传输测试

使用假的 Codex CLI 回显 stdin，覆盖：

- 多行 Markdown；
- 单双引号；
- 反引号；
- `$()`；
- 中文路径；
- Emoji；
- 代码围栏；
- 256 KiB 以上 Prompt。

断言 Worker 收到的内容与源文件逐字节一致。

### 9.2 实时事件测试

假的 CLI 分三次输出：

1. `thread.started`；
2. 延迟后输出 tool event；
3. 再延迟后退出。

在进程退出前断言：

- 日志文件已经存在；
- `thread_id` 已保存；
- `last_event_at` 已更新；
- `status` 能看到 `running`。

### 9.3 非零退出恢复测试

假的 CLI 先输出 `thread.started`，再以退出码 1 结束。

断言：

- Thread ID 不丢失；
- 状态为 `interrupted`；
- 错误标记为可恢复；
- 后续 `resume` 使用同一 Thread。

### 9.4 认证错误测试

模拟 WebSocket 401：

- 分类为 `authentication_failed`；
- 不执行无意义的同错误重试；
- 不泄漏认证信息；
- 保留 stderr 和结构化错误摘要。

### 9.5 流中断测试

模拟 HTTPS stream disconnect：

- 已知 Thread 时恢复同一 Thread；
- 未知 Thread 时进入明确的不可恢复状态；
- 重试次数和退避可观察；
- 达到预算后停止。

### 9.6 角色隔离测试

给 Worker 一个包含 `deepseek-coop` 字样的实现任务，断言：

- 不启动新的 coordinator；
- 不创建新的 Worktree；
- 不调用 Agent 或技能；
- 第一个可写动作发生在当前 Worktree。

### 9.7 失速检测测试

假的 Worker 连续发出只读工具事件但不产生 diff：

- 达到阈值后产生 `analysis_loop`；
- 协调器可发送一次纠偏；
- 继续无 diff 时进入 `stalled`；
- Worktree 和日志被保留。

### 9.8 中断恢复测试

运行中终止协调器或 Worker：

- 状态不能永久残留在无 PID 的 `implementing`；
- 下次 `status` 能识别孤儿状态；
- 可安全标记 `interrupted`；
- 已写日志和 Thread ID仍可用；
- `cancel` 重复调用安全。

### 9.9 端到端测试

使用本地 fake worker 完成：

```text
init
-> implement
-> streaming progress
-> test
-> awaiting_review
-> revise with same thread
-> test
-> finalize
```

同时验证原分支 HEAD 稳定检查、Worktree 保留策略和日志脱敏。

## 10. 修复验收标准

以下条件必须全部满足：

- Prompt 含任意合法 Markdown、中文和 PowerShell 特殊字符时仍能逐字节到达 Worker。
- `thread.started` 出现后 1 秒内写入 `state.json`。
- Worker 运行期间日志持续增长，`status` 能报告真实阶段和最近事件。
- Worker 非零退出时，已获得的 Thread ID不会丢失。
- 流中断可以恢复同一 Thread，不会无条件消耗全新尝试。
- 401 被准确分类，不伪装成普通 stream disconnect。
- Worker 无法再次调用 `deepseek-coop` 或创建嵌套协调流程。
- 无业务 diff 的分析循环能在预算内被发现并进入可解释状态。
- 正式中断后状态不残留为无进程的 `implementing`。
- 所有失败路径保留 Worktree、日志和结果，不泄漏密钥。
- `implement`、`revise`、`cancel` 和恢复操作具备明确幂等语义。
- 自动化测试覆盖参数传输、流事件、Thread 恢复、认证错误、失速和中断。

## 11. 建议修复顺序

1. **P0：** stdin Prompt 传输。
2. **P0：** 实时 stdout/stderr 读取和 JSONL 持久化。
3. **P0：** 收到 `thread.started` 立即保存，并在非零退出时保留。
4. **P0：** `cancel`、孤儿进程识别和中断状态恢复。
5. **P1：** 最小 Worker profile 和递归协调防护。
6. **P1：** 传输错误分类及同 Thread 恢复。
7. **P1：** 阶段预算、无 diff 告警和分析循环检测。
8. **P2：** 派发前依赖预检和状态可视化。

不要先修 UI 或调整重试次数。若底层仍是全量缓冲和退出后解析，增加重试只会重复消耗时间并继续丢失恢复信息。

## 12. 复现建议

修复 Agent 不应直接使用真实 DeepSeek API做第一轮验证。先实现一个 fake Codex CLI：

```text
fake-codex
  reads prompt from stdin
  emits configurable JSONL events with delays
  emits stderr independently
  supports configurable exit code
  can hang until cancelled
```

先用 fake runner 验证所有状态机和故障注入，再进行一次最小真实 DeepSeek Smoke Test：

1. 在临时 Git 仓库初始化一个 Run；
2. 要求 Worker 修改一个文本文件；
3. Prompt 必须包含中文、引号、代码块和 PowerShell 特殊字符；
4. 运行中查询 `status`；
5. 确认 Thread ID 和日志实时出现；
6. 确认产生 diff；
7. 执行测试；
8. 不执行真实合并，除非人工 Review 通过。

## 13. 现场保留说明

三个 Run 目录均已保留在：

```text
C:\Users\sunhao\AppData\Local\Codex\deepseek-coop\runs\
```

第三次运行的完整过程只能从全局 Session 日志读取，因为协调器在被终止前没有刷新自己的 `$rawLines`：

```text
C:\Users\sunhao\.codex\sessions\2026\07\31\
rollout-2026-07-31T23-09-44-019fb8b9-6010-78e0-9705-908cb01b23ac.jsonl
```

修复和测试期间不要删除这些现场文件。它们可用于验证新解析器能否正确重放已有事件，但在测试输出或提交内容中不得复制认证信息、环境变量或其他秘密。

## 14. 非目标

本次通信修复不包括：

- GifAgent 前端响应式重构；
- 修改 Gradio 页面代码；
- 重新执行或合并第三次 Worker 的工作树；
- 增加 DeepSeek 最大实施尝试次数；
- 通过删除引号、缩短 Prompt 或放宽验收条件规避协议缺陷。

