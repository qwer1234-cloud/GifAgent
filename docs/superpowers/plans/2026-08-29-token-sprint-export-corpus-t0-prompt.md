# Token Sprint — 用 exports 存量文本重跑 T0 探针 Prompt

> 把本文从「Prompt 正文」标题起整段发给 **W1 executor**（工作目录：`sprint/w1-enrichment` worktree）。
> 本任务**不**看 GIF 像素、**不**写生产库、**不**用 `library_snapshot.db` 里那 12,833 条空文本行。
> 先只做：从 `data/exports` 已有 **result JSON 文本** 重建 60 条探针并跑 T0。试验集爬坡等 T0 三门过了再说。

---

## Prompt 正文

你是 Token Sprint 的 **W1 executor**。用户要求：**不要再拿生产库空文本行做探针**；改用 `data/exports/` 里已经留下的管线结果（JSON 文本 + 仍在磁盘上的 GIF 路径作溯源），**先执行 T0 探针**。

工作目录：`.worktrees/sprint-w1-enrichment`（若无，从 `sprint_base_sha` 检出的 `sprint/w1-enrichment`）。
共享根：`C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint`
每次脚本传 `--sprint-root` 该路径。命令按 Git Bash。
严格执行：`docs/superpowers/plans/2026-08-28-token-sprint-w1-enrichment-implementation.md` 的全局约束 1–15，以及下面的增量范围。
契约真源：`docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md`。

禁止：`uv add`、改冻结区、写 `dist/` 或根层 `data/*.db*`、直连 GLM、**读取 GIF/JPG/视频内容**、原地覆盖已 `.ready` 的旧探针产物。

---

## 为什么要换输入（必须写进报告，不要省略）

上一轮 T0 失败的原因**不是** QC 阈值，也**不是** E: 卷文件断链。

1. **生产 `candidate_gifs` 是「有行无字」。** 快照约 12,833 条候选行都在，但 `vlm_summary_json='{}'`、`tags_json='[]'`、`scenario_keys_json='[]'`、`final_score=NULL`。这些是 schema 缺省，也是 Workbench `filesystem-folder-import`（`app/routers/candidates.py` `_materialize_filesystem_candidates_for_folder`）只登记路径、不跑 VLM 的合法结果。
2. **W1 P3 是二阶文本转换。** prompt 要求「根据已有画面描述生成英文检索关键词」。输入为空时，正确行为是 `{"candidate_id","refused":true}`。已完成的探针 `shards/shard-probe.p3.v1.407ef957-...jsonl` 六十条全拒绝，规则是对的，**不要重跑那份空 input。**
3. **文件断链是另一件事（W3）。** 约 2.9 万路径找不到（E: 卷 / 已删 exports）。即使用户把文件全找回，这 12,833 行文本照样是空的，旧 T0 一样会失败。本任务**不要**去修断链、不要扫 E:、不要打开 GIF。
4. **exports 里已经有「主富化」文本。** 自适应管线当时已经看过图，把 caption / emotional_core / aesthetic_notes / gif_worthiness / export_path 写进了 `data/exports/**/result.json` 与 `*_result.json`。本机盘点（执行前请复核）：
   - `data/exports/` 约 **4,692** 个 `.gif` 仍在（`adaptive_test` 最多，但该目录 JSON 多半只有导出计数，**没有**逐 clip 描述）。
   - 带 `top_clips` 的 result JSON：**约 306 条 unique clip 同时满足「caption 非空」且 `export_path` 指向的 GIF 仍存在**。T0 只要 60 条，这个子集够用。
   - T-1 的 `inputs_manifest.json` 里 `export_results` 为 `absent`，是因为 glob 写成了 `data/exports/*.json`（只匹配顶层）。嵌套 `result.json` 当时没被拷进共享根。本任务要补拷 **JSON 文本**，仍不拷 GIF。
5. **因此可以在冲刺红线内复活数据轨探针：** 不写生产库、不在 ZCode 外跑 VLM、不读图；只消费已经落盘的 JSON 文本。GIF 路径只做 `Path.exists()` 布尔过滤与溯源，**禁止** `open` 媒体字节。

