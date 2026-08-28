# Token Sprint W8 — Shadow FTS v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).
>
> **FROZEN until T-1 gates pass.** **执行时机：** W1 试验集产出且 approved enrichment manifest 存在之后，在**快照/开发库**上开发与演练。对生产库执行在冲刺窗口外、重构 P1–P3 门禁通过后由**用户手动**触发。本计划不得对 `dist/.../library.db` 写入。
>
> Canonical 输入是 approved **不可变 JSONL shards**（版本 + 分片哈希）。**禁止**以 `enrichment.db` 为 canonical，也不得用其中同名行覆盖 approved shard 内容。

**Goal:** additive v2 FTS，幂等全量重建；开关 `GIFAGENT_SEARCH_FTS_V2=1` 缺省关时现有搜索测试全绿。双次构建后行数、distinct `candidate_id`、固定查询集结果哈希三者一致；篡改任一获批 shard 字节后构建 **fail closed**。

**Architecture:** coordinator 维护 `approved_enrichment_manifest.json`（W1 pass 过门后，reviewer 签署的精确清单）。`build_fts_v2.py` 只消费该清单：逐文件重算 SHA，按 `(pass_id, prompt_version, prompt_sha256, shard_id, sha256)` 五元组验收后解析行。内容写入 `fts_v2_content`（PK `candidate_id`），然后单事务 `DELETE` v2 FTS 再重灌。`LibrarySearchService` 仅当 env 置位时读 v2 表。

**已知坑：** FTS5 `candidate_id UNINDEXED` **无唯一约束**，`INSERT OR REPLACE` 会留重复行（已验证）。因此 v2 必须「内容表驱动 + 全量 DELETE 重灌」，禁止对 FTS 虚表 upsert。

**Branch:** `sprint/w8-shadow-fts`. 合入 integration 需**全量 pytest** 门。

**File Map 均不在重构冻结区：** `workbench_schema.py`、`library_search.py` 允许本 W 修改。

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

### W8 额外禁区

- 不以 `enrichment.db` 为输入；构建器不得 `SELECT` 该库来填 FTS。
- 不写生产库；`--db` 必须指向快照或 tmp。
- 缺省行为不变：未设 env 时 `tests/test_library_search.py` 等现有测试全绿（允许跑，不修改那些测试文件）。
- 计划中不得出现 `uv add`。回滚 = 关开关或 `DROP` v2 表。
- 改 prompt / 改 shard 必须新 attempt 路径，不得原地 patch 已 approved 文件。

---

## File Map

**Modify**

- `app/services/workbench_schema.py` — additive DDL only
- `app/services/library_search.py` — env 开关读 v2；缺省路径不改

**Create**

- `scripts/token_sprint/build_fts_v2.py`
- `tests/token_sprint/test_search_fts_v2.py`
- `docs/reports/2026-08-token-sprint/w8-shadow-fts.md`

---

## DDL (lock)

加在 `FTS5_DDL` 之后或独立 `FTS_V2_DDL`，`apply_search_schema` 调用 `executescript`（幂等 `IF NOT EXISTS`）：

```sql
CREATE TABLE IF NOT EXISTS fts_v2_content (
    candidate_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    tags TEXT NOT NULL,
    source_path TEXT NOT NULL,
    enrichment_manifest_sha TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS candidate_search_fts_v2 USING fts5(
    candidate_id UNINDEXED,
    summary,
    tags,
    source_path,
    tokenize='porter unicode61'
);
```

不要给 v2 FTS 加 UNIQUE（FTS5 做不到）。唯一性只在 `fts_v2_content`。

每次构建：

```sql
BEGIN;
DELETE FROM candidate_search_fts_v2;
INSERT INTO candidate_search_fts_v2(candidate_id, summary, tags, source_path)
  SELECT candidate_id, summary, tags, source_path FROM fts_v2_content;
COMMIT;
```

先重建 `fts_v2_content`（按 candidate_id upsert），再灌 FTS。

---

## `approved_enrichment_manifest.json` (consumed, not authored here)

由 **coordinator** 写入。本脚本只读。形状：

```json
{
  "schema": "gifagent.token_sprint.approved_enrichment.v1",
  "passes": [
    {
      "pass_id": "p3",
      "prompt_version": "v1",
      "prompt_sha256": "...",
      "shards": {"shard-000": "<sha256>", "shard-probe": "<sha256>"}
    }
  ]
}
```

任一 shard 文件缺失、SHA 不符、`.done` 缺失、receipt 中 `artifact_sha256` 不符 → **整个构建拒绝**（exit 2），不要部分索引。

`prompt_sha256` 必须等于 `scripts/token_sprint/prompts/{pass_id}/{prompt_version}.md` 现算值（W1 `prompts.py`）。

---

### Task 1: DDL + default-off tests

**Files:** `app/services/workbench_schema.py`, `tests/token_sprint/test_search_fts_v2.py`（先测 schema 存在、env 未设时 `LibrarySearchService.search` 仍查 `candidate_search_fts`）。

