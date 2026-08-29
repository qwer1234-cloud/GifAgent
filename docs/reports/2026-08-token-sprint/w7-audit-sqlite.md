# W7 定向代码审计 — 维度 1：SQLite（busy_timeout / WAL / 连接生命周期）

- Workstream: W7 (read-only audit)，分支 `sprint/w7-code-audit`
- 基线: sprint_base_sha = a6c9fb0a0dcbe5871efeaa846b6937df2efc7e9e
- 方法: 静态阅读 + 调用链 grep，未运行任何针对生产 `data/*.db` 的写入；证据均到 file:line。
- 定级说明: 严重级在证据之后给出（模板要求），未预设。

## 0. 连接初始化清单（对照）

| 位置 | timeout | WAL | busy_timeout | foreign_keys | 备注 |
|------|---------|-----|--------------|--------------|------|
| `app/db.py:9-13` (`get_connection`) | 30s | 是 (:10) | 30000ms (:11) | 是 (:12) | 库 DB 主入口；每次调用都执行全部 PRAGMA |
| `app/quality_lab/schema.py:148-153` (`connect_quality_db`) | 30s | 是 | 30000ms | 是 | 与 db.py 一致 |
| `app/task_engine/schema.py:345-351` (`connect_task_db`) | 30s | 是 (:349) | 30000ms (:350) | 是 (:351) | 任务 DB 主入口 |
| `app/task_engine/worker.py:766-768` (heartbeat `own_conn`) | 30s | **否**（依赖文件已持久化 WAL） | 30000ms (:767) | 否 | 见 F-20260829-03 |
| `app/services/desktop_export_sync.py:453-454` | 30s | **否**（依赖文件已持久化 WAL） | **否** | 否 | 见 F-20260829-02 |
| `scripts/fit_score_calibration.py:60` | 默认 5.0s | -（mode=ro URI） | **否** | - | 只读快照脚本，W1 对照路径 |
| `scripts/normalize_candidate_vectors.py:25-28` / `scripts/backfill_candidate_vectors.py:27-30` | 30s | 是 | 30000ms | - | 与 db.py 一致 |
| `scripts/smoke_active_preference.py:197` / `scripts/smoke_library_workbench.py:76` | 默认 | -（:memory:） | - | - | 冒烟脚本，可接受 |

`timeout=30`（秒，sqlite3.connect 参数）与 `busy_timeout=30000`（毫秒，PRAGMA）在所有锚点都是成对且同义的 30 秒，**未发现秒/毫秒混用导致的实际数量级错误**。不一致点是“谁缺失 PRAGMA”（F-02/F-03），不是单位换算。

## 1. Findings

