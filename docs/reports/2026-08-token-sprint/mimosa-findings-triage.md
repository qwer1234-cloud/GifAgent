# Mimosa 深扫 findings 逐条分诊（triage）

- Scan: `scan-2026-08-29T10-51-24.177Z-fb9c560718d4`（密封产物 `findings.json`，覆盖度 partial/inconclusive，结论为静态启发式）
- 方法: 逐条打开 `location.file:line` 核实真实代码，回溯污点源到汇点，确认是否有外部可控输入到达及现有防护。**未改任何 `app/` 代码、未写生产库、未打开媒体字节**。
- 服务暴露面: FastAPI `127.0.0.1:8000`、Gradio `127.0.0.1:7861`（`app/ui/launcher.py:26-29`）——本机单用户信任域，无远程攻击者入口。

## 统计

| verdict | 数量 |
|---|---|
| false_positive | **36** |
| needs_hardening | **3**（`scripts/token_sprint/audit_data.py:662/732/734`，低影响） |
| real_issue | **0** |
| **P1（外部输入可触达的注入/穿越）** | **0** |

Mimosa 原始严重级（high 36 / low 3）与本分诊无关：39 条中无一条存在外部可控污点到达高危汇点。

## 汇总表（39 行，按 findings.json 顺序）

| # | class | file:line | verdict | 证据（污点源→汇点 / 现有防护） |
|---|---|---|---|---|
| 1 | insecure-randomness | app/quality_lab/ab_review.py:98 | FP | `random.Random(seed)` 仅做 A/B 盲评左右侧摆放随机化（可复现实验语义）；安全敏感的 `left_token/right_token` 用 `secrets.token_urlsafe(16)`（:106-107） |
| 2 | insecure-randomness | app/quality_lab/manifests.py:115 | FP | seed 由条目内容派生（`_compute_split_seed`），tune/holdout 切分要求确定性复现，非安全用途 |
| 3 | insecure-randomness | app/services/review_queue.py:90 | FP | `random.Random(seed)` 做队列 tie-break 确定性排序，docstring 明示 seed 为可复现参数 |
| 4 | other-security (advisory) | app/config.py:45 | FP | 「污点源」是环境变量 `GIFAGENT_CONFIG` → `open()` **读** YAML 配置；该路径只能由本机进程属主设置（能设 env 即已能执行任意代码），且用 `yaml.safe_load`；advisory 定级过高 |
| 5 | path-traversal | app/pipeline/direct.py:772 | FP | `open("data/adaptive_test_result.json","w")` 字面量路径 |
| 6 | path-traversal | app/pipeline/direct.py:775 | FP | `video_name = splitext(basename(video_path))[0]`（basename 剥离分隔符，Windows 下 `ntpath` 同样处理 `\`），`EXPORT_DIR` 为模块内常量/调用方内部值 |
| 7 | path-traversal | app/pipeline/scoring.py:100 | FP | 写 `frames_dir/scored_checkpoint.json`（helper :43-44）+ `.tmp`；`frames_dir` 为内部 work dir |
| 8 | path-traversal | app/pipeline/stage_io.py:113 | FP | `log_path = join(work_dir,"stage.log")`；`work_dir = Path(config["task_work_dir"]默认 data/task_work)/<Literal StageName>/<uuid4 hex>`（worker.py:395-396；stage_id=uuid4，repository.py:226） |
| 9 | path-traversal | app/pipeline/stage_io.py:169 | FP | `result_path + ".tmp"`；`result_path` 由 worker 按内部 job/stage 结构生成 |
| 10 | path-traversal | app/pipeline/stage_io.py:231 | FP | manifest 文件名来自 `_MANIFEST_NAME` 白名单，fallback 键为 `Literal["discover",...,"materialize"]`（models.py:11-14），无任意后缀 |
| 11 | path-traversal | app/pipeline/stages/materialize.py:402 | FP | `formal_export_dir = join(export_base, video_name)`（:206），`export_base` 为内部配置 `export_base_dir`，`video_name` 同 #6 无分隔符 |
| 12 | path-traversal | app/quality_lab/promotion.py:67 | FP | `_atomic_write` 通用 helper（:65-70）；调用点实参为常量 `CURRENT_CONFIG_PATH="data/current_config.json"`（:40） |
| 13 | path-traversal | app/quality_lab/promotion.py:252 | FP | `versioned_path = join(CONFIG_VERSIONS_DIR, f"{config_id}_{safe_ts}.json")`；`config_id = canonical_hash(config)`（provenance.py:26）为十六进制哈希，无 `..`/分隔符 |
| 14 | path-traversal | app/services/indexer.py:31 | FP | `FAISS_DIR = get("paths.faiss_dir","data/faiss")`（:19，本机配置文件/默认值），固定文件名 + `.tmp` 原子写 |
| 15 | path-traversal | app/task_engine/adaptive_adapter.py:241 | FP | `context.work_dir / "config_snapshot.json"`；work_dir 内部链同 #8 |
| 16 | path-traversal | app/task_engine/adaptive_adapter.py:276 | FP | `context.work_dir / "input_manifest.json"`，同上 |
| 17 | path-traversal | app/task_engine/adaptive_adapter.py:293 | FP | 同 #16（else 分支） |
| 18 | path-traversal | app/task_engine/worker.py:575 | FP | 扫描行号落在 `_save_result` 的 artifacts dict；实际 sink 为 :585 `open(work_dir / self._RESULT_FILE,"w")`，`_RESULT_FILE` 为类常量、work_dir 内部（注：扫描后 commit b4fbf316 对该文件做过 mypy 整改，行号有 ±10 漂移，finding 家族锚点不受影响） |
| 19 | path-traversal | app/ui/legacy_candidate_review.py:386 | FP | `PID_FILE = "data/batch_pid.txt"` 模块常量（:29）；同函数的用户输入 `video_dir` 只进子进程 argv，不进写路径 |
| 20 | path-traversal | app/ui/legacy_candidate_review.py:1444 | FP | `CONFIG_FILE = "configs/models.yaml"` 常量（:33 处 BATCH_LOG_FILE 同理） |
| 21 | path-traversal | app/ui/tabs/control.py:603 | FP | `log_path = join(dirname("data/batch_pid.txt"), "batch_subprocess.log")` 常量派生（PID_FILE :21） |
| 22 | path-traversal | app/ui/tabs/control.py:611 | FP | `open(PID_FILE,"w")`，PID_FILE 常量 |
| 23 | path-traversal | app/ui/tabs/settings.py:417 | FP | `CONFIG_FILE = "configs/models.yaml"` 常量（settings.py:26） |
| 24 | path-traversal | scripts/rag_synth_recover.py:66 | FP | 字面量 `'data/test_jur639_rag_result.json'`（一次性恢复脚本） |
| 25 | path-traversal | scripts/token_sprint/mutation_harness.py:365 | FP | `record = os.environ["W4_SENTINEL_RECORD"]` 由 harness 自身设置、写自身沙箱内 sentinel 记录；harness 另有 `is_relative_to(sandbox)` 导入源校验（:335, :371-378） |
| 26 | sql-injection | app/db.py:158 | FP | `f"ALTER TABLE media ADD COLUMN {col} {ddl}"` 中 `{col}/{ddl}` 来自同循环硬编码元组 `("cluster_id","TEXT")` 等 |
| 27 | sql-injection | app/db.py:164 | FP | 同上（`vlm_attempts/vlm_error` 字面量） |
| 28 | sql-injection | app/db.py:170 | FP | 同上（`frame_annotations` 字面量） |
| 29 | sql-injection | app/db.py:176 | FP | 同上（`annotations` 字面量） |
| 30 | sql-injection | app/db.py:182 | FP | 同上（`vector_refs` 字面量） |
| 31 | sql-injection | app/services/preference_schema.py:11 | FP | `f"PRAGMA table_info({table})"` 的 `{table}` 仅来自模块内字面量调用点（grep 全仓：candidate_gifs/preference_events/candidate_vectors/candidate_vector_exclusions，无外部实参） |
| 32 | sql-injection | app/services/preference_schema.py:20 | FP | `_ensure_columns` 的 table/col/ddl 全为调用点字面量（:178-186, :188-195） |
| 33 | sql-injection | app/services/preference_schema.py:173 | FP | 硬编码元组 `("artifact_id","TEXT"),("provenance_json","TEXT")` |
| 34 | sql-injection | app/task_engine/schema.py:156 | FP | `f"ALTER TABLE task_migrations ADD COLUMN {col} {ddl}"`，字面量元组（与 #26 同族） |
| 35 | sql-injection | app/task_engine/schema.py:283 | FP | `f"DROP INDEX IF EXISTS {index_name}"` 全仓唯一调用点传字面量 `"uq_active_job_directory"`（:164） |
| 36 | sql-injection | scripts/token_sprint/audit_data.py:662 | **needs_hardening** | `f'PRAGMA table_info("{table}")'` 的 `{table}` 来自被审计快照库自身的 `sqlite_master`（:697-701）；DB 是 CLI 指定的**外部文件**（`scan_snapshot(db_path,…)`），连接 `mode=ro` + `query_only`（:692-695）限死影响面；标识符经 f-string 双引号包裹但**不转义内嵌 `"`**，若审计不受信第三方快照，恶意 schema 名可改变查询结构（只读上下文内） |
| 37 | sql-injection | scripts/token_sprint/audit_data.py:732 | **needs_hardening** | `f'SELECT rowid, {select_cols} FROM "{table}"'`；列名/表名同为 schema 自引用（PRAGMA 回读），同 #36 的引号不转义问题 |
| 38 | sql-injection | scripts/token_sprint/audit_data.py:734 | **needs_hardening** | :732 的 `OperationalError` 回退分支，同构 f-string，同 #36 |
| 39 | ssrf | app/ui/launcher.py:167 | FP | `_wait_for_url(url,…)` 仅两个调用点：`f"http://{API_HOST}:{API_PORT}/api/status"`（:443）与 `f"http://{GRADIO_HOST}:{gradio_port}"`（:469）；host 为 `127.0.0.1` 常量、端口为固定值/内部 `choose_local_port` 结果，无外部可控 URL |

