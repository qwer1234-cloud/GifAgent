# Preference Memory 产品闭环 — 实施计划

> **For agentic workers:** 按任务顺序执行。每个 Task 以 focused pytest 结束。未经用户明确要求不要 `git commit`，不要 `git push`，不要改 `preference_memory.enabled`。
>
> **本文不是从零实现 Preference Memory。** P1 服务、API、Gradio Profile 页、reranker 已经在仓库里。本计划做两件事：**(A) 一次性补上生产库历史缺口**（向量 + holdout 隔离 + 重建画像）；**(B) 把向量写入接进新候选入库路径**，避免以后再跑全库 backfill。画像 rebuild / publish **不是**每条视频的操作。
>
> **禁止开始 LangGraph。** 编排重构排在本闭环稳定之后。

**Goal:** 在真实生产库上用现有 14,896 条反馈重建全局+分场景画像，用与训练集源视频互斥的 holdout 证明可以 publish，且 `enabled=false` 时导出排序仍等于基线。默认开关保持关闭，直到用户书面同意。

**Architecture:** 现有栈不动。闭环只消费 `preference_events`（append-only）+ `candidate_vectors`（`nomic-embed-text:latest` / 768）+ `PreferenceMemoryService.build_profile` + `PreferenceEvaluationService.evaluate` / `evaluate_source_grouped`。Rerank 公式已在 `app/services/reranker.py`。

缺三块，不要混成一次「全库补数据」：

| 缺口 | 频率 | 正确做法 |
|------|------|----------|
| 历史 13k 行缺向量 | **一次** | Task 1 backfill |
| 新候选入库不写向量 | **每条新候选** | Task 1b：有 VLM 文本就 upsert 向量 |
| holdout 与训练集源视频重叠 | 每次 rebuild 画像 | Task 2 `exclude_source_videos` |

`rank_dedup` 已经会算 embedding（去重 + 开开关时的 PreferenceRerank），但 **算完就丢**，不写入 `candidate_vectors`。`CandidateService.materialize_run_candidate` 已同步 FTS，**不同步向量**。Workbench 文件夹导入只有路径、没有 caption，此时禁止硬 embed 空文本。画像是全局 centroid，**禁止**每处理完一条视频就 `build_profile` / `publish`。

---

## 0. 执行前必读：当前事实（as_of 2026-09-02）

### 两套 SQLite，禁止搞混

| 路径 | 角色 | 实测 |
|------|------|------|
| `dist/GifAgentUI/data/library.db` | **本计划唯一生产库** | `candidate_gifs=13,625`，`preference_events=14,896`，`candidate_vectors=9,328`，current=`profile_8bbf21227ef5d798`（发布 2026-08-28T12:10Z） |
| `data/library.db` | 开发/默认 config | `candidate_gifs=78`，`events=10`，`vectors=0`，无 current。`configs/models.yaml` `database.path` 指向这里 |

`app.db.get_connection()`、Gradio Profile 页的 backfill、`scripts/evaluate_preference.py` **默认都打到 `data/library.db`**。对本计划无效。所有读写必须显式 `--db dist/GifAgentUI/data/library.db`（或临时改 config，用完改回）。

两库都有 `-wal`。执行前确认 GifAgentUI / Gradio **未打开该文件**。禁止删除、truncate、checkpoint 覆盖 WAL。

### 已有能力（不要重写）

- Schema / 事件 / 画像 / 发布：`app/services/preference_{schema,events,memory,evaluation,types}.py`
- 向量 backfill：`app/services/candidate_vectors.py` + `scripts/backfill_candidate_vectors.py`（已支持 `--db`）
- API：`app/routers/preference.py`（`/profiles/build`、`/publish`、`/evaluate`、`/vector-health`、preview、rollback）
- UI：`app/ui/tabs/profile.py`（build / publish / vector backfill；**默认连错库**）
- Rerank：`app/pipeline/ranking.py` 仅当 `preference_memory.enabled` 为 true 才调用
- 测试：`tests/test_preference_*.py`、`tests/test_preference_reranker.py`

