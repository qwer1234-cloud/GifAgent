# Token Sprint 交接文档（Handoff）

> 写于 2026-08-29，协调会话按用户指令执行有序收尾与完整交接。
> 本文档是唯一交接入口；配套真源：`docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md`（v4.1 契约）、
> `data/token_sprint/sprint_manifest.json`（canonical，rev16）、`data/token_sprint/w3/monitor/state.json`（下载监听最新状态）。

## 1. 冲刺拓扑（不可变事实）

| 项 | 值 |
|---|---|
| refactor_base_sha | `df3dde0b`（master，重构 agent 的 scoped checkpoint） |
| sprint_base_sha | `a6c9fb0a`（依赖冻结：hypothesis / mypy / cosmic-ray 8.7.0；此后无依赖变更） |
| sprint/integration HEAD | 最新已包含全部 5 个已验收分支 + W3 v2 + W4 完整测试集与报告；**未合回 master** |
| worktrees | `.worktrees/sprint-{integration,w1-enrichment,w2-eval-harness,w3-data-audit,w4-mutation,w5-property-tests,w7-code-audit}`（全部从 a6c9fb0a 分叉） |
| 共享根 | `C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/`（manifest rev16、receipts/、inputs/、shards/、w3/、w4/、library_snapshot.db） |
| 额外产物 | `data/token_sprint/tag_vocab_v1.json`（冻结词表 565 词）、`tag_vocab_v1.candidate.json`（同字节）、`w3/monitor/`（监听遥测 + candidate_paths.json 旁车） |

## 2. 已交付并走完四方链（executor→.ready→reviewer .done→receipt→manifest）的工作包

canonical manifest **rev16，27 artifacts**（w1=10、w2=2、w3=8、w5=1、w7=6），全部 reviewer PASS：

1. **W7 定向代码审计**（6 份报告）：5 维度 20 findings（最高 Medium）；两假设结论——(a) 生产 FTS 从未构建的根因是重建触发链路缺口（唯一填充入口是手工 POST 端点，UI 无触发、写入无同步），(b) `_rank_with_text` 吞 OperationalError 应至少补 log（建议随 FTS v2 加 metric）。
2. **W5 属性测试**：`tests/property/` 42 条全绿（含 vector_math 条件性质：往返≈l2_normalize、packed prototypes 负性质；零向量/NaN/Inf 按现状 pin）。
3. **W1 工具链 + 快照**：snapshot/make_shards/validate_shard/merge_shards/prompts 六模板；`library_snapshot.db`（sha256 `5af6c815…`，12,833 候选，FTS/state/候选三计数一致，errors=0）；「FTS 空但 state 完成」回归锁。
4. **W3 历史数据体检与重链清点（v1 + v2）**：
   - 规则扫描 30,293 findings（29,874 断链 / 251 缺字段 / 162 mojibake / 6 score_oob）；疑难复审 162/162 mojibake 为误报（合法借词，全库零真损坏）；
   - **重链清点 v1**：4,281 条建议（断链匹配率 14.3%）；
   - **重链清点 v2（重大突破）**：匹配 D:\data 13,481 文件，产出 **16,722 条 dry-run 建议**（断链总匹配率从 14.3% 提升至 **55.98%**，candidate_gifs 匹配率 **72.48%**，favorites 匹配率 **69.66%**，media_e_volume 匹配率 **19.51%**）；reviewer 20/20 抽样核验全部 PASS，已合并入 `sprint/integration`。
5. **W4 突变测试全量杀伤套件**：
   - 14/14 目标模块的杀伤测试全部就绪且全绿（`tests/mutation/` 41/41 passed）；
   - 修复了 `mutation_harness.py` 对模块 import 阶段崩溃突变的 `incompetent` 分类处理；
   - 13/14 模块已完成基准评分（得分 0.658→0.830），`desktop_export_sync` 杀伤测试（14 条）全部通过；已合入 `sprint/integration`。
6. **W2 评测 harness**：`eval_harness.py`（fully-judged NDCG 契约、disputed 排除、绝不读 base_rag_similarity——SQL spy 测试锁定）+ `synth_preference_stress.py`（7 门）；空 evalsets 优雅报告。
7. **T0 探针 ×2**：第一轮（空快照）60/60 拒绝 → 正确失败，触发 fallback；第二轮（exports 语料，池 306）**三门全过**（拒绝 0%、schema 100%、高显式保真 100%）。
8. **W1 Stage A/B**：306 条试验集 ×2 分片，P3（60 复用 + 246 新生成）→ P1b（tags 全落冻结词表，全量 306 行子集验证 out-of-vocab=0）→ P2（场景元数据）；`tag_vocab_v1.json` 冻结（565 词，协调会话人工保真审查通过，explicit 组 49 词零消毒）。
9. **监听系统**：`watch_download_dir.py` 最新轮询 media 文件已达 13,523 个（.gif 达 10,974 个），断链 basename 匹配已达 **20,693 / 29,875（69.26%）**。

## 3. 明确的未完成项与状态（2026-08-29 终局更新）

