# W7 定向代码审计 — 维度 5：资源泄漏（subprocess、文件句柄、临时目录）

- Workstream: W7 (read-only audit)，分支 `sprint/w7-code-audit`，基线 a6c9fb0a。
- 范围：`app/` 与 `scripts/` 的 `Popen`/`subprocess.run`、`TemporaryDirectory`/`mkdtemp`、显式 `open()` 句柄、worktree 外临时文件。未读取任何 GIF/JPG/视频内容（仅读源码）。

## 0. 做对了的部分（正证据）

- **临时目录全部有界**：`app/quality_moe/repair.py:178`、`app/services/temporal_evidence.py:193`、`app/services/video_fingerprint.py:57` 用 `tempfile.TemporaryDirectory` 上下文；`app/quality_moe/sampling.py:202` 的 `mkdtemp` 在 `finally: shutil.rmtree(tmp, ignore_errors=True)` 清理（:276-277），所有 early-return 路径（:214-239）都被 finally 覆盖。`scripts/resize_gifs.py:66-73` 的 palette/tmp gif 在 finally 删除。
- **长驻进程有 owner 化终止**：WSL keeper（`app/services/ollama_runtime.py:350-362` `Popen(wsl.exe --exec sleep infinity)`）只由本进程创建（`_start_keeper_if_needed` 先检查 `_keeper.poll()`，:339-344），`shutdown()` 幂等 terminate→kill（`ollama_runtime.py` `def shutdown`），并注册 `atexit`（:611）；launcher 正常关闭路径显式调用（`app/ui/launcher.py:505-510`）。
- **子进程超时覆盖率**：`app/` 中 22 处 `subprocess.run` 除 pipeline 三处 ffprobe（见 F-20260829-19）外全部带 timeout（2–120s）；`scripts/` 一次抽扫未见缺失（`test_video_batch.py:321` 的 `timeout=14400` 是 4 小时上限，有界）。
- **文件锁句柄闭环**：`InterProcessFileLock.acquire` 打开的 stream 在 busy 失败路径（`app/services/batch_queue.py:75-79`）与 `release()`（:92-107，finally 关闭）都闭环，支持 `with` 语义（:109-113）。
- **legacy 队列启动的日志句柄在 finally 关闭**（`app/ui/legacy_candidate_review.py:533-537`）——对照 control.py 的变体见 F-20260829-20。

## 1. Findings

### F-20260829-19
- Severity: Medium（无超时子进程 + 心跳续租（并发维度 F-20260829-06）= stage 永挂；同点位还缺 returncode 检查与空输出防护：`discover.py:23` 与 `gif_clip.py:70` 直接 `float(probe.stdout.strip())`，ffprobe 失败时对空串抛 `ValueError` → `classify_error` 归为 transient → 无意义重试 3 轮；`rank_dedup.py:254-258` 已示范正确写法）
- File:line: `app/pipeline/stages/discover.py:15-23`、`app/pipeline/stages/gif_clip.py:62-70`、`app/pipeline/stages/rank_dedup.py:246-253`（timeout 缺失）；`discover.py:23`、`gif_clip.py:70`（无 returncode 检查、float 未防护）；对照 `app/services/video_fingerprint.py:23-28,37-42`（同族调用带 `timeout=30`）
- Evidence: 三个 stage 对 `ffprobe` 的调用均 `capture_output=True, text=True` 但无 `timeout`。挂起场景：损坏容器使 ffprobe 卡在 demuxer 探测、网络盘/可弹出介质离线。泄漏形态是**子进程 + CPU worker 线程双卡死**，不是句柄累积。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_ffprobe_timeout_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_pipeline_ffprobe_calls_have_timeout():
      # 静态扫描：解析 app/pipeline/stages/{discover,gif_clip,rank_dedup}.py 的 AST，
      # 断言每个 subprocess.run 关键字含 timeout；再加一个运行时样例：
      # monkeypatch subprocess.run 记录 kwargs，跑 _stage_discover，检查 timeout 存在。
  ```
- Related hypothesis: none

### F-20260829-20
- Severity: Low（父进程句柄泄漏：每次 "Start batch" 泄漏一个独占写句柄，`batch_subprocess.log` 在批处理期间无法删除/轮转；进程退出才释放。legacy 版本（`legacy_candidate_review.py:508-518,533-537`）已示范 finally close，两处不同步）
- File:line: `app/ui/tabs/control.py:605-614`（`log_file = open(log_path, "w", encoding="utf-8", errors="replace")` → `Popen(stdout=log_file, ...)`，无 close）；对照 `app/ui/legacy_candidate_review.py:533-537`
- Evidence: Popen 后父进程不再使用 `log_file`，但从不 `close()`；子进程持有的是继承句柄，父进程这份纯多余。同一函数在 :611-612 还以非原子写创建 PID 文件（Windows 维度 F-20260829-15 已录，不重复计级）。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_control_log_handle_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_control_start_batch_closes_log_handle(tmp_path, monkeypatch):
      # stub Popen 记录 stdout 句柄；调 start_batch；
      # 断言句柄可被父进程立即重命名/删除（Windows 下 open-exclusively 检测）。
  ```
- Related hypothesis: none

### F-20260829-21
- Severity: Low（三条 `os._exit` 早退路径跳过 atexit，WSL keeper `wsl.exe --exec sleep infinity`（`ollama_runtime.py:350-362`）在进程死亡后残留，使 WSL 发行版持续保活；正常关窗路径不受影响（`launcher.py:505` 显式 shutdown））
- File:line: `app/ui/launcher.py:163-165`（API 线程死亡 → `os._exit(1)`）、`launcher.py:461-464`（Gradio 启动失败）、`launcher.py:471-478`（Gradio 就绪超时）；对照 `ollama_runtime.py:604-611`（`atexit.register(_atexit_shutdown)`，仅正常退出触发）
- Evidence: `os._exit` 不执行 atexit 钩子；这三条路径上 `graceful_shutdown`（含 `ollama_runtime.shutdown_runtime()`，launcher.py:505-510）尚未注册或未执行，keeper 进程成为孤儿。同时 `_setup_runtime_files`/`_link_data_files` 已完成的写操作不受影响（进程内 SQLite 连接均已关闭），泄漏面仅 keeper 与可能的 WSL VM 保活。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_keeper_survives_os_exit_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_api_thread_death_leaks_keeper(monkeypatch):
      # 构造 manager，注入假 keeper（记录 terminate 调用的对象）；
      # 模拟 launcher：注册 atexit 后直接 os._exit 路径（抽出为可调用函数）；
      # 断言（暴露问题）：keeper.terminate 未被调用。
  ```
- Related hypothesis: none

## 2. 汇总

| Finding | 一句话 | 定级 |
|---------|--------|------|
| F-20260829-19 | pipeline 三个 stage 的 ffprobe 无 timeout（+2 处 float 未防护） | Medium |
| F-20260829-20 | control.py 批处理日志句柄父进程不关（legacy 版已修，两处不同步） | Low |
| F-20260829-21 | os._exit 早退跳过 atexit → WSL keeper 孤儿 | Low |

跨维度重复引用（不重复计数）：`app/db.py` 连接不显式关闭 → SQLite 维度 F-20260829-01；FAISS 单例 → 并发维度 F-20260829-07；`.stage_result.json` 非原子写 → Windows 维度 F-20260829-14。
