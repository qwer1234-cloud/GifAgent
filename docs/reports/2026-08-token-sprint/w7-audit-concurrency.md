# W7 定向代码审计 — 维度 2：并发（TaskWorker lease/heartbeat、desktop_export_sync 调度线程、Gradio 回调共享状态）

- Workstream: W7 (read-only audit)，分支 `sprint/w7-code-audit`，基线 a6c9fb0a。
- 覆盖对象：`app/task_engine/worker.py`、`app/task_engine/repository.py`、`app/ui/launcher.py`、`app/services/desktop_export_sync.py`、`app/services/indexer.py`、`app/ui/**`（Gradio 回调）、`app/services/ollama_runtime.py`。
- 定级在证据之后给出。

## 0. 做对了的部分（正证据，避免误报）

- **租约认领原子**：`TaskRepository.claim_stage` 全程 `BEGIN IMMEDIATE`（`app/task_engine/repository.py:265`），先提交 `in_transaction` 遗留事务（:262-263），认领 UPDATE 带 `lease_owner` 条件，双 worker 不会同时拿到同一 stage。
- **租约丢失防护（P1-4/Phase 5）**：`_run_stage` 用**每次调用新建的局部** `lease_lost = threading.Event()`（`app/task_engine/worker.py:737-740`），提交结果前检查 `lease_lost.is_set()`（:819-823），异常路径 :840-848 也不再写库——状态不跨 stage 泄漏。
- **Gradio 回调无共享可变状态**：会话数据走 `gr.State`（如 `app/ui/tabs/search.py:186-187`、`app/ui/tabs/review.py:747-749`）；模块级 dict（`app/ui/tabs/review.py:25,200` 等）均为只读常量表。`app/ui/tabs/profile.py:262-292` 的后台回填线程用 `_BACKFILL_STATE_LOCK` + `is_alive()` 双检，只允许一个 worker；`app/ui/workbench.py:72-78` 的路径缓存是幂等计算。
- **ollama_runtime 有锁**：`OllamaRuntimeManager` 的 `ensure_ready`/`invalidate`/`shutdown` 都在 `self._lock` 内（`app/services/ollama_runtime.py:209,240,248`）， keeper 进程所有权有 `_keeper` 单字段管理（:339-355）。
- **desktop sync 的合流（coalescing）**：`request_sync` 与 `_run_loop` 用同一 `Condition`（`app/services/desktop_export_sync.py:470-475, 526-533`），`_pending` 消费与 `_active` 置位在同一临界区，`wait_until_idle` 的注释承诺成立（:514-524）。

## 1. Findings

### F-20260829-05
- Severity: Medium（静默失败放大为重复执行：心跳线程一旦持续失败，主线程既不知道也不停止，stage 会在租约到期后被第二个 worker 认领，同一 stage 双跑；单机 launcher 默认 gpu/cpu 各 ≥1 worker（`app/ui/launcher.py:240-284`），且 CLI `scripts/task_worker.py` 可再起一个 worker 共享同一任务库）
- File:line: `app/task_engine/worker.py:794-798`（吞异常）、`worker.py:771-793`（心跳循环）、`worker.py:807`（`adapter.run` 无超时）
- Evidence: 心跳循环里 `except Exception:` 只做 `rollback`，**无日志、无计数、无上限**。持续失败（例如任务库被第三方连接以 `BEGIN IMMEDIATE` 长时间占用超过 busy_timeout=30s，或 db_path 解析错误导致 `no such table`，见 F-20260829-03）时，线程每 `heartbeat_sec` 静默空转，`lease_lost` 永远不会被置位；同时 `adapter.run(context)` 没有任何超时包装（:807），主线程继续执行。租约 90s（`worker.py:194` 默认）到期后，其他 worker 依 `claim_stage` 的 `lease_expires_at <= now` 分支（`repository.py:270-273`）重新认领同一 stage，两个进程/线程并发运行同一 stage 并都可能走到 `_commit_stage_result`（后到者因 `complete_stage_with_artifacts` 的 owner 校验被拒——需要另证，此处只断言“双跑副作用：work_dir 内产物与 `.stage_result.json` 被两方交替写入”）。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_heartbeat_silent_failure_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_heartbeat_sustained_failure_does_not_stop_stage(tmp_path):
      conn = connect_task_db(tmp_path / "t.db"); repo = TaskRepository(conn)
      worker = TaskWorker(repo, "w1", adapters, lease_seconds=2, heartbeat_seconds=1)
      stage = <创建并 claim 一个 stage>
      blocker = sqlite3.connect(tmp_path / "t.db"); blocker.execute("BEGIN IMMEDIATE")
      # 用一个 sleep(10) 的假 adapter 运行 worker._run_stage
      # 断言（当前行为）：期间无任何日志输出；10s 后另一 worker 可认领同一 stage
      # 期望行为（W8 可改）：心跳持续失败 N 次后置 lease_lost / 至少 logging.warning
  ```
- Related hypothesis: none

### F-20260829-06
- Severity: Medium（指数级放大：任何无超时的 subprocess 卡死都会变成“永不失败的 stage”，且心跳在持续续租，不会降级为 needs_attention；与资源维度 F-20260829-16 是同一根因的两面）
- File:line: `app/task_engine/worker.py:807`（无超时调用 `adapter.run`）；放大器：`app/pipeline/stages/discover.py:15-23`、`app/pipeline/stages/gif_clip.py:62-70`、`app/pipeline/stages/rank_dedup.py:246-253`（ffprobe 无 timeout）
- Evidence: `TaskWorker._run_stage` 对 `adapter.run(context)` 没有超时或取消机制；adaptive pipeline 的 stage 子进程由 `subprocess.run` 驱动（`app/task_engine/adaptive_adapter.py:86`），其中三个 ffprobe 调用未传 `timeout`（对照同文件族 `app/services/video_fingerprint.py:23-28` 有 `timeout=30`）。ffprobe 在损坏文件/网络盘上可能无限挂起；心跳线程照常续租（:771-793），CPU worker 线程被永久占用，任务既不失败也不进入 `needs_attention`，Control 页只能看到“运行中”。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_stage_no_timeout_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_discover_stage_hangs_on_broken_file(tmp_path):
      broken = tmp_path / "v.mp4"; broken.write_bytes(b"\x00" * 64)
      # 在 Linux CI 上可用 FIFO 模拟阻塞 ffprobe；Windows 上可用
      # monkeypatch subprocess.run 为永不返回的 Event.wait。
      # 断言：run_once 在 N 秒后仍未返回（暴露无超时），
      # 且 lease_expires_at 持续被心跳推进。
  ```
