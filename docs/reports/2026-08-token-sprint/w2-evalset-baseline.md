# W2 评测 harness + 标注集 — 基线报告（w2-evalset-baseline）

- workstream: W2（分支 `sprint/w2-eval-harness`，基于 `sprint_base_sha=a6c9fb0a0dcbe5871efeaa846b6937df2efc7e9e`）
- as_of: 2026-08-29（W2 executor 会话）
- producer: `w2-executor`

## 1. 范围裁定与本报告状态

canonical `sprint_manifest.json` 的 `scope_rulings`（manifest 记录于 rev6 的 `t0_probe` 裁决，
当前 manifest revision 已推进）：

- `w2_harness_tools_task_1_3`: **active (deterministic)** — 本冲刺交付。
- `w2_model_annotation_and_evalsets_labeling`: **cancelled (T0 fallback)**。
- `t0_probe.decision`（原文摘录）: *"T0 fallback per design: W1/W2 model-annotation portions
  CANCELLED (Stage A/B, judged-subset labeling, pairwise, P1a/P1b/P2/P4/P5); deterministic
  tools delivered; budget shifts to engineering track"*（`refuse_rate: 1.0`，
  `root_cause`: snapshot 文本字段上游从未填充）。

因此本冲刺 **实际交付 = Task 1 + Task 3（确定性工具）**；Task 2（build-pools 子命令）、
Task 4–6（查询库、双模板标注、judged 子集）一律未执行；共享根 `evalsets/` 保持为空
（这是本冲刺的预期状态，不是缺陷）。

## 2. 交付物

| 文件 | 内容 |
|------|------|
| `scripts/token_sprint/eval_harness.py` | 确定性检索评测 harness（NDCG@10/@24，fully-judged 契约） |
| `scripts/token_sprint/synth_preference_stress.py` | profile build 7 门压测（内存 SQLite，10 夹具 / 14 门级断言） |
| `tests/token_sprint/test_eval_harness.py` | 26 个测试 |
| `tests/token_sprint/test_synth_preference_stress.py` | 28 个测试 |

复用 `app.quality_lab.metrics.ndcg_at_k`（只调用，未改）；只读 `LibrarySearchService`、
`evaluate_profile_gates`（`preference_memory.py` 在重构冻结区，只 import 不修改）；
无新依赖（未触碰 `pyproject.toml` / `uv.lock`）、无模型端点代码
（`grep -iE "bigmodel|GLM_API"` 在四个新文件上为空）、未读任何 GIF/JPG/视频内容、
未写任何生产库。

### qrels 契约（已锁定并强制校验）

行 schema 按 W2 计划锁定：`{query_id, pool_version, ranker_set_hash("sha256:"+64hex),
candidate_id, grade∈{0,1,2,3}, rationale, pool_sources:{fts_v1|hybrid|fts_v2: 1-based rank|null},
label_tier∈{silver,gold}, status∈{judged,disputed,adjudicated}}`。
违反即 fail-closed 退出码 2（逐行号报错）；同一 `pool_version` 出现多个
`ranker_set_hash` 视为可追溯性违规（exit 2）；重复 `(query_id, candidate_id)` 拒绝。

### NDCG 计算契约

- 只对**目标 `pool_version`** 下 fully judged 的查询计算：该查询的**全部** pool 候选
  `status ∈ {judged, adjudicated}`；任何一个 `disputed` pair → **整条查询**排除，
  绝不隐式按 grade 0 计。
- 排序列表：按指定 ranker 的已存 1-based rank 升序（null 排尾、candidate_id 决胜，
  完全确定）；未被该 ranker 检出的已判候选留在尾部（贡献 0 DCG、抬高 ideal DCG，
  即"漏检相关候选会被惩罚"）。`--rerun` 时现场只读重跑 FTS-only / hybrid 并过滤到 pool。
- `base_rag_similarity`（= `gif_worthiness`）**在任何路径都不被读取**（测试含 SQL spy
  与双库对照断言，见 §3）。
- `fts_v2`：现场重跑在 W8 表存在前一律 `skipped:fts_v2_live_ranking_arrives_with_w8`，
  不产出伪造分数（已存 rank 仍可用）。
- hybrid：需要查询向量；先用现有 `app.services.embedding.compute_text_embedding`，
  失败则回退 `evalsets/query_vectors.json` 缓存；两者皆缺 → 整列
  `skipped:no_query_vector_available`；快照无 `candidate_vectors` →
  `skipped:snapshot_has_no_candidate_vectors`。**不会**静默退化或用 worthiness 顶替。
- 确定性：查询按 `query_id` 升序聚合、均值用 `math.fsum`、浮点统一 round(6)，
  同输入两次运行的报告**逐字节一致**。

## 3. 空输入行为与确定性验证（实测证据）

### 3.1 真实共享根上的空输入运行（两次）