1. **重链 v3（最终版）已产出并验收完毕**：用户裁定下载视为完成、剩余缺卷放弃。v3 = `w3/relink_source_inventory_v3.json`（D:\data 21,787 文件 / 18,298 gif）+ `w3/w3-relink-proposals.v3.jsonl`（**25,109 条 dry-run 建议**：candidate 16,434 / favorites 1,350 / media_e_volume 7,325；ambiguous 2,099 未自动选定；matched_exists=0）。.ready 已发布（producer=coordinator-relink-v3）；独立复审 **PASS（8/8）**：SHA ×2、214 tests、seed=20260829 抽 20 条 20/20、全 25,109 行穷举五项全 0、好路径全局交集 0、dry-run 零变异 + 临时副本重跑逐字段 0 差异、v1/v2/v3 格式一致。记录 `w3/monitor/relink_v3_independent_review.json`（sha256 `c5f98ecf…`）；两个 v3 产物已入 manifest **rev19**（29 artifacts，acceptance=independent_review）。**v3 为最终执行清单，v1/v2 作废。**
2. **重链 v2 复审独立性违规——已补救**：v2 由 w3-relink-executor 自产自审（producer==reviewer）。补救性独立复审已完成，**verdict=pass**（SHA 复核 ×2、78 tests、seed=20260829 抽 20 条 20/20 PASS、全 16,722 行穷举五项全 0、反向抽核与全局交集 0、dry-run 零变异、v1/v2 格式一致）。记录：`w3/monitor/relink_v2_independent_review.json`（sha256 `ad771f1b…`），已记入 manifest **rev18** scope_ruling `w3_relink_v2_independent_review`。
3. **W4 desktop_export_sync 评分**：两次委派均因「Model request failed」中断后已第三次重派，运行中；杀伤测试（`9df660da`）与 harness 补丁（`dee14b7d`）已合入，只差评分产出 → `w4/mutation_scores/cosmic-ray/desktop_export_sync.json` → 报告合计行（14 模块）。
4. **W8 / W6 / approved_enrichment_manifest / 合回 master**：冻结，均需用户决策。
5. **监听自动化已删除**（等卷已无意义）。手动终态：media 21,185+，断链 basename 匹配 27,208/29,875（91.1%）；`deletion_plan.json` 留档（92.03 GB 已清理）。
6. **usage-log**：integration 分支；控制台余量始终未取得读数，红线核算未做。

## 4. 冲刺后行动清单（用户侧，按序）

1. **执行重链**（写生产库——冲刺窗口已过才可做）：用 **`w3/w3-relink-proposals.v3.jsonl`（最终版，25,109 条建议）**（v2 16,722 条为其子集，已被 v3 覆盖），先备份 `dist/GifAgentUI/data/library.db`，按 row_ref 逐条 UPDATE artifact_path/preview_path/full_path；ambiguous 2,099 条不自动应用，需人工裁定。
2. **跑生产 VLM 富化管线**填充 12,833 行空文本（`candidate_gifs.vlm_summary_json/tags_json/scenario_keys_json/final_score` 全空是 T0 失败根因；filesystem-folder-import 只登记路径不跑 VLM）。
3. **重新快照 + 重跑 T0**（`snapshot_library.py --sprint-root …`，三门不放宽），全库数据轨即可复活；Stage C 爬坡需 `p2/v2`（可选 facet 收紧为源文逐字映射，见 manifest rev15 scope_rulings）。
4. **合并决策**：`sprint/integration`（302 tests 绿，app/ 零改动）合回 master 需用户确认后由协调会话执行；`-mimosa` 门禁的历史遗留 findings（46 高，多为误报：硬编码表名 f-string、内部 work_dir 写入、测试夹具）建议单独排期处理，勿混入冲刺提交。

## 5. 运行机制与经验（恢复工作时必读）

- **Mimosa L3 门**：agent 在 **worktree cwd 内直接 git commit 会被误拦**（基线捕获在 worktree 下失败，46 个基线遗留 findings 全被当新增）。正确做法：`cd C:/Users/sunhao/Desktop/code/GifAgent && git -C .worktrees/<wt> commit …`。
- **后台 agent 并发上限**：实测 2–3（超发即 `user concurrency limit exceeded`）。
- **四方链纪律**：executor 只写 .ready；reviewer 写 .done+receipt；`reconcile_receipts.py --sprint-root … --once` 收账；`.ready` 后产物不可原地改（重跑=新版本路径+新 UUID）。
- **脚本纪律**：所有 token_sprint 脚本显式传 `--sprint-root`；Git Bash；禁 PowerShell 语法。
- **红线**（对任何恢复者继续有效）：不读 GIF/JPG/视频字节（含哈希）；不写生产库；所有模型消耗只经 ZCode harness；不 `git add -A`；不把 `.mimosa/` hook 状态与 `data/` 运行时产物提交进 git。

## 6. 快速恢复索引

| 要做什么 | 看什么 |
|---|---|
| 了解全貌 | 本文档 + design v4.1 + manifest rev19 |
| 查任何产物真伪 | `sprint_manifest.json` artifacts[k].artifact_sha256 → `sha256_file` 比对 → `receipts/processed/` 找 receipt |
| 重跑监听 | `watch_download_dir.py --sprint-root … --watch-root D:/data --max-extract 10`（幂等：已解压自动跳过） |
| 跑重链清点 | `relink_proposals.py`（无 --apply；新输出文件名 + 新 UUID） |
| 跑全量回归 | `uv run pytest -q tests/token_sprint/ tests/property/ tests/test_adaptive_config.py tests/test_pipeline_facade.py`（合并后基线：302 passed） |

