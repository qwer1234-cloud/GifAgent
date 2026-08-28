# Token Sprint W1 — Enrichment Toolchain + Sharded Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).
>
> **FROZEN until T-1 gates pass.** Do not run any Task until canonical `sprint_manifest.json` contains `sprint_base_sha` and the coordinator has created branch `sprint/w1-enrichment` from that SHA. If T0 (Task 7) fails any of the three content gates, **cancel all model-annotation passes**; still deliver the deterministic tools (Tasks 1–6).

**Goal:** 交付零模型消耗的快照 / 分片 / QC / merge 工具链；在快照上强制全量重建 FTS（先清空 FTS 与 `search_index_state`）；用 60 条探针过 T0 内容与保真门后再爬坡分片富化。获批 JSONL shards 是 W8 的 canonical 输入；`enrichment.db` 只是带 `shard_sha256` 的可重建缓存。

**Architecture:** 只读 `inputs_manifest.json` 钉死的生产库绝对路径 → `VACUUM INTO` 共享根快照 → SQL 清空 FTS/state → 调用现有 `LibrarySearchService.rebuild_index()`（增量续跑，所以必须先清）。分层导出文本分片；ZCode 会话按版本化 prompt 产出 JSONL；executor 发 `.ready`，reviewer 发 `.done` + receipt；merge 只吃 `.done` 分片。不改 `library_search.py` 产品代码。

**Tech Stack:** Python 3.11+、stdlib sqlite3、现有 `LibrarySearchService` / `json_guard.parse_json_response`。无新依赖。无直连模型 API。

**Companion:** `docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md` v4.1。Coordinator: `2026-08-28-token-sprint-coordinator-implementation.md`。

**Branch / worktree:** `sprint/w1-enrichment` @ `.worktrees/sprint-w1-enrichment`。合并只进 `sprint/integration`。

**Shell:** Git Bash 优先。每次调用传 `--sprint-root`。共享根：`C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint`。

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

### W1 额外禁区

- 不修改 `app/services/library_search.py`（快照重建只调用现有函数 + 快照库上的 SQL 清理）。产品 FTS 改动属 W8。
- 不写生产库。不 `VACUUM INTO` 到根层 `data/`。
- 不内置模型调用。prompt 即兴改写禁止；改模板 = 新建 `prompt_version` 并重过 QC 门。
- 不碰 `scripts/token_sprint/coordinator/`。import coordinator 的 `common` / `protocol` 允许。
- 不把 `enrichment.db` 标成 W8 canonical。merge 前必须验证 `.done.artifact_sha256`、receipt 与 shard 实际哈希一致。
- T0 任一门失败：取消 P1a/P1b/P2/P3/P4/P5 的模型标注；Tasks 1–6 的工具与测试仍须交付。
- 计划中不得出现 `uv add`。

---

## File Map

**Create**

- `scripts/token_sprint/snapshot_library.py`
- `scripts/token_sprint/make_shards.py`
- `scripts/token_sprint/validate_shard.py`
- `scripts/token_sprint/merge_shards.py`
- `scripts/token_sprint/prompts.py` — `prompt_sha256(pass_id, prompt_version)`
- `scripts/token_sprint/prompts/p3/v1.md`
- `scripts/token_sprint/prompts/p1a/v1.md`
- `scripts/token_sprint/prompts/p1b/v1.md`
- `scripts/token_sprint/prompts/p2/v1.md`
- `scripts/token_sprint/prompts/p4/v1.md`（Stage C 才用，可先提交骨架）
- `scripts/token_sprint/prompts/p5/v1.md`（同上）
- `tests/token_sprint/test_snapshot_fts_rebuild.py`
- `tests/token_sprint/test_make_shards.py`
- `tests/token_sprint/test_validate_shard.py`
- `tests/token_sprint/test_merge_shards.py`
- `docs/reports/2026-08-token-sprint/w1-snapshot.md`（Task 2 后）
- `docs/reports/2026-08-token-sprint/w1-t0-probe.md`（Task 7 后）

