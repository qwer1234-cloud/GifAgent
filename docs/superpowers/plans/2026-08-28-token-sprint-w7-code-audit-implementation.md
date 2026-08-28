# Token Sprint W7 — Directed Code Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).
>
> **FROZEN until T-1 gates pass.** Read-only：不改产品代码。Finding 必须落到 `file:line`，并附最小复现测试**草图**（可放报告，不必立刻变成绿测试；若草图足够短可新建 `tests/token_sprint/test_w7_{id}_sketch.py` 标 `pytest.mark.skip` 原因 `w7-sketch`）。真实 bug 不修。

**Goal:** 五份维度报告 + `w7-summary.md`（收入 W4 存活突变 / W6 ignore）。两个登记假设分别给结论与证据，**不预设定级**。

**Branch:** `sprint/w7-code-audit`.

**两假设（v4 修正）：** 生产 `candidate_search_fts` 为 0 行属实，但服务已 `degraded=True` 且诊断 `FTS index: 0/N...`，UI 展示（`app/services/library_search.py:156`、`app/ui/tabs/search.py:92`）。「静默无告警」**不成立**。须分别调查：

- **(a) 索引生命周期** — 生产索引为何从未构建（初始化 / 重建触发链路缺口）。
- **(b) 异常可观测性** — `_rank_with_text` 在 `library_search.py:449` 吞掉 `sqlite3.OperationalError` 是否应保留日志/指标。

定级以调查结论为准，不要在开写前写 P1。

---

## Constraints

### 全局约束（从设计文档原样复制）

1. **所有模型消耗只经 ZCode harness。** 不新增任何调用 GLM/智谱端点的运行时代码或脚本；
   不把活动额度 API Key 写进任何配置、代码或文档。
2. **T-1 验收前 W1–W8 禁止执行**（实施方案文本可撰写）。所有 W 分支必须从
   `sprint_base_sha` 创建，只合入 `sprint/integration`；未经用户确认不合回 `master`。
3. 不修改「与进行中重构的关系」列出的文件；管线符号只经 facade import。
4. **共享根传递协议：** `scripts/token_sprint/*` 所有脚本必须接受 `--sprint-root` 参数；
   未传参时才读环境变量 `GIFAGENT_SPRINT_ROOT` 作为显式 fallback；两者皆缺或路径不存在时
   立即报错退出。禁止依赖 PowerShell 专有语法；脚本须可在 Git Bash 与 cmd 下运行。
5. 不写根层 `data/*.db*`、`data/exports/`、`data/labels/`、checkpoint、`dist/`、真实桌面同步目录。
   产物只进共享根。消费跨 Workstream 产物前必须按 canonical manifest 校验 SHA-256。
6. **manifest 单写者 + review 哈希绑定协议：** executor 只原子发布产物与 `.ready`；
   reviewer 校验 `.ready.artifact_sha256` 与实际文件一致后，原子写 `.done` 与唯一 receipt。
   receipt 文件名使用 Windows 安全时间戳 `YYYYMMDDTHHMMSSfffZ` + UUID，内容至少包含
   `receipt_id`、`artifact_path`、`artifact_sha256`、`ready_path`、`ready_sha256`、`done_path`、
   `done_sha256`、producer、reviewer、workstream、ts。canonical `sprint_manifest.json` 只由
   协调会话在验证实际文件、`.ready`、`.done`、receipt 四者一致后原子重写；重复 receipt
   按 `receipt_id` 幂等处理。原子写的临时文件必须与目标文件同目录。
   此四方链只约束跨 Workstream 数据/工程产物；`sprint_manifest.json`、`inputs_manifest.json`、
   `approved_enrichment_manifest.json`、`usage-log.md` 与 receipt 归档状态属于 coordinator 控制面，
   由协调会话单写者原子维护，不为自身生成 receipt。canonical manifest 应记录其他控制面文件
   的 SHA 与 schema/revision，但不得递归记录自己的 SHA。
7. 不改 `configs/models.yaml` / `configs/models.adult_candidate.yaml`。
8. **依赖与 lockfile 由协调会话独占**：所有依赖决策必须在 T-1、`sprint_base_sha` 形成前
   完成；任何 Workstream 不得运行 `uv add` 或改 `pyproject.toml` / `uv.lock`
   （白名单：`hypothesis`、`mypy`、`cosmic-ray`）。基线形成后不得追加白名单依赖。
9. 测试只用 `tmp_path` / 内存 SQLite / 共享根内快照副本。新测试只进三个新目录：
   `tests/property/`、`tests/mutation/`、`tests/token_sprint/`；不改已有测试文件。
10. 数据分片包只处理**文本**。不让任何 agent 读取 GIF、JPG 或视频内容。
11. 分片纪律：每片 50–200 条；一片一个会话；输出独立
    `shard-XX.{pass}.{prompt_version}.{attempt_id}.jsonl`；`attempt_id` 使用 UUID，禁止覆盖旧产物；
    拒绝行记 `{"candidate_id": ..., "refused": true}`；合法 JSON 但成人语义被消毒的行
    视同失败（保真检查识别）。
12. 工作包状态：executor 只产绑定 artifact SHA 的 `.ready`；`.done` 只由 reviewer 对同一
    SHA 实跑测试/QC 后原子生成；reviewer 再发布同 SHA receipt。`.ready` 后禁止原地修改产物，
    红线与台账只统计 `.done`。
