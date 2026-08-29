# W8 Shadow FTS v2 — 构建与验证报告

- 分支：`sprint/w8-shadow-fts`（基于 `sprint_base_sha` = `a6c9fb0a`）
- 执行：w8-executor（ZCode harness，零模型 token，纯确定性工具）
- 日期：2026-08-29

## 1. 输入与 fail-closed 校验

- 消费清单：`data/token_sprint/approved_enrichment_manifest.json`
  - 文件 SHA-256：`040277a328b4f4edc3b336b92743d41f855721e98dce3f4e1608273fb005fd4d`（与 coordinator 下发值一致）
  - 7 个已验收分片：p3×3（含 probe-export）+ p1b×2 + p2×2；p2.v2 Stage C 新分片**不在**清单内，未消费。
- `scripts/token_sprint/build_fts_v2.py` 按清单逐分片执行：
  1. `(pass_id, prompt_version, shard_id, sha256)` 四元组验收（文件名模式 + 重算 SHA-256 比对，大小写不敏感）；
  2. `.ready`/`.done` 存在性 + `protocol.verify_hash_chain` 四方链校验（receipt 按 `receipt_id == manifest_artifact_id` 定位，含 `receipts/processed/` 递归扫描，歧义即拒绝）；
  3. 任一失败 → exit 2，**不打开数据库写入**。
- 说明：实际 approved manifest schema 为 `approved_enrichment_manifest.v1`，条目不含 `prompt_sha256` 字段，故按 coordinator 指令执行四元组验收（计划文中五元组的 prompt 维度由 manifest 自身版本与 reviewer 签署链绑定）。
- 数据库保护：`--db` 拒绝 `inputs_manifest.json.source_db_abs`（生产库 pin）、任何 `dist` 目录段、以及快照原件 `<sprint-root>/library_snapshot.db`（字符串前缀检查 + `resolve()` 后比较）。`enrichment.db` 全程未被读取（有专门测试证明）。

## 2. 快照演练（Task 4）

- 快照原件：`data/token_sprint/library_snapshot.db`，SHA-256 `5af6c81519303b2acb034c55ed10dca0163bde71ad4d1689c61c26400b7161d8`，与 W1 `.ready` pin **一致**（原件未写入）。
- 复制方式：sqlite3 backup API（只读 URI 源 → `data/token_sprint/w8/fts_v2_work/library.shadow.db`），正确处理 WAL；副本 candidate_gifs 12833 行。
- 构建命令（Git Bash 与 cmd 兼容，`--sprint-root` 显式传入；缺省 fallback `GIFAGENT_SPRINT_ROOT`；两者皆缺 exit 2）：

```bash
uv run python scripts/token_sprint/build_fts_v2.py \
  --sprint-root C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint \
  --db C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/w8/fts_v2_work/library.shadow.db
```

### 构建结果（两次构建完全一致）

| 指标 | 值 |
|---|---|
| `fts_v2_content` 行数 | 306 |
| `fts_v2_content` distinct candidate_id | 306 |
| `candidate_search_fts_v2` 行数 | 306 |
| `candidate_search_fts_v2` distinct candidate_id | 306 |
| 分片接受数 | 7 |
| rows_per_pass | p1b=306, p2=306, p3=366 |
| same_pass_overwrites | 60 |
| manifest_sha256（写入每行 `enrichment_manifest_sha`，306/306 一致） | `040277a3…005fd4d` |
| v1 `candidate_search_fts`（未触碰） | 12833 行 |

- p3 共 366 行（shard-001 153 + shard-002 153 + probe-export 60）；probe-export 的 60 个候选与 shard-001/002 重叠，按文档化规则「manifest 清单顺序，后写 wins」由后两条全量分片胜出（same_pass_overwrites=60），全部 306 候选三个 pass 覆盖齐全（306 行均有 p1b tags 与 p2 summary）。

### 双次构建确定性证据

单事务全量重建（`BEGIN IMMEDIATE` → DELETE content → INSERT（按 candidate_id 排序）→ DELETE FTS v2 → INSERT…SELECT…ORDER BY → 唯一性/行数复核 → COMMIT）。连续两遍构建，以下指标逐项相等：

- content_rows / content_distinct_candidate_id / fts_rows / fts_distinct_candidate_id：`306`
- 固定查询集 `("kitchen","bedroom","couple","smiling","intimacy","nudity","test")` 的 `MATCH` 命中 candidate_id 有序列 SHA-256，聚合摘要：
  - **`4f9116c7f53c9a528f0ced5065385d2990b157ee9ca40d72d98cc2f1cae1530d`**（两次一致）
  - 单项示例：`kitchen` → `c4440646…c34e038b`；`test` → `e3b0c442…b855`（空集 = sha256("")，符合预期）

### 篡改 fail-closed 演练（真实分片字节）

将 7 个获批分片字节逐字复制到临时镜像（重建 `.ready`/`.done`/receipt 绑定副本路径），基线构建 exit 0；对 `shard-002.p2.v1.eea809c5…jsonl` 篡改 1 字节（`bedroom`→`Bedroom`）后重建：

