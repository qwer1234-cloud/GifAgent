# Token Sprint W2 — Eval Harness + Judged Set Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).
>
> **FROZEN until T-1 gates pass.** **Hard dependency:** do not generate candidate pools or qrels until canonical manifest shows W1 snapshot FTS rebuild `.done`（三计数一致且 `errors==0`）。T0 失败则取消本 Workstream 的模型标注；harness 与 `synth_preference_stress.py` 仍须交付。

**Goal:** 可重复的检索评测：候选查询库与**完整标注 eval 子集**分离；NDCG 只对目标 `pool_version` 下 fully judged 查询计算；双模板不一致标 `disputed` 并在裁决前排除；对比真实 FTS-only vs hybrid（W8 后增量新 pool 版本）。禁止用 `base_rag_similarity`（它是 `gif_worthiness`）。

**Architecture:** harness 只读 W1 快照 + canonical manifest 校验 SHA。池 = 三路 top-24 并集。标注在 ZCode 内按两个措辞模板各跑一次。指标复用 `app.quality_lab.metrics.ndcg_at_k`。

**Tech Stack:** Python 3.11+、现有 pytest、`ndcg_at_k`、`LibrarySearchService`。无新依赖。无直连模型。

**Companion:** design v4.1；W1 enrichment plan；W7 两假设框架（空 FTS 定性描述不预设定级）。

**Branch:** `sprint/w2-eval-harness`.

**规模契约（判断量必须与查询规模匹配）：**

| 集合 | 规模 | 单位 |
|------|------|------|
| `query_bank_v1.jsonl` | 100–200 条 | 查询（人工种子 30–50 + 扩写，`origin: human\|expanded`） |
| `judged_queries_v1.json` | **20–40 条**（下限 20） | 从库中选出的 fully-judged 目标 |
| 单查询池 | 三路 top-24 并集，24–72 唯一候选 | candidate |
| 子集 pair | **480–2,880** | query-candidate pair |
| 双模板判断 | **960–5,760** | 模型判断次数 |

先按 T0 实测容量选定 judged 条数，再扩。两个单位必须分别记入报告。每次池生成写 `pool_version`、`ranker_set_hash`（参与 ranker 名称、配置、代码 SHA、查询库 SHA 的稳定哈希）及逐查询候选集合哈希。W8 加入必须**新建** `pool_version`；差集未完成双模板+争议处理前，该查询在新版本下不得 fully judged，也不得进该版本 NDCG。

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

### W2 额外禁区

- **禁止**用 `candidate_gifs.base_rag_similarity` 或 `gif_worthiness` 当检索相关性基线。
- 不改 `library_search.py` / `preference_memory.py`（后者在重构冻结区）。
- 管线符号只经 `from scripts.test_video_adaptive import extract_config` 等 facade；本 W 不需要管线。
- NDCG 不得包含：非目标 `pool_version`、未 fully judged、`status in {disputed}`（裁决前）。disputed **绝不**隐式按 grade 0 计。
- T0 失败：取消 query 扩写与 pairwise/judged 模型标注；Task 1–3 仍交付。
- 计划中不得出现 `uv add`。

---

## File Map

**Create**

- `scripts/token_sprint/eval_harness.py`
- `scripts/token_sprint/synth_preference_stress.py`
- `scripts/token_sprint/prompts/eval_judge/v1a.md` — 模板 A
- `scripts/token_sprint/prompts/eval_judge/v1b.md` — 模板 B（同任务不同措辞）
- `tests/token_sprint/test_eval_harness.py`
- `tests/token_sprint/test_synth_preference_stress.py`
- `docs/reports/2026-08-token-sprint/w2-evalset-baseline.md`

**Runtime (shared root)**

- `evalsets/query_bank_v1.jsonl`
- `evalsets/judged_queries_v1.json`
- `evalsets/pools/{pool_version}.json`
- `evalsets/qrels/{pool_version}.jsonl`

**Do not modify:** W1 脚本、coordinator 包、`app/quality_lab/metrics.py`（只调用）、重构 File Map。

---

## qrels schema (lock)

```json
{
  "query_id": "q001",
  "pool_version": "pv-fts-hybrid-1",
  "ranker_set_hash": "sha256:...",
  "candidate_id": "c123",
  "grade": 2,
  "rationale": "text",
  "pool_sources": {"fts_v1": 3, "hybrid": 7, "fts_v2": null},
  "label_tier": "silver",
  "status": "judged"
}
```

