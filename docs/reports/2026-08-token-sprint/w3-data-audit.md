# W3 历史数据体检报告（纯规则扫描）

- 生成时间：2026-08-29T04:31:39+08:00
- 共享根：`C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint`
- 扫描器：`scripts/token_sprint/audit_data.py`（零模型调用）
- 数据根（存在性 stat 基准，不读内容）：`C:/Users/sunhao/Desktop/code/GifAgent`

## 一、输入覆盖率（inputs_manifest 逐项）

| key | 状态 | SHA 校验 | 记录数 | 命中 |
|-----|------|----------|--------|------|
| adaptive_test_result | copied | ok | 116 | 64 |
| batch_checkpoint | copied | ok | 5 | 5 |
| export_results | absent | n/a | 0 | 0 |
| library_snapshot | copied | ok | 306722 | 30224 |
| export_results | absent_fallback | n/a | 0 | 0 |
| version2_doc | copied | n/a | 1 | 0 |

**absent 项 fallback 说明**：`export_results`（`C:/Users/sunhao/Desktop/code/GifAgent/data/exports/*.json`）在 T-1 收集时不存在，inputs_manifest 标记为 `absent`；按计划以 `missing_optional` 记录并继续。本轮未读取任何 worktree 相对路径 `data/exports/`，export_results 维度的规则命中数为 0 仅表示"无输入"，不代表该数据源健康。

## 二、规则命中汇总

| 规则 | 命中数 | 其中疑难片（hard_case） |
|------|--------|--------------------------|
| `missing_field` | 251 | 0 |
| `score_oob` | 6 | 0 |
| `broken_artifact` | 29874 | 0 |
| `mojibake` | 162 | 162 |
| `ts_contradiction` | 0 | 0 |

### 按数据源细分

| 数据源 | 规则 | 命中 |
|--------|------|------|
| adaptive_test_result | `missing_field` | 58 |
| adaptive_test_result | `score_oob` | 6 |
| batch_checkpoint | `broken_artifact` | 5 |
| library_snapshot | `broken_artifact` | 29869 |
| library_snapshot | `missing_field` | 193 |
| library_snapshot | `mojibake` | 162 |

## 三、命中样例（每规则最多 10 条 record_ref）

### `broken_artifact`

- `completed_entry[0]` 字段 `completed_path`（batch_checkpoint）：path does not exist (stat only, relative_to_data_root): path:c:\users\sunhao\desktop\towatch\other\fillupmymom.24.10.24.lauren.phillips.busty.redhead.trying.different.methods.xxx.2160p.mp4-wrb[xc]\fillupmymom.24.10.24.lauren.phillips.busty.redhead.trying.different.methods.4k.mp4
- `completed_entry[1]` 字段 `completed_path`（batch_checkpoint）：path does not exist (stat only, relative_to_data_root): path:c:\users\sunhao\desktop\towatch\other\intimatepov.24.02.17.catherine.knight.spoiling.your.horny.mistress.xxx.2160p.mp4-vsex[xc]\intimatepov.24.02.17.catherine.knight.spoiling.your.horny.mistress.xxx.2160p.mp4
- `completed_entry[2]` 字段 `completed_path`（batch_checkpoint）：path does not exist (stat only, relative_to_data_root): path:c:\users\sunhao\desktop\towatch\other\loveherfeet.23.12.09.kayley.gunner.the.best.christmas.morning.xxx.2160p.mp4-p2p[xc]\loveherfeet.23.12.09.kayley.gunner.the.best.christmas.morning.xxx.mp4
- `completed_entry[3]` 字段 `completed_path`（batch_checkpoint）：path does not exist (stat only, relative_to_data_root): path:c:\users\sunhao\desktop\towatch\other\nympho.26.08.10.octavia.red.xxx.2160p.mp4-nbq[xc]\nympho.26.08.10.octavia.red.4k.mp4
- `completed_entry[4]` 字段 `completed_path`（batch_checkpoint）：path does not exist (stat only, relative_to_data_root): path:c:\users\sunhao\desktop\towatch\other\penthousegold.26.06.10.riley.reign.4k\489155.com@penthousegold.26.06.10.riley.reign.4k.mp4
- `candidate_gifs#80 (cand_68308cd18d302911)` 字段 `artifact_path`（library_snapshot）：path does not exist (stat only, relative_to_data_root): data\exports\adaptive_test\JUR-639\JUR-639@@@001_167s-174s.gif
- `candidate_gifs#80 (cand_68308cd18d302911)` 字段 `preview_path`（library_snapshot）：path does not exist (stat only, relative_to_data_root): data\exports\adaptive_test\JUR-639\JUR-639@@@001_167s-174s.gif
- `candidate_gifs#81 (cand_e62c43513133850a)` 字段 `artifact_path`（library_snapshot）：path does not exist (stat only, relative_to_data_root): data\exports\adaptive_test\JUR-639\JUR-639@@@002_527s-534s.gif
- `candidate_gifs#81 (cand_e62c43513133850a)` 字段 `preview_path`（library_snapshot）：path does not exist (stat only, relative_to_data_root): data\exports\adaptive_test\JUR-639\JUR-639@@@002_527s-534s.gif
- `candidate_gifs#82 (cand_bde8f31b1ef51757)` 字段 `artifact_path`（library_snapshot）：path does not exist (stat only, relative_to_data_root): data\exports\adaptive_test\JUR-639\JUR-639@@@003_992s-999s.gif

