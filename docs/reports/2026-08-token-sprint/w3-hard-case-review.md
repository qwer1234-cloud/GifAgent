# W3 疑难样本复审定级报告（LLM 会话逐条裁定）

- 裁定时间：2026-08-29
- executor：`w3-hardcase-executor`（模型会话逐条裁定，无外部模型 API 调用）
- 共享根：`C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint`
- 输入分片（canonical，SHA 已核）：
  - `shards/w3-hard.001.b0d75d16-f2f7-41bc-bad8-13afb1f7ad6f.jsonl`（81 行，sha256 `344c95f19469a897d3d07bfd85ced645e23f5d88bfce5e495636558ce8a74ab2`）
  - `shards/w3-hard.002.1976d3e5-a0fc-4c55-8ba9-36c320f80e7c.jsonl`（81 行，sha256 `193b6d0daa04cca433c3b170a039d4731bedb396222517ca9c1d475374a43a73`）
- 输出产物：`data/token_sprint/w3/w3-hard-case-adjudication.v1.4e486bad-0ae9-4bfa-aa64-ad2eab978f00.jsonl`（162 行，sha256 `3162fbc491ec8b4e9f71267ba406c791fc031b7f3209fb909ca5f8f4c3641fe4`，已发 `.ready`，producer=`w3-hardcase-executor`）
- 快照库访问方式：`sqlite3.connect('file:...?mode=ro', uri=True)` + `PRAGMA query_only=ON`，全程只读，未写任何 db；未 open 任何 GIF/JPG/MP4。

## 一、范围说明

纯规则扫描共 30293 条命中，其中 30131 条为高置信度（`broken_artifact` 29874、`missing_field` 251、`score_oob` 6），已随规则扫描报告发布；疑难分片（low confidence）恰好为 162 条，全部是 `mojibake` 规则命中（`library_snapshot` 的 `annotations` 8 条 + `frame_annotations` 154 条）。因此本轮复审定级维度只有 `mojibake`；任务书中提到的 `missing_field` / `broken_artifact` / `score_oob` 三维不存在疑难分片样本，本轮无裁定对象。

## 二、方法论（独立验证，不采信初审结论）

对 162 条逐条执行四步，全部程序化留痕：

1. **源核对**：按 `record_ref`（如 `annotations#1192 (ann_16cdb5e76236)`）解析表名 + rowid，在只读快照库取整行；162/162 的 `text_excerpt` 与源字段**精确子串匹配**（命中字段：`why_i_like_it` 154 条、`caption` 8 条），且 annotation_id 与 candidate_id 全部一致。
2. **全字段非 ASCII 清点**：对每条命中行扫描全部文本字段（`summary/emotional_core/aesthetic_notes_json/why_i_like_it/tags_json/scene_type/caption/raw_json`），逐字符登记码点。结果：全部非 ASCII 字符仅有 7 种 —— `é(U+00E9)×328、à(U+00E0)×6、—(U+2014)×4、å(U+00E5)×2、ç(U+00E7)×2、è(U+00E8)×2、á(U+00E1)×2`，无任何其他字符。
3. **mojibake 特征扫描**（全部为 0 命中）：
   - C1 控制字符（U+0080–U+009F）：0；
   - 替换符 U+FFFD：0；
   - 0xC3 前导错位二元组（`Ã[\x80-\xBF]`）与 `â[\x80-\xBF...]`（`â€™`/`â€œ` 类 cp1252 双重编码）序列：0；
   - CJK 码点（GBK 双重编码特征）：0。
4. **cp1252→UTF-8 往返测试**：对 741 个非空文本字段做 `encode('cp1252').decode('utf-8')` 往返，**0 条发生文本变化**（493 条 SAME，324 条 RT_FAIL——RT_FAIL 全部因为孤立重音字节如 0xE9 不是合法 UTF-8 序列，这恰恰是"未发生双重编码"的证据：真实双重编码文本往返会干净还原）。再对命中字段内的含重音词逐个提取：全部是规范拼写的外来词/人名（见下表）。

## 三、汇总表

| 维度 | confirmed | false_positive | needs_human | 合计 |
|------|-----------|----------------|-------------|------|
| `mojibake` | 0 | **162** | 0 | 162 |
| `missing_field` | – | – | – | 0（无疑难样本） |
| `broken_artifact` | – | – | – | 0（无疑难样本） |
| `score_oob` | – | – | – | 0（无疑难样本） |
| **合计** | **0** | **162** | **0** | **162** |

按裁定类别细分（162 条全部 `severity=low`，`repair_hint=null`）：

