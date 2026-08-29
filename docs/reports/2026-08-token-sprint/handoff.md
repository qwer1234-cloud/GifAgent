# Token Sprint 交接文档（Handoff）

> 写于 2026-08-29，协调会话按用户指令（额度临近阈值即停止）执行有序收尾。
> 本文档是唯一交接入口；配套真源：`docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md`（v4.1 契约）、
> `data/token_sprint/sprint_manifest.json`（canonical，rev15）、`data/token_sprint/w3/monitor/state.json`（下载监听终态）。

## 1. 冲刺拓扑（不可变事实）

| 项 | 值 |
|---|---|
| refactor_base_sha | `df3dde0b`（master，重构 agent 的 scoped checkpoint） |
| sprint_base_sha | `a6c9fb0a`（依赖冻结：hypothesis / mypy / cosmic-ray 8.7.0；此后无依赖变更） |
| sprint/integration HEAD | `13eabaa7`（5 个已验收分支 + usage-log 合入；**未合回 master**） |
| worktrees | `.worktrees/sprint-{integration,w1-enrichment,w2-eval-harness,w3-data-audit,w4-mutation,w5-property-tests,w7-code-audit}`（全部从 a6c9fb0a 分叉） |
| 共享根 | `C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/`（manifest rev15、receipts/、inputs/、shards/、w3/、w4/、library_snapshot.db） |
| 额外产物 | `data/token_sprint/tag_vocab_v1.json`（冻结词表 565 词）、`tag_vocab_v1.candidate.json`（同字节）、`w3/monitor/`（监听遥测 + candidate_paths.json 旁车） |

## 2. 已交付并走完四方链（executor→.ready→reviewer .done→receipt→manifest）的工作包

canonical manifest **rev15，25 artifacts**（w1=10、w2=2、w3=6、w5=1、w7=6），全部 reviewer PASS：

1. **W7 定向代码审计**（6 份报告）：5 维度 20 findings（最高 Medium）；两假设结论——(a) 生产 FTS 从未构建的根因是重建触发链路缺口（唯一填充入口是手工 POST 端点，UI 无触发、写入无同步），(b) `_rank_with_text` 吞 OperationalError 应至少补 log（建议随 FTS v2 加 metric）。
2. **W5 属性测试**：`tests/property/` 42 条全绿（含 vector_math 条件性质：往返≈l2_normalize、packed prototypes 负性质；零向量/NaN/Inf 按现状 pin）。
3. **W1 工具链 + 快照**：snapshot/make_shards/validate_shard/merge_shards/prompts 六模板；`library_snapshot.db`（sha256 `5af6c815…`，12,833 候选，FTS/state/候选三计数一致，errors=0）；「FTS 空但 state 完成」回归锁。
4. **W3 历史数据体检**：规则扫描 30,293 findings（29,874 断链 / 251 缺字段 / 162 mojibake / 6 score_oob）；疑难复审 162/162 mojibake 为误报（合法法语/瑞典语借词，全库 TEXT 列反向扫描零真损坏）；**重链清点 v1**（4,281 条 dry-run 建议，reviewer 20/20 抽检 + 反向核验通过）。
5. **W2 评测 harness**：`eval_harness.py`（fully-judged NDCG 契约、disputed 排除、绝不读 base_rag_similarity——SQL spy 测试锁定）+ `synth_preference_stress.py`（7 门）；空 evalsets 优雅报告。
6. **T0 探针 ×2**：第一轮（空快照）60/60 拒绝 → 正确失败，触发 fallback；第二轮（exports 语料，池 306）**三门全过**（拒绝 0%、schema 100%、高显式保真 100%）。
7. **W1 Stage A/B**：306 条试验集 ×2 分片，P3（60 复用 + 246 新生成）→ P1b（tags 全落冻结词表，全量 306 行子集验证 out-of-vocab=0）→ P2（场景元数据）；`tag_vocab_v1.json` 冻结（565 词，协调会话人工保真审查通过，explicit 组 49 词零消毒）。
8. **监听系统**（已停止，可重启）：`scripts/token_sprint/coordinator/watch_download_dir.py` + 曾每 30 分钟 cron 轮询；终态 media 12,121+、断链 basename 匹配 17,586/29,875（58.9%）且仍在增长。

## 3. 明确的未完成项（按优先级）