### `missing_field`

- `top_clips[0]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null
- `top_clips[1]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null
- `top_clips[2]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null
- `top_clips[3]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null
- `top_clips[4]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null
- `top_clips[5]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null
- `top_clips[6]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null
- `top_clips[7]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null
- `top_clips[8]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null
- `top_clips[9]` 字段 `candidate_id`（adaptive_test_result）：agreed key 'candidate_id' is missing or null

### `mojibake`

- `annotations#1192 (ann_16cdb5e76236)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 1 suspicious chars — The authentic, unposed intimacy captured against exotic backdrops offers a refre
- `annotations#1588 (ann_6fb0fc8bb587)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 1 suspicious chars — The unfiltered portrayal of vulnerability and authenticity evokes a cinema verit
- `annotations#2771 (ann_2a118759c04a)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 1 suspicious chars — The film's nuanced portrayal of intimacy through subtle physical and emotional c
- `annotations#3098 (ann_e7ad31d0cb0f)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 1 suspicious chars — The unflinching portrayal of vulnerability and sensuality through nudity, framed
- `annotations#3484 (ann_c5a2f01d668c)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 1 suspicious chars — The unflinching portrayal of vulnerability and trust in intimate moments transce
- `annotations#3569 (ann_5a30d3af2de7)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 3 suspicious chars — The unpolished, cinéma vérité style captures raw emotional vulnerability through
- `annotations#4924 (ann_b081cc56eeb6)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 1 suspicious chars — The authentic portrayal of vulnerability and connection through meticulous atten
- `annotations#6686 (ann_e22507784adc)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 1 suspicious chars — The use of close-ups and blurred backgrounds creates a raw, emotionally resonant
- `frame_annotations#1514 (fa_2f4bcd9e40b8)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 1 suspicious chars — the scene captures a moment of vulnerability between two individuals, showcasing
- `frame_annotations#2256 (fa_0747bc453d4b)` 字段 `why_i_like_it`（library_snapshot）：mojibake signature (low confidence), 1 suspicious chars — This scene is powerful in evoking desire as it shows an intimate and risqué mome

### `score_oob`

- `$.duplicate_groups[1].max_sim` 字段 `max_sim`（adaptive_test_result）：value 1.0000001192092896 outside [0,1]
- `$.duplicate_groups[4].max_sim` 字段 `max_sim`（adaptive_test_result）：value 1.0000001192092896 outside [0,1]
- `$.duplicate_groups[5].max_sim` 字段 `max_sim`（adaptive_test_result）：value 1.0000001192092896 outside [0,1]
- `$.duplicate_groups[12].max_sim` 字段 `max_sim`（adaptive_test_result）：value 1.0000001192092896 outside [0,1]
- `$.duplicate_groups[14].max_sim` 字段 `max_sim`（adaptive_test_result）：value 1.0000001192092896 outside [0,1]
- `$.duplicate_groups[25].max_sim` 字段 `max_sim`（adaptive_test_result）：value 1.0000001192092896 outside [0,1]

## 四、疑难样本分片清单（本轮仅列片，不派模型）

| 分片文件 | 行数 | attempt_id | sha256 |
|----------|------|------------|--------|
| `w3-hard.001.b0d75d16-f2f7-41bc-bad8-13afb1f7ad6f.jsonl` | 81 | `b0d75d16-f2f7-41bc-bad8-13afb1f7ad6f` | `344c95f19469a897d3d07bfd85ced645e23f5d88bfce5e495636558ce8a74ab2` |
| `w3-hard.002.1976d3e5-a0fc-4c55-8ba9-36c320f80e7c.jsonl` | 81 | `1976d3e5-a0fc-4c55-8ba9-36c320f80e7c` | `193b6d0daa04cca433c3b170a039d4731bedb396222517ca9c1d475374a43a73` |

分片纪律：每片 ≤100 行；`attempt_id` 为 UUID，禁止覆盖旧产物；分片只含文本摘录与记录引用，不含任何媒体内容。