### 反馈存量

生产库 `preference_events`：like 10,287 / favorite 1,857 / dislike 1,594 / neutral 1,158。发布之后又有 **3,259** 条新事件（最新 2026-09-01）。画像已过期。

有效评分事件（like/dislike/favorite，且未被 supersede / undo）必须 **每条 target 都有** `candidate_vectors` 行，否则 `evaluate_profile_gates` 会因 `candidate_vector_count < effective_feedback_count` 挡住 build。

### Holdout 门（现有代码语义，必须遵守）

`PreferenceEvaluationService.evaluate`：

- holdout JSONL 每行：`candidate_id`、`rating` ∈ `{like,dislike,neutral}`、`judged_at`
- `favorite` **不在** `GAIN_MAP` 里；导出 holdout 时把 `favorite` 写成 `like`
- `MIN_HOLDOUT_JUDGMENTS = 30`
- 任一 holdout 候选的 `source_video_sha256` 若出现在「watermark 之前的 training events」里 → `source_video_overlap` → `can_publish=false`

因此：**必须先切出 holdout 源视频，build 时排除这些视频的事件。** 现有 `build_profile` 没有 exclude 参数，本计划 Task 2 补上。禁止为过门而删除或改写历史 `preference_events`。

`evaluate()` 的 Like@20 / NDCG@20 是按 `candidate_gifs.final_score` 排的，**不是** PreferenceReranker 对比。质量对比用 `evaluate_source_grouped()` 的 `base_ndcg_at_20` vs `preference_ndcg_at_20`。Publish 硬门仍是 `evaluate().can_publish`。

---

## Constraints

1. **生产库钉死** `dist/GifAgentUI/data/library.db`。不要写 `data/library.db`（除非用户明确说在开发库演练）。
2. **不要改** `scripts/test_video_adaptive.py` 的 `SAMPLE_INTERVAL` / `REFINE_THRESHOLD` / `WORTHINESS_THRESHOLD` / `MERGE_GAP` / `OUTPUT_RATIO` / `MAX_OUTPUT`。
3. Preference Memory **只影响 rerank 与候选向量持久化**。不改抽帧、VLM prompt、导出 GIF 成败、主 FAISS、FTS 重建算法。允许在候选 **已经写入 `candidate_gifs` 且已有 VLM 文本** 之后追加 `candidate_vectors` upsert。Embedding 失败 **不得**让 GIF 导出 / materialize 失败（记 exclusion 或 warning，候选行保留）。
4. **不要**把 `configs/models.yaml`（及 `models.opt_*.yaml`）里的 `preference_memory.enabled` 改成 `true`，除非用户在 Task 6 明确同意。
5. **不要删除、UPDATE 历史 `preference_events`。** 反馈是 append-only。纠正用新 event + `supersedes_event_id`。
6. Embedding 锁定 `nomic-embed-text:latest`、768 维。换模型等于新向量空间，本计划禁止。
7. 不读 GIF/JPG/视频字节。向量文本来自 `vlm_summary_json` / `tags_json` / `scenario_keys_json`（`build_candidate_embedding_text`）。
8. 不开始 LangGraph，不改 `pipeline_stage2.py` / `vlm_loop.py` 的 top-level 副作用。Task 1b 只允许改候选入库/富化后的向量 upsert，以及（可选）materialize 成功后调用已有 `CandidateService`。
9. 主 `media` / 主 FAISS 不自动写入。没有用户 `promote` 就不要 promote。
10. 测试用 `tmp_path` 或内存 SQLite。生产库只在标明 `--db dist/...` 的脚本里打开。
11. 用户可见文案中文；标识符与测试名英文。
12. Commit 仅在用户要求时；每次只 stage 本 Task File Map 列出的文件。禁止 `git add -A`。

---

## File Map

**Create**

