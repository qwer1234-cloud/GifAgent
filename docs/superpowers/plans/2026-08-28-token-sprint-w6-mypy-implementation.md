# Token Sprint W6 — mypy strict (Standby Pool) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).
>
> **DO NOT START by default.** 启动条件（协调会话裁定）：T+24h 额度红线未达（剩余 > 55%）**且**放大器 ① 加数据分片 ② 并行会话 ③ 扩 W4 模块 ④ 全包 reviewer 复核 均已用尽。T-1 未验收、或条件未写进 canonical manifest `w6_enabled: true` 时本计划冻结。`mypy` 包已在 `sprint_base_sha`；**`[tool.mypy]` 必须由协调会话落盘**，本 W 提请 coordinator 提交配置，不得自己改 `pyproject.toml` / `uv.lock`。

**Goal:** 白名单模块 mypy strict 清零；只加注解不改行为；`# type: ignore[code]` 位置登记 W7。每模块清零后单独 commit。

**Branch:** `sprint/w6-mypy`（仅启用时从 `sprint_base_sha` 创建）。

**范围顺序：**

1. `app/quality_lab/`（全部）
2. `app/services/` 排除：`preference_memory.py`、`preference_*.py`、`candidate_vectors.py`、`reranker.py`、`embedding.py`、以及 W8 所属 `library_search.py`、`workbench_schema.py`
3. `app/task_engine/` 排除 `artifacts*`（重构冻结 / 包拆分中）
4. `app/ui/` **跳过**

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

### W6 额外禁区

- 不改运行时行为（禁止为过 mypy 而改 if/return）。
- 不注解冻结区文件。不碰 `library_search.py` / `workbench_schema.py`（W8）。
- **不得** `uv add` 或编辑 `pyproject.toml`。需要 `[tool.mypy] strict = true` 时写一份 patch 文本交给 coordinator 提交。
- 每日收盘由 coordinator 合入 `sprint/integration`。

---

## File Map

**Modify (whitelist modules only):** 加 `from __future__ import annotations`（若尚无）与类型注解。

**Create:** `docs/reports/2026-08-token-sprint/w6-mypy.md`、`docs/reports/2026-08-token-sprint/w6-type-ignores.md`（给 W7）。

**Coordinator-owned change (request, don't land):** `pyproject.toml` `[tool.mypy]`。

建议配置（交给 coordinator）：

```toml
[tool.mypy]
python_version = "3.11"
strict = true
files = ["app/quality_lab"]
exclude = ["app/ui", "app/pipeline"]
```

`files` 随模块清零扩大；不要一开始扫全仓。

---

### Task 0: Enable gate

读 manifest。无 `w6_enabled: true` → 打印「W6 standby, not started」并 **exit 0 without code changes**。

---

### Task 1: Request mypy config

把上述 TOML 片段发给 coordinator。等待 `sprint/integration` 出现该 commit 后再 rebase/merge 进本分支。本 Task 无产品 diff。

---

### Task 2: `app/quality_lab/` module-by-module

顺序：`metrics.py` → `calibration.py` → `promotion.py` → `schema.py` → 其余。每文件：

```bash
uv run mypy app/quality_lab/metrics.py
uv run pytest -q tests/quality_lab/test_metrics.py
git add -- app/quality_lab/metrics.py
git commit -m "types: strict mypy for quality_lab.metrics"
```

`type: ignore` 必须带 error code，并追加到 `w6-type-ignores.md`（file:line, code, reason）。

---

### Task 3: `app/services/` whitelist

跳过排除名单。优先无冻结、文件较小者：`clip_merge.py`、`grid_select.py`、`taste_map.py`、`export_ranking.py`、`timeline.py`、`json_guard.py`、`collections.py`、`desktop_export_sync.py`、`narrative_curation.py`。

每模块独立 commit + 该模块现有测试。全量 `uv run pytest -q` 在每日本收盘前跑一次（W6 验收门）。

---

### Task 4: `app/task_engine/` minus artifacts

`repository.py`、`fingerprints.py`、`worker.py` 若时间不够按红线裁剪。`artifacts*` 跳过。

---

### Task 5: Report

`w6-mypy.md`：清零模块列表、剩余错误、ignore 表。Reviewer 抽查 ignore 是否掩饰真实 any 泄漏。

---

## Verification matrix

| Gate | Pass |
|------|------|
| 启动条件 | manifest `w6_enabled` |
| 无行为变化 | 相关 pytest 不回归 |
| 配置所有权 | `git diff pyproject.toml` 在 W6 分支为空（除非 coordinator merge 进来） |
| 冻结区 | 无 diff |
| ignore | 全部登记 W7 文件 |

---

## Done looks like

- 白名单 mypy 清零或报告剩余。
- 产品行为测试绿。

---

## Explicit non-goals

- 注解 UI。改逻辑过类型。自己改 lockfile。默认启动。
