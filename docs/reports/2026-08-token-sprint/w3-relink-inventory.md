# W3 重链清点报告（relink inventory, dry-run only）

- 生成时间：2026-08-29（executor：w3-relink-executor，ZCode 会话）
- 清点对象（重链源）：`D:\data`（Git Bash: /d/data）——**正在重新下载中**，本报告全部数字为时点值（as_of）
- **as_of：2026-08-29T06:40:11+0800**
- 上游收账产物：`data/token_sprint/w3/w3-audit-findings.json`（sha256 `85dedacf…44ff`，读前已对照 canonical `sprint_manifest.json` 校验一致）
- 快照库：`data/token_sprint/library_snapshot.db`（**只读** `mode=ro` + `PRAGMA query_only=ON`；sha256 `5af6c815…7161` 同样对 manifest 校验一致）
- 工具：`scripts/token_sprint/relink_proposals.py`（本轮新增，纯元数据，零模型调用）

## 一、D:\data 清点（as_of 2026-08-29T06:40:11+0800）

| 项 | 值 |
|----|----|
| 文件总数 | 3,115 |
| 目录总数 | 267 |
| 总字节（stat 元数据） | 82,365,708,797（约 76.7 GiB） |
| .gif | 1,880 |
| .jpg | 983 |
| .mp4 / .webm | 0 / 0 |
| 其他扩展名 | 252 |
| stat 失败 | 0 |

**方法与红线**：仅 `os.scandir` 递归 + `entry.stat()` 取 size/mtime；**未 open/读取/哈希任何媒体字节**（测试用 monkeypatch 监视 `builtins.open` 证明零媒体打开）。相对路径清单与 {size, mtime} 已写入 `relink_source_inventory.json`。

**时点性声明**：D:\data 仍在下载（本次任务期间三次运行分别为 2,843 → 3,074 → 3,115 个文件，.gif 从 1,609 → 1,880 持续增长）。上表与下述匹配率**仅为 as_of 快照**；下载推进后匹配率只升不降。可用下述命令随时重跑（产物为不可覆盖语义时应换版本号发布）：

```bash
uv run python scripts/token_sprint/relink_proposals.py \
  --sprint-root "C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint" \
  --relink-source "D:/data"
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

## 三、匹配率（as_of 2026-08-29T06:40:11+0800）

| 来源组 | 目标数 | 其中断链（findings） | matched | unmatched | ambiguous | unknown | matched/断链 |
|--------|--------|----------------------|---------|-----------|-----------|---------|--------------|
| candidate_gifs（artifact+preview，25,666 目标 = 12,833 唯一路径） | 25,666 | 19,102 | 3,902 | 13,610 | 0 | 8,154 | **20.4%** |
| favorites（full_path） | 2,045 | 1,546 | 343 | 1,013 | 0 | 689 | **22.2%** |
| media_e_volume（file_path） | 9,221 | 9,221 | 36 | 9,185 | 0 | 0 | **0.39%** |
| checkpoint（completed_path） | 5 | 5 | 0 | 5 | 0 | 0 | **0%** |
| 合计（断链口径） | 29,874+12,837 全库 | 29,874 | 4,281 | — | 0 | 8,843 | **14.3%**（断链口径） |

- exports 断链合并口径（candidate_gifs + favorites 断链目标）：matched 4,245 / 20,648 = **20.6%**。
- matched 按 column 分：`artifact_path` 1,951、`preview_path` 1,951（同行两列同路径成对）、`full_path` 343、`file_path` 36。
- 全部 matched 目标的旧路径当前 `exists()` 均为 False（broken），与新断链结论一致。

### 对 D:\data 内容结构的解读（为什么 media 组匹配率极低）

- D:\data 当前内容是**源/原始素材**的重新下载（场景目录 + 中文合集目录），`.gif` 1,880 个大多直接落在盘根或按原目录名归位；
- `media_e_volume` 的 9,221 个 `E:/data/originals\…gif` 中只有 36 个已出现在 D:\data——该卷的 gif 原件大头尚未下载到；
- `candidate_gifs`/`favorites` 的 exports 目标中，约 1/3 落在 `unknown` 档：其**父目录**（场景标题目录，如 `RoughAnalwithaGothicSlut,…-Pornhub.com`、`LapkaLu\<标题>`）已在 D:\data 出现，但目录内的 exports gif 还没出现（或永远不会以同名出现）。这是"下载进行中"最直接的信号。

## 四、ambiguous 处理方式

- 本轮 as_of 时点 **ambiguous = 0**（D:\data 中无同名多文件与目标撞名）。处理逻辑已实现并有测试覆盖：同名多文件 → 记 `ambiguous`、列出全部候选路径（inventory 内 `ambiguous_unknown_detail`，单条候选列表默认截取 20 条、明细总量截取 1,000 条，计数不截断）、**不生成建议行**；父目录同名恰唯一时仅附 `preferred_new_path` 参考字段，不改变 ambiguous 定性。
- `unknown` 明细样例同写进 inventory 的 `ambiguous_unknown_detail`（1,000 条上限），供下载完成后人工复核。

## 五、dry-run 重链建议（JSONL）

- 产物：`data/token_sprint/w3/w3-relink-proposals.v1.jsonl`，共 **4,281 行**，每行：

```json
{"table": "candidate_gifs", "row_ref": "candidate_gifs#80 (cand_…)", "column": "artifact_path",
 "old_path": "data\\exports\\adaptive_test\\…gif", "new_path": "D:\\data\\…gif",
 "match": "basename", "source_group": "candidate_gifs", "old_path_status": "broken",
 "apply_recommendation": true, "new_path_rel_source": "…", "note": "dry-run suggestion only; …"}
