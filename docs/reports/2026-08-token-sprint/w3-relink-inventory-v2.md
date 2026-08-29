# W3 重链清点报告 v2（relink inventory v2, dry-run only）

- 生成时间：2026-08-29（executor：w3-relink-executor，ZCode 会话）
- 清点对象（重链源）：`D:\data`（Git Bash: /d/data）——**正在重新下载中**，本报告全部数字为时点值（as_of）
- **as_of：2026-08-29T10:02:43+0800**
- 上游收账产物：`data/token_sprint/w3/w3-audit-findings.json`（sha256 `85dedacf…44ff`，读前已对照 canonical `sprint_manifest.json` 校验一致）
- 快照库：`data/token_sprint/library_snapshot.db`（**只读** `mode=ro` + `PRAGMA query_only=ON`；sha256 `5af6c815…7161` 同样对 manifest 校验一致）
- 工具：`scripts/token_sprint/relink_proposals.py`（纯元数据，零模型调用）

## 一、D:\data 清点（as_of 2026-08-29T10:02:43+0800）

| 项 | 值 |
|----|----|
| 文件总数 | 13,481（v1: 3,115） |
| 目录总数 | 901（v1: 267） |
| 总字节（stat 元数据） | 390,529,966,289（约 363.7 GiB，v1: 76.7 GiB） |
| .gif | 9,680（v1: 1,880） |
| .jpg | 2,520（v1: 983） |
| .mp4 / .webm | 0 / 0 |
| 其他扩展名（分卷压缩包等） | 1,281（v1: 252） |
| stat 失败 | 0 |

**方法与红线**：仅 `os.scandir` 递归 + `entry.stat()` 取 size/mtime；**未 open/读取/哈希任何媒体字节**。相对路径清单与 {size, mtime} 已写入 `relink_source_inventory_v2.json`。

**时点性声明**：D:\data 仍在下载。相比 v1，文件数由 3,115 增长到 13,481，.gif 从 1,880 增长到 9,680。可用下述命令随时重跑：

```bash
uv run python scripts/token_sprint/relink_proposals.py \
  --sprint-root "C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint" \
  --relink-source "D:/data" \
  --inventory-out "C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/w3/relink_source_inventory_v2.json" \
  --proposals-out "C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/w3/w3-relink-proposals.v2.jsonl"
```

## 二、匹配方法

1. **目标路径（四来源）**：
   - `candidate_gifs`：全库 12,833 行 × `artifact_path` + `preview_path` = 25,666 条目标（其中 19,102 条在 findings 中记为断链；快照库读取，非仅断链行——重链映射对全库都有意义）；
   - `favorites`：全库 2,045 行 × `full_path`（其中 1,546 条断链）；
   - `media_e_volume`：findings 中 9,221 条 `media.file_path`（全部断链，指向 `E:/data/originals\…`）；
   - `checkpoint`：findings 中 5 条 `batch_checkpoint` 已完成输入视频。
2. **匹配规则**：提取目标 basename，与 D:\data 文件 basename **精确匹配（大小写不敏感）**。
   - 命中 1 个 → `matched`（唯一匹配，可出建议）；
   - 命中 >1 个 → `ambiguous`（列出全部候选，不自动选定；若其中恰有一个与旧路径父目录同名，另附 `preferred_new_path` 仅供参考）；
   - 命中 0 个且旧路径**父目录名**存在于 D:\data → `unknown (parent_dir_present_basenames_differ)`——疑似可映射但文件名不一致（改名/重编码/尚未下载完成），**不硬凑**；
   - 命中 0 个且 stem（去扩展名）与某源文件一致仅扩展名不同 → `unknown (stem_match_extension_differs)`，同样不硬凑；
   - 其余 → `unmatched`。
3. `matched_exists`（旧路径当前仍存在却去重链）全组为 0，即所有匹配都落在真实断链上，没有"把好路径改坏"的建议混入。

## 三、匹配率（as_of 2026-08-29T10:02:43+0800）

| 来源组 | 目标数 | 其中断链（findings） | matched | unmatched | ambiguous | unknown | matched/断链 |
|--------|--------|----------------------|---------|-----------|-----------|---------|--------------|
| candidate_gifs（artifact+preview，25,666 目标 = 12,833 唯一路径） | 25,666 | 19,102 | 13,846 | 6,790 | 1,072 | 3,958 | **72.48%**（v1: 20.4%） |
| favorites（full_path） | 2,045 | 1,546 | 1,077 | 527 | 38 | 403 | **69.66%**（v1: 22.2%） |
| media_e_volume（file_path） | 9,221 | 9,221 | 1,799 | 7,422 | 0 | 0 | **19.51%**（v1: 0.39%） |
| checkpoint（completed_path） | 5 | 5 | 0 | 5 | 0 | 0 | **0%** |
| 合计（断链口径） | 29,874+12,837 全库 | 29,874 | 16,722 | — | 1,110 | 4,361 | **55.98%**（v1: 14.3%） |

