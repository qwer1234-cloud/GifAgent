# W7 定向代码审计 — 维度 4：HTTP 超时/重试语义 + 假设 (b) FTS OperationalError 可观测性

- Workstream: W7 (read-only audit)，分支 `sprint/w7-code-audit`，基线 a6c9fb0a。
- 范围：`app/services/llm_client.py`、`app/services/embedding.py`、`app/ui/api_client.py`，以及全部散点 httpx 调用方；**只列不一致，不改代码对齐**（plan 明确 non-goal）。

## 1. 客户端横评表

| 客户端 / 调用点 | 超时形式 | 数值 | connect/read 分离 | 重试 | 重试间状态处理 | 失败语义 |
|---|---|---|---|---|---|---|
| `embedding._post_with_retries`（`app/services/embedding.py:129-133`） | `httpx.Timeout(config.request_timeout_s, connect=5.0)` | read=request_timeout_s(config) | **是**（connect=5.0，:132） | **是**：attempts 来自 `config.retry_attempts`，指数退避（:174），可重试状态码 `408/429/500/502/503/504`（`ollama_runtime.py:29`），Timeout/TransportError 亦重试（:147-152） | 每次尝试前 `ensure_runtime_ready()` 重新解析 WSL 地址（:125-127），失败即 `invalidate_runtime()`（:146-152） | 抛结构化 `EmbeddingRuntimeError(retryable=...)`（:176-184） |
| `embedding._ollama_describe_image`（`app/services/embedding.py:231-243`） | 标量 | 120 | **否** | **无** | 无 | **吞掉一切异常返回 None**（:243-244） |
| `embedding.check_embedding_service`（:63-66） | `httpx.Timeout(read=5.0, connect=3.0)` | 5/3 | 是 | 无（探测一次） | - | 抛 `EmbeddingServiceUnavailable` |
| `llm_client.generate_llm_text`（`app/services/llm_client.py:126-135,142-156,162-175`） | 标量 | `llm.timeout_s` 默认 120（:29）；调用方 `app/services/llm.py:63` 显式传 120 | **否**（标量同时作用于 connect/read/write/pool） | **客户端内无**；重试在调用方：`app/services/llm.py:66` `for attempt in range(3)`（无退避、无状态码分类） | 无地址重解析（ollama base_url 固定读配置/默认 127.0.0.1:11434，:49） | `raise_for_status()` 抛 HTTPStatusError |
| `llm_client.wait_for_llm`（:185-197） | 标量 | ping 10 | 否 | 轮询式：每 2s 一次直到 30s（自造重试） | 无 | 返回 bool |
| `api_client._request`（`app/ui/api_client.py:29-36`） | 标量 | 默认 10（:31） | 否 | 无 | 无 | 返回 `{"error": ...}` dict（吞 RequestError，:35-36） |
| `quality_lab.runner.TaskEngineClient`（`app/quality_lab/runner.py:79`） | 标量 `httpx.Client(timeout=10.0)` | 10 | 否 | 无（409 有专门处理 :100-110） | 无 | 抛/返回错误 |
| `app/ui/tabs/search.py:23-28`（timeline） | 标量 | 15 | 否 | 无 | 无 | 吞异常返回占位 HTML（:31-32） |
| `app/ui/tabs/search.py:80-88`（search） | 标量 | 30 | 否 | 无 | 无 | 吞异常返回错误文案（:87-88） |
| `app/ui/launcher.py:167`（就绪探测） | 标量 | 2 | 否 | 轮询 30 次 ×1s | 无 | 返回 bool |
| `app/services/scheduler.py:36-46`（`_wait_for_model`） | 标量 | 10 | 否 | 轮询 30s/2s | 无 | 静默 `except: pass` |
| `app/ui/legacy_candidate_review.py:118-140` | 标量（127/132 附近） | 短 | 否 | 无 | 无 | dict/异常各异 |

## 2. Findings（不一致点）