- Related hypothesis: none

### F-20260829-07
- Severity: Medium（后果是 FAISS 索引与 `vector_refs`/`id_map.json` 静默不一致；触发概率低——需要两个线程同时 add——但单用户 GUI 里 FastAPI 线程池与 Gradio 回调确实可能并发命中 `get_index()`）
- File:line: `app/services/indexer.py:170-174`（无锁懒加载单例）、`app/services/indexer.py:104-125`（`add` 无锁读改写）、`app/main.py:180-186`（API 线程调用 `get_index().search`）、`app/services/scorer.py:52-55`（同）
- Evidence: `get_index()` 的 check-then-set 没有锁，两个线程可各自构造 `MediaIndex`（各读一次磁盘索引）；落败实例被丢弃，胜出实例之外的所有 `add` 全部丢失。`add()` 内部序列 `idx_pos = self.index.ntotal → index.add(vec) → faiss.write_index(...tmp) → os.replace → _load_id_map/_save_id_map`（:111-118）不是原子的：并发 add 会拿到相同 `idx_pos`，`id_map.json` 后写者覆盖前写者，`vector_refs` 却插入了对应 SQL 行 → `verify_index`（:177 起）口径的 faiss_count/id_map/sql_count 三方漂移。此外 FAISS `IndexFlatIP` 并发 `add`/`search` 无同步（同一实例）。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_faiss_singleton_race_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_get_index_and_add_thread_safety(tmp_path, monkeypatch):
      monkeypatch.setattr(indexer, "FAISS_DIR", str(tmp_path))
      indexer._media_index = None
      threads = [threading.Thread(target=lambda: indexer.get_index()) for _ in range(8)]
      [t.start() for t in threads]; [t.join() for t in threads]
      # 静态/动态断言：MediaIndex.__init__ 只执行一次；
      # 并发 add(vector_i, media_i) 后 len(id_map) == add 次数。
  ```
- Related hypothesis: none

### F-20260829-08
- Severity: Low（生产 launcher/关闭路径传入真实 `stop_event`，窗口小；但 `stop(stop_event=None)` 是公开 API，测试或未来调用者可触发）
- File:line: `app/services/desktop_export_sync.py:479-485`（`stop` 先置 `self._thread = None`）、`:487-501`（`start` 只检查 `self._thread is not None`）
- Evidence: `stop()` 把 `_thread` 置 None 后才 join；若 `stop_event` 为 None（:495-498），循环线程因 `while True` + `self._wake.wait(timeout=0.5)` 仍然存活。此后再调 `start()`（:487-496）会通过 `_thread is None` 检查并spawn 第二个 `_run_loop`，两个循环同时监听同一 `_wake`，`_pending` 触发只唤醒其中一个——合流语义尚在，但“同一时刻最多一个 run”由两个线程共同维持，`run_reconciliation` 可能并发执行，桌面复制失去串行保证。`wait_until_idle` 的等待只认 `_active/_pending` 标志，无法区分有几个循环。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_sync_scheduler_restart_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_stop_without_event_then_start_spawns_second_loop():
      s = DesktopSyncScheduler(run=lambda: time.sleep(0.5))
      ev = threading.Event(); s.start(ev)
      s.stop(None)                       # 不传 event：旧循环未退出
      t2 = s.start(threading.Event())    # 允许启动第二个循环
      # 断言（暴露问题）：存活的目标为 _run_loop 的线程数 >= 2
  ```