**Runtime (shared root, not in git)**

- `library_snapshot.db`
- `enrichment.db`
- `tag_vocab_v1.json`
- `shards/shard-XXX.input.jsonl`
- `shards/shard-XXX.{pass}.{prompt_version}.{attempt_id}.jsonl` + `.ready` / `.done`

**Do not modify:** `app/services/library_search.py`, `app/services/workbench_schema.py`, coordinator 包, `pyproject.toml`.

---

## Sidecar schema (lock)

`enrichment.db` 由 `merge_shards.py` 创建：

```sql
CREATE TABLE IF NOT EXISTS candidate_enrichment (
    candidate_id      TEXT NOT NULL,
    pass_id           TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    output_json       TEXT NOT NULL,
    shard_id          TEXT NOT NULL,
    shard_sha256      TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (candidate_id, pass_id, prompt_version)
);
CREATE TABLE IF NOT EXISTS enrichment_refusals (
    candidate_id   TEXT NOT NULL,
    pass_id        TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    shard_id       TEXT NOT NULL,
    shard_sha256   TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'refused',
    PRIMARY KEY (candidate_id, pass_id, prompt_version)
);
```

`reason` 仅 `'refused' | 'sanitized'`。

---

## Snapshot rebuild contract (lock)

`snapshot_library.py` 顺序不得改：

1. 读 `<sprint-root>/inputs_manifest.json`，校验其 SHA 与 canonical manifest `control_plane` 记录一致（若 canonical 尚无该字段则至少重算文件 SHA 并写入 W1 报告）。
2. `source_db_abs = Path(manifest["source_db_abs"]).resolve(strict=True)`。只读打开确认是 SQLite。
3. 目标 `library_snapshot.db` 若已存在：拒绝覆盖，要求 `--force` 才删后重建。
4. 对源库执行 `VACUUM INTO '<snapshot-abs>'`。失败（database is locked 等）→ 打印中文「请先关闭 GifAgent GUI 后重试」，exit 2。不要 fallback 到 Python 逐表拷贝（避免静默不一致）。
5. 打开快照（WAL + `busy_timeout=30000` + `row_factory=Row`）。
6. **先** `DELETE FROM candidate_search_fts;`（FTS5 虚表允许 DELETE）以及 `DELETE FROM search_index_state;`，`commit`。
7. `LibrarySearchService(conn).rebuild_index()`。
8. 验收：`errors == 0` 且
   `SELECT COUNT(*) FROM candidate_search_fts` ==
   `SELECT COUNT(DISTINCT candidate_id) FROM candidate_search_fts` ==
   `SELECT COUNT(*) FROM candidate_gifs`。
9. 打印快照 SHA-256 与三计数。W1 会话写绑定快照 SHA 的 `.ready`；reviewer 重跑计数后写 `.done` + receipt。后续一切计数以快照为准。

`rebuild_index()` 是基于 `search_index_state.last_candidate_id` 的增量续跑（`library_search.py:225`）。不清空就会在「FTS 为空但 state 已完成」时直接 `scanned=0` 返回。这是 `test_snapshot_fts_rebuild.py` 要锁死的回归。

---

## Phase 0 — Deterministic tools (zero model tokens)

### Task 1: FTS-empty-but-state-complete regression lock

**Files:**

- Create: `tests/token_sprint/test_snapshot_fts_rebuild.py`

本 Task **先写测试**。测试构造内存/tmp sqlite：`candidate_gifs` 有 N≥3 行，`apply_search_schema`，把 `search_index_state` 写成 `last_candidate_id` = 最大 id、`indexed_count=N`，但 **不插入** FTS 行（或 FTS 为空）。

然后调用将抽出的核心函数 `force_rebuild_fts(conn) -> RebuildReport`（先写在测试旁的 import；Task 2 把函数放到 `snapshot_library.py`）。

断言：报告 `errors == 0`；三计数相等且等于 N。对比：若只调 `rebuild_index()` **而不**先 DELETE，则 FTS 仍为 0（证明回归锁不是假绿）。