- `scripts/preference_holdout_split.py` — 按源视频切 train/holdout，写 JSONL + exclude 清单
- `tests/test_preference_holdout_split.py`
- `tests/test_candidate_vector_ingest.py` — 入库即写向量 / 空文本跳过 / embed 失败不挡候选
- `docs/reports/2026-09-preference-memory/product-loop.md` — 执行记录（命令、数字、sha256）

**Modify**

- `app/services/candidate_vectors.py` — 抽出可单条调用的 `upsert_candidate_vector(...)`（backfill 复用，禁止复制 embed 逻辑）
- `app/services/candidates.py` — `materialize_run_candidate` 在 FTS 之后 best-effort 写向量
- `scripts/enrich_library_candidates.py` — UPDATE 文本成功后刷新向量（hash 变了才 embed）
- `app/routers/candidates.py` — 文件夹导入：无 caption/tags 则 **跳过** 向量，不要 embed 空串
- `app/pipeline/stages/materialize.py` 或现有 import 钩子 — 成功 GIF 写入 library 时走 `CandidateService`（若当前仍只落磁盘）
- `app/services/preference_memory.py` — `evaluate_profile_gates` / `build_profile` / `preview_profile` 增加 `exclude_source_videos`
- `app/routers/preference.py` — build body 可选 `exclude_source_videos: list[str]`
- `scripts/evaluate_preference.py` — 增加 `--db`，以及 `--grouped` 跑 `evaluate_source_grouped`
- `tests/test_preference_profiles.py` 或新测试文件 — exclude 后 overlap 门可通过
- `tests/test_candidate_materialization.py` — 有文本时出现向量行；Ollama 挂了时候选行仍在

**Do not modify**

- `scripts/test_video_adaptive.py` 采样参数
- `configs/models.yaml` 的 `enabled`（Task 6 用户批准前）
- FTS / `library_search.py` / Token Sprint 产物
- `docs/HANDOFF_NEW_CHAT.md`（过时，且把 Preference Memory 写成「还在设计阶段」——以本文件为准）

---

## Task 0: 预检与基线快照（只读）

**Files:** 无代码。记录写入 `docs/reports/2026-09-preference-memory/product-loop.md`。

### Step 1: 确认 GUI 未占用生产库

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'GifAgent|python' } | Select-Object ProcessName, Id
```

若 UI 正打开 `dist/GifAgentUI/data/library.db`，停下再继续。

### Step 2: 只读采集基线

对生产库 `mode=ro` 记录：

- `COUNT(*)`：`candidate_gifs`、`candidate_vectors`、`preference_events`
- current profile_version / published_at
- `preference_events` 按 rating 计数
- 有效评分 target 中缺向量的数量
- `git rev-parse HEAD`、`git status --short`

预期数量级（允许漂移，但数量级必须对）：candidates ~13k，events ~15k，vectors ~9k，缺向量约 4k。

若你连上的是 78 行的 `data/library.db`，**停下来**，你打错库了。

### Step 3: 备份

在仓库外复制（不要 commit db）：

```
dist/GifAgentUI/data/library.db
dist/GifAgentUI/data/library.db-wal   # 若存在，与主文件一起拷
dist/GifAgentUI/data/library.db-shm
```

备份目录建议：`data/token_sprint/backups/library.pre-prefloop.<UTC>.db`（仅本地，gitignore）。

**验收：** 报告里有 SHA/计数；备份文件存在。无代码 diff。

---

## Task 1: 向量 backfill（先反馈目标，再视情况铺开）

**Files:** 不改代码。使用现有 `scripts/backfill_candidate_vectors.py`。

Ollama 必须能提供 `nomic-embed-text:latest`。失败时脚本 exit 2。

### Step 1: dry-run 反馈目标

```powershell
uv run python scripts/backfill_candidate_vectors.py `
  --db dist/GifAgentUI/data/library.db `
  --feedback-only --missing-only --dry-run
```

记下 `missing`。这些行不补完，build 会被 vector coverage 门挡住。

### Step 2: 写入反馈目标向量

```powershell
uv run python scripts/backfill_candidate_vectors.py `
  --db dist/GifAgentUI/data/library.db `
  --feedback-only --missing-only
