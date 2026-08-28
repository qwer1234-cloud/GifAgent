# W1 快照报告 — library_snapshot.db

日期：2026-08-29（快照产生于 W1 确定性工具链会话）
工具：`scripts/token_sprint/snapshot_library.py`（W1 实施方案 "Snapshot rebuild contract (lock)"）
生产者：`w1-executor`（本会话）；`.ready` 已按四方哈希链协议发布。

## 快照结果

| 项目 | 值 |
|------|-----|
| 快照路径 | `C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/library_snapshot.db` |
| 快照 SHA-256 | `5af6c81519303b2acb034c55ed10dca0163bde71ad4d1689c61c26400b7161d8` |
| `candidate_search_fts COUNT(*)` | **12833** |
| `candidate_search_fts COUNT(DISTINCT candidate_id)` | **12833** |
| `candidate_gifs COUNT(*)` | **12833** |
| rebuild `errors` | **0**（scanned=12833, inserted=12833） |
| 三计数一致 | 是（12833 == 12833 == 12833） |

验收（设计原文）：`COUNT(*) == COUNT(DISTINCT candidate_id) == candidate_gifs COUNT(*)` 且 `errors == 0` —— **通过**。

独立复核：会话内用单独的只读连接重算三计数与文件 SHA，结果与工具输出一致；
WAL 已 checkpoint 归零并删除 `-wal`/`-shm` 边车，主文件 SHA 复核不变。

## 执行过程

1. 读取 `inputs_manifest.json`：SHA-256 `8e99f7cb6d9d51956b9e87de5e16f9bacde06ebf8cd8a8a3f7b09de5b56246ac`
   与 canonical `sprint_manifest.json` `control_plane.inputs_manifest.json.sha256` 一致。
2. `source_db_abs` 钉死路径 `C:/Users/sunhao/Desktop/code/GifAgent/dist/GifAgentUI/data/library.db`
   （250,834,944 字节），只读打开确认 SQLite。
3. `VACUUM INTO` 到共享根 `library_snapshot.db`（未使用 `--force`，此前无快照）。
4. 快照库：WAL + `busy_timeout=30000`，**先** `DELETE FROM candidate_search_fts; DELETE FROM search_index_state;`
   再调用现有 `LibrarySearchService.rebuild_index()`（增量续跑逻辑，先清空才保证全量重灌；
   回归锁见 `tests/token_sprint/test_snapshot_fts_rebuild.py`，10 项测试通过）。
5. 三计数验收通过后计算快照 SHA-256 并打印。

## 源库漂移说明

- manifest 钉死时的源库 SHA：`bcfc1b0a664b0447a67fe349f2d444b0410b1a1a1a3e9689fc9845b290279042`
- 快照时源库实际 SHA：`4a9822943b7a1121a78ac154e32bc9e532fb9994a0bfd8c97cbcd71201a69ca7`

生产库自 pin 以来正常漂移（设计文档已声明"生产库计数在漂移"）。
**后续 W1/W8 一切计数以本快照为准（12833 条），不再使用生产库计数。**
本会话对生产库只读（`mode=ro` URI + `VACUUM INTO`），未写入任何字节。

## 协议状态

- `.ready`：`C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/library_snapshot.db.ready`
  （producer=`w1-executor`，`qc_summary` 含快照 SHA-256 与三计数）。
- 待 reviewer 重跑三计数并核验 SHA 后写 `.done` + receipt（本会话不越权）。
- coordinator 可运行 `reconcile_receipts.py --once` 收账。

## 后续（T0 探针，模型消耗前的最后一步）

```bash
python scripts/token_sprint/make_shards.py --sprint-root "$SPRINT_ROOT" --probe
```

按 W1 零模型边界，本会话**未**执行探针导出与 T0 模型处理（Task 7 Step 2/3 未开始）。