- [ ] **Step 1: 写测试。** 复用 `tests/test_library_search.py` 的 `_insert_candidate` 思路，但不要修改该文件；把最小 insert helper 写在本测试内。

- [ ] **Step 2: 此时实现尚未落地，测试应 FAIL 在 import。** 若你先做 Task 2 再跑，也可以；顺序上允许 Task 1–2 同一提交，但仍须包含「不清空则跳过」的对照断言。

- [ ] **Step 3: 不单独提交。** 与 Task 2 一起提交。

---

### Task 2: `snapshot_library.py`

**Files:**

- Create: `scripts/token_sprint/snapshot_library.py`
- Modify: `tests/token_sprint/test_snapshot_fts_rebuild.py`（改为从脚本 import `force_rebuild_fts`）

**CLI:**

```text
python scripts/token_sprint/snapshot_library.py --sprint-root <path> [--force]
```

抽出 `force_rebuild_fts(conn)` 供测试：DELETE FTS + DELETE state + `LibrarySearchService(conn).rebuild_index()` + 三计数断言（失败 raise `RebuildContractError`）。

脚本入口：读 inputs_manifest → VACUUM INTO `<sprint-root>/library_snapshot.db` → `force_rebuild_fts`。禁止 import `app.pipeline.*` 内部。禁止读 GIF。

`--sprint-root` 经 `scripts.token_sprint.coordinator.common.resolve_sprint_root`。

- [ ] **Step 1: 实现 + 让 Task 1 测试变绿。** 另加：`tmp_path` 上用最小 sqlite 文件测「拒绝覆盖已存在快照，除非 --force」。不要对真实生产库跑本测试。

- [ ] **Step 2:**

```bash
uv run pytest -q tests/token_sprint/test_snapshot_fts_rebuild.py
```

Expected: PASS。对照路径证明不清空会跳过；清空路径三计数一致。

- [ ] **Step 3: Commit**

```bash
git add -- scripts/token_sprint/snapshot_library.py tests/token_sprint/test_snapshot_fts_rebuild.py
git commit -m "feat: snapshot library FTS rebuild clears state before incremental resume"
```

---

### Task 3: `make_shards.py` including `--probe`

**Files:**

- Create: `scripts/token_sprint/make_shards.py`, `tests/token_sprint/test_make_shards.py`

**CLI:**

```text
python scripts/token_sprint/make_shards.py --sprint-root <path> [--probe] [--size 100] [--out-prefix shard]
```

输入：`<sprint-root>/library_snapshot.db`（必须存在）。字段只导出文本：

```json
{"candidate_id": "...", "vlm_summary_json": "...", "tags_json": "...", "scenario_keys_json": "...", "final_score": 0.0, "stratum": "high|mid|low"}
```

禁止导出 `artifact_path` / `preview_path` 指向的媒体；路径字段不要写入分片。

分层（探针与试验集共用）：用快照 `final_score` 三分位 + tags/summary 是否含显式词表（内置小写英文词表常量 `EXPLICIT_MARKERS`，至少含常见成人行为词；词表本身是代码常量，不是模型产出）。三层各尽量 20 条组成 `--probe` 的 60 条；不足时从相邻层补齐并在 stderr 声明，不得静默少生成。

常规模式：按层 round-robin 切成 50–200 条/片，写入 `shards/shard-XXX.input.jsonl`。XXX 零填充 3 位。禁止覆盖已有 input 片（已存在 → exit 2，换 prefix 或加 `--force`）。

- [ ] **Step 1: 实现。** 用 `tmp_path` 造快照库测：probe 恰好 60 行、三层标签都出现；size=50 时每片 50–200；缺快照 exit 2。

- [ ] **Step 2:**