### F-20260829-01
- Severity: Low（一致性/可观测性；CPython 引用计数通常会在函数返回时兜底关闭，实际泄漏是有界的，但显式契约缺失 + 每次连接 3 条 PRAGMA 的开销在每请求路径上重复发生）
- File:line: `app/db.py:195-201`（`save_checkpoint`）、`app/db.py:203-215`（`load_checkpoint`）；同类模式：`app/main.py:42,72,118,140,152,170`、`app/routers/candidates.py:279,336,452,524,576,611,714,763,806`、`app/services/embedding.py:312,333`、`app/services/indexer.py:116,134,181,216`、`app/services/llm.py:52`、`app/services/scorer.py:58,121`、`app/services/vision.py`、`app/services/preprocess.py:15,68,89`、`app/services/scanner.py`
- Evidence: `save_checkpoint`/`load_checkpoint` 各自 `conn = get_connection()` 后执行 SQL 并 `commit`/返回，全程无 `conn.close()`、无 `contextlib.closing`。对照 `app/routers/workbench.py:195-200` 与 `app/services/desktop_export_sync.py:453-461`，那里有 `finally: conn.close()`。sqlite3 的 `with conn:` 只管事务不管关闭，因此没有隐式兜底。每次 `get_connection()` 还会执行 `PRAGMA journal_mode=WAL`（`app/db.py:10`）——已在 WAL 的库上这仍需短暂持有共享锁，叠加 FastAPI/Gradio 线程池的并发请求，会放大 writer 持锁期间的 busy 等待。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_sqlite_conn_close_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_checkpoint_helpers_close_connection(tmp_path, monkeypatch):
      monkeypatch.setattr(app.db, "DB_PATH", str(tmp_path / "lib.db"))
      app.db.init_db()
      app.db.save_checkpoint("phase", "m1")
      app.db.load_checkpoint("phase")
      # 断言：无残留连接。可用 sys.getrefcount / weakref.proxy 追踪，
      # 或直接静态断言源码文本 "get_connection()" 与 "conn.close()" 成对出现。
  ```
- Related hypothesis: none

### F-20260829-02
- Severity: Low（同机场景下 library.db 已被 db.py 持久化为 WAL，缺省 PRAGMA 实际继承文件属性；只有当用户把 `--library-db` 指向一个非 WAL 的外部库时，长读与管线 writer 的锁竞争才会放大）
- File:line: `app/services/desktop_export_sync.py:453-454`
- Evidence: `conn = sqlite3.connect(str(db_path), timeout=30)` 只设 timeout，不设 `journal_mode` / `busy_timeout` / `foreign_keys`，与 `app/db.py:9-12` 的入口约定不一致。连接从 :453 一直存活到 :461 `conn.close()`，期间执行 `_sync_favorites` + `_sync_pbfs`（含逐文件 `shutil.copy2`，见 :217-223），生命周期覆盖整个同步过程。WAL 是文件的持久属性（`app/db.py:10` 在生产路径已写入），所以缺省 PRAGMA 在同机默认路径下无实际差异——这是“不一致”而非“已证实的锁事故”。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_sync_conn_pragmas_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_run_reconciliation_connection_uses_same_pragmas(tmp_path):
      # 构造一个 journal_mode=delete 的库并放一条 favorite 记录
      # 调 run_reconciliation(library_db=...)，在 _sync_favorites 期间
      # 从另一连接 BEGIN IMMEDIATE 写入，断言不会因 busy_timeout 缺省 5s
      # 而抛 sqlite3.OperationalError("database is locked")。
  ```
- Related hypothesis: none

### F-20260829-03
- Severity: Low（生产为文件库时 WAL 由 connect_task_db 持久化继承；但 db_path 解析失败时心跳退化为完全静默，见并发维度 F-20260829-08 的另一面）
- File:line: `app/task_engine/worker.py:766-768`
- Evidence: `_heartbeat_loop` 内 `own_conn = sqlite3.connect(db_path, timeout=30)` 后只设 `busy_timeout=30000`，不设 `journal_mode`。`db_path` 来源 `worker.py:754-759`：优先 `self._db_path`，否则 `PRAGMA database_list` 的 `row["file"]`——对 `:memory:` 库该值为空字符串，`sqlite3.connect("")` 会开一个空的临时文件库，后续 `UPDATE task_stages` 必然 `no such table`，且异常在 :794-798 被整体吞掉。生产路径 `app/ui/launcher.py:258` 用 `connect_task_db()`（文件库，WAL 已持久化），因此该 finding 在生产的影响是“约定不一致 + 测试语义失真”，不是当前线上锁问题。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_heartbeat_conn_pragma_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_heartbeat_own_conn_pragma_contract(tmp_path):
      conn = connect_task_db(tmp_path / "task.db")
      repo = TaskRepository(conn); worker = TaskWorker(repo, "w", {})
      # 静态断言：_heartbeat_loop 源码中 own_conn 初始化后
      # 应设置与 connect_task_db 相同的 PRAGMA 集合（或经工厂函数创建）。
  ```
- Related hypothesis: none

### F-20260829-04
- Severity: Low（仅当配置把 `database.path` 设为裸文件名时触发；当前默认 `data/library.db` 不触发）
- File:line: `app/db.py:8`
- Evidence: `os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)`——当 `DB_PATH` 为裸文件名（如 `library.db`）时 `os.path.dirname` 返回 `""`，`os.makedirs("", exist_ok=True)` 抛 `FileNotFoundError`。对照 `app/quality_lab/schema.py:144-146` 与 `app/task_engine/schema.py:340-342`，那里都做了 `if parent:` 判空。三处连接工厂中只有 db.py 没有判空。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_dbpath_dirname_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_get_connection_with_bare_filename(tmp_path, monkeypatch):
      monkeypatch.chdir(tmp_path)
      monkeypatch.setattr(app.db, "DB_PATH", "library.db")
      app.db.get_connection().close()  # 期望不抛 FileNotFoundError
  ```
