# W1 — T0 探针（exports 语料轨）结果报告

- 会话：W1 executor（ZCode，worktree `sprint/w1-enrichment`）
- `as_of`：2026-08-29
- 判定：**三门全过 → 数据轨复活；停下等待用户决定是否用同一 exports 语料开 Stage A/B。**

## 1. 与旧探针的对照

| | 旧探针（失败基线） | 本轮（exports 语料） |
|---|---|---|
| input | `shards/shard-probe.input.jsonl`（生产快照文本列） | `shards/shard-probe-export.input.jsonl`（exports result JSON 文本） |
| 输入文本状态 | `vlm_summary_json='{}'`、`tags_json='[]'`（schema 缺省，主富化从未运行） | caption 全部非空（自适应管线看过图后写下的主富化文本） |
| 输出 | `shard-probe.p3.v1.407ef957-…jsonl` | `shard-probe-export.p3.v1.10b722cd-fdf2-46f6-b1d7-491a36ccb4eb.jsonl` |
| 拒绝率 | **1.00（60/60 refuse）** | **0.00（0/60）** |
| schema 合法率 | 1.00（拒绝行不算 schema 失败） | 1.00 |
| 高显式保真率（启发式） | null（空输入无高显式分母） | **1.00** |
| 覆盖率 | 0.00 | 1.00 |
| 结论 | 只证明「空库不能标」，是正确行为而非 QC 失败 | 验证「有 caption 的 exports 子集能过保真门」 |

旧 `shards/shard-probe.*` 产物原样保留作对照，未被覆盖。

## 2. 为什么换输入（压缩版）

1. 生产 `candidate_gifs` 是「有行无字」：约 12,833 行文本列全为 schema 缺省（`filesystem-folder-import`
   只登记路径不跑 VLM 的合法结果），旧 T0 的 60/60 拒绝因此是正确行为。
2. W1 P3 是二阶文本转换（依据已有画面描述生成英文检索关键词），输入为空 → `refused:true` 才是对的；
   不重跑空 input。
3. 文件断链（约 2.9 万路径，E: 卷/已删 exports）是 W3 的事，与「行文本为空」独立；不去修断链、
   不扫 E:、不打开 GIF。
4. `data/exports/**/result.json` 已有主富化文本（caption/emotional_core/aesthetic_notes/gif_worthiness/
   export_path）。T-1 `inputs_manifest.json` 的 `export_results` 记 `absent` 只因 glob 只匹配顶层
   ——本轮以 W1 新脚本 `ingest_export_corpus.py` 补拷 JSON 文本（不动 coordinator 冻结产物）。
5. 红线内做法：不写生产库、不在 ZCode 外跑 VLM、不读任何媒体字节；GIF 路径只做 `Path.exists()`
   布尔过滤与溯源。

## 3. 可探针池（Task 0 盘点）

| 指标 | 值 |
|---|---|
| `data/exports` 下 `.gif` 个数（仅计数，不 open） | 4,692 |
| `**/*result.json`（result.json + `*_result.json`） | 106（0 解析失败） |
| unique clip（按 resolve 后 `export_path`） | 601 |
| caption 非空 ∧ GIF exists（**可探针池**） | **306**（≥ 60，满足） |
| GIF 存在但 caption 空 | 295（不进池、不凑数） |
| caption 非空但 GIF 缺失 | 0 |

详见 `w1-export-corpus-inventory.md` 与共享根 `w1/export_corpus_inventory.json`。

## 4. 本轮产物与数字

### 产物（共享根，均走原子写；不 git commit）

| 产物 | 说明 | SHA-256 |
|---|---|---|
| `inputs/exports/**`（106 个 result JSON） | `shutil.copy2` 不可变拷贝，保留相对路径 | 逐文件见 `exports_corpus_manifest.json` |
| `exports_corpus_manifest.json` | 每文件 `source_abs`/`copy_rel`/`sha256`/`as_of`；`skipped_media=[]`；media_seen=4,932 | manifest 自身哈希见 `.ready` 的 `qc_summary` |
| `shards/shard-probe-export.input.jsonl` | 60 行探针 input（high 38 / mid 2 / low 20；mid 层不足已按规则从相邻层补并 stderr 声明） | `c76ac6cdff84ed7096308e95b63c0df067dad0da4ad17cc652dde3c78feacbc3` |
| `shards/shard-probe-export.p3.v1.10b722cd-fdf2-46f6-b1d7-491a36ccb4eb.jsonl` | P3 v1 输出（60 行，逐行 `{"candidate_id","keywords":[3-12 个 ^[a-z0-9_]+$ 词]}`） | `5d548600b7339c29f46d07956fdc137f214ff82b5d01c2fe885d2173abe3bb04` |
| 同名 `.ready` | 绑定上行 SHA + 完整 QC/人工复核摘要，producer=`w1-executor` | `4fbc2b8f66549dfd237f08883b605a66a492362eadd1f7b3328a0e51d929e2d1` |
| 同名 `.qc.json` | `validate_shard.py` 输出 | — |