```bash
uv run pytest -q tests/token_sprint/test_make_shards.py
```

Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add -- scripts/token_sprint/make_shards.py tests/token_sprint/test_make_shards.py
git commit -m "feat: export stratified text-only shards and 60-row T0 probe"
```

---

### Task 4: `validate_shard.py`

**Files:**

- Create: `scripts/token_sprint/validate_shard.py`, `tests/token_sprint/test_validate_shard.py`

**CLI:**

```text
python scripts/token_sprint/validate_shard.py --sprint-root <path> --shard <path> --pass-id p3 --prompt-version v1 --input-shard <path>
```

输出 QC JSON（stdout + 同目录 `<shard>.qc.json` 原子写）：

```json
{
  "n": 0,
  "schema_ok_rate": 0.0,
  "refuse_rate": 0.0,
  "fidelity_rate_high_stratum": null,
  "suspected_sanitized": [],
  "errors": []
}
```

规则：

- 每行必须是 JSON object。合法 JSON 用 `app.services.json_guard.parse_json_response` 不是必须（行已是 JSONL），但模型原始输出若先落成非 JSONL 文本，校验前用它。本工具假设输入已是 JSONL。
- 拒绝行：`{"candidate_id": "...", "refused": true}` 计入 refuse，不算 schema 失败。
- pass schema（v1）：
  - `p3`：`keywords` 为 3–12 个小写英文 token 的 list
  - `p1b`：`tags` 为 list[str]，且 ⊆ `tag_vocab_v1.json`（若词表文件尚不存在则跳过子集检查并在 QC 标 `vocab: missing`）
  - `p2`：`scene` object，键至少 `setting`, `act`, `finish`（值允许 null）
  - `p1a`：本 pass 的输出是词表聚合，不走逐 candidate schema；validate 对 p1a 文件检查词形 `^[a-z0-9_]+$`
- **语义保真启发式（高显式层）：** 对 input 行 `stratum=="high"` 或 input 文本含 `EXPLICIT_MARKERS` 的样本，检查 output 字符串（keywords/tags/scene 序列化）是否仍保留至少一个 marker 或同义 stem。零保留 → 记入 `suspected_sanitized`，该行视同失败（计入 fidelity 分母）。合法 JSON 但被消毒 = 失败，不是成功。
- T0 门（探针）：拒绝率 ≤ 15%；schema 合法率 ≥ 95%；高显式层保真率 ≥ 90%（脚本只算启发式；人工复核在 Task 7 记录）。

- [ ] **Step 1: 实现 + 测试：** 消毒行（input 含 explicit、output 全 euphemism）必须 fail；纯 refused 行计入 refuse 且不拉低 schema_ok；非法 schema 拉低 schema_ok。

- [ ] **Step 2:**

```bash
uv run pytest -q tests/token_sprint/test_validate_shard.py
```

Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add -- scripts/token_sprint/validate_shard.py tests/token_sprint/test_validate_shard.py
git commit -m "feat: shard QC for schema, refusals, and sanitization heuristic"
```

---

### Task 5: `merge_shards.py` + sidecar cache

**Files:**

- Create: `scripts/token_sprint/merge_shards.py`, `tests/token_sprint/test_merge_shards.py`

**CLI:**

```text
python scripts/token_sprint/merge_shards.py --sprint-root <path>
```

只合并同时满足的分片：存在 `.done`；`.done.artifact_sha256` == 实际文件 SHA；`receipts/processed/` 或 `receipts/` 中有绑定同一 SHA 的 receipt。缺一则跳过并 stderr 列出。插入每行时写入 `shard_sha256`。拒绝/消毒行进 `enrichment_refusals`。可重复执行：主键冲突时以新 shard 为准（replace），因为 canonical 仍是 JSONL 而非 db。

重建检查测试：丢掉 `enrichment.db`，只从 `.done` JSONL 再 merge，行集合（candidate_id, pass_id, prompt_version, shard_sha256）一致。

- [ ] **Step 1: 实现。** 测试用 `protocol.write_ready/write_done/write_receipt` 在 `tmp_path` 走四方链。未 `.done` 的分片不得出现在 db。

- [ ] **Step 2:**

