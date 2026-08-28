# Token Sprint W3 — Historical Data Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).
>
> **FROZEN until T-1 gates pass.** 输入一律读共享根 `inputs/` 下 T-1 不可变副本 + W1 快照（若已 `.done`）。**禁止**读 worktree 相对路径 `data/exports/`（worktree 中不存在）。规则先行；ZCode 只审疑难片。修复脚本只交付 `--dry-run`。

**Goal:** 纯规则扫出缺字段、分数越界、断链 `artifact_path`、编码乱码、时间戳矛盾；疑难 ≤100 条/片交模型定级；报告 `w3-data-audit.md`。

**Architecture:** `audit_data.py` 读 `inputs_manifest.json`，只打开 `status: copied` 的文件和（可选）校验过 SHA 的 `library_snapshot.db`。媒体文件不读；`artifact_path` 只做路径存在性检查（`Path.exists`），不 `open` GIF。

**Branch:** `sprint/w3-data-audit`.

**Mojibake 线索：** 仓库根若存在 `version2_*.md`，只把其当作编码问题的**文本样例**（读 md，不读媒体）。

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

### W3 额外禁区

- 不读 `data/exports/` worktree 相对路径。不写生产库。不 `--apply` 修复。
- glob 到 `.gif/.jpg/.mp4` 必须跳过，不得交给模型。
- 计划中不得出现 `uv add`。不改冻结区。

---

## File Map

**Create**

- `scripts/token_sprint/audit_data.py`
- `scripts/token_sprint/repair_data_dry_run.py` — 只打印将执行的 SQL/JSON patch，默认 `--dry-run` 且**没有** `--apply` 开关
- `tests/token_sprint/test_audit_data.py`
- `docs/reports/2026-08-token-sprint/w3-data-audit.md`

**Do not modify:** coordinator、W1 脚本（可 import common）、产品代码。

---

### Task 1: Scanner + tests

**CLI:**

```text
python scripts/token_sprint/audit_data.py --sprint-root <path> [--use-snapshot]
```

启动时：

1. 读 `inputs_manifest.json`，对每个 `required: true` 条目 `status` 必须 `copied` 且副本 SHA 匹配，否则 exit 2。
2. 每个 `copied` 文件重算 SHA-256，与清单不一致 → exit 2（fail closed）。
3. `absent` 可选源：报告一节 `missing_optional`，继续。
4. `--use-snapshot`：校验 `library_snapshot.db` SHA 对得上 canonical W1 artifact；只读查询文本列。

规则（JSON 结果 / sqlite 行）：

| id | check |
|----|--------|
| `missing_field` | 约定键缺失或 null（candidate_id, timestamps） |
| `score_oob` | 分数不在 `[0,1]` 且非 null（gif_worthiness / final_score / sex_act） |
| `broken_artifact` | `artifact_path` 非空且 `Path(p).exists()` 为 False（不打开文件） |
| `mojibake` | 文本含 `Ã`/`å`/`ä` 典型 UTF-8 被当成 Latin-1 的 trigram，或 `decode('utf-8')` 失败 |
| `ts_contradiction` | `end_ts < start_ts`、`updated_at < created_at`（可解析 ISO 时） |

疑难：规则命中但置信低（例如 summary 仅含少量 Latin 扩展字符）写入 `shards/w3-hard.{attempt_id}.jsonl`，每片 ≤100。**不自动**开模型；Task 3 才审。

测试 `tmp_path`：伪造 inputs_manifest + 坏 JSON + 好 JSON；断言副本 SHA 被篡改时 exit 2；断言扫描器不 `open` 后缀 `.gif`（可用 monkeypatch 监视）。

- [ ] **Step 1–2:** `uv run pytest -q tests/token_sprint/test_audit_data.py`
- [ ] **Step 3: Commit** `feat: rule-based audit over T-1 immutable input copies`

---

### Task 2: dry-run repair script

对每类 finding 打印建议 patch（JSON pointer 或 SQL `UPDATE` 文本）。拒绝执行。`--sprint-root` 必填。测试：跑完不改变输入文件 mtime/size。

```bash
git add -- scripts/token_sprint/repair_data_dry_run.py tests/token_sprint/test_audit_data.py
git commit -m "feat: dry-run-only historical data repair suggestions"
```

---

### Task 3: Real scan + optional LLM hard cases

```bash
python scripts/token_sprint/audit_data.py --sprint-root "$SPRINT_ROOT" --use-snapshot
```

写 `w3-data-audit.md`：覆盖率（清单每一 key 的 copied/absent）、finding 计数、样例 id。

若有疑难片且额度允许：ZCode 定级（P0/P1/P2/ignore）。Reviewer 抽查 **20 条** LLM 结论，误报必须为 0（验收门：抽查 20 条无误报）。连续误报 → 停模型、只保留规则结果。

T0 与本 W 无强制绑定；但成人文本保真：定级时不得把显式词当乱码删掉。

```bash
git add -- docs/reports/2026-08-token-sprint/w3-data-audit.md
git commit -m "docs: W3 historical data audit report"
```

产物 JSON 走 `.ready`/`.done`/receipt。

---

## Verification matrix

| Gate | Pass |
|------|------|
| 只读副本 | 测试：相对 `data/exports` 路径不存在时仍成功（用 copies） |
| SHA fail-closed | 篡改副本 → exit 2 |
| 全覆盖 | 报告列出 inputs_manifest 每一 key |
| 抽查 | 20 条 LLM 无误报（若未跑 LLM：写明 N/A，规则结果仍交付） |
| 无 apply | 无 `--apply` 代码路径（`rg apply repair_data` 仅注释/拒绝） |

---

## Done looks like

- 扫描器可在无生产 exports 的 worktree 运行。
- 报告 + dry-run 修复脚本在 git；数据副本仍在共享根。

---

## Explicit non-goals

- 自动修复写入。读 GIF。修产品代码。