## P1 / needs-hardening 详情

### P1：无

39 条中不存在任何「外部可控输入 → 注入/穿越汇点」的通路。所有 high 级 finding 的汇点输入要么是字面量/常量，要么是内部生成的 uuid4/Literal 白名单/内容哈希，要么是本机操作者信任域（配置文件、环境变量、CLI 参数）。

### needs_hardening：scripts/token_sprint/audit_data.py（3 处，合并修复）

- **位置**: `_table_columns`（:662）、`_scan_table`（:732、:734）
- **污点链**: 被审计 DB 文件（CLI `db_path` 参数）→ `sqlite_master.name` / `PRAGMA table_info` 回读 → f-string 标识符插值 → `cursor.execute`
- **现状防护**: `sqlite3.connect(uri, uri=True)` + `mode=ro`、`PRAGMA query_only=ON`（:693-695）；表名经 `f'"{table}"'` 双引号包裹但**未转义内嵌双引号**（SQLite 标识符允许含 `"`，需 `""` 转义）
- **可利用性**: 仅当把**恶意构造的第三方快照 DB** 交给该审计脚本时成立；影响被只读连接限制在「改变查询结构/读自身文件/报错」，无写路径。定级 low、非 P1
- **最小修复提案（不改代码，报告）**:
  1. 标识符转义：`def _quote_ident(name): return '"' + name.replace('"', '""') + '"'`，在 `_table_columns` 与 `_scan_table` 两处替换现有 `f'"{c}"'` 写法（约 4 行改动）；
  2. 或在 `_scan_table` 循环内对 `table`/`columns` 做 `re.fullmatch(r"[\w ]+", name)` 校验，不符合的表计入 `skipped`（reason: `unsafe_identifier`），与现有 `SNAPSHOT_SKIP_*` 机制天然衔接。