```bash
uv run pytest -q tests/token_sprint/test_merge_shards.py
```

Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add -- scripts/token_sprint/merge_shards.py tests/token_sprint/test_merge_shards.py
git commit -m "feat: merge only .done shards into rebuildable enrichment cache"
```

---

### Task 6: Versioned prompt templates (v1)

**Files:** 上表 `scripts/token_sprint/prompts/**/v1.md`

每个文件 YAML front matter + 正文：

```markdown
---
pass_id: p3
prompt_version: v1
---
```

正文是给 ZCode 会话的英文指令：逐条读 input JSONL 的文本字段，产出一行 JSON。明确：不要消毒成人语义；无法完成则输出 `{"candidate_id":"...","refused":true}`；不要读图。p3 要求英文检索关键词。p1a 要求从试验集聚合 300–800 个 `snake_case` 英文词。禁止在会话里改这些文件；改 = 新版本号。

实现 `scripts/token_sprint/prompt_hash.py` **或** 在 `validate_shard.py` 增加 `--prompt-file`：计算 prompt 文件 SHA-256，QC 摘要带 `prompt_sha256`。W8/approved manifest 需要它。更简单：把 `prompt_sha256` 函数放到 `make_shards` 不合适；放到 `scripts/token_sprint/prompts.py`：

```python
def prompt_sha256(pass_id: str, prompt_version: str) -> str: ...
```

测试：改一个字节 SHA 变。

- [ ] **Step 1: 写 v1 模板 + `prompts.py` + 短测试 `tests/token_sprint/test_prompt_version.py`。**

- [ ] **Step 2:**

```bash
uv run pytest -q tests/token_sprint/test_prompt_version.py tests/token_sprint/test_validate_shard.py tests/token_sprint/test_snapshot_fts_rebuild.py tests/token_sprint/test_make_shards.py tests/token_sprint/test_merge_shards.py
```

Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add -- scripts/token_sprint/prompts.py scripts/token_sprint/prompts tests/token_sprint/test_prompt_version.py
git commit -m "feat: versioned enrichment prompts with sha256 lock"
```

---

## Phase 1 — Real snapshot + T0 probe

### Task 7: 真实快照 + 60 探针 + T0 门

**前置：** T-1 验收；`inputs_manifest.json` 有效。本 Task 消耗 ZCode tokens（P3 探针）。

- [ ] **Step 1: 快照（关 GUI）**

```bash
SPRINT_ROOT="/c/Users/sunhao/Desktop/code/GifAgent/data/token_sprint"
python scripts/token_sprint/snapshot_library.py --sprint-root "$SPRINT_ROOT"
```

记录三计数与 SHA 到 `docs/reports/2026-08-token-sprint/w1-snapshot.md`。executor 对 `library_snapshot.db` 写 `.ready`；请另一会话 reviewer 重跑 COUNT 后写 `.done` + receipt。通知 coordinator `reconcile_receipts.py --once`。

- [ ] **Step 2: 探针片**

```bash
python scripts/token_sprint/make_shards.py --sprint-root "$SPRINT_ROOT" --probe
```

Expected: `shards/` 下 60 行 input。

- [ ] **Step 3: ZCode 会话按 `prompts/p3/v1.md` 处理探针，产出**
  `shards/shard-probe.p3.v1.{uuid}.jsonl`（原子写，不可原地改）。跑：

```bash
python scripts/token_sprint/validate_shard.py --sprint-root "$SPRINT_ROOT" \
  --shard shards/shard-probe.p3.v1.<uuid>.jsonl --pass-id p3 --prompt-version v1 \
  --input-shard shards/shard-probe.input.jsonl
```

门：拒绝 ≤15%；schema ≥95%；高显式层启发式保真 ≥90%。然后**人工复核全部高显式层 20 条 + 全部 suspected_sanitized**。结论写入 `docs/reports/2026-08-token-sprint/w1-t0-probe.md`。从 ZCode 使用统计记录本包消耗，交给 coordinator 写入 usage-log（本会话不改 usage-log）。

