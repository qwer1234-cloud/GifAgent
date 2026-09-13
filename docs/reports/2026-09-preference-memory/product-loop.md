# Preference Memory 产品闭环 — 执行记录

- 执行时间 (UTC): 2026-09-02
- Git Commit: 8fc5fdc3426feb91d58f62ae1771b49fe7579928
- 目标数据库: dist/GifAgentUI/data/library.db

---

## Task 0: 预检与基线快照

### 1. 数据库基线统计 (只读模式 mode=ro)

- candidate_gifs: 13,625
- candidate_vectors: 9,328
- preference_events: 14,896
  - like: 10,287
  - favorite: 1,857
  - dislike: 1,594
  - neutral: 1,158
- 有效评分事件 (Effective Scoring Events, latest non-superseded like/dislike/favorite): 11,810
- 有效评分目标 candidate_id 数量: 11,810
- 有效评分目标中缺失向量 (Missing Vectors): 2,483
- 当前生效画像 (preference_profile_current):
  - profile_version: profile_8bbf21227ef5d798
  - published_at: 2026-08-28T12:10:01.561012+00:00

### 2. 数据库备份

- 备份文件: data/token_sprint/backups/library.pre-prefloop.20260902_110901.db
- 文件大小: 291,540,992 字节

---

## Task 1: 向量 backfill

### 1. Dry Run 结果

- scanned: 11,810
- missing: 2,483
- skipped_existing: 9,327

### 2. Backfill 执行结果

- processed: 11,810
- inserted: 2,483
- failed: 0
- missing_only: true
- only_feedback: true
- embedding_model: nomic-embed-text:latest (768-d)

### 3. Backfill 后验证

- 有效评分 target 数量: 11,810
- candidate_vectors 总行数: 11,811 (包含 1 个非 feedback 历史候选)
- 有效评分 target 缺失向量数: 0 (100% 覆盖)

---

## Task 1b: 候选入库与富化实时向量写入 (已完成)