1. **W4 desktop_export_sync 评分**：13/14 模块已完成评分（总体突变得分 0.658→0.830，见 `w4-mutation-scores.md`）。该模块 14 条杀伤测试**已写好并全绿**（commit `c9a57fe3` 后的 `f…` 提交，branch sprint/w4-mutation 干净），缺：before/after 评分运行（703 突变体，约 1–2h，用 `scripts/token_sprint/mutation_harness.py` 按其他模块同流程）+ 更新 `w4-mutation-scores.md` 合计行（14 模块口径）+ `w4-surviving.md` 登记。
2. **W3 重链 v2**：下载完成后重跑 `relink_proposals.py`/清点（**新版本路径**，禁止覆盖已 .ready 的 v1）；届时 matched 预计大幅超过 4,281。
3. **W8 影子 FTS / W6 mypy / approved_enrichment_manifest**：全冻结。W8 的触发条件（试验集规模分片）未达——试验集仅 306 条；是否按试验集规模启用 W8 由用户决定。
4. **监听自动化已删除**（cron `每30分钟监听D:\data…` 已 CronDelete）。如需恢复：重建 cron，命令模板在 usage-log 与 `watch_download_dir.py` docstring。
5. **usage-log**：`docs/reports/2026-08-token-sprint/usage-log.md`（integration 分支）记录了全部会话消耗；控制台余量始终未取得用户读数，红线核算未做。

## 4. 冲刺后行动清单（用户侧，按序）

1. **执行重链**（写生产库——冲刺窗口已过才可做）：用 `w3/w3-relink-proposals.v1.jsonl`（v1）+ 下载完成后的 v2，先备份 `dist/GifAgentUI/data/library.db`，按 row_ref 逐条 UPDATE artifact_path/preview_path/full_path。
2. **跑生产 VLM 富化管线**填充 12,833 行空文本（`candidate_gifs.vlm_summary_json/tags_json/scenario_keys_json/final_score` 全空是 T0 失败根因；filesystem-folder-import 只登记路径不跑 VLM）。
3. **重新快照 + 重跑 T0**（`snapshot_library.py --sprint-root …`，三门不放宽），全库数据轨即可复活；Stage C 爬坡需 `p2/v2`（可选 facet 收紧为源文逐字映射，见 manifest rev15 scope_rulings）。
4. **W4 收尾**（1–2h）：补 desktop_export_sync 评分运行 + 报告行，然后 `tests/mutation/` 全绿即可合入。
5. **合并决策**：`sprint/integration`（302 tests 绿，app/ 零改动）合回 master 需用户确认后由协调会话执行；`-mimosa` 门禁的历史遗留 findings（46 高，多为误报：硬编码表名 f-string、内部 work_dir 写入、测试夹具）建议单独排期处理，勿混入冲刺提交。

## 5. 运行机制与经验（恢复工作时必读）

- **Mimosa L3 门**：agent 在 **worktree cwd 内直接 git commit 会被误拦**（基线捕获在 worktree 下失败，46 个基线遗留 findings 全被当新增）。正确做法：`cd C:/Users/sunhao/Desktop/code/GifAgent && git -C .worktrees/<wt> commit …`。用户曾选择"调整门禁"但未生效，实际靠此路径完成全部提交。
- **后台 agent 并发上限**：实测 2–3（超发即 `user concurrency limit exceeded`，被杀的 agent 工作残留需人工盘点，W4 即一例）。
- **四方链纪律**：executor 只写 .ready；reviewer 写 .done+receipt；`reconcile_receipts.py --sprint-root … --once` 收账；`.ready` 后产物不可原地改（重跑=新版本路径+新 UUID）。
- **脚本纪律**：所有 token_sprint 脚本显式传 `--sprint-root`；Git Bash；禁 PowerShell 语法。
- **红线**（对任何恢复者继续有效）：不读 GIF/JPG/视频字节（含哈希）；不写生产库；所有模型消耗只经 ZCode harness；不 `git add -A`；不把 `.mimosa/` hook 状态与 `data/` 运行时产物提交进 git。

## 6. 快速恢复索引

| 要做什么 | 看什么 |
|---|---|
| 了解全貌 | 本文档 + design v4.1 + manifest rev15 |
| 查任何产物真伪 | `sprint_manifest.json` artifacts[k].artifact_sha256 → `sha256_file` 比对 → `receipts/processed/` 找 receipt |
| 重跑监听 | `watch_download_dir.py --sprint-root … --watch-root D:/data --max-extract 10`（幂等：已解压自动跳过） |
| 跑重链 v2 | `relink_proposals.py`（无 --apply；新输出文件名 + 新 UUID） |
| 补 W4 评分 | `mutation_harness.py` + 参照 `w4-mutation-scores.md` 方法节 |
| 跑全量回归 | `uv run pytest -q tests/token_sprint/ tests/property/ tests/test_adaptive_config.py tests/test_pipeline_facade.py`（合并后基线：302 passed） |