- Related hypothesis: none

### F-20260829-09
- Severity: Low（静默停摆类问题；`request_background_sync` 之后不再有任何同步发生且无日志）
- File:line: `app/services/desktop_export_sync.py:536-543`
- Evidence: `_run_loop` 的回调异常处理是 `except Exception`，`finally` 会清 `_active` 并 `notify_all`，随后线程**直接退出循环**——若回调抛 `BaseException`（如 `KeyboardInterrupt` 注入、orjson 之类 cython 扩展抛出的非 Exception 基类——当前依赖里暂无，属防御性缺口），或 `self._run_callback()` 本身是包装后 re-raise BaseException 的 callable，后台同步从此静默停止；后续 `request_sync()` 置位的 `_pending` 无人消费，`wait_until_idle(timeout=3600)` 会等满一小时。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_sync_loop_baseexception_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_scheduler_loop_dies_on_baseexception():
      s = DesktopSyncScheduler(run=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
      ev = threading.Event(); s.start(ev)
      s.request_sync(); time.sleep(0.3)
      s.request_sync()                    # 线程已死，无人消费
      # 断言（暴露问题）：s._pending 仍为 True 且线程不存活
  ```
- Related hypothesis: none

### F-20260829-10
- Severity: Low（当前无调用方，属文档/潜伏缺陷；docstring 承诺与线程模型矛盾）
- File:line: `app/task_engine/worker.py:289-309`
- Evidence: `TaskWorker.heartbeat` 的 docstring 写 "This is safe to call from a background thread"，但实现用 `self._repo.conn`（:300-305）——sqlite3 连接默认 `check_same_thread=True`，跨线程调用会抛 `ProgrammingError`。仓库内 grep 未发现任何调用者（心跳实际走 `_heartbeat_loop` 的独立连接），因此是潜伏问题；若未来有人按 docstring 从信号处理器/线程调用，会得到与文档相反的行为。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_heartbeat_cross_thread_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_public_heartbeat_from_other_thread_raises():
      # 在主线程创建 repo，另开线程调用 worker.heartbeat(stage_id)
      # 断言当前抛 sqlite3.ProgrammingError（与 docstring 承诺相反）
  ```
- Related hypothesis: none

### F-20260829-11
- Severity: Low（可观测性：launcher 会打印一次崩溃，但 UI 无提示、无自动重启，gpu/cpu worker 数从此静默降容）
- File:line: `app/ui/launcher.py:254-274`（`_loop` 的 except-print-exit）
- Evidence: 每个 task worker 线程包在 `try/except Exception: print(...)` 里，线程因任何异常退出后仅控制台打印一行（frozen exe 下是黑窗控制台），没有任何重启或 UI 通知；`_start_task_worker` 返回的 threads 列表只用于 shutdown join。长时间会话中 worker 死亡 → 任务队列停摆，用户视角等同“卡住”。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_worker_no_restart_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_worker_thread_death_is_final():
      # monkeypatch TaskWorker.run_forever 抛 RuntimeError；
      # 调 _start_task_worker 后等待线程退出，
      # 断言（暴露问题）：无重启线程、无 UI 可见状态变化。
  ```
- Related hypothesis: none

## 2. 汇总

| Finding | 一句话 | 定级 |
|---------|--------|------|
| F-20260829-05 | 心跳吞异常 + adapter.run 无超时 → 静默租约失效可双跑 | Medium |
| F-20260829-06 | stage 无超时 → 挂起即永挂（心跳续租） | Medium |
| F-20260829-07 | FAISS 单例与 add() 无锁 → 并发静默丢索引 | Medium |
| F-20260829-08 | sync 调度 stop(None) 后重启可双循环 | Low |
| F-20260829-09 | sync 循环对 BaseException 无防护 → 静默停摆 | Low |
| F-20260829-10 | 公共 heartbeat() 的 docstring 与线程模型矛盾 | Low |
| F-20260829-11 | worker 线程死亡无重启无 UI 提示 | Low |