### 1. 代码改动
- [`app/services/candidate_vectors.py`](file:///C:/Users/sunhao/Desktop/code/GifAgent/app/services/candidate_vectors.py): 新增公共函数 `upsert_candidate_vector`，支持根据 content-first 规则单条嵌入并写入 `candidate_vectors`，包含防重哈希、模型维度校验及失败 exclusion 记录。
- [`app/services/candidates.py`](file:///C:/Users/sunhao/Desktop/code/GifAgent/app/services/candidates.py): `CandidateService.__init__` 支持注入 `embed_fn`；`materialize_run_candidate` 在同步 FTS 后 best-effort 自动调用 `upsert_candidate_vector` 写入向量，杜绝新增候选产生向量空洞。
- [`scripts/enrich_library_candidates.py`](file:///C:/Users/sunhao/Desktop/code/GifAgent/scripts/enrich_library_candidates.py): 候选富化 VLM 结构化文本后，自动触发 `upsert_candidate_vector` 实时嵌入。
- [`tests/test_candidate_vector_ingest.py`](file:///C:/Users/sunhao/Desktop/code/GifAgent/tests/test_candidate_vector_ingest.py): 新增 4 项自动化测试（自动入库、幂等防重、异常降级、独立 upsert）。

### 2. 生产库增量补齐
- 对发布后新增的 33 条事件产生的 21 条缺失有效评分候选完成 backfill。
- 生产库有效评分目标向量覆盖率恢复至 **100% (11,831 / 11,831)**，候选向量总数 11,832。

---

## Task 2: Holdout 源视频隔离 + build exclude

### 1. 新增/修改文件
- 新建 `scripts/preference_holdout_split.py`: 按源视频切分 train/holdout，写出 `holdout.jsonl`、`exclude_source_videos.json`、`split_summary.json`。
- 新建 `tests/test_preference_holdout_split.py`: 针对源视频隔离、门控评估、overlap 检测进行自动化测试。
- 修改 `app/services/preference_memory.py`: `evaluate_profile_gates`、`preview_profile`、`build_profile` 支持 `exclude_source_videos`。
- 修改 `app/services/preference_evaluation.py`: `evaluate` 与 `evaluate_source_grouped` 读取 build config 中的 `exclude_source_videos` 并正确排除。
- 修改 `app/routers/preference.py`: `BuildRequest` 与 `PreviewRequest` 支持 `exclude_source_videos`。
- 修改 `scripts/evaluate_preference.py`: 增加 `--db` 与 `--grouped` 选项。

### 2. 测试验证
- `pytest tests/test_preference_*.py tests/test_candidate_*.py`: 77 passed in 5.08s
- `mypy`: 0 issues

---

## Task 3: 生产库切分与 Profile Build (未 Publish)

### 1. 源视频切分结果 (Seed: 20260902)
- Holdout 视频数: 5
- Holdout 判定事件数: 43 (Like: 39, Dislike: 4)
- Holdout 最大单视频份额: 37.21% (<= 40%)
- 训练集视频数: 633
- 训练集有效反馈数: 11,767 (Like: 10,192, Dislike: 1,575)
- 训练集最大单视频份额: 1.15% (<= 40%)
- 产物路径: `docs/reports/2026-09-preference-memory/holdout/` (`holdout.jsonl`, `exclude_source_videos.json`, `split_summary.json`)

### 2. Build 结果
- Profile Version: `profile_fdddd3c754081c8f`
- Event Watermark: `2026-09-01T14:45:47.728284+00:00`
- Status: `completed`
- Gate Reasons: `[]`

---

## Task 4: Holdout 评估

### 1. 发布硬门评估结果 (`evaluate`)
- `can_publish`: `true`
- `gate_reasons`: `[]` (无 source_video_overlap，全部门控通过)
- `like_at_20`: `0.0`
- `dislike_at_20`: `0.0`
- `ndcg_at_20`: `0.0`

### 2. 源视频分组质量评估 (`evaluate_source_grouped`)
- `source_video_integrity`:
  - `integrity_ok`: `true`
  - `overlap_violations`: 0
  - `training_source_videos`: 637
  - `holdout_source_videos`: 5
  - `shared_source_videos`: 0
- `base_ndcg_at_20`: 0.0
- `preference_ndcg_at_20`: 0.0
- `ndcg_delta`: 0.0
- `vector_coverage`: 100% (43/43)
- **质量对比性质说明**: 43 条 holdout 判定因早期历史导入候选的 `final_score` / `base_rag_similarity` 字段为 NULL，质量评估指标（NDCG）表现为空列对比，仅能证明源视频隔离硬门通过，**不能**作为 PreferenceReranker 质量提升的有效依据。

---

## Task 5: 画像发布 (Publish)

### 1. 发布执行记录
- 用户确认: 已确认
- 发布时间 (UTC): `2026-09-02T11:15:48.783785+00:00`
- 新生效画像版本: `profile_fdddd3c754081c8f`
- 前一画像版本: `profile_8bbf21227ef5d798`
- Publication ID: 13
- 事件表计数检验: 14,929 条（严格 append-only，无任何删除或修改）
- 开关状态: `configs/models.yaml` 中 `preference_memory.enabled` 维持 `false`

---

## Task 6: 启用开关状态

- 当前开关: `preference_memory.enabled = false`
- 说明: 新画像 `profile_fdddd3c754081c8f` 已就绪并在 `preference_profile_current` 生效。由于当前 holdout 样本未提供充分的排序正向收益证据，**重排开关严格保持关闭**，搜索与导出排序完全等同基线。

---

## Task 7: 回归验证与总结

### 1. 回归测试
- `pytest tests/test_preference_*.py tests/test_candidate_*.py`: 77 passed (5.08s)
- `mypy`: 0 issues (8 source files clean)

### 2. 状态总结表

| 检查项 | 状态 / 数值 |
|---|---|
| 生产数据库路径 | `dist/GifAgentUI/data/library.db` |
| 备份快照 | `data/token_sprint/backups/library.pre-prefloop.20260902_110901.db` |
| 向量 Backfill (有效评分目标) | 11,831 / 11,831 (100% 覆盖，总向量 11,832) |
| 实时写入能力 (Task 1b) | `CandidateService` 与 `enrich_library_candidates` 已接入实时写向量 |
| Holdout 切分配置 | Seed 20260902 (5 个视频, 43 条判定, 最大份额 37.21%) |
| 当前生效画像版本 | `profile_fdddd3c754081c8f` (已于 2026-09-02T11:15:48Z 发布) |
| 前一画像版本 | `profile_8bbf21227ef5d798` (可通过 rollback 随时恢复) |
| Reranker 开关状态 | `preference_memory.enabled = false` (导出行为等价基线) |
| 事件表状态 | 14,929 行 (严格 append-only，无任何删除或修改) |

### 3. 未做项 (按计划明确排除)
- LangGraph 编排重构 (排在本闭环稳定后)
- 断链 2,661 条候选 / 798 条未富化文件修复
- 全量库候选（非反馈目标）剩余向量铺满
