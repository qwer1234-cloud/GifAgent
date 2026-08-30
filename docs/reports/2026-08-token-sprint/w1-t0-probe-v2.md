# W1 — T0 探针 v2 重跑（富化后全库快照）结果报告

- 会话：W1 T0 v2 rerun executor（ZCode，worktree `sprint/w1-t0v2`，基线 master `55f3eceb`）
- `as_of`：2026-08-29
- 判定：**T0 三门全过（拒绝率 0.00%、schema 100%、高显式保真 95% ≥ 90%）+ 人工复核通过
  （复核后 `suspected_sanitized=[]`）→ 富化后的 v2 全库数据轨复活。**

## 1. 背景

原 T0（生产快照，主 sprint root `t1`）失败根因：folder-import 只登记路径、文本字段全空
→ 60/60 refuse（正确失败，不是 QC 故障）。随后本地 VLM 富化修复了这一点：
v2 快照 `data/token_sprint/t1v2/library_snapshot.db`（sha256
`b541e634129c06268c23384fad47e90b7f9a09b928f13840ca9fb2cf1c58683c`，13,625 candidates）
中 **12,827/13,625（94.1%）candidate 行 caption 非空**。本轮在该快照上重跑 T0 三门探针。

exports 语料探针（`w1-t0-probe-export.md`）已验证「有 caption 的 exports 子集能过保真门」；
本轮验证的是**全库主轨**（快照 candidate_gifs 本体）。

## 2. 三次 T0 对照

| | 旧探针（t1，失败基线） | exports 探针（子集轨） | 本轮（v2 全库轨） |
|---|---|---|---|
| input | t1 `shard-probe.input.jsonl`（空文本列） | `shard-probe-export.input.jsonl` | `shard-v2-probe.input.jsonl` |
| 输入文本状态 | `vlm_summary_json='{}'` | caption 全非空 | 12,827 行 caption 非空（94.1%） |
| 输出 | 60/60 refuse | 0/60 refuse | 0/60 refuse |
| 拒绝率 | 1.00 | 0.00 | **0.00** |
| schema 合法率 | 1.00 | 1.00 | **1.00** |
| 高显式保真率（启发式） | null（空分母） | 1.00（分母 38） | **0.95（分母 40；38/40 保留 marker）** |
| `suspected_sanitized`（复核后） | — | `[]` | **`[]`**（2 个启发式误报已复核清除，见 §6） |
| 结论 | 空库不能标（正确行为） | exports 子集过门 | **全库数据轨复活** |

## 3. 工具适配（最小披露式，均配回归测试）

### 3.1 探针池定义：`--pool-filter caption+gif`（make_shards.py）

任务书定义池 =「caption 非空 AND gif 存在」。master 版 `make_shards.py` 导出快照全部行、
无此过滤。适配（opt-in，默认行为不变）：

- `load_rows` 追加 SELECT `artifact_path`（仅用于存在性判定；`EXPORT_FIELDS` 不含路径，
  导出仍是 text-only）；
- `caption_from_summary()`：从 `vlm_summary_json` 取 `caption` 字符串值，畸形/非对象/非字符串
  一律 fail-closed 为空；
- `filter_pool()`：caption 先过滤，再对 `artifact_path` 做 `Path.is_file()` 布尔判定
  （**绝不打开任何媒体字节**）；统计 `rows_total/caption_empty/gif_missing/pool`。

### 3.2 分层判定修复：marker 匹配改读 JSON 字符串**值**（make_shards.py + validate_shard.py）

首轮构建（工具未修前）出现分层退化：**high = 12,827/12,827（100%）**。根因：v2 富化在
每行 `vlm_summary_json` 里写了数值元数据字段 `"sex_act": <int>`，marker 常量的
`\bsex` 词首匹配命中了**字段名** `sex_act`（而非内容），把所有行（含纯剧情行）压进 high 层，
mid/low 全空。退化输入 `shards/shard-probe.input.jsonl`
（sha256 `131bd6ce077efbdf61e9647c639f6d6332f86a4b575e0213f1a53a8c8c3d7cd9`）**原样保留作证据，未覆盖**。

修复（语义对齐 make_shards 文档串「tags/summary/scenario **text** contains a marker」——
字段名不是正文）：

- `_summary_content_text()`：`vlm_summary_json` 只取 JSON 字符串值参与匹配；畸形/非对象
  payload 回退原始串（保守方向：只会增加、不会隐藏匹配）；
- `tags_json`/`scenario_keys_json` 为字符串数组，序列化文本 = 值本身，维持原样；
- 新公共助手 `row_content_text()` 同时被 `validate_shard._input_row_is_high_explicit`
  使用，保证**保真分母与分层判定永远一致**（否则 low 行会因原始串里的 `sex_act` 字段名
  被错误拉进保真分母）。
