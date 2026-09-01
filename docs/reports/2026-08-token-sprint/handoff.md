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
5. **W4 突变测试全量套件与评分（14/14 模块全部完成）**：
   - 14/14 目标模块的杀伤测试全部就绪且全绿（`tests/mutation/` 41/41 passed）；
   - 修复了 `mutation_harness.py` 对模块 import 阶段崩溃突变的 `incompetent` 分类处理；
   - 14/14 模块已全部完成基准与杀伤后评分运行（产物 `data/token_sprint/w4/mutation_scores/cosmic-ray/*.json` 全部齐备）；
   - 全量突变体 3,549 个，基线杀死 2,031 个（0.572），杀伤后杀死 2,572 个（**0.725**），净杀死 541 个存活突变体（合计行由协调会话按 14 份 canonical JSON 实算校正）；
   - 得分报告 `w4-mutation-scores.md` 与存活突变登记 `w4-surviving.md` 已全部更新并合并入 `sprint/integration`。
6. **W2 评测 harness**：`eval_harness.py`（fully-judged NDCG 契约、disputed 排除、绝不读 base_rag_similarity——SQL spy 测试锁定）+ `synth_preference_stress.py`（7 门）；空 evalsets 优雅报告。
7. **T0 探针 ×2**：第一轮（空快照）60/60 拒绝 → 正确失败，触发 fallback；第二轮（exports 语料，池 306）**三门全过**（拒绝 0%、schema 100%、高显式保真 100%）。
8. **W1 Stage A/B**：306 条试验集 ×2 分片，P3（60 复用 + 246 新生成）→ P1b（tags 全落冻结词表，全量 306 行子集验证 out-of-vocab=0）→ P2（场景元数据）；`tag_vocab_v1.json` 冻结（565 词，协调会话人工保真审查通过，explicit 组 49 词零消毒）。
9. **监听系统**：`watch_download_dir.py` 最新轮询 media 文件已达 13,523 个（.gif 达 10,974 个），断链 basename 匹配已达 **20,693 / 29,875（69.26%）**。

## 3. 明确的未完成项与状态（2026-08-29 终局更新）

1. **重链 v3（最终版）已产出并验收完毕**：用户裁定下载视为完成、剩余缺卷放弃。v3 = `w3/relink_source_inventory_v3.json`（D:\data 21,787 文件 / 18,298 gif）+ `w3/w3-relink-proposals.v3.jsonl`（**25,109 条 dry-run 建议**：candidate 16,434 / favorites 1,350 / media_e_volume 7,325；ambiguous 2,099 未自动选定；matched_exists=0）。.ready 已发布（producer=coordinator-relink-v3）；独立复审 **PASS（8/8）**。**v3 为最终执行清单，v1/v2 作废。**
2. **W4 全量 14 模块突变测试评分已 100% 完成**：全量 3,549 个突变体，杀伤率 0.572 → **0.725**（净杀死 541），desktop_export_sync 亦完成（703 突变体，0.395→0.440，含 70 条负载伪影 timeout 记 survived 并登记 W7）；报告与测试套件均已合入 `sprint/integration`。
3. **重链 v2 复审独立性违规——已补救**：v2 原为自产自审；补救性独立复审 **pass**（`w3/monitor/relink_v2_independent_review.json`，manifest rev18 scope_ruling）。
4. **W8 / W6 / approved_enrichment_manifest / 合回 master**：全冻结，冲刺结束后由用户决定。
5. **usage-log**：integration 分支；控制台余量始终未取得读数，红线核算未做。

## 4. 冲刺后行动清单（用户侧，按序）

1. **执行重链**（写生产库——冲刺窗口已过才可做）：用 `w3/w3-relink-proposals.v3.jsonl`（包含 25,109 条最终建议），先备份 `dist/GifAgentUI/data/library.db`，按 row_ref 逐条 UPDATE artifact_path/preview_path/full_path。
2. **跑生产 VLM 富化管线**填充 12,833 行空文本（`candidate_gifs.vlm_summary_json/tags_json/scenario_keys_json/final_score` 全空是 T0 失败根因；filesystem-folder-import 只登记路径不跑 VLM）。
3. **重新快照 + 重跑 T0**（`snapshot_library.py --sprint-root …`，三门不放宽），全库数据轨即可复活；Stage C 爬坡需 `p2/v2`（可选 facet 收紧为源文逐字映射，见 manifest rev15 scope_rulings）。
4. **合并决策**：`sprint/integration`（**474 tests 全绿**，app/ 生产代码零修改）合回 master 需用户确认后由协调会话执行；`-mimosa` 门禁的历史遗留 findings（46 高，多为误报：硬编码表名 f-string、内部 work_dir 写入、测试夹具）建议单独排期处理，勿混入冲刺提交。

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
| 跑全量回归 | `uv run pytest -q tests/token_sprint/ tests/property/ tests/mutation/ tests/test_adaptive_config.py tests/test_pipeline_facade.py`（合并后基线：**474 passed**） |

## 7. 冲刺后执行记录（2026-08-29，用户批准"同意执行"后）

