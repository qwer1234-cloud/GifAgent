# W7 定向代码审计 — 总结（w7-summary）

- Workstream: W7，分支 `sprint/w7-code-audit`，基线 `sprint_base_sha = a6c9fb0a0dcbe5871efeaa846b6937df2efc7e9e`。
- 性质：P0 read-only 审计。`git diff -- app/` 为空（未改任何产品代码/测试）；全部产物为本目录六份 markdown。Finding 只登记、不修复（修复归 W8/后续）。
- 执行说明：commit 按协调会话要求从主 checkout 以 `git -C .worktrees/sprint-w7-code-audit commit` 执行，未触发 Mimosa L3 拦截。

## 1. 五维报告索引与计数

| 维度 | 报告 | Findings | 最高严重级 |
|------|------|----------|-----------|
| 1 SQLite（busy_timeout/WAL/连接生命周期 + 假设 a） | `w7-audit-sqlite.md` | 4（F-01..F-04，均 Low） | Low |
| 2 并发（lease/heartbeat、sync 调度线程、Gradio 共享状态） | `w7-audit-concurrency.md` | 7（F-05..F-11；Medium×3，Low×4） | **Medium** |
| 3 Windows（路径/编码/原子写） | `w7-audit-windows.md` | 4（F-12..F-15；Medium×2，Low×2） | **Medium** |
| 4 HTTP（超时/重试一致性 + 假设 b） | `w7-audit-http.md` | 2（F-17..F-18，均 Low；假设 b 结论定级 Low） | Low |
| 5 资源泄漏（subprocess/句柄/临时目录） | `w7-audit-resources.md` | 3（F-19..F-21；Medium×1，Low×2） | **Medium** |
| **合计** | | **20** | **Medium（无 High/P0）** |

编号 F-16 空缺（Windows 报告定稿时移除了一个交叉引用条目，为避免重号不回收）。每条 finding 均含 file:line 证据与最小复现测试草图（草图按计划允许留在报告内，未新增 `tests/token_sprint/` 文件——全部为 `pytest.mark.skip(reason="w7-sketch")` 级别的伪代码，落地由 W8/后续执行）。

## 2. 假设 (a)：索引生命周期 — 结论

**一句话版**：生产 `candidate_search_fts` 恒为 0 行的根因是**重建触发链路缺口**——FTS 表在 `init_db`（`app/db.py:148` → `workbench_schema.py:40-52`）只被创建为空表，唯一填充者 `LibrarySearchService.rebuild_index`（`app/services/library_search.py:311-316`）仅暴露为手工 `POST /api/workbench/search/rebuild`（`app/routers/workbench.py:214-224`），Gradio UI 全目录 grep `rebuild` 为 0 命中、候选写入路径（`app/services/candidates.py:79`、`app/routers/candidates.py:145`）与 FTS 无同步（DDL 无触发器），因此索引自创建起从未被构建且永不自愈；但服务以 `degraded=True` + 诊断文本降级可见（`app/services/library_search.py:156-167`），UI 有展示（`app/ui/tabs/search.py:92-106`）——「静默无告警」不成立，缺口在**自动修复触发器缺失**而非可观测性。修复建议（归 W8）：启动/候选写入后自动重建，或在搜索页暴露重建按钮。

## 3. 假设 (b)：`_rank_with_text` 异常可观测性 — 结论

**一句话版**：不应保留静默——注释给出的理由（"表为空"）不成立（空表 MATCH 返回 0 行而不抛错），该 `except sqlite3.OperationalError: pass`（`app/services/library_search.py:449-451`）实际捕获的是索引损坏/表缺失等真故障，并把故障折叠成 `total=0` 的"无结果"（向量分支以 `fts_results` 非空为前提，`:455`）；`index_health` 的同型吞法（`:186-187`）还会把"损坏"误报成"0/N 未建索引"，与假设 (a) 的"从未构建"症状混同。**最低修复：加 `logging.warning`（无新依赖）；metric（如 `fts_match_error_total`）为更优方向，但 `app/` 当前无指标框架，建议随 W8 FTS v2 落地，W7 不引入。** 定级：Low（诊断质量缺陷）。

## 4. W4 存活突变登记（存活-as-bug）

**占位**：canonical `sprint_manifest.json`（共享根 `data/token_sprint/`，revision 1，as_of 2026-08-28T18:02Z）中 `artifacts: []`——W4 突变测试产物尚未按 manifest 发布，evalsets 目录为空。按全局约束 5/6，消费前须校验 `.ready`/`.done`/SHA-256 四方链；当前无可校验对象，故本节不登记任何突变项。W4 产物发布后由 reviewer/协调会话按流程补录本节与抽查表。

## 5. W6 ignore 登记

**占位**：同理，W6 的 mypy 产物未出现在 canonical manifest `artifacts` 中，无 `.done` 可校验，本节不登记任何 ignore 条目。

## 6. Reviewer 抽查表（reviewer 填写；本节为 executor 预留结构）

按计划「每维度抽查 3 条，误报率 < 1/3」：

| 维度 | 抽查 Finding | 证据复核（file:line 成立?） | 复现草图合理? | 误报? |
|------|--------------|---------------------------|---------------|-------|
| 1 SQLite | F-__ / F-__ / F-__ | | | |
| 2 并发 | F-__ / F-__ / F-__ | | | |
| 3 Windows | F-__ / F-__ / F-__ | | | |
| 4 HTTP | F-__ / F-__ / F-__ | | | |
| 5 资源 | F-__ / F-__ / F-__ | | | |

（reviewer 结论与 `.done`/receipt 由 reviewer 会话产出；executor 不写 `.done`、不改 canonical manifest。）

## 7. 验收矩阵自查

| Gate | 状态 |
|------|------|
| 两假设各有证据与结论，无「无告警 P1」预设 | 通过（§2/§3，均以触发链/语义核查为据） |
| 每条 finding 有 file:line | 通过（20/20） |
| 抽查 5×3，误报率 <1/3 | 待 reviewer |
| 只读：`git diff -- app/` 为空 | 通过（见提交前校验记录） |
| degraded 事实引用 `library_search.py:156` 与 `search.py:92` | 通过（sqlite 报告 §2 与 §1/假设 a） |