不要改 `tests/test_library_search.py`。本 Task 结束后：

```bash
uv run pytest -q tests/test_library_search.py tests/token_sprint/test_search_fts_v2.py
```

Expected: 现有测试绿；新测试证明 v2 表可创建且默认搜索不用它。

```bash
git add -- app/services/workbench_schema.py tests/token_sprint/test_search_fts_v2.py
git commit -m "feat: additive FTS v2 content table and virtual index schema"
```

---

### Task 2: `build_fts_v2.py` fail-closed inputs

**CLI:**

```text
python scripts/token_sprint/build_fts_v2.py --sprint-root <path> --db <snapshot-or-tmp.db>
```

拒绝 `--db` 指向 `inputs_manifest.source_db_abs` 或 `dist/` 下路径（字符串前缀检查 + `resolve` 后比较）。

步骤：读 approved manifest → 对每个 shard `sha256_file` → 校验 `.done` + receipt 链（import `protocol.verify_hash_chain`）→ 解析 JSONL 文本行，映射到 `summary/tags`（按 pass 合并规则：P3 keywords 追加进 tags 文本；P2 scene 进 summary；冲突时 **JSONL 后写 wins 但必须同一 shard_sha 来源**；不同 shard 同一 candidate 同一 pass 以 manifest 清单顺序，文档化）。写入 content 表 `enrichment_manifest_sha` = approved 文件 SHA。然后单事务重灌 FTS v2。

测试（全 `tmp_path`）：

1. 合法链 → 构建成功，`COUNT(*) == COUNT(DISTINCT candidate_id)`。
2. 改 shard 一个字节 → exit 2，FTS v2 行数不变（或不存在）。
3. 缺 `.done` → exit 2。
4. 提供 `enrichment.db` 但故意不放 JSONL → 仍失败（证明没走 db）。
5. **双次构建：** 连续跑两遍，行数、distinct id、对固定查询 `MATCH 'test'` 的 `candidate_id` 有序列的 SHA-256 三者一致。

```bash
uv run pytest -q tests/token_sprint/test_search_fts_v2.py
git add -- scripts/token_sprint/build_fts_v2.py tests/token_sprint/test_search_fts_v2.py
git commit -m "feat: build FTS v2 from approved immutable JSONL shards only"
```

---

### Task 3: `library_search.py` env switch

读 `os.environ.get("GIFAGENT_SEARCH_FTS_V2") == "1"` 时，`_rank_with_text` 的 FROM 改为 `candidate_search_fts_v2`（表不存在则 degraded 诊断明确 `FTS v2 missing`，不要吞掉所有错误到空结果而不诊断）。

缺省：行为与现在完全一致，包括 OperationalError 吞掉（那是 W7 范围，本 W 不「顺便」加 log，以免扰乱缺省测试）。

```bash
GIFAGENT_SEARCH_FTS_V2= uv run pytest -q tests/test_library_search.py tests/test_library_search_api.py
# Git Bash:
GIFAGENT_SEARCH_FTS_V2=1 uv run pytest -q tests/token_sprint/test_search_fts_v2.py
```

未 export 变量时现有测试必须绿。

```bash
git add -- app/services/library_search.py tests/token_sprint/test_search_fts_v2.py
git commit -m "feat: opt-in GIFAGENT_SEARCH_FTS_V2 reader for shadow index"
```

---

### Task 4: Snapshot drill (after W1 trial)

当 approved manifest 有真实 shard：对 `library_snapshot.db` **复制一份** `library_snapshot_fts_v2.db`（不要写原快照，以免破坏 W2）。`--db` 指向副本。记录三一致哈希到 `w8-shadow-fts.md`。`.ready`/`.done`/receipt。

**不要**对生产库跑。

```bash
git add -- docs/reports/2026-08-token-sprint/w8-shadow-fts.md
git commit -m "docs: W8 shadow FTS snapshot drill evidence"
```

---

### Task 5: Full pytest gate before merge

```bash
uv run pytest -q
```

Expected: PASS。然后请 coordinator 合入 `sprint/integration`。

---

## Verification matrix

| Gate | Pass |
|------|------|
| 双次构建三一致 | 测试锁死 |
| 溯源 | 每行 `enrichment_manifest_sha` + shard 五元组 |
| 篡改 fail closed | 测试 |
| 缺省关 | 现有 library_search 测试绿 |
| 不用 enrichment.db | 测试 |
| 非生产 db | CLI 拒绝 dist/ 源路径 |

---

## Done looks like

- 开关缺省关，v2 可在快照上幂等构建。
- W2 可开新 `pool_version` 标差集（W2 计划）；本 W 不标 qrels。

---

## Explicit non-goals

- 生产 apply。重算 vectors。修 FTS v1 无唯一约束（只在 v2 规避）。开开关当默认。