结论：旧 T0 证明「空库不能标」；新 T0 验证「有 caption 的 exports 子集能不能过保真门」。两件事不要混。

---

## 范围（先做完这些再停下来汇报）

**做：**

1. 把 exports 里的 result JSON 不可变拷贝进共享根，写清单与 SHA。
2. 从中抽出「caption 非空 + GIF 文件仍存在」的文本语料（不读图）。
3. 分层抽 **60 条**（高/中/低显式各 20）写成新的 probe input（新 `attempt_id` / 新文件名）。
4. 按 `scripts/token_sprint/prompts/p3/v1.md` 做 P3（英文检索关键词）。
5. `validate_shard.py` 过 T0 三门；高显式 20 条 + 疑似消毒行人工复核。
6. 写报告；产物走 `.ready`（你）→ 另派/等待 reviewer `.done` + receipt。不要自己写 canonical manifest。

**先不要做：** 全库 12,833 再探针；Stage A/B 试验集爬坡；W8；写回 `library.db`；对 GIF 跑 VLM。T0 若再失败，停止模型标注并报告，不要降门。

---

## Task 0 — 复核盘点（零模型）

在共享根写 `docs/reports/2026-08-token-sprint/w1-export-corpus-inventory.md`（git）以及 `data/token_sprint/w1/export_corpus_inventory.json`（共享根，不入库）。

统计并写入：

| 指标 | 怎么数 |
|------|--------|
| `data/exports` 下 `.gif` 个数 | `rglob` 计数即可，不 open |
| `**/*result.json` 个数 | 含 `result.json` 与 `*_result.json` |
| unique clip（按 resolve 后的 `export_path`） | caption 空 / 非空；`Path(export_path).exists()` 是/否 |
| 可探针池 | caption.strip() 非空 **且** GIF exists |

若可探针池 **< 60**：停止，列出差多少、哪些 JSON 缺 caption。不要用空 caption 行凑 60，不要用 `adaptive_test` 里只有 gif_count、没有 top_clips 的 JSON 充数。

`export_path` 若是相对路径，相对仓库根 `C:/Users/sunhao/Desktop/code/GifAgent` 做 `resolve()`。已验证样例：`data/exports/sample_opt_q8vl_centiscale_sexgate_2026-08-25/...gif`。

---

## Task 1 — 不可变拷贝 JSON（补 T-1 漏网）

不要改 coordinator 已冻结的 `inputs_manifest.json` 历史记录（那是 T-1 as_of）。另写：

`data/token_sprint/inputs/exports_corpus_manifest.json`

对每个拷贝的 JSON：`shutil.copy2` 到 `data/token_sprint/inputs/exports/<保留相对路径>/`，记录 `source_abs`、`copy_rel`、`sha256`、`as_of`。只拷 `.json`。任何 `.gif/.jpg/.mp4` 进入拷贝列表必须跳过并记 `skipped_media`。

若已有 `scripts/token_sprint/coordinator/prepare_inputs.py`，不要改它的默认 glob；本任务用 W1 新脚本：

`scripts/token_sprint/ingest_export_corpus.py --sprint-root <path> --exports-root C:/Users/sunhao/Desktop/code/GifAgent/data/exports`

测试（`tests/token_sprint/test_ingest_export_corpus.py`，`tmp_path`）：只拷 JSON、跳过 gif、SHA 对得上。

`--sprint-root` 走 `scripts.token_sprint.coordinator.common.resolve_sprint_root`。

---

## Task 2 — 从 JSON 建文本语料（仍零模型）

新脚本 `scripts/token_sprint/make_export_shards.py`（或给现有 `make_shards.py` 加 `--source export-corpus`，**禁止**默认再读空的 snapshot 文本列）。

每条输出 JSONL（字段对齐 W1 探针，全部文本）：

```json
{
  "candidate_id": "exp_<sha256-of-resolved-export-path-12hex>",
  "vlm_summary_json": "{\"caption\":\"...\",\"emotional_core\":\"...\",\"reason\":\"...\"}",
  "tags_json": "[\"...\"]",
  "scenario_keys_json": "[\"...\"]",
  "final_score": 0.87,
  "stratum": "high|mid|low",
  "source_result_json": "inputs/exports/...",
  "export_path": "C:/.../foo.gif",
  "gif_exists": true
}
```

