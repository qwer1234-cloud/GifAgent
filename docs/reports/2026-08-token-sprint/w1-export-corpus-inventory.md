# W1 — exports 语料盘点（Task 0，零模型）

- `as_of`: 见 `data/token_sprint/w1/export_corpus_inventory.json` 的 `as_of` 字段
- 机器盘点脚本：一次性内联 Python（rglob 计数 + JSON 解析；**未 open 任何 GIF/JPG/MP4 字节**，
  GIF 路径只做 `Path.exists()` 布尔判断）
- 盘点产物（共享根，不入库）：`data/token_sprint/w1/export_corpus_inventory.json`
  （schema `gifagent.token_sprint.w1.export_corpus_inventory.v1`）

## 为什么换输入（压缩版）

1. 生产 `candidate_gifs` 是「有行无字」：快照约 12,833 条候选行的
   `vlm_summary_json='{}'`、`tags_json='[]'`、`scenario_keys_json='[]'`、`final_score=NULL`
   （`filesystem-folder-import` 只登记路径不跑 VLM 的合法结果）。旧 T0 的 60/60 拒绝
   证明的是「空库不能标」，不是 QC 门太严。
2. W1 P3 是二阶文本转换（「根据已有画面描述生成英文检索关键词」），输入为空时正确行为就是
   `refused:true`。旧探针 `shards/shard-probe.p3.v1.407ef957-…jsonl` 的 60 条拒绝是正确行为，
   不重跑那份空 input；旧 `shards/shard-probe.input.jsonl` 留作失败基线，禁止覆盖。
3. 文件断链（约 2.9 万路径找不到，E: 卷/已删 exports）是 W3 的事，与「行文本为空」独立；
   即使文件全找回来，那 12,833 行文本照样为空。本任务不修断链、不扫 E:、不打开 GIF。
4. `data/exports/**/result.json` 是当时自适应管线看过图后写下的「主富化」文本
   （caption / emotional_core / aesthetic_notes / gif_worthiness / export_path）。
   T-1 的 `inputs_manifest.json` 里 `export_results` 为 `absent` 只因 glob 写成
   `data/exports/*.json`（只匹配顶层），嵌套 result JSON 当时没拷进共享根——本任务补拷
   JSON 文本（Task 1），仍不拷 GIF。
5. 因此可以在冲刺红线内复活数据轨 T0：不写生产库、不在 ZCode 外跑 VLM、不读图，
   只消费已落盘的 JSON 文本；GIF 路径只做 `exists()` 过滤与溯源。

## 盘点数字（2026-08-29 采集）

| 指标 | 值 | 统计口径 |
|------|-----|---------|
| `data/exports` 下 `.gif` 个数 | **4,692** | `rglob('*.gif')` 计数，不 open |
| 其中唯一（resolve 后）路径数 | 4,692 | 无重复 |
| `**/*result.json` 个数（`result.json` + `*_result.json`） | **106** | 0 个解析失败 |
| 其中含非空 `top_clips` 的 result JSON | 23 | 83 个只有导出计数/无 clip 描述（`adaptive_test` 为主） |
| unique clip（按 resolve 后 `export_path`） | **601** | 相对路径相对仓库根 `C:/Users/sunhao/Desktop/code/GifAgent` resolve |
| —— caption 非空 | 306 | `caption.strip()` 非空 |
| —— GIF `exists()` 为真 | 601 | 仅布尔判断 |
| —— caption 非空 ∧ GIF 存在（**可探针池**） | **306** | T0 只需 60，池足够 |
| —— caption 非空但 GIF 缺失 | 0 | — |
| —— GIF 存在但 caption 空 | 295 | 不进池，不凑数 |

### 按导出目录分解（result JSON 维度）

| 目录 | result JSON | 含 top_clips | clips 引用 | caption 非空 |
|------|------------:|-------------:|-----------:|-------------:|
| `ab_lil_karina` | 12 | 12 | 162 | 161 |
| `adaptive_test` | 88 | 5 | 246 | 76 |
| `sample_opt_model_2026-08-23` | 1 | 1 | 29 | 12 |
| `sample_opt_pipeline3_2026-08-23` | 1 | 1 | 26 | 14 |
| `sample_opt_pipeline_2026-08-23` | 1 | 1 | 2 | 2 |
| `sample_opt_q8vl_2026-08-24` | 1 | 1 | 23 | 15 |
| `sample_opt_q8vl_centiscale_2026-08-25` | 1 | 1 | 55 | 14 |
| `sample_opt_q8vl_centiscale_sexgate_2026-08-25` | 1 | 1 | 58 | 12 |

（`unique clip` 按路径去重后跨 result 文件合并；上表 clips 引用数之和 601 与 unique 数一致。）

## 结论与下游

- 可探针池 **306 ≥ 60**，满足 T0 抽样需求 → 继续执行 Task 1–3（拷 JSON 文本、建语料分片、
  P3 探针）。未触发「池 < 60 则停止」分支。
- `export_path` 样例已验证为相对仓库根：`data/exports/sample_opt_q8vl_centiscale_sexgate_2026-08-25/…gif`
  （混有 `\` 分隔符，`Path.resolve()` 在 Windows 下正确处理）。
- `adaptive_test` 大量 JSON 确实只有 gif_count 类导出计数、没有逐 clip 描述——与预期一致，
  不用于凑池。
- 红线遵守：不 open 媒体字节；不改 coordinator 脚本；不动冻结区；生产库零写入。