- Related hypothesis: none

## 2. 假设 (a)：生产 `candidate_search_fts` 为 0 行的生命周期归因

**事实链（全部 file:line 证据）：**

1. **建表但从不填充**：`apply_search_schema`（`app/services/workbench_schema.py:40-52`）只执行 `FTS5_DDL`（:20-37）创建空的 `candidate_search_fts` 与 `search_index_state`，DDL 中**没有**任何触发器、`content=` 同步表或填充逻辑。`init_db` 在启动时调用它（`app/db.py:148`；生产入口 `app/ui/launcher.py:95` → `_init_database` → `init_db(apply_preference=True)`）。
2. **唯一的 FTS 写入者是 `rebuild_index`**：全仓库对 `candidate_search_fts` 的 INSERT 只出现在 `app/services/library_search.py:311-316`（及冒烟脚本 `scripts/smoke_library_workbench.py:173`）。
3. **候选写入路径不触达 FTS**：`candidate_gifs` 的 INSERT（`app/services/candidates.py:79`、`app/routers/candidates.py:145`）只写 `candidate_gifs`，无任何后续 FTS 同步。
4. **`rebuild_index` 的唯一触发点是手工 HTTP 端点**：`POST /api/workbench/search/rebuild`（`app/routers/workbench.py:214-224`）。对 `app/ui/` 全目录 grep `rebuild` 为 0 命中——Gradio 搜索页只调用 `/search`（`app/ui/tabs/search.py:80-84`）与 `/videos/{id}/timeline`（:23-28），**没有任何按钮/回调触发重建**；启动路径（`app/main.py`、`app/ui/launcher.py`）也没有调用。
5. **服务并非无告警**：每次搜索都计算 `index_health` 并在 `degraded=True` 时返回诊断（`app/services/library_search.py:156-167`，诊断文本 `FTS index: {indexed}/{total}...` 在 :204-214 生成）；UI 展示于 `app/ui/tabs/search.py:92-106`（`degraded: ...` 拼进状态文本）。计划 v4 修正成立：「静默无告警」不成立。

**结论（假设 a）**：生产索引从未构建的根因是**重建触发链路缺口**——`rebuild_index` 只以手工 POST 端点形式存在，初始化路径（init_db → apply_search_schema）只建空表，候选写入路径（candidates.py / routers/candidates.py）只写基表，Gradio UI 无重建入口；于是 FTS 表自创建起恒为 0 行，服务以 degraded 降级运行且永不自愈。定级建议交由 W8（FTS v2）处理：最小修复是把 rebuild 挂到启动/候选写入后的自动触发，或在搜索页暴露重建按钮；本报告不修代码。

**附带观察（不另立 finding）**：`rebuild_index` 的 `search_index_state` 维护把 `total_count` 记为本次运行的行数、`indexed_count` 以 `state["indexed_count"] + inserted` 累加（`app/services/library_search.py:324-338`），重复重建时 cumulative 会偏离真实总数——只影响诊断展示，不影响 FTS 行数本身。

## 3. W1 快照路径对照（只读，不改 W1）

`scripts/fit_score_calibration.py:60` 以 `file:{db}?mode=ro` URI 只读打开，未设 `busy_timeout`（默认 5s）。W1 的快照副本是静态文件、无并发 writer，5s 默认值可接受；列出仅为完整性。`scripts/token_sprint/` 当前只有 `coordinator/`，未见 W1 快照脚本落盘，无法进一步对照。