```

- **本轮绝未执行任何 UPDATE**：脚本没有 `--apply` 模式，传入 `--apply` 直接 exit 2（有测试）；快照库以 `mode=ro`+`query_only` 打开，运行前后 findings/db 的 size+mtime_ns 不变（有测试）。建议是给用户在冲刺窗口外人工研判用的。
- 一致性核验：1,909 个唯一 `new_path`；其中 78 个被多条 old_path 指向——逐一核对后全部是**同一文件被两张表引用**（`favorite_gifs.full_path` 存绝对路径形式，`candidate_gifs` 行存相对路径形式，如 `JUR-639@@@001_167s-174s.gif`），同名同尾缀，非错误；已写进 inventory 的 `proposal_summary.multi_old_path_note`，仍建议 apply 前人工抽查。
- 只有 `old_path_status="broken"`（= `apply_recommendation: true`）的行才值得执行；本轮 4,281 行全部为 broken。

## 六、产物与收账状态

| 产物 | sha256 | .ready |
|------|--------|--------|
| `data/token_sprint/w3/relink_source_inventory.json` | `89046b6c8e5774980ceaa1c67eab22e67c7cb04c47fa27372949db5928e759ac`（1,215,383 B） | `data/token_sprint/w3/relink_source_inventory.json.ready`（sha256 `1290a71d…e08`） |
| `data/token_sprint/w3/w3-relink-proposals.v1.jsonl` | `0b05f50d797de0e6fd5dcc49024ee2b153fa809b09be6184777338866168ad77`（4,578,963 B） | `data/token_sprint/w3/w3-relink-proposals.v1.jsonl.ready`（sha256 `b7b8dfc0…a239`） |

- producer 均为 `w3-relink-executor`；`.done`/receipt/canonical manifest 留给 reviewer 与协调会话（非本 executor 职责）。
- 已收账的 `w3-audit-findings.json`、其 `.ready`/`.done` 与 manifest 均未改动（脚本读前/读后校验，测试断言零改动）。

## 七、测试

```
uv run pytest -q tests/token_sprint/ --no-header
78 passed in 4.74s
```

新增 `tests/token_sprint/test_relink_proposals.py`（8 例）：媒体零打开（monkeypatch 监视 `builtins.open`）、matched/ambiguous/unmatched/unknown 四档各一例、ambiguous 候选列表与 unknown 原因、`--apply` 拒绝 exit 2、输入文件 mtime/size 零改动、manifest SHA 篡改 fail-closed exit 2、manifest 未收账时降级继续。

## 八、局限（诚实声明）

1. **basename 匹配的盲区**：改名、重编码（扩秽名/转码）的文件不会命中；本轮用 `unknown` 档（父目录在场 / stem 同扩展名异）显式标注 8,843 条"疑似可映射但不硬凑"，需下载完成后人工或更强指纹（phash——本轮禁止读媒体字节，未做）复核。
2. **时点性**：D:\data 下载进行中，全部计数与匹配率是 as_of 快照；三次试跑间 gif 数已从 1,609 涨到 1,880。**结论只能用于判断"方向与规模"，最终 relink 决策应在下载完成后重跑一次**。
3. **相对/绝对双写**：candidate 用相对路径、favorite 用绝对路径指向同一文件，建议行里 new_path 统一为绝对路径；用户如需保持 candidate 列的相对路径语义，须自行换算。
4. **不验证内容**：按红线未读任何媒体字节，basename 相同不代表内容相同（尤其重编码场景）；phash/逐字节校验属于后续工作，需另行授权。
5. `preview_path` 与 `artifact_path` 同行同值成对出现，匹配率按"目标"计会自然翻倍；按唯一路径口径 candidate_gifs 为 12,833 唯一路径中 1,951 唯一路径 matched。

## 九、未决问题

1. 下载完成后需要重跑并重新发布新版 inventory/proposals（建议文件名带 v2/新 attempt，禁止覆盖本次已 .ready 的产物）。
2. `E:/data/originals` 的 9,185 条未匹配 gif 是否仍存在于其他备份介质，需用户确认；若确认永久丢失，再走"置状态位"方案（W3 报告第七节建议 1）。
3. checkpoint 的 5 个 4K 输入视频（`C:\Users\sunhao\Desktop\ToWatch\other\…mp4`）本轮 0 匹配；D:\data 目前无 .mp4，下载是否包含这些源片待确认。
4. 78 个跨表双引用目标与 8,843 条 unknown 的人工抽查批量未定（建议 reviewer 验收时抽 20 条，对齐 W3 误报率门惯例）。