13. 用量记录：每类工作包完成后记录 ZCode 使用统计；每 2 小时抄录控制台余量。
    两者写入 `docs/reports/2026-08-token-sprint/usage-log.md`（协调会话独占）。
14. 用户可见文案中文，代码标识与测试名英文。commit 风格对齐仓库现状。
15. 每个 Workstream 在独立 git worktree + 分支（`sprint/w{N}-{slug}`）上工作，
    并作为独立 ZCode 工作区打开。

### W7 额外禁区

- 不修改 `app/` 产品代码（草图测试除外，且 skip）。
- 审计冻结区文件**可以读**，但不要提交对它们的补丁。
- 计划中不得出现 `uv add`。本 W 可不新增 token_sprint 脚本；若新增必须 `--sprint-root`。

---

## File Map

**Create**

- `docs/reports/2026-08-token-sprint/w7-audit-sqlite.md`
- `docs/reports/2026-08-token-sprint/w7-audit-concurrency.md`
- `docs/reports/2026-08-token-sprint/w7-audit-windows.md`
- `docs/reports/2026-08-token-sprint/w7-audit-http.md`
- `docs/reports/2026-08-token-sprint/w7-audit-resources.md`
- `docs/reports/2026-08-token-sprint/w7-summary.md`

**Optional create:** skipped sketch tests under `tests/token_sprint/`.

**Do not modify:** 产品文件、冻结区。

Finding 模板：

```markdown
### F-YYYYMMDD-NN
- Severity: (after evidence; unset until then)
- File:line:
- Evidence:
- Repro sketch:
- Related hypothesis: a / b / none
```

---

### Task 1: SQLite dimension

搜索 `busy_timeout` / `journal_mode` / `sqlite3.connect`：

已知锚点：`app/db.py:9-11`（timeout=30 + WAL + busy_timeout=30000）、`app/quality_lab/schema.py:152`、`app/task_engine/schema.py:349`、`app/task_engine/worker.py:767`（busy_timeout 但是否 WAL？）。

列出**不一致**：谁没 WAL、谁 timeout 秒 vs 毫秒混用、连接是否按请求关闭。W1 快照路径应对照，但不改 W1。

假设 (a)：从 UI/启动路径 grep `rebuild_index` 调用点。若生产 GUI 从不调用 → 记录链路缺口 file:line。不要写成「无告警」。

- [ ] 写 `w7-audit-sqlite.md`
- [ ] Commit `docs: W7 sqlite connection and FTS lifecycle audit`

---

### Task 2: Concurrency

读 `TaskWorker` lease/heartbeat（`app/task_engine/worker.py`）、`desktop_export_sync` 调度线程、Gradio 回调是否共享 conn/list。Finding 带最小竞态草图。

```bash
git add -- docs/reports/2026-08-token-sprint/w7-audit-concurrency.md
git commit -m "docs: W7 concurrency audit for worker lease and sync threads"
```

---

### Task 3: Windows

路径分隔符、`open(..., encoding=`、原子写（对照 coordinator `os.replace` 同目录规则）。结合 W3 mojibake 证据（若 W3 报告已 `.done`，先校验 SHA 再引用）。不要重复扫描媒体。

```bash
git commit -m "docs: W7 Windows path, encoding, and atomic-write audit"
```

---

### Task 4: HTTP

比较 `app/services/llm_client.py`（`timeout_s` 默认 120）、`app/services/embedding.py`（`httpx.Timeout` connect/read 分离、失败重试）、`app/ui/api_client.py`（默认 timeout 10）。表格式列出不一致。不改代码对齐。

假设 (b)：`_rank_with_text` except OperationalError: pass（`:449`）。结论选项必须显式写：保留静默 / 应 log / 应 metric。定级放调查之后。

```bash
git commit -m "docs: W7 HTTP timeout audit and FTS OperationalError observability"
```

---

### Task 5: Resource leaks

subprocess（ffmpeg、ollama）、文件句柄、`TemporaryDirectory` cleanup、worktree 外临时文件。`scripts/` 与 `app/` 都搜 `Popen`/`subprocess.run` 未超时者。

```bash
git commit -m "docs: W7 subprocess and file-handle leak audit"
```

---

### Task 6: Summary + W4/W6 registry

`w7-summary.md`：五维计数、两假设结论段落、W4 存活-as-bug、W6 ignore。Reviewer **每维度抽查 3 条**，误报率 < 1/3；把抽查表附在 summary。

报告目录作为 artifact → `.ready`；reviewer 抽查后 `.done` + receipt。

```bash
git add -- docs/reports/2026-08-token-sprint/w7-summary.md
git commit -m "docs: W7 audit summary with two FTS hypotheses resolved"
```

---

## Verification matrix

| Gate | Pass |
|------|------|
| 两假设 | 各有证据与结论，无「无告警 P1」预设 |
| file:line | 每条 finding 有 |
| 抽查 | 5×3，误报 <1/3 |
| 只读 | `git diff -- app/` 空 |
| degraded 事实 | 引用 library_search.py:156 与 search.py:92 |

---

## Done looks like

- 六份 markdown 在 git。
- 产品代码无 diff。

---

## Explicit non-goals

- 修复 finding。实现 FTS v2（W8）。改超时值。
