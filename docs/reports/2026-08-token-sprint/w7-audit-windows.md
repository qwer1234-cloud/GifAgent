# W7 定向代码审计 — 维度 3：Windows 路径分隔符 / 编码 / 原子写

- Workstream: W7 (read-only audit)，分支 `sprint/w7-code-audit`，基线 a6c9fb0a。
- 平台事实：目标环境为 win32 + zh-CN  locale（GBK/cp936 为默认 ANSI 代码页），Python 3 文本模式 `open()`/`subprocess text=True` 在未显式传 `encoding` 时使用 locale 编码。
- W3 报告引用说明：共享根 `data/token_sprint/` 目前无 `reports/`，W3 数据审计报告未发布（未 `.done`），故本报告不引用 W3 产物，全部以 W7 自己的 file:line 证据为准（符合计划“若 W3 已 .done 先校验 SHA 再引用”的前置条件——条件未满足）。

## 0. 做对了的部分（正证据）

- **原子写模式与 coordinator 规则一致（临时文件与目标同目录 + `os.replace`）**：`app/pipeline/stage_io.py:168-171`、`app/pipeline/scoring.py:99-102`、`app/pipeline/stages/materialize.py:259,287` 与 `:401-404`、`app/quality_lab/promotion.py:64-70`、`app/quality_moe/repair.py:380,466`、`app/services/batch_queue.py:200-208`（`mkstemp(dir=target.parent)` + `os.fdopen(fd, "w", encoding="utf-8")` + `os.replace`，`finally` 清残留）、`app/services/collections.py:96-99`、`app/services/desktop_export_sync.py:217-223`（`mkstemp(dir=dest.parent)` + `os.replace`）。
- **全部 `ensure_ascii=False` 的 JSON 写出都显式带 `encoding="utf-8"`**（逐点核对：`stage_io.py:169`、`scoring.py:100`、`direct.py:772,775`、`materialize.py:402`、`promotion.py:67,252`、`batch_queue.py:202`）——非 ASCII（中文标签/摘要）不会落到 GBK 编码路径。
- **文本读取显式 UTF-8**：`app/ui/workbench.py:93-95`（layout.css）、`app/services/indexer.py:63,81`（id_map/manifest）。
- **product `app/` 内未发现硬编码反斜杠路径或盘符**（grep `C:\`、`E:\` 型字面量仅命中 scripts/ 下的历史一次性脚本）；`app/services/preprocess.py:16` 用 `f"{frames_dir}/{media_id}"` 正斜杠拼接——Windows API 接受正斜杠，仅为风格问题，不构成 finding。

## 1. Findings

### F-20260829-12
- Severity: Medium（zh-CN Windows 上 `%APPDATA%`/`wsl.exe` 输出是 UTF-8 字节流，用 GBK 解码：报错文案/地址串 mojibake；遇 GBK 中非法字节序列直接抛 `UnicodeDecodeError` 使 `_run_ollama` 崩溃。`encoding="utf-8"` 的正确写法在同一个仓库里已有（对照项），属明确不一致）
- File:line: `app/services/scheduler.py:23`（问题点）；对照 `app/services/preprocess.py:38-39`（正确示范：`encoding="utf-8", errors="replace"`）
- Evidence: `subprocess.run(cmd, capture_output=True, text=True, timeout=60)` 未传 `encoding`/`errors`，解码走 locale（cp936）。`cmd` 由 `_ollama_list` 等传入（`wsl.exe ... ollama ...` / `ollama ps` 类命令，`scheduler.py:29-46`），其 stdout/stderr 为 UTF-8。含中文/emoji 的错误输出或非 GBK 可映射字节会导致解码异常或乱码进入日志与返回值。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_scheduler_decode_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_run_ollama_decode_is_locale_independent(monkeypatch):
      def fake_run(cmd, **kw):
          assert kw.get("encoding") == "utf-8", "text=True 无 encoding → cp936"
          return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      monkeypatch.setattr(scheduler_module.subprocess, "run", fake_run)
      ModelScheduler()._run_ollama(["ollama", "ps"])
  ```
- Related hypothesis: none