`grade` ∈ {0,1,2,3}。`pool_sources` 值为该 ranker 下 1-based rank 或 `null`。
`label_tier`: `silver`（双模板一致）| `gold`（人工裁决后）。
`status`: `judged` | `disputed` | `adjudicated`。
不一致 → `status=disputed`，**从该 `pool_version` 的 NDCG 排除整条查询**直到 `adjudicated`（升 gold）。不要只排除那一个 pair 却把查询标 fully judged。

`ranker_set_hash` 计算：对规范化 JSON `{"rankers": [...], "library_search_sha": "<blob sha of file at sprint_base>", "query_bank_sha": "...", "pool_version": "..."}` 做 SHA-256。代码 SHA 用 `git rev-parse HEAD` 不够（W 分支会变）；用 `sprint_manifest.sprint_base_sha` + `app/services/library_search.py` 文件哈希。

---

## Phase 0 — Harness (no labels yet)

### Task 1: `eval_harness.py` deterministic metrics

**Files:** `scripts/token_sprint/eval_harness.py`, `tests/token_sprint/test_eval_harness.py`

**CLI:**

```text
python scripts/token_sprint/eval_harness.py --sprint-root <path> \
  --pool-version pv-fts-hybrid-1 \
  --qrels evalsets/qrels/pv-fts-hybrid-1.jsonl \
  --snapshot <sprint-root>/library_snapshot.db
```

消费前：校验 snapshot SHA 与 canonical manifest 中 W1 快照 artifact 一致。不一致 exit 2。

行为：

1. 载入 qrels，过滤 `pool_version` 匹配且查询下**所有 pool 候选**均有 `status in {judged, adjudicated}` 且无 `disputed` 的 query_id。
2. 对每个 fully judged 查询，按指定 ranker 的已存排名（或现场重跑 FTS-only / hybrid）构造 relevance 列表，调 `ndcg_at_k(..., 10)` 与 `ndcg_at_k(..., 24)`。
3. 报告 JSON：`pool_version`、`ranker_set_hash`、`n_judged_queries`、`n_bank_queries`、`coverage`、`ndcg@10`、`ndcg@24`、逐查询明细。
4. **同输入两次运行数字必须完全一致**（测试锁死）。
5. 若某查询 pool 里有 `disputed`：该查询不得出现在汇总平均里。
6. CLI `--ranker fts_v1|hybrid|fts_v2`。`fts_v2` 在 W8 表不存在时跳过并注明，不得报假分数。

现场重跑 ranker：只读快照。FTS-only = `LibrarySearchService(conn, embedder=None)`。hybrid = 传入 embedder **仅当**快照已有 `candidate_vectors`；embedder 用现有 `app.services.embedding` 的查询向量函数，但**不要**为此新写 GLM/智谱客户端。若 hybrid 需要在线 embedding 而环境无本地模型：harness 读快照里已存的 query 向量缓存文件 `evalsets/query_vectors.json`（由可选步骤生成）；缺缓存则 hybrid 列标 `skipped`，不得静默用 `gif_worthiness` 顶替。

- [ ] **Step 1:** 用 `tmp_path` 造 2 条查询：一条 fully judged，一条 disputed。断言平均 NDCG 只含第一条；`base_rag_similarity` 列即使存在也不被读取（测试 spy/monkeypatch 断言）。两次运行 equal。

- [ ] **Step 2:**

```bash
uv run pytest -q tests/token_sprint/test_eval_harness.py
```

Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add -- scripts/token_sprint/eval_harness.py tests/token_sprint/test_eval_harness.py
git commit -m "feat: eval harness NDCG only over fully judged pool versions"
```

---

### Task 2: pool builder + ranker_set_hash

同一 `eval_harness.py` 增加子命令：

```text
python scripts/token_sprint/eval_harness.py --sprint-root <path> build-pools \
  --queries evalsets/query_bank_v1.jsonl \
  --pool-version pv-fts-hybrid-1