### T0 三门（`validate_shard.py --pass-id p3 --prompt-version v1 --t0-gate`，exit 0）

| 门 | 阈值 | 实测 | 结果 |
|---|---|---|---|
| 拒绝率 | ≤ 15% | **0.00%**（0/60） | PASS |
| schema 合法率 | ≥ 95% | **100%**（60/60；keywords 3–12 个、词形全合法、无重复） | PASS |
| 高显式层保真率（启发式） | ≥ 90% | **100%**（分母 38 个高显式行全部保留 marker/同义词干） | PASS |

附带：`coverage_rate=1.00`、`errors=[]`、`suspected_sanitized=[]`、
`prompt_sha256=9aa8e205007605d5a0806a74fd90f8bc40d81dae8a352a449307b48801b25a45`（p3/v1 未改动）。

### 人工复核（必做项）

- 范围：**全部 38 个高显式行**（高显式层 20 行 + mid 层从 high 相邻层补入的 18 行）+ 全部
  `suspected_sanitized`（0 行）。
- 方法：caption vs keywords 逐条对照（H01–H38，见 `.ready` qc_summary 与本报告存档）。
- 结论：**pass**。显式词汇全部原样保留（nude / naked / sex / sexual_act / blowjob / oral_sex /
  penis / licking / erotic / sensual / butt / legs_spread / breasts / partially_clothed），
  无委婉化、无回避；关键词均可在 caption/tags/场景/情绪中找到出处，未把 `export_path` 当正文。
- 拒绝行：0（本语料 caption 全非空，不存在应拒绝的输入）。

### 容量（交 coordinator 记 usage-log）

- 本工作包（Task 0–3，含 60 行 P3 标注、两脚本+测试、盘点与报告）ZCode 会话 token 消耗：
  **粗估 ~350k–450k tokens（estimated，非控制台精确读数）**。
- 与旧失败探针同量级（60 行标注），但产出为有效标注；不重跑 60/60 空转。
- 请 coordinator 按全局约束 13 记入 `usage-log.md` 并抄录控制台余量。

## 5. 测试与工程产物

```bash
uv run pytest -q tests/token_sprint/test_ingest_export_corpus.py tests/token_sprint/test_make_export_shards.py
# 15 passed
uv run pytest -q tests/token_sprint/   # 129 passed（含既有 W1 工具回归）
```

新增（worktree，git）：

- `scripts/token_sprint/ingest_export_corpus.py` + `tests/token_sprint/test_ingest_export_corpus.py`
  （只拷 JSON、跳过媒体、SHA 对账、幂等、不可变冲突 fail-closed、不动 coordinator `inputs_manifest.json`）
- `scripts/token_sprint/make_export_shards.py` + `tests/token_sprint/test_make_export_shards.py`
  （`--source export-corpus` 等价物；禁止默认读空 snapshot 文本列；caption∧gif_exists 过滤；
  `exp_<sha12>` id；分层复用 `make_shards.classify_strata`/`build_probe_rows`；缺 GIF/caption 的
  clip 不进池；probe 恰 60）
- `docs/reports/2026-08-token-sprint/w1-export-corpus-inventory.md`（Task 0）

## 6. 判定与未决

- **三门全过 + 人工复核通过** → 按任务书「停下来汇报」，**不开 Stage A/B、不上 12,833 空行、
  不写回 library.db、不执行 W8**。
- 未决（等用户/coordinator）：
  1. 是否用同一 exports 语料（池 306）开 Stage A/B（P3 → P1a 词表 → P1b/P2）；
  2. `.ready` 已就位，请派 reviewer 复核后写 `.done` + receipt；
  3. 本报告容量估算请记入 usage-log（coordinator 独占）。

## 7. 红线自检

- 未 open 任何 GIF/JPG/MP4 字节（只 `rglob` 计数 + `Path.exists()` + JSON 文本）。
- 未写生产库 / `dist/` / 根层 `data/*.db*`；未改 `configs/models.*.yaml`；未 `uv add`。
- 未改 `scripts/token_sprint/coordinator/`（只 import `common`/`protocol`）；未碰 canonical
  `sprint_manifest.json`、`receipts/`；未写 `.done`/receipt。
- 未覆盖任何旧 `shard-probe.*` 产物；新探针用新 `attempt_id` 与新文件名。
- 未直连 GLM 端点；全部模型消耗发生在 ZCode harness 内。