- **重大突破**：
  - `candidate_gifs` 断链匹配率从 20.4% 跃升至 **72.48%**（13,846 / 19,102）；
  - `favorites` 断链匹配率从 22.2% 跃升至 **69.66%**（1,077 / 1,546）；
  - `media_e_volume` 断链匹配率从 0.39% 提升至 **19.51%**（1,799 / 9,221）；
  - 总建议数从 v1 的 **4,281** 行增长到 **16,722** 行（近 4 倍增长）。
- 全部 matched 目标的旧路径当前 `exists()` 均为 False（broken），与断链结论一致。

### 对 D:\data 内容结构的解读

- D:\data 当前内容随着下载与解压进行，媒体文件已达到 12,229 个（.gif 9,680 + .jpg 2,520）；
- `candidate_gifs` 与 `favorites` 的绝大部分 exports gif 已在 D:\data 中被找回；
- `media_e_volume` 的 9,221 个 `E:/data/originals\…gif` 中已有 1,799 个出现在 D:\data，匹配率持续上升。

## 四、ambiguous 处理方式

- 本轮由于 D:\data 解压文件增加，出现部分同名文件（ambiguous）：
  - `candidate_gifs`：1,072 条
  - `favorites`：38 条
  - 处理逻辑：同名多文件 → 记 `ambiguous`、列出全部候选路径写入 inventory，**不自动生成建议行**，避免误链；父目录同名恰唯一时附 `preferred_new_path` 供参考。
- `unknown` 明细样例降至 4,361 条（v1: 8,843 条），写入 inventory 的 `ambiguous_unknown_detail` 供人工复核。

## 五、dry-run 重链建议（JSONL）

- 产物：`data/token_sprint/w3/w3-relink-proposals.v2.jsonl`，共 **16,722 行**，每行：

```json
{"table": "candidate_gifs", "row_ref": "candidate_gifs#80 (cand_…)", "column": "artifact_path",
 "old_path": "data\\exports\\adaptive_test\\…gif", "new_path": "D:\\data\\…gif",
 "match": "basename", "source_group": "candidate_gifs", "old_path_status": "broken",
 "apply_recommendation": true, "new_path_rel_source": "…", "note": "dry-run suggestion only; …"}
```

- **本轮绝未执行任何 UPDATE**：脚本没有 `--apply` 模式（传入直接 exit 2）；快照库以 `mode=ro`+`query_only` 打开，运行前后 findings/db 的 size+mtime_ns 不变。建议是给用户在冲刺窗口外人工研判用的。
- 只有 `old_path_status="broken"`（= `apply_recommendation: true`）的行才值得执行；本轮 16,722 行全部为 broken。

## 六、产物与收账状态

| 产物 | sha256 | .ready |
|------|--------|--------|
| `data/token_sprint/w3/relink_source_inventory_v2.json` | `68f85a4ea8baf875d47e7daa51d038c1dd0478cc126ea8f806635eb933e93bb4`（5,191,855 B） | `data/token_sprint/w3/relink_source_inventory_v2.json.ready` |
| `data/token_sprint/w3/w3-relink-proposals.v2.jsonl` | `422048597e37b6bba8dd68e4f9dae916f9d461544d7f04308f8adaad341d031f`（17,896,155 B） | `data/token_sprint/w3/w3-relink-proposals.v2.jsonl.ready` |

- producer 均为 `w3-relink-executor`。
- 已收账的 `w3-audit-findings.json`、其 `.ready`/`.done` 与 manifest 均未改动。

## 七、测试

```
uv run pytest -q tests/token_sprint/ --no-header
78 passed in 4.74s
```

## 八、局限（诚实声明）

1. **basename 匹配的盲区**：改名、重编码（扩秽名/转码）的文件不会命中；本轮用 `unknown` 档显式标注 4,361 条"疑似可映射但不硬凑"，需下载完成后人工复核。
2. **时点性**：D:\data 下载仍在进行中，最终 relink 决策可在下载全部完成后重跑刷新。
3. **相对/绝对双写**：candidate 用相对路径、favorite 用绝对路径指向同一文件，建议行里 new_path 统一为绝对路径。
4. **不验证内容**：按红线未读任何媒体字节，basename 相同不代表内容相同；逐字节/phash 校验属于后续工作。

## 九、未决问题

1. 下载全部完成后可再次重跑刷新 inventory/proposals。
2. `E:/data/originals` 未匹配的 gif（7,422 条）是否在后续下载包中，待下载完成后复查。
3. 1,110 条 ambiguous 与 4,361 条 unknown 的人工抽查研判。