## 与 W7 结论的异同

1. **W7 实际并未覆盖本家族**：通读 6 份 `w7-*.md`（sqlite / concurrency / windows / http / resources / summary），其 findings 为 F-20260829-01..22，主题是连接生命周期、busy_timeout/WAL、超时重试矩阵、Windows 路径编码与原子写——grep 全部 w7 报告，**没有任何对路径穿越 / SQL 注入 / 弱随机的定性文字**。summary 里「误报率 < 1/3」是 W7 自身 reviewer 抽查的验收条款，与 Mimosa findings 无关。因此「W7 曾定性该家族多为误报」在 W7 文档中**无出处**，不能作为依据引用。
2. **方向上一致**：本分诊独立核实后同样以误报为绝对主导（36/39），且误报机理与任务书给出的三个家族模式吻合——f-string 硬编码标识符（db.py / preference_schema.py / task_engine/schema.py，共 10 条）、内部 work_dir 写入（pipeline / task_engine / quality_lab / ui 常量，共 20 条）、非安全用途 `random`（3 条）。这是独立验证结果，不是沿用。
3. **差异点 A（相对「一律误报」的笼统说法）**：`scripts/token_sprint/audit_data.py` 3 条**不是**纯误报——被审计对象是外部 DB 文件，schema 标识符构成真实（虽然低影响、只读受限）的污点源，故标 needs_hardening 并给出最小修复。
4. **差异点 B**：Mimosa 的定级系统性偏高：`config.py:45` 的 high advisory（环境变量读配置）和全部 path-traversal 的 high 在本机单用户信任域内均无远程可达性；Mimosa 报告自身也承认覆盖度 partial/inconclusive（`proofGaps` 提示需人工确认数据流）。
5. **行号漂移备注**：`worker.py:575` 的扫描行号因扫描后的 mypy 整改 commit（b4fbf316）有约 10 行漂移，实际 sink 在 :585；finding 家族锚点（findingId `a0fec…`）判定不受影响。

## 复核方法与限制

- 逐条核对了 39 个 location 的当前源码（HEAD = a8273bed）；污点回溯只沿静态调用链（grep 调用点 + 常量定义），未运行动态分析。
- `app/` 与 `scripts/` 自扫描以来仅 `worker.py`（行号漂移）与新增未跟踪文件有变化，不影响任一 verdict。
- 局限：静态分诊无法排除「未来调用点以非字面量实参调用这些 helper」的回归风险；建议在 `_atomic_write` 类 helper 与 `_quote_ident` 落地时补一条 docstring 级契约（仅接受受控标识符）。