| category | 条数 | 说明 |
|----------|------|------|
| `legitimate_latin1_loanword` | 160 | é/à/ç/è 均为规范法语借词：risqué、cliché/clichéd/clichés、(cinéma) vérité/verité、naiveté、ménage à trois、décor、derrière、façade |
| `legitimate_foreign_proper_name` | 2 | 瑞典语 å（Stellan Skarsgård）、捷克语 á（Václav Marhoul），拼写正确 |

## 四、代表性案例（每类 3 个）

### A. 单字符 é 借词（rule 唯一命中字符）

1. `annotations#1192 (ann_16cdb5e76236)`（media_050bf16599f6，`why_i_like_it`）：…"contrast to clichéd tropical cinema"。`clichéd` 为规范法语借词，é 码点 U+00E9，无伴随控制字符。
2. `frame_annotations#121105 (fa_d0f0258531f4)`（media_f5c278995352，`why_i_like_it`）："it's risqué and unconventional content"。`risqué` 规范拼写。
3. `frame_annotations#63194 (fa_fbeef222c596)`：…"intimate and risqué moment…"。同上，é 单字符，往返测试不变。

### B. 多字符法语短语

1. `frame_annotations#22459 (fa_c5c5bf8eb6e6)`（media_8691a936d1d5，`caption`="ménage à trois"）：`à`(U+00E0)+`é`(U+00E9) 均为规范法语短语组成部分， caption 整体为合法短语。
2. `frame_annotations#40706 (fa_f45967fd6b38)`：…"traditional Japanese décor"。`décor` 规范拼写。
3. `frame_annotations#76684 (fa_2f52cc8fdf0a)`：…"close-up on someone's derrière—with an ordinary domestic setting"。`derrière` 为借词；句中 `—`(U+2014) 是标准 em-dash 标点，非 C1 残留。

### C. 外文专有名词

1. `frame_annotations#62097 (fa_3ab0207a16c8)`（media_f7279e30f717，`caption`）：'Lars von Trier's Nymphomaniac' 提及 Charlotte Gainsbourg 与 Stellan Skarsgård。`å`(U+00E5) 为瑞典语人名正确拼写（且与影片事实相符）。
2. `frame_annotations#80615 (fa_b3cb325cd2e4)`：'The Painted Bird' directed by Václav Marhoul。`á`(U+00E1) 为捷克人名正确拼写。
3. `frame_annotations#66495 (fa_b0fe4d16179c)` 语境组参照 `annotations#1588 (ann_6fb0fc8bb587)`："cinema verité style"。即便 é 缺失（verité 而非 vérité）也只是拼写变体/缺符，属字幕风格问题而非编码损坏——编码层完全健康。

## 五、误报结论

**mojibake 维度 162/162 全部为误报（false_positive），无一条真实编码损坏。** 162 条命中全部是"低置信度单字符 Latin-1 借词"判断的独立复核确认：源文本编码层完全健康（无 C1 控制符、无 U+FFFD、无 0xC3 错位、无 GBK/CJK 垃圾、cp1252→UTF-8 往返零变化），所有非 ASCII 字符均处于规范拼写的外来词或人名中，语义上下文为连贯英文影评文本。executor 初审方向（全部为合法借词）经独立验证成立，但本轮系逐条重新取证裁定，非沿袭初审。

### 对规则的改进建议（不改本轮产物，仅记录）

`audit_data.py` 的 mojibake 规则目前对文本字段中任何 Latin-1 重音字母整行告警，在影评语料上产生 100% 误报噪声。建议后续：命中字符限定为 C1 控制符 / U+FFFD / `Ã+Latin-1 补充区符号` / `â€` 序列 / CJK 突现等破坏性特征，或维护影评常用借词白名单（risqué、cliché、vérité、ménage à trois、Skarsgård…），可将该规则疑难量降为 0 而不漏报真实损坏。

## 六、产物与收账

| 产物 | 路径 | sha256 |
|------|------|--------|
| 裁定 JSONL | `C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/w3/w3-hard-case-adjudication.v1.4e486bad-0ae9-4bfa-aa64-ad2eab978f00.jsonl` | `3162fbc491ec8b4e9f71267ba406c791fc031b7f3209fb909ca5f8f4c3641fe4` |
| .ready（producer=w3-hardcase-executor） | 同路径 + `.ready` | 绑定上表 artifact sha256 |

`.done` / canonical manifest / receipt 由 reviewer 与 coordinator 收账，不在本 executor 职责内。