```

可加 `--limit` 分批。`failed>0` 时看 `candidate_vector_exclusions`，不要盲 `--retry-excluded`。

### Step 3（可选）: 其余缺向量候选

不挡 build。有助于 rerank 覆盖。同样 `--db` + `--missing-only`，去掉 `--feedback-only`。

### Step 4: 验证

有效评分 target 的 `candidate_vector_count == effective_feedback_count`（或非常接近；exclusion 需写入报告）。

**验收：** dry-run 与实跑 JSON 写入 product-loop 报告。生产库向量行数上升。无产品代码 diff。

---

## Task 1b: 新视频入库即写向量（禁止以后再全库补）

Task 1 只修历史。若不做本 Task，每处理一批新视频 `candidate_gifs` 又会领先 `candidate_vectors`，下次 rebuild 画像再次被 coverage 门挡住。

### 什么必须在线，什么禁止在线

| 操作 | 何时 | 说明 |
|------|------|------|
| upsert `candidate_vectors` | 候选行 **已有** caption/summary/tags/scenario 文本时 | 与 FTS 同步同一生命周期 |
| 刷新向量 | `vlm_summary_json` / `tags_json` / `scenario_keys_json` 更新且 `source_text_hash` 变化 | enrich / 二次 VLM |
| 写 `preference_events` | 用户 like/dislike/favorite/undo | **已经**是在线路径，不要接到流水线 |
| 同步 FTS | 候选 INSERT | **已经**在 `CandidateService` 里 |
| `build_profile` / `publish` | 反馈积累后、用户触发或单独批作业 | **禁止**每条视频结束就跑 |
| 开 `preference_memory.enabled` | 用户书面同意 | 与入库无关 |

Ranking 阶段（`_rank_clips_with_preference` / `_compute_clip_embeddings`）可以继续用内存向量做去重和 rerank；**落库发生在候选有 `candidate_id` 且写入 SQLite 之后**。不要在 `rank_dedup` 里提前 INSERT 半成品行。

### Step 1: 失败测试

`tests/test_candidate_vector_ingest.py`（内存库 + fake `embed_fn`）：

1. `materialize_run_candidate` 带非空 `vlm_summary.caption` → `candidate_vectors` 1 行，model=`nomic-embed-text:latest`，dim=768，`normalized=1`。
2. 同一候选再 materialize（idempotent）→ 向量行仍 1 条，不重复 INSERT 堆行。
3. `embed_fn` 抛 `EmbeddingServiceUnavailable` → 候选行存在，无向量行或有 exclusion，函数不 raise。
4. caption 为空且 tags 为空 → **不调用** embed_fn（文件夹导入形态）。
5. 更新 `vlm_summary_json` 后调用同一 upsert → `source_text_hash` 变了才重新 embed。

```powershell
uv run pytest -q tests/test_candidate_vector_ingest.py tests/test_candidate_materialization.py
```

预期：FAIL。

### Step 2: 抽出单条 upsert

Modify `app/services/candidate_vectors.py`：

- 新增 `upsert_candidate_vector(conn, candidate_id, *, embed_fn=..., embedding_model=..., embedding_dim=...) -> dict`
- 内部：读行 → `build_candidate_embedding_text` → 空文本则 `{status: "skipped_empty_text"}` 且不写库
- 已有行且 hash+schema+model 相同 → `{status: "unchanged"}`
- 否则 embed + `_upsert_vector`
- `backfill_candidate_vectors` 改为调用它，行为不变

禁止第二套 text schema。Embedding 文本必须走 `build_candidate_embedding_text`，不要用 `_clip_embedding_text`（那是 ranking 用的另一套拼接）。

### Step 3: 接到三条写入路径

1. **`CandidateService.materialize_run_candidate`**
   FTS 成功后 best-effort `upsert_candidate_vector`。默认用 `compute_text_embedding`。测试可注入 embed_fn（给 `CandidateService` 可选 `embed_fn` 参数，默认生产函数）。

2. **`scripts/enrich_library_candidates.py`**
   `UPDATE ... vlm_summary_json` 成功并 commit 后立刻 upsert。失败记日志，不要回滚已经写好的富化文本。

3. **`_materialize_filesystem_candidates_for_folder`**
   保持「只登记路径」。空文本 skip。注释写明：向量等 enrich 或带 VLM 的 `CandidateService` 路径。

### Step 4（推荐，仍属本计划）: 流水线成功 GIF → library

当前 `app/pipeline/stages/materialize.py` **只写磁盘 / PBF / result JSON**，不调用 `CandidateService`。所以「处理完新视频」并不会出现在生产库里，更不会有向量。

在 materialize **已经成功 publish GIF 之后**（不影响 GIF 成败）：

- 用该 clip 的 VLM caption / emotion / tags 组 `CandidateService` payload
- `materialize_run_candidate`（顺带写 FTS + 向量）
- 库打不开或 embed 挂了：warning + 继续，stage 仍 succeeded
- `--db` / `get_connection()` 必须是用户正在用的 library（打包 UI 是 `dist/.../library.db`）

若这一步范围太大、会碰到路径/sha 争议：**至少完成 Step 3**，并在报告里写明「新视频仍需一次 import 才会入库；但 import/enrich 之后不必再跑 backfill」。不要 silently 宣称已闭环。

**不要**在这里调用 `build_profile` 或 `publish`。

### Step 5: 回归

```powershell
uv run pytest -q tests/test_candidate_vector_ingest.py tests/test_candidate_materialization.py tests/test_search_fts_improvements.py tests/test_preference_reranker.py
```

**验收：** 新带文本的候选入库后 `candidate_vectors` 有对应行；Ollama 挂了 GIF 流程仍成功；空文本导入不打 embedding API；没有 per-video profile rebuild。

---

## Task 2: Holdout 源视频隔离 + build exclude

这是本计划 **第二块** 必要产品代码（第一块是 Task 1b 的在线向量）。

### Step 1: 写失败测试

`tests/test_preference_holdout_split.py`（内存库）：

1. 三个 source video，各 ≥10 条 like/dislike。
2. split 把 video-H 放入 holdout，其余 train。
3. `build_profile(exclude_source_videos={video-H})` status=`built`。
4. holdout JSONL 用 video-H 的候选；`evaluate(...).can_publish is True`，`gate_reasons` 不含 `source_video_overlap`。
5. 若不 exclude 就 evaluate 同一 holdout → `can_publish is False`。
6. `preference_events` 行数在 build 前后不变。

再补 `evaluate_profile_gates`：exclude 后 `source_video_count` / `effective_feedback_count` 不含被排除视频。

```powershell
uv run pytest -q tests/test_preference_holdout_split.py
```

预期：FAIL（exclude 参数尚不存在）。

### Step 2: 实现 split 脚本

Create `scripts/preference_holdout_split.py`：

- 必参：`--db`、`--output-dir`
- 只读打开 db（`mode=ro` + `query_only` 若可能）
- 用与 `load_latest_scoring_events` **相同** 的有效事件定义
- 按 `source_video_sha256` 分组
- 贪心把视频划入 holdout，直到：
  - holdout 判定行 ≥ 30
  - holdout 源视频 ≥ 3
  - 单视频占 holdout 判定 ≤ 40%
- 训练侧仍须满足画像门：有效反馈 ≥ 30、like≥15、dislike≥10、源视频≥3、单视频份额 ≤ 40%
- 写：
  - `holdout.jsonl`（`favorite`→`like`；保留 `candidate_id`/`rating`/`judged_at`）
  - `exclude_source_videos.json`（sha 列表）
  - `split_summary.json`（两边计数）
- `--seed` 默认 `20260902`，划分确定

划不出同时满足 train 门 + holdout 门的集合 → exit 2，不要降低门。

### Step 3: `exclude_source_videos` 贯穿 gates/build/preview

Modify `evaluate_profile_gates(..., exclude_source_videos: frozenset[str] | None = None)`：在统计 effective/like/dislike/视频份额之前丢掉这些 `source_video_sha256`。

`build_profile` / `preview_profile` 把同一集合传下去。centroid 只用过滤后的 events。

`BuildRequest` 增加可选 `exclude_source_videos: list[str] = []`。默认空列表 = 今日行为不变。

### Step 4: `evaluate_preference.py` 增加 `--db`

与 backfill 脚本同样：`--db` 默认仍可是 `data/library.db`，但文档命令必须显式传生产路径。可选 `--grouped` 调用 `evaluate_source_grouped`。

### Step 5: 跑测试

```powershell
uv run pytest -q tests/test_preference_holdout_split.py tests/test_preference_profiles.py tests/test_preference_evaluation.py tests/test_preference_events.py tests/test_preference_reranker.py
```

预期：全绿。`enabled=false` 的 reranker 测试仍绿。

**验收：** 空 exclude 时旧测试行为不变；有 exclude 时 overlap 门可过。

---

## Task 3: 在生产库切 split + dry-run build（不 publish）

**Files:** 只往 `docs/reports/2026-09-preference-memory/` 写报告和 split 产物（jsonl/json，不要 commit 生产 db）。

### Step 1: 切分

```powershell
uv run python scripts/preference_holdout_split.py `
  --db dist/GifAgentUI/data/library.db `
  --output-dir docs/reports/2026-09-preference-memory/holdout `
  --seed 20260902
```