```

对每条查询跑 fts_v1 与 hybrid 的 top-24，并集写入 `evalsets/pools/{pool_version}.json`。含逐查询 `candidate_ids` 哈希。W8 之后的新版本只标并集差集。

测试：固定内存 FTS 内容时，两次 `build-pools` 的 `ranker_set_hash` 与候选集合一致。

- [ ] **Step 1: 实现 + 测试。**
- [ ] **Step 2:** `uv run pytest -q tests/token_sprint/test_eval_harness.py`
- [ ] **Step 3: Commit** `feat: snapshot pool builder with ranker_set_hash`

---

### Task 3: `synth_preference_stress.py` (7 gates, memory SQLite)

**Files:** `scripts/token_sprint/synth_preference_stress.py`, `tests/token_sprint/test_synth_preference_stress.py`

对 `evaluate_profile_gates`（`app/services/preference_memory.py`）用内存库压 7 个门，**不写真实库**：

1. `effective_feedback_count < 30` (`MIN_EFFECTIVE_FEEDBACK`)
2. `like_count < 15`
3. `dislike_count < 10`
4. `source_video_count < 3`
5. `max_single_video_share > 0.40`
6. 无向量 / embedding_model mismatch
7. `candidate_vector_count < effective_feedback_count`

每个门单独构造刚好失败与刚好通过的夹具，断言 `gate_reasons` 字符串前缀稳定。`--sprint-root` 仍必填（即使本脚本只写 tmp；用于统一协议）。默认 `--db :memory:`。禁止 `preference_memory.enabled` 翻转（non-goal）。

注意：`preference_memory.py` 在重构 File Map 内 → **只 import 调用，不修改**。

- [ ] **Step 1: 实现。** 测试 7 门 × 2 = 14 断言，全内存。
- [ ] **Step 2:** `uv run pytest -q tests/token_sprint/test_synth_preference_stress.py`
- [ ] **Step 3: Commit** `test: stress preference profile gates on in-memory sqlite`

---

## Phase 1 — Labels (T0 + W1 snapshot required)

### Task 4: Human seed queries + optional expansion

- [ ] **Step 1:** 人工写 30–50 条 `query_bank_v1.jsonl`（`origin: human`）。英文检索句，覆盖角色/行为/场景。不读图。写入共享根 `evalsets/`。`.ready` + reviewer `.done`。
- [ ] **Step 2:** 若 T0 已通过且额度允许：ZCode 扩写到 100–200（`origin: expanded`）。扩写受 T0 保真纪律约束：不得消毒。T0 失败则跳过本步。
- [ ] **Step 3:** 从库中选 **至少 20** 条进 `judged_queries_v1.json`。记录预计 pair 数与双模板判断次数，**先算再扩**。写进 `w2-evalset-baseline.md`。
- [ ] **Step 4:** Commit 仅 markdown 报告（jsonl 在共享根，不入库）。

### Task 5: Dual-template judging

对 judged 查询的并集池：模板 A、模板 B 各标一次（`eval_judge/v1a.md` / `v1b.md`）。一致 → silver + judged。不一致 → disputed，**整查询**暂不进 NDCG。人工裁决 → gold + adjudicated。

银一致率门：≥ 80%（disputed 不算失败，但计入一致率分母：一致 / (一致+争议)）。

产出 qrels JSONL → validate 每行含 `pool_version`、`ranker_set_hash`、`pool_sources` → `.ready` / `.done`。

跑两次 `eval_harness.py`，数字一致。

### Task 6: Baseline report

写 `docs/reports/2026-08-token-sprint/w2-evalset-baseline.md`：

- 生产 FTS 为空的**复现证据**（W1 快照重建前的 inputs 计数或 T-1 采集笔记）。定性描述遵循 W7：(a) 索引为何从未构建；(b) `_rank_with_text` 吞 `OperationalError`。**不预设定级、不断言「无告警」。** 服务已返回 `degraded=True` 且 UI 展示（`library_search.py:156`、`search.py:92`）。
- judged 规模、pair 数、判断次数、silver 一致率、NDCG 表（仅 fully judged）。

```bash
git add -- scripts/token_sprint/prompts/eval_judge docs/reports/2026-08-token-sprint/w2-evalset-baseline.md
git commit -m "docs: W2 evalset baseline with judged-only NDCG"
```

---

## T0 / W1 failure fallback

- W1 快照未 `.done`：禁止 build-pools 与标注。harness 单元测试仍可在 tmp 库跑。
- T0 失败：取消 Task 4 扩写与 Task 5；Task 1–3 合入 integration。

---

## Verification matrix

| Gate | Pass |
|------|------|
| NDCG 过滤 | 测试：disputed 查询不进平均 |
| 两次运行 | 同 pool_version 数字一致 |
| 可追溯 | 每条 qrels 有 pool_version / ranker_set_hash / pool_sources |
| 规模 | judged ≥20；报告同时给出 pair 与判断次数 |
| 银一致率 | ≥80%（若标注发生） |
| 无假基线 | 测试断言不读 `base_rag_similarity` |
| 7 gates | synth 脚本 14 夹具绿 |

---

## Done looks like

- harness 可在无模型条件下测通。
- 若标注发生：指标只含 fully judged；W8 新 pool 版本未标完差集前不进该版本表。
- 空 FTS 证据按两假设书写，无「静默无告警」结论。

---

## Explicit non-goals

- 用 worthiness 当 qrel。修产品 bug。直连 GLM。读媒体。改冻结区。