- exports 语料（`{"caption","emotional_core","reason"}` 纯字符串值）与空快照行为不变；
  全量 `tests/token_sprint/` 271 passed 含既有回归。

修复后分层（池 12,827）：**high 10,847 / mid 0（全快照 final_score 为 NULL，q66 不存在）
/ low 1,980**。probe 按「相邻层填充规则」取：high 20 + mid 20（自 high 剩余层补入）+
low 20 = 60，stderr 注记：`note: probe layer 'mid' short, filled from adjacent layers`。

## 4. 池构建数字

| 指标 | 值 |
|---|---|
| 快照 candidates | 13,625 |
| `rows_total` | 13,625 |
| `caption_empty`（含畸形 JSON，fail-closed） | 798 |
| `gif_missing`（caption-ok 行中 artifact 缺失） | 0 |
| **池（caption 非空 ∧ GIF exists）** | **12,827** |
| 分层 high / mid / low | 10,847 / 0 / 1,980 |
| probe：high / mid（自 high 补）/ low | 20 / 20 / 20 = **60** |
| gif-exists 判定方式 | `Path.is_file()` 布尔，未打开任何字节 |

- 12,827 行 caption 与 GIF 存在性完全同起同落（798 行两者皆缺：folder-import 残留）。
- input：`data/token_sprint/t1v2/shards/shard-v2-probe.input.jsonl`，sha256
  `0c6e6145be8cd6b71dd94d20b8a6005a70177504eef0bf482a92e4281457360c`（60 行 = 40 high + 20 low）。

## 5. T0 三门原文（`validate_shard.py --t0-gate`，exit 0）

```json
{
  "coverage_rate": 1.0,
  "errors": [],
  "fidelity_rate_high_stratum": 0.95,
  "n": 60,
  "pass_id": "p3",
  "prompt_sha256": "0b9be9f5040b9ffedfe9ecb6c46910b387db9834309f559d4f1672026c785cd6",
  "prompt_version": "v1",
  "refuse_rate": 0.0,
  "schema_ok_rate": 1.0,
  "suspected_sanitized": [
    "cand_7a3bab89afdcbf28",
    "cand_5be7450d409de53e"
  ],
  "t0_gates": {
    "fidelity_rate_high_stratum_ge_90pct": true,
    "refuse_rate_le_15pct": true,
    "schema_ok_rate_ge_95pct": true
  },
  "vocab": null
}
```

| 门 | 阈值 | 实测 | 结果 |
|---|---|---|---|
| 拒绝率 | ≤ 15% | 0.00%（0/60） | PASS |
| schema 合法率 | ≥ 95% | 100%（60/60；keywords 3–12、词形合法、无重复） | PASS |
| 高显式保真率（启发式） | ≥ 90% | 95%（38/40 分母行保留 marker/同义词干） | PASS |

- 拒绝行 0：本池 caption 全非空，不存在应拒绝的输入。
- `coverage_rate=1.00`、`errors=[]`。
- prompt sha 说明：`p3/v1.md` 内容与 exports 探针所用副本**逐字节相同（仅换行符差异）**；
  exports 报告的 `9aa8e205…` 为 LF 检出的哈希，本轮 `0b9be9f5…` 为 CRLF 检出的哈希
  （归一化后内容相等，已用脚本验证）。prompt 本体未改动。

## 6. 人工保真复核（必做项）

- 范围：**全部 40 个高显式分母行**（20 high + 20 mid 层自 high 补入）+ 随机 10 个 low 行
  （`random.seed(20260829)`，可复现）= 50 行；另复核全部启发式 `suspected_sanitized`（2 行）。
- 方法：caption/tags/scenario/emotional_core vs keywords 逐条对照；查消毒、臆测、模板话。

### 6.1 高显式 40 行（H=probe 序号）