- exit code **2**，stderr：`error: approved shard byte drift: … manifest sha 3a82d84c… != actual …`
- 库未被写入（镜像内 shadow.db 字节未变；单测以成功构建后的文件 SHA 前后相等断言）。

补充 fail-closed 场景（单测覆盖）：缺 `.done`、缺 receipt、无效 JSON 行、pass schema 违例、`enrichment.db` 存在但分片缺失（证明未走 db）、生产/dist/快照原件目标拒绝、`--sprint-root` 与 env 双缺 exit 2。

## 3. 合并规则（实现内文档化）

处理顺序 = manifest 清单顺序 → 分片内行序；每个 `(candidate_id, pass_id)` 后写 wins：

- `p1b.tags` → tags 文本（首现去重）；
- `p3.keywords` → 追加进 tags 文本（在 p1b 之后，首现去重）；
- `p2.scene` → summary 文本，按 key 排序序列化 `key=value`（null 值丢弃）；
- `{"refused": true}` 行跳过；既非 refusal 又不符合 pass schema 的行 → 整体构建失败（exit 2）；
- `source_path` 置空字符串：approved 分片不含路径素材，清单之外一概不消费。

### 与协调说明的一处事实核对

协调说明称「W1 库快照里 306 条 exports 语料的文本在快照 annotations/相关表」——实测快照内**无任何 `exp_*` 行**（candidate_gifs/media/annotations 均无；exports 语料 ingest 时间戳 2026-08-28T21:16Z 晚于快照 pin 19:50Z）。FTS v2 内容因此完全来自 approved 分片（与计划「canonical 输入是 approved 不可变 JSONL shards」一致），快照仅作为 v2 表的宿主副本。

## 4. env 门（GIFAGENT_SEARCH_FTS_V2）

| 场景 | 命令 | 结果 |
|---|---|---|
| 缺省关（未 export） | `uv run pytest -q tests/test_library_search.py tests/test_library_search_api.py` | **44 passed** |
| 缺省关（空值 `GIFAGENT_SEARCH_FTS_V2=`） | 同上 + `tests/token_sprint/test_search_fts_v2.py` | **75 passed**（44 现有 + 31 新增） |
| 开（`GIFAGENT_SEARCH_FTS_V2=1`） | `uv run pytest -q tests/token_sprint/test_search_fts_v2.py` | **31 passed** |

- 开关仅在 `_rank_with_text` 将 FROM 切到 `candidate_search_fts_v2`；v2 表缺失时 degraded 诊断明确含 `FTS v2 missing`（不吞错）；缺省路径行为与现状逐字节一致（含 OperationalError 吞掉语义）。
- 现有测试文件 `tests/test_library_search.py`、`tests/test_library_search_api.py` 零修改。

## 5. 产物

- 阴影索引库：`data/token_sprint/w8/fts_v2_work/library.shadow.db`
  - artifact SHA-256：`5e574416f7d8661d06f9a6428b845f3674f3f7e43651795a63362e6910d458ab`
  - `.ready`：`data/token_sprint/w8/fts_v2_work/library.shadow.db.ready`
    （`protocol.write_ready` 发布，producer=`w8-executor`，SHA-256 `5022d377de7f36a1f49430e70a9f4f92d500a8eb5a11f4b2ff6d87cc9ff7585b`）
- 报告：`docs/reports/2026-08-token-sprint/w8-shadow-fts.md`

## 6. 全量 pytest 门（合入 integration 前）

命令：`cd .worktrees/sprint-w8-shadow-fts && uv run pytest -q tests/ --no-header`

- 结果原文：**3 failed, 1808 passed, 2 skipped, 23 warnings in 222.08s (0:03:42)**
- 3 个失败全部位于 `tests/test_preference_preflight.py`（`scripts/preference_memory.collect_status`），与 FTS/搜索无关；**在未改动的 sprint_base（a6c9fb0a）干净 worktree 上复跑同样 3 failed, 1 passed**，确认为预存的环境依赖失败（生产库正被富化进程写入的共享环境影响），非本 W 引入。
- 本 W 新增 `tests/token_sprint/test_search_fts_v2.py`：31 passed；`tests/test_library_search.py` / `tests/test_library_search_api.py` 缺省关全绿。

## 7. 分支与提交

`sprint/w8-shadow-fts`（自 `sprint_base` a6c9fb0a）：

1. `2993ab16` feat: additive FTS v2 content table and virtual index schema
2. `3c2b1edb` feat: build FTS v2 from approved immutable JSONL shards only
3. `677e7a07` feat: opt-in GIFAGENT_SEARCH_FTS_V2 reader for shadow index
4. `2040b13f` docs: W8 shadow FTS snapshot drill evidence
5. （本条）docs: W8 full pytest gate numbers and artifact hashes

红线遵守：未打开任何 GIF/JPG/视频字节；未写生产库与快照原件；未改 `pyproject.toml`/`uv.lock`；全部 `git add` 显式路径；无新增 GLM/智谱端点代码。