- [ ] **Step 4: 判定**

  - 三门全过 → 进入 Task 8。
  - **任一门失败 → STOP 模型标注。** 提交已有工具与 T0 报告，分支可合入 integration。预算转工程轨。不要开始 P1a/P3 试验集。

- [ ] **Step 5: Commit 报告（不要 commit 快照 db / jsonl）**

```bash
git add -- docs/reports/2026-08-token-sprint/w1-snapshot.md docs/reports/2026-08-token-sprint/w1-t0-probe.md
git commit -m "docs: record W1 snapshot counts and T0 probe verdict"
```

---

### Task 8: Stage A/B trial set (only if T0 passed)

试验集 200–500 条。顺序：P3 →（P1a 词表冻结，人工保真审查）→ P1b → P2。每轮独立过 QC。每片 50–200 条，一片一会话。输出文件名含 uuid `attempt_id`。`.ready` 后禁止改文件。

Stage A：P1a 聚合 `tag_vocab_v1.json`（300–800 词，英文小写 snake_case）。**冻结前必须人工审查语义保真。** 写共享根，不入库。coordinator 后续把该文件 SHA 记入 approved enrichment manifest（W1 只发 receipt）。

Stage B 每轮 QC：schema ≥95%（爬坡后 Stage C 升到 ≥98%）、拒绝 ≤15%、保真抽检通过。

Stage C（弹性）：仅当红线要求加码且 T0/试验集门仍绿。P4/P5 用已提交的 v1 模板；改模板须新版本。

- [ ] **Step 1:** `make_shards.py` 导出试验集 input 片。
- [ ] **Step 2:** 逐 pass 执行、validate、`.ready` / reviewer `.done`。
- [ ] **Step 3:** `merge_shards.py` 建缓存；抽查 `shard_sha256` 列与文件一致。
- [ ] **Step 4:** 写 `docs/reports/2026-08-token-sprint/w1-trial.md`。Commit 仅报告与 prompt 若有新版本。

---

## T0 failure fallback (mandatory)

若 Task 7 失败：

1. 不运行 Task 8。
2. 工具链（Tasks 1–6）保持可合入。
3. 在 `w1-t0-probe.md` 写明「模型标注取消，确定性工具已交付」。
4. 通知 coordinator：W2 的标注轨同样取消（W2 计划有对称 fallback）；W2 harness 仍可开发。

---

## Verification matrix

| Gate | Command | Pass |
|------|---------|------|
| Rebuild contract | `pytest -q tests/token_sprint/test_snapshot_fts_rebuild.py` | 空 FTS + 完成 state 仍全量重灌 |
| Shards | `pytest -q tests/token_sprint/test_make_shards.py` | probe=60；片大小 50–200；无媒体字段 |
| QC | `pytest -q tests/token_sprint/test_validate_shard.py` | 消毒行失败；refuse 单独计 |
| Merge | `pytest -q tests/token_sprint/test_merge_shards.py` | 非 .done 不入库；可从 JSONL 重建 |
| Snapshot (real) | 三 COUNT + errors==0 | 写入 w1-snapshot.md |
| T0 | 三门 + 人工复核高显式层 | 或 fallback 取消标注 |
| Protocol | 快照与每片走 ready/done/receipt | 不改 canonical manifest |
| Isolation | 不改 `library_search.py` | `git diff -- app/services/library_search.py` 空 |

---

## Done looks like

- 工具脚本均支持 `--sprint-root`，Git Bash/cmd 可跑。
- 回归测试锁死「FTS 空但 state 完成」必须全量重灌。
- T0 有书面结论；失败则无试验集标注。
- 获批 JSONL + receipt 可被 W8 按 SHA 消费；`enrichment.db` 可删可重建。

---

## Explicit non-goals

- 改产品搜索代码或建 FTS v2（W8）。
- 直连 GLM、读 GIF/视频、覆盖旧 jsonl、改 prompt 而不升版本。
- 承诺全库覆盖率。