| # | candidate_id | 保留的显式 marker/证据 | 判定 |
|---|---|---|---|
| 1 | cand_55348b7606e4dece | `lingerie`+`genitals`+`sexual_contact` 原词保留 | pass |
| 2 | cand_2859320f6d73f0f1 | `lingerie`、`fondling` 原词 | pass |
| 3 | cand_9bfb237b2872fb9a | `lingerie` | pass |
| 4 | cand_2c978b4cff3f24f7 | `oral_sex` 原词（无委婉化） | pass |
| 5 | cand_4198a0b0ee9049d0 | `penetration_from_behind`（cap: sexually penetrated from behind） | pass |
| 6 | cand_9aefb854dab14202 | `genital_kissing` 原词（tag） | pass |
| 7 | cand_cb4d835555b881c2 | `kitchen_counter_sex`+`penetration_from_behind` | pass |
| 8 | cand_97795e00e974a3ab | `rope_bondage`（cap: bound by ropes at her wrists 的直接英文命名）+`lingerie` | pass |
| 9 | cand_0751fc6a909036d4 | `lingerie`、`touching_buttocks` 原词 | pass |
| 10 | cand_cadf259a9083be6c | `sensual_motion` 原词（tag；同义词干 sensual） | pass |
| 11 | cand_05411bec9a778de3 | `lingerie` | pass |
| 12 | cand_7dced5bece742f16 | `erect_penis`/`kissing_penis`/`licking_shaft` 原词 | pass |
| 13 | cand_b1a9bee5dff25c64 | `intercourse` 原词 | pass |
| 14 | cand_89616f905e6aa73d | `genital_area` 原词（cap） | pass |
| 15 | cand_7a3bab89afdcbf28 | **启发式误报**，见 6.3 | pass（复核清除） |
| 16 | cand_5be7450d409de53e | **启发式误报**，见 6.3 | pass（复核清除） |
| 17 | cand_5daeeda71061dc3c | `sexual_intercourse` 原词 | pass |
| 18 | cand_68e3547d2cbe2773 | `orgasmic_release` 原词（cap: orgasmic release） | pass |
| 19 | cand_cf509cbffa04e49f | `sensual` 原词（tag） | pass |
| 20 | cand_1f9229cd85d81371 | `lingerie`、`touching_buttocks` | pass |
| 21 | cand_54464915a476b11c | `lingerie` | pass |
| 22 | cand_2580e35f24331986 | `oral_sex` 原词（tag） | pass |
| 23 | cand_882dfb19801522fe | `lingerie` | pass |
| 24 | cand_14875c5674167c8d | `lingerie`、`climaxing`（cap 原词） | pass |
| 25 | cand_a25ba30e349cf009 | `intercourse`、`thong` 原词 | pass |
| 26 | cand_83ca9e67d14641f8 | `lingerie` | pass |
| 27 | cand_486edb0ed3def03d | `sexual_intercourse` 原词 | pass |
| 28 | cand_6a08ea9e2e368352 | `lingerie` | pass |
| 29 | cand_3dbe54008f9f764c | `genital_contact` 原词（cap） | pass |
| 30 | cand_568e9b09d8419e90 | `oral_sex` 原词（tag） | pass |
| 31 | cand_487b69f3e54389e8 | `lingerie` | pass |
| 32 | cand_0718b9d45245908f | `sexual_contact` 原词（tag） | pass |
| 33 | cand_3dbe67fdb0a5325c | `sexual_contact`、`thrusting_motion` 原词 | pass |
| 34 | cand_7fefbcea504b59f2 | `lingerie`、`touching_buttocks`（cap 原词） | pass |
| 35 | cand_72aab81702161283 | `sexual_position` 原词（tag） | pass |
| 36 | cand_5fe235bbfc050b21 | `lingerie`、`touching_backside`（cap: touch her backside） | pass |
| 37 | cand_6fabd8ad7254c18f | `orally_penetrating_mouth`（cap 原词组）+`oral_sex` | pass |
| 38 | cand_7776befdefb31f7f | `oral_sex` 原词 | pass |
| 39 | cand_e2f04a9235b38062 | `sexual_intercourse` 原词 | pass |
| 40 | cand_e67fda0d9aa79008 | `intercourse` 原词 | pass |

显式词汇（lingerie / oral_sex / penis / intercourse / penetration / orgasm / genital /
nude 系 / bondage 系）全部原样保留或以直接词干出现；无委婉化、无回避、无把路径当正文。

### 6.2 随机 10 个 low 行（seed 20260829）

| candidate_id | 证据（关键词全部可溯源） | 判定 |
|---|---|---|
| cand_68308cd18d302911 | `restrained_physical_contact`=cap 原词组；其余=tags | pass |
| cand_e62c43513133850a | `business_suit`/`hallway`/`camera_follow`=tags 原词 | pass |
| cand_bde8f31b1ef51757 | `buttoning_shirt`/`seated_man`/`dark_robe`=cap+tags | pass |
| cand_8e658026cc7373c4 | `holding_card`/`professional_setting`=cap+tags | pass |
| cand_9a8ae2ae46f29823 | `shoulder_bag`=cap "bag over her shoulder" | pass |
| cand_9d1099c573f9af93 | `profile_shot`/`slow_pan`=cap "focus on her profile"/"slowly pans" | pass |
| cand_af499803cf4f901f | `closeup_hands`/`focused_expression`=cap 原词组 | pass |
| cand_57dc3cb930e8f764 | `concerned_expression`/`wooden_doors`=tags | pass |
| cand_f75907c52776c94d | `interlaced_fingers`/`ring`=cap 原词 | pass |
| cand_457129262f938e20 | `quiet_contemplation`=tag 原词；`melancholy`=emotional_core | pass |

### 6.3 启发式 `suspected_sanitized` 的 2 行 → 复核均为误报