```bash
uv run python scripts/token_sprint/eval_harness.py \
  --sprint-root C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint \
  --pool-version pv-fts-hybrid-1 --qrels evalsets/qrels/pv-fts-hybrid-1.jsonl
# exit=0（两次），diff 两次 stdout → 完全一致（byte-identical）
```

要点（报告 JSON 摘录）：

- `snapshot.manifest_check: "ok"` — harness 在消费前按 canonical manifest 校验了
  W1 快照 SHA（`5af6c815…b7161d8`，与 manifest artifact 一致；不一致会 exit 2，
  未注册的非 canonical 快照需显式 `--allow-uncanonical-snapshot`，仅限测试夹具）。
- `n_qrels_rows_total: 0`、`n_pool_queries: 0`、`n_judged_queries: 0`、
  `ndcg_at_10: null`、`ndcg_at_24: null`、`coverage: 0.0` — **空 evalsets 优雅报告，
  不崩溃、不产出伪造分数**，notes 明确标注
  "evalsets are empty (expected this sprint after the T0 fallback)"。
- `ranker_set_hash: null`（无 qrels 行时无值可报）。

### 3.2 测试证据

```text
=== CMD: uv run pytest -q tests/token_sprint/test_eval_harness.py ===
..........................                                               [100%]
26 passed in 2.26s
=== CMD: uv run pytest -q tests/token_sprint/test_synth_preference_stress.py ===
............................                                             [100%]
28 passed in 2.07s
=== CMD: uv run pytest -q tests/token_sprint/ ===
...............................                                          [100%]
103 passed in 7.21s
```

（`tests/token_sprint/` 合计 103 = 本 W2 新增 54 + 既有 coordinator 协议测试 49；
后两者为既有文件，本 W 未改动。）

确定性 / 契约类断言（均绿）：

- disputed 查询整条排除、均值恰等于其余 judged 查询的值
  （`test_report_contains_only_fully_judged_queries`、
  `test_disputed_never_counts_as_grade_zero`）。
- 同输入两次运行 dict 与 JSON 逐字节一致；qrels 行序打乱不影响报告
  （`test_two_runs_identical_pure_function`、`test_report_independent_of_qrels_row_order`、
  `test_live_fts_v1_two_cli_runs_byte_identical`、`test_hybrid_live_with_cache_runs_and_is_deterministic`）。
- **无假基线**：SQL spy 断言 live rerun 执行的所有 SQL 不含 `base_rag_similarity`；
  双库对照（worthiness 值反转、且与真实相关序及字典序都相反）→ 报告逐字节一致且
  NDCG=1.0（若读了 worthiness 会得到 0.63）
  （`test_no_sql_statement_reads_base_rag_similarity`、`test_live_rerun_ignores_worthiness_values`）。
- 快照溯源 fail-closed：SHA 不匹配 / 未注册快照 / manifest 缺失 → exit 2
  （`test_snapshot_sha_mismatch_exits_2` 等）。
- skipped 不造假分数：`--rerun --ranker fts_v2`、hybrid 无向量/无缓存 →
  `ranker_status` 以 `skipped:` 开头、NDCG 为 null
  （`test_live_fts_v2_skipped_without_fake_scores`、`test_hybrid_live_skipped_*`）。

### 3.3 synth_preference_stress 7 门实测

```bash
uv run python scripts/token_sprint/synth_preference_stress.py \
  --sprint-root C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint   # exit=0
# all_expectations_met: true；fixtures: 10 passed / 10；
# gate 1..7（含 6a/6b 双变体）：fail_reason_prefix_present=True,
# pass_reason_prefix_absent=True, exact=True（8/8 矩阵项全绿）
```

7 门各自有"刚好失败 / 刚好通过"边界夹具（29/30 条有效反馈、14/15 like、9/10 dislike、
2/3 源视频、share 0.4333 vs 恰 0.40（严格 `>`）、无向量 / 模型不匹配、29/30 目标向量），
`gate_reasons` 精确字符串被锁死（前缀稳定）。全程内存 / tmp SQLite，
未写共享根与任何生产库，未触碰 preference-memory 开关与配置文件。

## 4. "生产 FTS 为空"复现证据（定性；W7 两假设框架，不预设结论）

### 4.1 复现证据（本会话只读采集）

**生产库**（`dist/GifAgentUI/data/library.db`，`mode=ro` 只读连接，as_of 2026-08-29）：

| 观测 | 值 |
|------|----|
| `candidate_search_fts` 行数 | **0**（表存在于 sqlite_master，但为空） |
| `search_index_state` 行数 | **0**（表存在，无任何重建进度记录） |
| `candidate_gifs` | 13,007（持续漂移；设计文档 as_of 2026-08-28 记录 11,622；一切以 W1 快照为准） |
| `candidate_vectors` | 9,328 |
| 非空 `vlm_summary_json` / `tags_json` | **0 / 13,007** 与 **0 / 13,007** |