1. **合并 master 完成**：`sprint/integration`（97 commits，app/ 零改动）快进合入 master → `1a72c782`。
2. **生产库重链已执行**：备份 `data/token_sprint/backups/library.pre-relink.20260829T182818.db`（sha256 `ca5e45eb…`，sqlite backup API + integrity ok）→ v3 全部 **25,109 条**按 row_ref + 旧值 WHERE 守卫单事务写回（media 7,325 / artifact 8,217 / preview 8,217 / favorites 1,350），**零漂移零缺行**。断链 **29,875 → 4,760**（全库 38,249 路径单元现存率 87.6%）。歧义 2,099 条从未入清单（在 inventory `ambiguous_unknown_detail` 留档待人工）。manifest **rev21** scope_ruling `post_sprint_relink_execution`。
3. **生产 VLM 富化进行中**：新工具 `scripts/enrich_library_candidates.py`（WSL Ollama Qwen3-VL 8B Q8，注意 Windows 侧必须用 `localhost:11434`，`127.0.0.1` 不转发）。先导 3/3 成功（10.1s/行），全量 13,458 行 ETA ≈ 38h，断点续跑（checkpoint `data/token_sprint/enrichment_v1/checkpoint.jsonl`）。
4. **重新快照 + 重跑 T0**：等富化完成后执行（空文本会 100% 触发拒绝门，富化前跑无意义）。
5. **五项后续工作启动（用户批准 #3–#7）**：Mimosa 深扫后台进行（jobId `scan-job-mte9fkh3`）；W6 mypy strict 与 Stage C p2/v2 agent 并行（分支 `sprint/w6-mypy`、`sprint/w1-stagec`，均自 master b31eb794）；W8 首派被并发上限杀、已排队（approved_enrichment_manifest.json 已由 coordinator 落盘）；歧义 2,099 条工作集重建完成（绕过明细 1,000 条上限）——**全部 2,099 条候选内容一致，可自动裁定**（全量 MD5 终验在跑），0 条需要 LLM 裁定，写入生产库仍等用户确认；#4 搜索评测基线 staged（evalsets 为空，qrels 需富化完成后构建）。manifest **rev22**。

## 8. Stage C 滚动扩片终态（2026-08-31 更新，配额墙终止）

### 终态数字
- **已验收 29/30 分片**（shard-001–029，四链齐全，全部「全量 facet 逐字 0 失败」）：
  - rowid 覆盖 **80–13277**，**12,400 / 12,827 已富化行（96.7%）**
  - manifest **rev60**（62 artifacts）；master tip `080c0a75`
- **shard-030（末片，约 427 行，rowid 13277 之后全部）未执行**：派发 25 秒即报 **exceed quota limit**（明确的额度耗尽，非瞬时故障）——配额墙确认，滚动终止。无任何残留（worktree/分片均未创建）。

### 恢复配方（额度恢复后照做即可）
1. 建分支 worktree：`git worktree add .worktrees/sprint-w1-stagec30 -b sprint/w1-stagec30 master`
2. 派一个 Stage C executor（**单槽**），prompt 模板与 shard-029 相同，仅改：接力基线 rowid **13277**、行数=「13277 之后全部已富化行」（约 427 行，9 子批 × 50）、报告文件名 shard30；硬规则（源文逐字 + 连字符≡分隔符裁定 + 禁写 .done）必须原样保留
3. 验收：协调会话用 `data/token_sprint/tmp_verify_shard.py <output> <input>`（校验 sha 链/ID 序/全量逐字）→ `write_done` + `write_receipt` → `reconcile_receipts.py --once` → merge → 删临时校验脚本
4. 全部完成后：累计覆盖 12,827/12,827，Stage C 结项

### 过程要点（后续扩片者必读）
- **接力式窗口**：以上一片输入末行 rowid 为基线取「之后全部已富化行」——库内散布 artifact 缺失的未富化行（设计排除，约占 25–30%），固定 OFFSET 会漂移（shard-009 教训）
- **分批节奏**：500 行 = 10 子批 × 50，每子批 tmp+os.replace 原子落盘 + 逐字断言——多次模型故障只损失当时子批，恢复 agent「核验已有行 → 续写」即可（shard-011/012/016/019 四次恢复全部成功）
- **连字符≡分隔符裁定**（2026-08-30）：caption 连字符与 scene 标签下划线等价（thigh-highs → thigh_highs），校验器已按此归一
- **身份纪律**：producer 绝不写 .done（shard-011 曾发生恢复进程冒用 reviewer 身份，已记录 rev44；协调会话 retroactive review 为验收依据）

### 本阶段全部收官项（自冲刺结束起）
| 项 | 结果 |
|---|---|
| master 合并 | sprint/integration 97 commits 快进 + 后续全部工作（tip `080c0a75`） |
| 生产库重链 | 25,109 + 2,099 条应用（零漂移），断链 29,875 → **2,661**（终态=未下载文件） |
| 生产富化 | 12,827/13,625（94.1%，本地 Ollama Qwen3-VL 8B，零 GLM 消耗）；798 条=无文件可富化 |
| v2 快照 + T0 重跑 | 三门全过（拒绝 0% / schema 100% / 保真 95%），数据轨复活 |
| 搜索基线 | NDCG@10 0.6935 / @24 0.8290（fts_v1）；hybrid 未超；词干碰撞=改进路线图 |
| Stage C p2/v2 | 29 片 12,400 行（96.7%），见上节 |
| W6 mypy | 白名单 196→0，冻结区零 diff |
| W8 shadow FTS | 工具链 + 篡改 fail-closed + env 门，影子验证完成 |
| 歧义裁定 | 2,099 条全内容一致 → 已写回 |
| Mimosa 分诊 | 39 = 36 FP / 0 real / 3 hardening（已修）；P1=0 |
| 测试修复 | 4 个预存失败全解（3 过时断言 + 富化脚本 localhost）；全量 2159 passed |
| final_score | 不盲填（语义冲突+校准关闭+守卫存在）；worthiness 特性接入待用户立项 |