## 五、结构性观察（非规则命中，供修复决策参考）

- 全表分数列为 null：`candidate_gifs` {'final_score': 12833, 'profile_score': 12833, 'base_rag_similarity': 12833}（按规则定义 null 分数不计 `score_oob`/`missing_field`，单独列观察）
- 快照表覆盖：20 张表（显式规则 20 张、通用规则 0 张），共 306722 行
- 快照跳过表（镜像/内部）：`candidate_search_fts`, `candidate_search_fts_config`, `candidate_search_fts_content`, `candidate_search_fts_data`, `candidate_search_fts_docsize`, `candidate_search_fts_idx`, `search_index_state`, `sqlite_sequence`
- 源视频已不存在（仅 stat 探测，未读内容，共 1 处）：`C:\Users\sunhao\Desktop\ToWatch\other\Penthousegold.26.06.10.Riley.Reign.4k\489155.com@Penthousegold.26.06.10.Riley.Reign.4k.mp4`
- 混合时区时间戳对（naive vs aware，无法可靠比较，未计命中）：共 78 处，样例：`candidate_gifs#80 (cand_68308cd18d302911)`, `candidate_gifs#81 (cand_e62c43513133850a)`, `candidate_gifs#82 (cand_bde8f31b1ef51757)`（字段 updated_at<created_at）

## 六、方法与限制

- 本轮为纯规则扫描，零模型调用；疑难片仅产出清单，模型定级留待 Task 3。
- 全部输入为 T-1 不可变副本，SHA-256 逐项复核一致后才扫描（fail closed）。
- `artifact_path` 类字段只做 `Path.exists` 存在性 stat，未 open 任何 GIF/JPG/视频；相对路径以生产检出根为基准解析，未使用 worktree 相对路径。
- LLM 抽查（20 条无误报门）：本轮 N/A（未派模型），规则结果照常交付。
- 产物 JSON：`C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/w3/w3-audit-findings.json`

## 七、executor 结论与修复建议（人工研判层）

1. **broken_artifact（29,874，最大宗）**：9,221 条 `media.file_path` 全部指向 `E:/data/originals/…`（E: 卷当前不可用/已移除）；9,551 条 `candidate_gifs.artifact_path`（另有同数量 `preview_path`）指向生产根 `data/exports/adaptive_test/` 下已删除的子目录；1,546 条 `favorite_gifs.full_path` 同因失效；`batch_checkpoint` 的 5 条已完成输入视频全部被移动/删除（含本次 adaptive 运行的源视频）。建议：不要批量置 NULL——先用 basename 在 E: 卷/备份/回收站做 relink 搜索（`repair_data_dry_run.py` 的 Option A 模板），确认永久丢失后再置状态位。
2. **missing_field（251）**：58 条 `top_clips` 无 `candidate_id`（历史 adaptive 结果只以 rank/export 文件名定位，可按 `rank-1` 与 `gif_exports[k].quality_assessment.candidate_id` 对齐回填，dry-run 脚本已给 JSON patch 模板）；193 条 `frames.timestamp` 为空。
3. **score_oob（6）**：全部为 `duplicate_groups[].max_sim = 1.0000001192…`，即 float32 精度对 1.0 的溢出（约 +1.2e-7），非真实越界；建议规则层加 1e-6 容差或 clamp，不作为数据缺陷处置。
4. **mojibake（162，全部低置信 → 疑难片 2 片 × 81 条）**：抽样显示多为 vérité/cliché/risqué 等合法法语借词（Latin-1 扩展字符少量出现），大概率应判 ignore；强签名（C1 控制符、0xC3/0xE2 前导对）命中为 0。交模型定级时须遵守成人文本保真：不得把显式成人用词当乱码删除。
5. **ts_contradiction：0 命中**（end<start、updated_at<created_at、undone_at<created_at、finished_at<started_at 均未发现矛盾）。
6. **结构性观察**：`candidate_gifs` 全表 12,833 行的 `final_score`/`profile_score`/`base_rag_similarity` 全为 null（候选层从未回填打分，规则定义下不计命中，单列观察）；78 处 naive/aware 混合时区 `created_at` 无法与 `updated_at` 可靠比较（多集中于早期 naive 写入行）。
7. **export_results（absent）**：T-1 收集时生产 `data/exports/*.json` 无匹配文件，按计划 fallback 记录并继续；该维度无输入可扫，其"0 命中"不代表健康。

*本节为 executor 对规则输出的人工研判，非模型结论；LLM 定级（P0/P1/P2/ignore）待 Task 3 对 2 片疑难样本执行后补充。强签名 mojibake 命中数：0。*