| candidate_id | 机制 | 复核结论 |
|---|---|---|
| cand_7a3bab89afdcbf28 | 唯一 marker "sexual" 来自富化 `reason` 字段**否定句**（"without overt **sexual** movement"）；caption/tags/scene 正文（男子掩面而坐、女子西装陪同）无任何显式内容 | **非消毒**。keywords 忠实于画面内容（man_crying/emotional_distress 等）；给非显式画面强加 "sexual" 关键词才是臆测 |
| cand_5be7450d409de53e | 同上（"lacks … clear **sexual** contact"）；正文为走廊对话过肩镜头 | **非消毒**。同上 |

**复核后实际 `suspected_sanitized = []`**；保真失败 0 行。
衍生动作命名披露（2 处，均为对已描述动作的直接英文命名，非臆测）：
`rope_bondage`（cand_97795e00e974a3ab ← "bound by ropes"）、
`oral_stimulation`（cand_7dced5bece742f16 ← "kissing and licking the erect penis"）。

## 7. 产物与哈希

| 产物 | 说明 | SHA-256 |
|---|---|---|
| `data/token_sprint/t1v2/shards/shard-v2-probe.input.jsonl` | 60 行探针 input（40 high + 20 low） | `0c6e6145be8cd6b71dd94d20b8a6005a70177504eef0bf482a92e4281457360c` |
| `data/token_sprint/t1v2/shards/shard-probe.p3.v2.2282ebe4-01bd-4c79-8ce1-6f3d4a64b4ad.jsonl` | P3 v1 输出（60 行，`{"candidate_id","keywords":[3-12]}`；每 20 行原子落盘 20/40/60） | `3485a4d8021eb79f83430ef32774997085f56f5a860044292eba873e755cfea3` |
| 同名 `.ready` | producer=`w1-t0v2-executor`，绑定上行 SHA + 池/三门/人工复核摘要 | `1f6aa0ad9805229138a88baa11d5f63237b11773bf8d3c61bd01646ae66cae99` |
| 同名 `.qc.json` | validate_shard 原文输出（§5） | — |
| `shards/shard-probe.input.jsonl` | **退化分层首轮输入，证据保留，未覆盖** | `131bd6ce077efbdf61e9647c639f6d6332f86a4b575e0213f1a53a8c8c3d7cd9` |

git（worktree `sprint/w1-t0v2`，显式路径提交）：`scripts/token_sprint/make_shards.py`、
`scripts/token_sprint/validate_shard.py`、`tests/token_sprint/test_make_shards_pool_filter.py`、
本报告。

## 8. 测试

```bash
.venv python -m pytest tests/token_sprint/ -q   # 271 passed（含 8 个本轮新增回归）
```

新增 `tests/token_sprint/test_make_shards_pool_filter.py`：caption 提取 fail-closed 矩阵、
池过滤统计、`is_file()` 零字节占位（不读字节）、CLI 双模式（filter on/off）、非法
`--pool-filter` 拒绝、字段名不泄漏进导出、`sex_act` 字段名不再拉 high（分类与保真分母
两侧对齐）、显式内容值仍判 high、畸形 JSON 保守回退、clean 行不进 `suspected_sanitized`、
显式行无 marker 输出仍被标。

## 9. 未决（等 coordinator/reviewer）

1. `.ready` 已就位（§7 哈希），请派 reviewer 复核后写 `.done` + receipt（producer=`w1-t0v2-executor`）。
2. 分层剩余噪声：极少数行的唯一 marker 位于富化 `reason` 评论句（含否定语境）→ 保真
   启发式对这些行必然误报（本轮 2/40，门内）。若后续 Stage A/B 想消掉，可考虑匹配时
   排除 `reason` 字段或引入否定窗口——属语义变更，留 coordinator 决策，本轮未动。
3. mid 层因全快照 `final_score` 为 NULL 而恒空（探针已按相邻层规则补足并注记）；
   评分回填后 mid 层才会自然出现。

## 10. 红线自检

- 未 open/读取任何 GIF/JPG/MP4 字节：GIF 只做 `Path.is_file()` 布尔；测试夹具为 0 字节占位文件。
- 快照与生产库只读：`mode=ro` + `PRAGMA query_only=ON`；未写任何 `.db`。
- 未动 `pyproject.toml`/`uv.lock`；未 `uv add`；未改 `configs/models.*.yaml`。
- 未修改主 sprint root（t1）任何既有产物；一切写入 t1v2 或 worktree；t1v2 内退化首轮输入保留未覆盖。
- 未改 `scripts/token_sprint/coordinator/`（只 import `common`/`protocol`）；未写 `.done`/receipt。
- 未直连 GLM 端点；全部模型消耗（60 行 P3 标注 + 复核）发生在 ZCode harness 内。