### F-20260829-13
- Severity: Medium（frozen exe 首启一次性拷贝：library.db 处于 WAL 模式（`app/db.py:10`），只拷主文件会丢掉尚未 checkpoint 进主库的已提交事务；若源库正被另一实例写入还可能得到撕裂页/`malformed`。FAISS 的 `media_index.faiss` 与 `id_map.json` 是成对状态，逐文件 copy2 无成对原子性）
- File:line: `app/ui/launcher.py:58-75`（`_link_data_files`：:65-67 拷 library.db；:71-75 逐文件拷 faiss 目录）
- Evidence: 拷贝只针对 `library.db` 本体，不含 `library.db-wal` / `library.db-shm`；WAL 模式下最近的提交常驻 `-wal`。FAISS 目录循环 `shutil.copy2` 逐个文件，若拷贝期间 `MediaIndex.add`（`app/services/indexer.py:106-118` 的 faiss→id_map 顺序写）在源端进行，会得到 faiss 新/id_map 旧（或反之）的撕裂对。触发面：仅当 exe 位于项目内且项目 `data/library.db` 存在（launcher.py:51-55 的条件）且另一实例同时在写。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_link_data_wal_copy_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_link_data_files_copies_wal_sidecars_or_refuses_live_db(tmp_path):
      # 造 src/library.db（WAL）+ 写入一条事务（留在 -wal 中，不做 wal_checkpoint）
      # 调 _link_data_files(exe_dir, data_dir, project_data)
      # 断言：exe_db 内能读到该事务（要求拷 -wal 或先 checkpoint），当前实现失败。
  ```
- Related hypothesis: none

### F-20260829-14
- Severity: Low（`.stage_result.json` 损坏时 `_try_recover` 按“无法恢复”处理并重跑（`worker.py:451-455` 返回 False），代价是多一次 stage 执行；但租约过期后两 writer 交替写同一文件时可能读到“半新半旧且能解析”的结果文件——`schema_version/stage_id/stage_name` 校验（:458-463）会拦截跨 stage 错配，残余风险是同 stage 两 attempt 的内容混淆。对照同仓库 `app/pipeline/stage_io.py:168-171` 的原子写惯例，这里缺 `tmp + os.replace`）
- File:line: `app/task_engine/worker.py:575-576`（`_save_result`）
- Evidence: `with open(result_file, "w") as f: json.dump(data, f)` —— 非原子、非 fsync。崩溃/断电时留下截断 JSON。编码未显式指定，但 `json.dump` 默认 `ensure_ascii=True` 输出纯 ASCII，GBK 环境下安全（这是与 F-20260829-12 不同类的“未显式 encoding 但被内容兜底”情形，特此记录以免误报）。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_save_result_atomic_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_stage_result_write_is_atomic(tmp_path):
      # monkeypatch json.dump 在中途抛 KeyboardInterrupt，
      # 断言 result_file 要么是完整旧内容要么不存在（tmp+os.replace 语义）；
      # 当前实现会留下截断文件。
  ```
- Related hypothesis: none

### F-20260829-15
- Severity: Low（句柄与文件卫生：父进程句柄泄漏使 `batch_subprocess.log` 在批处理运行期间无法删除/轮转；PID 文件非原子写是既有模式的复制）
- File:line: `app/ui/tabs/control.py:605-614`（`log_file = open(...)` 后 `Popen(stdout=log_file, ...)`，父进程从不 `close()`；:611-612 `open(PID_FILE, "w")` 非原子无 encoding）；同型：`app/ui/legacy_candidate_review.py:381-386`
- Evidence: 每次 "Start batch" 在父进程留下一个打开的写句柄直到进程退出（Windows 上该文件被独占锁定）。PID 文件为纯数字文本，无 encoding 风险被内容兜底；写入非原子，窗口极小。`taskkill` 调用（control.py:564-566）带了 `timeout=10`，无超时问题。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_batch_log_handle_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_start_batch_closes_parent_log_handle(tmp_path, monkeypatch):
      # monkeypatch Popen 为记录传入 stdout 句柄的 stub；
      # 调 start_batch 后断言该句柄已关闭（os.fstat 检查 / weakref.finalize）。
  ```
- Related hypothesis: none

### 交叉引用（不单独立 finding，不计数）
- `app/pipeline/stages/discover.py:15-23`、`gif_clip.py:62-70`、`rank_dedup.py:246-253`：ffprobe `text=True` 无 `encoding`（stdout 为纯数字时长，实际安全；stderr 可能含 UTF-8 路径 → GBK mojibake），且无 `timeout`。超时部分归入并发维度 F-20260829-06 与资源维度对应 finding；编码部分因 stdout 内容为 ASCII、影响限于 stderr 诊断文本，不单独定级。

## 2. 汇总

| Finding | 一句话 | 定级 |
|---------|--------|------|
| F-20260829-12 | `scheduler.py:23` text=True 无 encoding → GBK 解码 UTF-8 输出 | Medium |
| F-20260829-13 | frozen 启动拷贝 WAL 库缺 -wal 副本 + FAISS 成对文件非原子 | Medium |
| F-20260829-14 | `.stage_result.json` 非原子写（ensure_ascii 兜底了编码风险） | Low |
| F-20260829-15 | 批处理日志句柄父进程不关 + PID 文件非原子 | Low |

注：plan 中提到的 W3 mojibake 证据因 W3 报告未发布（共享根无 reports/）未被引用；F-20260829-12 独立成立于本仓库代码。