**W1 快照**（canonical，`data/token_sprint/library_snapshot.db`，sha 与 manifest 一致，
w1-reviewer `.done` verdict=pass）：显式清空 FTS + state 后强制全量重建，
三计数一致（candidate_gifs=FTS=distinct candidate_id=12,833）、`errors=0`、
state 与 FTS 一致 —— 证明**重建路径本身可用**；快照内
非空 `vlm_summary_json`/`tags_json` 同样为 **0/12,833**（与 manifest `t0_probe.root_cause`
独立核实一致：文本字段上游从未填充）。

### 4.2 两假设（W7 框架）—— 仅陈述事实与假设，不定级

**假设 (a)：索引生命周期缺口。** 生产库中 FTS 表与 state 表都存在，但两者均为 0 行：
即生产不仅 FTS 为空，`search_index_state` 也从未留下任何一次重建（哪怕部分批次）的
进度记录。这与"state 显示已完成但 FTS 被清空"是**不同的状态组合**；初步事实更符合
"重建在生产环境从未被触发/从未写入第一批"。生产重建的初始化/触发链路是否存在缺口、
缺口在何处，属于 W7 的调查结论范围，本报告不预设。

**假设 (b)：异常可观测性。** `_rank_with_text` 在 FTS MATCH 处捕获
`sqlite3.OperationalError` 后静默继续（`app/services/library_search.py:449-451`）。
这是独立成立的代码事实。同时必须指出（与"静默无告警"的笼统说法相反）：
搜索服务对空/缺索引**并非完全不可见** —— `index_health()`/`search()` 返回
`degraded=True` 与 `FTS index: 0/N candidates indexed` 诊断文本
（`library_search.py:158`、`:207`），UI 侧读取并展示
（`app/ui/tabs/search.py:92-105`）。`_rank_with_text` 的吞异常是否应保留日志/指标，
由 W7 定级后处理；本报告不改产品代码。

**补充事实（不推断）：** 生产与快照的 caption/tags/scenario 文本字段均为 0 行非空。
这意味着即使生产 FTS 被重建，索引内容也只是空文本（W1 快照已实测该路径可执行）。
该事实同时解释了 T0 探针 60/60 全拒绝 → 模型标注取消的因果链
（manifest `t0_probe`）。

## 5. 规模契约：本冲刺实际 vs 计划

| 单位 | 计划契约 | 本冲刺实际（T0 fallback） |
|------|---------|--------------------------|
| `query_bank_v1.jsonl` | 100–200 条 | 0（未生成） |
| `judged_queries_v1.json` | 20–40 条 fully judged | 0（标注取消） |
| 单查询池 pair | 480–2,880 | 0 |
| 双模板模型判断 | 960–5,760 次 | 0 |
| NDCG 数字 | 仅 fully judged 查询 | `null`（空输入优雅报告，非 0 伪装） |

harness 已按 fully-judged 契约就绪：未来一旦有 pool 构建（新建 `pool_version` +
`ranker_set_hash`）与双模板标注产出 qrels，即可直接运行本报告 §3.1 的命令得到
NDCG@10/@24 与 judged 覆盖率，无需改 harness。

## 6. 验证矩阵对照（W2 行）

| Gate | 状态 |
|------|------|
| NDCG 过滤（disputed 不进平均、不按 grade 0） | PASS（测试锁死） |
| 两次运行数字一致（同 pool_version） | PASS（纯函数 + CLI 双路径） |
| 可追溯（每条 qrels 有 pool_version / ranker_set_hash / pool_sources） | PASS（schema 强制 + 一致性 fail-closed） |
| 规模（judged ≥20；pair 与判断次数分记） | N/A — 标注因 T0 fallback 取消（见 §5） |
| 银一致率 ≥80% | N/A — 未发生标注 |
| 无假基线（不读 `base_rag_similarity`） | PASS（SQL spy + 双库对照） |
| 7 gates（synth 14 夹具绿） | PASS（10 夹具 / 14 门级断言全绿） |

## 7. 未决问题 / 移交 W7 与后续

1. 假设 (a)/(b) 的定级与结论归 W7；本报告仅提供生产只读复现数据与 file:line 证据。
2. 生产 `search_index_state` 为 0 行意味着若要在生产重建索引，应先核对
   `rebuild_index()` 的增量续跑语义（W1 已在快照上验证"先清空再重建"路径）。
   生产库执行属冲刺窗口外，本 W 未做、也不得做。
3. evalsets 契约文件（query bank / qrels / pools）留待未来有数据轨时按 §5 契约填充；
   `build-pools` 子命令按 scope_ruling 本冲刺未实现，harness 的存储 rank 输入可来自
   任何符合 qrels schema 的产出方。
4. `docs/reports/2026-08-token-sprint/usage-log.md` 由协调会话独占维护（约束 13），
   本报告不代写。