映射：

- `caption` ← clip.`caption`（必填非空，否则丢弃）
- `emotional_core` / `reason` / `aesthetic_notes` ← clip 同名字段
- `tags_json` ← clip.`aesthetic_notes`；可追加该 result 的 `synthesis.tags`（去重、小写）
- `scenario_keys_json` ← `emotional_core` + `scene_type`（若 synthesis 有）
- `final_score` ← clip.`final_score` 若在 `[0,1]`，否则 `gif_worthiness`
- **不要**把 `export_path` 当模型输入正文；P3 prompt 只看 summary/tags/scenario
- `gif_exists` 必须为 true 才进池（用户要求用留存 GIF 对应的数据；路径只 exists，不读字节）

分层（与 W1 计划一致）：`final_score` 三分位 + `EXPLICIT_MARKERS` 是否出现在 caption/tags。`--probe`：三层各 20，共 60。不足从相邻层补并 stderr 声明。写入：

`shards/shard-probe-export.input.jsonl`

禁止覆盖 `shards/shard-probe.input.jsonl`（那是失败基线，留作对照）。

测试：`tmp_path` 造 70 条有 caption 的假 result.json + 假 `.gif` 空文件（0 字节 create，不写图片内容）→ probe 恰好 60；无 caption 的 clip 不出现；缺 GIF 的 clip 不出现。

```bash
uv run pytest -q tests/token_sprint/test_ingest_export_corpus.py tests/token_sprint/test_make_export_shards.py
```

---

## Task 3 — T0 探针（唯一消耗模型的一步）

1. 生成 input 后，按 `prompts/p3/v1.md` 逐条产出。输入空/无描述才允许 refuse；**本语料 caption 非空，默认不应大面积 refuse。** 无法完成才 `{"candidate_id":"...","refused":true}`。禁止消毒成人语义。禁止看图。
2. 原子写新文件：

`shards/shard-probe-export.p3.v1.{uuid}.jsonl`

3. QC：

```bash
python scripts/token_sprint/validate_shard.py --sprint-root "$SPRINT_ROOT" \
  --shard shards/shard-probe-export.p3.v1.<uuid>.jsonl \
  --pass-id p3 --prompt-version v1 \
  --input-shard shards/shard-probe-export.input.jsonl
```

门（与设计 T0 相同，不得放宽）：

- 拒绝率 ≤ 15%
- schema 合法率 ≥ 95%
- 高显式层保真率 ≥ 90%（启发式）+ **人工复核全部高显式 20 条 + 全部 suspected_sanitized**

4. 自检通过后 `write_ready`（绑定该 jsonl 的 sha256）。不要改文件。
5. 报告：`docs/reports/2026-08-token-sprint/w1-t0-probe-export.md`，必须包含：
   - 与旧探针的对照（60/60 refuse vs 本轮数字）
   - 本节「为什么要换输入」的压缩版
   - 三门结果、容量（ZCode 用量，交给 coordinator 记 usage-log）
   - 可探针池规模（期望 ~306）

**判定：**

- 三门全过 → 停下来汇报，问用户是否开 Stage A/B（仍只用该 exports 语料，不上 12,833 空行）。
- 任一门失败 → 停止模型标注；工具与盘点仍交付。不要用空 snapshot 回退重跑。

---

## 红线清单

- 不 open GIF/JPG/MP4。exists + JSON 文本足够。
- 不 UPDATE 生产 `library.db` / `dist/.../library.db`。
- 不把 `base_rag_similarity` 当检索相关性。
- 不覆盖旧 `shard-probe.*` 产物。
- 不改 `scripts/token_sprint/coordinator/`（可 import common/protocol）。
- canonical manifest 只由协调会话在 receipt 四方链验证后重写。

---

## 完成后返回给用户 / 协调会话

1. 可探针池计数（caption∧gif_exists）
2. 新 input / output 路径与 sha256、`.ready` 路径
3. T0 三门数字与人工复核结论
4. pytest 命令与结果
5. 未决：是否用同一语料做 Stage A/B（等用户）

现在从 Task 0 盘点开始，不要执行 W8，不要碰空 snapshot 探针。