### Step 2: preview / dry-run build

用 exclude 列表调用 `preview_profile` 或 `build_profile(dry_run=True)`（Python 或 API，连接必须是生产库）。

`status` 必须是 `ready`/`built`，不能是 `blocked`。若 blocked，把 `gate_reasons` 写入报告并停下来修（通常是向量还没补齐，回 Task 1）。

**不要** `publish`。**不要** `enabled=true`。

### Step 3: 真 build（仍不 publish）

`dry_run=False` 插入 `preference_profile_builds`。记下 `profile_version`。

确认 `preference_profile_current` **仍是** `profile_8bbf21227ef5d798`。

**验收：** 新 build 行存在且 `status=completed`；current 指针未变。

---

## Task 4: Holdout 评估

### Step 1: publish 硬门

```powershell
uv run python scripts/evaluate_preference.py `
  --db dist/GifAgentUI/data/library.db `
  --profile-version <NEW_VERSION> `
  --holdout docs/reports/2026-09-preference-memory/holdout/holdout.jsonl
```

必须 `can_publish=true`，`gate_reasons=[]`。exit 0。

### Step 2: 质量对比（不替代硬门）

`--grouped`（Task 2 加上后）：记录 `base_ndcg_at_20`、`preference_ndcg_at_20`、`ndcg_delta`、`pairwise_win_rate`、`vector_coverage`。

解释规则（写进报告，供用户决定是否 publish）：

- `can_publish=false` → 禁止 publish
- `ndcg_delta < 0` 且幅度明显 → **不要**建议 publish，停下来问用户
- `ndcg_delta ≥ 0` 或持平 → 可以建议 publish

`evaluate()` 的 Like@20 看的是 `final_score` 列，不要把它当成 Preference Memory 已生效的证据。

**验收：** JSON 报告落盘 `docs/reports/2026-09-preference-memory/holdout-eval.json`。

---

## Task 5: 用户确认后才 publish

**STOP。** 把新 `profile_version`、holdout 数字、`ndcg_delta` 给用户看。等用户明确说「发布」或「publish」。

### Step 1: publish

```powershell
# 示例：生产库上调用 PreferenceMemoryService.publish
# 或 POST /api/preference/profiles/<version>/publish
# 前提：API 进程的 database.path 已指向 dist 库，否则会发到空开发库
```

推荐用一段显式 `--db` 的小脚本或 Python 一次性调用，**不要**假设 Gradio 连的是生产库。

### Step 2: 验证

- `preference_profile_current.profile_version` == 新版本
- `preference_profile_publications` 多一行，`previous_profile_version` 为 `profile_8bbf21227ef5d798`
- 事件表行数未减

回滚（若用户要撤）：`POST /profiles/{old}/rollback` 或 `PreferenceMemoryService.rollback`。

**验收：** current 已切换；`enabled` 仍为 false。关开关时新视频导出排序与 publish 前一致。

---

## Task 6: 启用开关（再次 STOP）

Publish ≠ 启用。`configs/models.yaml`：

```yaml
preference_memory:
  enabled: false
