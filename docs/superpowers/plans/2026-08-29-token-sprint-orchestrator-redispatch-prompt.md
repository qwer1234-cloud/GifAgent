# Token Sprint — 主 Agent 重新派发（exports 语料 T0）Prompt

> 把本文从「Prompt 正文」标题起整段发给**协调会话 / 主 Agent**。
> 它必须派发子 Agent，不要自己把 W1 探针做完。T-1 已完成，不要重跑基线。

---

## Prompt 正文

你是 GifAgent「GLM Token Sprint」的**协调会话（主 Agent）**。用户要求你**重新派发**子 Agent：数据轨不要停在「生产库空文本 → T0 全拒绝」上，改为用 `data/exports/` 里已有管线 JSON 文本重跑 T0 探针。

你自己独占控制面（canonical manifest、receipts 归档、usage-log、合入 `sprint/integration`）。W1 业务由子 Agent 在 `sprint/w1-enrichment` worktree 执行。

T-1 已经验收过。**不要**重做 checkpoint、`uv add`、Cosmic Ray spike、或更换 `sprint_base_sha`。

---

## 局势（据此派发，不要再诊断一轮就停）

上一轮 T0 从 `library_snapshot.db` 抽了 60 条。那 12,833 行 `candidate_gifs` 是 filesystem 入库壳：`vlm_summary_json='{}'`、`tags_json='[]'`、`scenario_keys_json='[]'`、`final_score=NULL`。P3 是「已有描述 → 英文检索词」，输入为空只能 refuse。`shards/shard-probe.p3.v1.407ef957-*.jsonl` 六十条全拒绝是**正确失败**，不要重跑那份 input。

W3 的路径断链（E: / 已删 exports）是**另一件事**。本轮不修断链、不扫 E:、不打开 GIF。

`data/exports/` 里自适应管线已经写过 caption。盘点约数（子 Agent Task 0 必须复核）：约 4,692 个 GIF 仍在；带 `top_clips` 的 result JSON 里约 **306** 条 unique clip 同时「caption 非空」且 `export_path` 的 GIF 仍存在。T0 只需 60。T-1 的 `export_results: absent` 是因为 glob 只匹配了顶层 `data/exports/*.json`，嵌套 JSON 没进共享根——补拷 JSON，仍不拷 GIF。

这不违反红线：不写生产库、不在 ZCode 外跑 VLM、不读图。GIF 路径只 `Path.exists()`。

详细 executor 任务在：

`docs/superpowers/plans/2026-08-29-token-sprint-export-corpus-t0-prompt.md`

派 W1 时把该文件 **「Prompt 正文」整段**作为子 Agent 任务（可再加下面的工作目录/分支行）。

---

## 硬规则（仍有效）

1. 所有模型消耗只经当前 harness。禁止直连 GLM / 写入 API Key。
2. 禁止 `git add -A`、禁止合回 `master`、禁止改 `pyproject.toml` / `uv.lock`。
3. 禁止修改重构 File Map。产物只进 `C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/`。
4. `--sprint-root` 每次显式传。Git Bash。禁止 PowerShell 专有语法。
5. executor 只发产物 + `.ready`；reviewer 发 `.done` + receipt。你验证四方哈希链后才改 `sprint_manifest.json`。不要给控制面文件自己发 receipt。
6. 子 Agent 在已有 worktree 上工作。不要新建基线。
7. **禁止覆盖** `shards/shard-probe.input.jsonl` 与已 `.ready` 的旧探针。新文件名必须带 `probe-export` 与新 UUID。
8. 遇到未覆盖决策：纯新增、可回滚、不碰冻结区；仍歧义则问用户。

共享根：`C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint`

---

## 立刻派发（按这个顺序，不要并行开 W8 / 试验集）

### 1. W1 executor — exports 语料 T0（现在就派）

- 工作目录：`C:/Users/sunhao/Desktop/code/GifAgent/.worktrees/sprint-w1-enrichment`（若缺失：从现有 `sprint_base_sha` 重建 `sprint/w1-enrichment`，不要换 SHA）
- 分支：`sprint/w1-enrichment`
- 任务正文：完整粘贴 `2026-08-29-token-sprint-export-corpus-t0-prompt.md` 的 Prompt 正文
- 只做到：盘点 → 拷 JSON → 建 60 条 `shard-probe-export.input.jsonl` → P3 → `validate_shard.py` → `.ready` + 报告 `w1-t0-probe-export.md`
- 停在 T0 判定。**不要**自行开 Stage A/B、不要写生产库、不要读 GIF 字节

可探针池若 < 60：子 Agent 停止；你向用户汇报，不要凑空 caption。

### 2. W1 reviewer — T0 产物就绪后再派（不要和 executor 同时写同一 jsonl）

对同一 `artifact_sha256` 重跑 `validate_shard.py`，抽检高显式层与 suspected_sanitized。通过才写 `.done` + receipt（`YYYYMMDDTHHMMSSfffZ` + UUID）。不改产物、不碰 canonical manifest。

你随后：

```bash
python scripts/token_sprint/coordinator/reconcile_receipts.py --sprint-root "C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint" --once
```

并在 usage-log 记本包 ZCode 消耗（协调会话独占该文件）。

### 3. 不要派 / 先别派

- **W8**：等本轮 T0 `.done` 且你写了 `approved_enrichment_manifest.json` 之后
- **W2 标注 / judged 子集 / pairwise**：等本轮 T0 三门通过。W2 harness 若已在写测试可继续，但 `build-pools` 不得用空 snapshot 文本列当 qrel
- **W6**：仍默认不启动
- **对 12,833 空行再探针**：禁止
- **修 E: 断链 / 拷 GIF 进共享根**：禁止

### 4. 工程轨（若已在跑，不要打断）

W4 / W5 / W7 继续原计划。本重新派发只复活 **exports 子集上的 W1 T0**，不是全库数据轨开闸。

W3 不要为了本任务重扫生产库。断链 finding 已存在则保持。

---

## T0 之后你怎么闸门

三门（拒绝 ≤15%、schema ≥95%、高显式保真 ≥90% + 人工复核）**全过**：向用户汇报，**问一声**是否用同一 exports 语料开 W1 Stage A/B。未经用户确认不要派试验集子 Agent。

**任一门失败**：数据轨模型标注保持取消；已交付的 ingest/shard 工具可合 `sprint/integration`。不要放宽阈值，不要退回空 snapshot 探针。

---

## 派发 W1 时附加的三行（贴在 executor 任务顶部）

```
工作目录：C:/Users/sunhao/Desktop/code/GifAgent/.worktrees/sprint-w1-enrichment
分支：sprint/w1-enrichment（已有，从 sprint_base_sha；禁止换基线）
共享根：C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint
```

Reviewer 任务顶部同样这三行，并写明：只复核 `shard-probe-export.p3.v1.<uuid>.jsonl` 及其 `.ready` 所绑 SHA。

---

## 完成后向用户报告

1. 派了哪些子 Agent、worktree、状态（running / .ready / .done）
2. Task 0 复核后的可探针池规模（caption∧gif_exists）
3. 新探针路径与 sha256；是否触碰了旧 `shard-probe.*`（必须是否）
4. T0 三门数字（若已跑完）
5. 仍冻结：W6、W8、生产库写入、合回 master、12,833 空行标注

现在立刻派 W1 executor。不要重跑 T-1，不要自己写 P3 分片。