### F-20260829-17
- Severity: Low（同一仓库对同一后端（Ollama）存在两套重试哲学；`_ollama_describe_image` 的静默 None 使图片嵌入链路在服务抖动时直接退化为"无嵌入"，且无任何日志，与同模块 `_post_with_retries` 的结构化错误形成落差）
- File:line: `app/services/embedding.py:223-244`（对照 :103-184）
- Evidence: 同一模块内，文本嵌入走 `_post_with_retries`（attempts+退避+状态码分类+地址重解析），而 VLM 图像描述 `_ollama_describe_image` 用固定 `timeout=120` 标量、零重试、`except Exception: return None`（:243-244）。下游 `compute_image_embedding`（:292-301）把 None 视为"无法描述"，`compute_media_embedding`（:304-328）据此静默放弃整个 media 的向量。三种失败表象（重试后抛错 / 不重试抛错 / 静默 None）并存。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_describe_image_retry_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_describe_image_silent_on_transient_503(monkeypatch):
      calls = []
      def fake_post(*a, **kw):
          calls.append(1)
          raise httpx.ConnectTimeout("t")
      monkeypatch.setattr(embedding.httpx, "post", fake_post)
      assert embedding._ollama_describe_image("x.jpg") is None
      assert len(calls) == 1   # 暴露：文本嵌入路径会重试 N 次，这里 1 次即弃
  ```
- Related hypothesis: none

### F-20260829-18
- Severity: Low（超时值与形式不统一的维护性问题：`2/3/5/10/15/30/60/120` 八种取值、标量 vs `httpx.Timeout` 两种形式散布 11+ 处；LLM 标量 120s 意味着 connect 也等 120s——对本地 11434 端口，服务未起时每个请求要等满 TCP 超时之外的 120s 才抛错，而 embedding 同场景 connect 只等 5s）
- File:line: `app/services/llm_client.py:29,126-135`（对照 `app/services/embedding.py:32-43,132`）；全表见上文 §1
- Evidence: `httpx.post(..., timeout=timeout_s)` 标量会把 connect/write/pool 一并设为 120；`generate_llm_text` 的三个 provider 分支均如此（:134,155,174）。`llm.py:66` 的调用方 3 次重试无退避，最坏 3×120s=6 分钟占用 Gradio/FastAPI worker 线程。`api_client.py:31` 默认 10s 又远小于 search 页的 30s（`tabs/search.py:83`）——同为本地 8000 端口的 Control 页与 Search 页行为不一致。
- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_timeout_matrix_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_local_llm_connect_timeout_is_bounded():
      # 指向未监听端口的 base_url，计时 generate_llm_text 首次抛错耗时；
      # 期望（对齐 embedding 语义）connect 段有独立小上限，当前为 120s 标量。
  ```
- Related hypothesis: none

## 3. 假设 (b)：`_rank_with_text` 吞掉 `sqlite3.OperationalError` 是否应保留日志/指标

- File:line: `app/services/library_search.py:449-451`（`except sqlite3.OperationalError: pass`，注释 "FTS table might be empty or not exist"）；同型吞法 `library_search.py:186-187`（index_health 内 `indexed = 0`）、`library_search.py:133-134`（filter 计数吞掉）。

### 事实核查

1. **注释的理由对"空表"不成立**：FTS5 表为空时 `MATCH` 查询正常返回 0 行，**不抛** `OperationalError`。因此 :449 的 except 实际捕获的是真故障：表不存在（理论上 `LibrarySearchService.__init__` 已 `apply_search_schema`，:103，正常路径不可能）、**FTS 索引损坏**（`database disk image is malformed`）、`unable to use function MATCH in the requested context` 等查询构造问题。
2. **吞掉后的用户可见后果**：`fts_results` 为空 dict → `_rank_with_text` 中向量分支以 `fts_results` 非空为前提（:455 `if self.embedder is not None and fts_results`），故 `vec_sims` 亦空 → `scored` 为空（:461-471）→ `total=0`。即**带文本的搜索把索引故障呈现为"0 结果"**，与"确实无匹配"在数据上不可区分；`pre_total > 0`（:130-137）证明候选存在，进一步加剧误导。
3. **degraded 信号覆盖不到这一层**：`index_health()`（:169-223）只统计行数覆盖（`indexed >= total`），自身 `OperationalError → indexed=0`（:186-187）会把"索引损坏"报告成"0/N 未建索引"——同一症状、两种根因，现有诊断文本无法区分。

### 结论（三选一，显式）

**应保留可观测性：最低限度加 log（`logging.warning` 或 `logging.exception`，带 query 文本截断与异常串），metric 为更优但当前 `app/` 无任何指标框架（grep 无 prometheus/statsd/counter），引入新依赖违反 sprint 约束 8。**

- 保留静默：**否**——如上，静默把损坏折叠成"0 结果"，属于可观测性缺陷而非容错（容错语义应是"降级到无 FTS 排序"并同时记录原因）。
- 应 log：**是（最低修复）**。一行 `logger.warning("FTS MATCH failed; falling back: %s", exc)` 不改变行为、不加依赖、让 degraded 诊断可归因。
- 应 metric：**方向正确但当前无基础设施**；建议随 W8 FTS v2 一并落地（计数器：fts_match_error_total），W7 不引入。
- 定级：Low（诊断质量缺陷，不丢数据、不崩溃；触发前提是 FTS 表异常，而这正是假设 (a) 判定"从未构建"之外的第二种 0 行成因——损坏）。

- Repro sketch:
  ```python
  # tests/token_sprint/test_w7_fts_opearational_error_logging_sketch.py
  @pytest.mark.skip(reason="w7-sketch")
  def test_rank_with_text_logs_operational_error(tmp_path, caplog):
      conn = sqlite3.connect(tmp_path / "t.db"); conn.row_factory = sqlite3.Row
      # 建 candidate_gifs + candidate_vectors 最小行集；**不**建 candidate_search_fts
      svc = LibrarySearchService(conn, embedder=None)
      with caplog.at_start? -- 用 caplog.at_level(logging.WARNING):
          page = svc.search(SearchQuery(text="hello"))
      assert page.total == 0                     # 现行为：静默 0 结果
      assert any("OperationalError" in r.message for r in caplog.records)  # 期望：有日志
  ```
- Related hypothesis: b