```

打开后 `_rank_pipeline_clips` 才会走 `PreferenceReranker`。

**STOP，等用户明确说「打开 preference_memory.enabled」。**

若同意：

1. 只改 yaml 的 `enabled: true`（三个 models yaml 是否一起改，问用户；默认只改正在使用的那份）。
2. 跑：

```powershell
uv run pytest -q tests/test_preference_reranker.py tests/test_preference_profiles.py
```

3. 用**一个**短视频跑 adaptive 导出，确认日志出现 `Preference Memory: ranked all candidates...`，且 `inactive_reasons` 被记录。
4. 关开关再跑同一视频，排序应回到基线（P1-G4）。

若用户不同意：保持 `enabled: false`。已 publish 的画像只是「就绪」，搜索/导出行为不变。

---

## Task 7: 回归与报告

```powershell
uv run pytest -q tests/test_preference_holdout_split.py tests/test_preference_profiles.py tests/test_preference_evaluation.py tests/test_preference_events.py tests/test_preference_reranker.py tests/test_preference_preflight.py
uv run mypy
```

更新 `docs/reports/2026-09-preference-memory/product-loop.md`：

- 生产库路径与备份
- 向量 before/after
- holdout 视频数、行数、seed
- 新 profile_version 与是否已 publish / 是否已 enabled
- holdout JSON 指标
- 未做项：LangGraph、断链 2,661、未富化 798、全库向量若未铺满

不要改 Token Sprint 的 `handoff.md` 去宣称 Preference Memory「已 100% 产品闭环」，除非 Task 5+6 都完成。

---

## 验收矩阵

| 门 | 证据 |
|----|------|
| 打对库 | 操作的 db 是 13k 候选 / 15k 事件，不是 78 行开发库 |
| 事件不可变 | `COUNT(preference_events)` 不下降 |
| 向量覆盖（历史） | 有效评分 target 均有 768-d nomic 向量，build 不被 vector 门挡住 |
| 向量在线（新视频） | 带 VLM 文本的新 `candidate_gifs` 行同时有向量；空文本导入不 embed；embed 失败不挡导出 |
| 画像非在线 | 流水线 / materialize / enrich **没有**调用 `build_profile` / `publish` |
| Train/holdout 隔离 | 源视频交集为空；`can_publish=true` |
| Build ≠ Publish | current 在用户确认前仍是旧 version |
| 关开关等价基线 | `enabled=false` 时 reranker 测试与导出顺序不变 |
| 开开关需书面同意 | yaml 未擅自改为 true |
| 无 LangGraph | 无新 langgraph 依赖、无 graph 文件 |

---

## 给执行 Agent 的启动提示

```text
请先阅读 docs/superpowers/plans/2026-09-02-preference-memory-product-loop-implementation.md。
不要从 2026-06-27 那份 P1-1 计划重写 Preference Memory。服务已经存在。
生产库是 dist/GifAgentUI/data/library.db，不是 data/library.db。
从 Task 0 开始。Task 1 是一次性历史 backfill；Task 1b 必须把向量写入接进 CandidateService / enrich，不要只补库。
不要每条视频 rebuild/publish 画像。不要 publish，不要把 preference_memory.enabled 改为 true，不要开始 LangGraph。
未经我要求不要 commit。
```

---

## 明确不在本计划内

- LangGraph（L2-1…L2-8）
- 补下 2,661 条断链、798 条缺文件未富化
- 全量 P1b/P3 标签扩片
- 默认打开 `GIFAGENT_SEARCH_FTS_V2`
- 换 embedding / 重建主 FAISS
- 云 API / NSFW 路由
