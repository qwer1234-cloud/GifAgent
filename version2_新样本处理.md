# GifAgent 新样本处理与长期偏好记忆方案 v2

本文档给后续执行 Agent 使用。目标是在现有全量收藏 GIF RAG 入库完成后，新增一套“新视频候选 GIF + 用户反馈 + 长期偏好画像”的闭环系统。

核心原则：

```text
主库保持稳定，新样本先进入候选层。
用户反馈进入事件层。
事件定期汇总为偏好画像。
偏好画像只参与候选重排，不直接污染基础收藏索引。
```

用户偏好方向已经明确：以 A+C 为主。

```text
A: 稳定个人审美
C: 分场景记忆
```

也就是说，主收藏库是长期稳定审美先验；后续从新视频里采到的 GIF 不自动写入主库，而是进入候选表。用户对候选 GIF 做 like、dislike、neutral 后，系统把这些反馈汇总成全局画像和分场景画像，用于下一次新视频候选重排。

## 1. 当前项目基础

当前代码中已有的相关结构：

```text
media
frames
annotations
frame_annotations
feedback
vector_refs
video_clips
```

已有 API：

```text
POST /api/feedback
GET  /api/review/next
GET  /api/review/{media_id}
GET  /api/media/{media_id}/score
```

已有索引：

```text
data/faiss/media_index.faiss
data/faiss/id_map.json
data/faiss/manifest.json
```

当前 `feedback` 表绑定的是 `media_id`：

```sql
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id TEXT PRIMARY KEY,
    media_id TEXT NOT NULL,
    user_rating TEXT CHECK(user_rating IN ('like','dislike','neutral')),
    corrected_tags_json TEXT,
    favorite_reason TEXT,
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY(media_id) REFERENCES media(media_id)
);
```

这适合审核主库媒体，但不适合直接表达“新视频候选 GIF”的反馈。新样本可能还没有进入 `media`，也不应该默认进入 `media`。因此 v2 需要新增候选层和偏好事件层。

## 2. 设计目标

### 2.1 必须实现

1. 新视频发现出的 GIF 先进入候选表，不直接进入主 `media`。
2. 用户可以对候选 GIF 做 `like`、`dislike`、`neutral`。
3. 反馈必须保留为事件流，不覆盖历史。
4. 系统可以从事件流构建：
   - 全局偏好画像
   - 分场景偏好画像
5. 新视频候选排序时使用：
   - 基础收藏库 RAG 相似度
   - 全局偏好画像
   - 分场景偏好画像
   - dislike 排斥项
6. 明确提供“晋升到主库”的操作。只有用户明确选择后，候选 GIF 才能进入 `media`。

### 2.2 不做的事

第一版不要做：

- 不训练神经网络 reranker。
- 不实时重写主 FAISS 索引。
- 不把所有 liked candidate 自动加入主收藏库。
- 不把一次 like/dislike 立刻强改全局偏好。
- 不删除或重置主库。

### 2.3 成功标准

实现完成后应满足：

```text
1. 新视频候选可以落库、审核、反馈。
2. 主 media 表不会因为候选生成而增长，除非用户显式 promote。
3. preference_events 能追踪所有反馈。
4. preference_profiles 能从反馈事件重建。
5. rerank 后，明确 dislike 过的相似模式会被降权。
6. 分场景 profile 样本不足时自动回退到 global profile。
7. 所有新增功能有测试覆盖。
```

## 3. 总体架构

推荐分为四层：

```text
base_library
  现有 media / annotations / vector_refs / media_index
  含义：稳定个人收藏库，长期审美先验

candidate_layer
  candidate_gifs / candidate_frames / candidate_vectors
  含义：从新视频中发现的候选 GIF，尚未进入主收藏库

feedback_layer
  preference_events
  含义：用户对候选或主库媒体做出的 like/dislike/neutral 事件

profile_layer
  preference_profiles
  含义：从 feedback_layer 汇总出的全局画像和分场景画像
```

数据流：

```text
新视频
  -> 抽帧/采样
  -> VLM/LLM 标注
  -> base RAG 初筛
  -> candidate_gifs 落库
  -> 用户审核 like/dislike/neutral
  -> preference_events 追加事件
  -> rebuild preference_profiles
  -> 后续候选 rerank 使用 profile
```

候选晋升流：

```text
candidate_gifs
  -> 用户明确 promote
  -> 写入 media / frames / annotations
  -> 写入主 FAISS
  -> 保留 candidate 记录并标记 promoted
```

## 4. 数据库设计

### 4.1 新增 `candidate_gifs`

用途：保存新视频中发现的候选 GIF 或候选片段。它是新样本的主入口。

建议 schema：

```sql
CREATE TABLE IF NOT EXISTS candidate_gifs (
    candidate_id TEXT PRIMARY KEY,

    source_video_id TEXT,
    source_video_path TEXT NOT NULL,
    source_video_sha256 TEXT,

    start REAL NOT NULL,
    end REAL NOT NULL,
    duration REAL NOT NULL,

    representative_frame_path TEXT,
    exported_gif_path TEXT,
    export_status TEXT DEFAULT 'not_exported'
        CHECK(export_status IN ('not_exported','exported','failed')),

    caption TEXT,
    summary TEXT,
    emotional_core TEXT,
    aesthetic_notes_json TEXT,
    why_i_like_it TEXT,
    tags_json TEXT,
    scene_type TEXT,

    scenario_keys_json TEXT,

    base_rag_score REAL DEFAULT 0,
    profile_score REAL DEFAULT 0,
    dislike_penalty REAL DEFAULT 0,
    final_score REAL DEFAULT 0,
    score_json TEXT,

    status TEXT DEFAULT 'candidate'
        CHECK(status IN ('candidate','liked','disliked','neutral','promoted','rejected','archived')),

    promoted_media_id TEXT,

    model_info_json TEXT,
    quality_status TEXT DEFAULT 'unchecked',
    quality_errors_json TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

索引：

```sql
CREATE INDEX IF NOT EXISTS idx_candidate_status ON candidate_gifs(status);
CREATE INDEX IF NOT EXISTS idx_candidate_source_video ON candidate_gifs(source_video_sha256);
CREATE INDEX IF NOT EXISTS idx_candidate_score ON candidate_gifs(final_score);
CREATE INDEX IF NOT EXISTS idx_candidate_emotion ON candidate_gifs(emotional_core);
CREATE INDEX IF NOT EXISTS idx_candidate_scene_type ON candidate_gifs(scene_type);
```

说明：

- `source_video_id` 可以为空。若源视频已进入 `media`，再关联。
- `source_video_path` 必填，方便从未入库视频直接发现候选。
- `scenario_keys_json` 是场景记忆的关键字段。
- `promoted_media_id` 只有候选被晋升到主库后才有值。

### 4.2 新增 `candidate_vectors`

用途：保存候选 GIF 的 embedding 引用。第一版可以不单独建 FAISS，只把向量 JSON 存表，用于 centroid 计算。候选量变大后再加候选 FAISS。

建议 schema：

```sql
CREATE TABLE IF NOT EXISTS candidate_vectors (
    vector_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    vector_type TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    source_text TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidate_gifs(candidate_id)
);
```

`vector_type` 建议值：

```text
candidate_text
candidate_frame_text
candidate_clip_text
```

第一版使用 `candidate_text` 即可，来源为：

```text
summary + emotional_core + why_i_like_it + tags
```

索引：

```sql
CREATE INDEX IF NOT EXISTS idx_candidate_vectors_candidate ON candidate_vectors(candidate_id);
CREATE INDEX IF NOT EXISTS idx_candidate_vectors_type ON candidate_vectors(vector_type);
```

### 4.3 新增 `preference_events`

用途：保存用户反馈事件流。不要覆盖，不要聚合写回。所有后续画像都从这里重建。

建议 schema：

```sql
CREATE TABLE IF NOT EXISTS preference_events (
    event_id TEXT PRIMARY KEY,

    target_type TEXT NOT NULL
        CHECK(target_type IN ('media','candidate_gif')),
    target_id TEXT NOT NULL,

    rating TEXT NOT NULL
        CHECK(rating IN ('like','dislike','neutral')),

    reason TEXT,
    corrected_tags_json TEXT,
    scenario_keys_json TEXT,

    embedding_model TEXT,
    embedding_dim INTEGER,
    target_vector_json TEXT,

    score_snapshot_json TEXT,
    model_info_json TEXT,

    source TEXT DEFAULT 'review_ui'
        CHECK(source IN ('review_ui','api','import','script')),

    created_at TEXT NOT NULL
);
```

索引：

```sql
CREATE INDEX IF NOT EXISTS idx_preference_events_target ON preference_events(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_preference_events_rating ON preference_events(rating);
CREATE INDEX IF NOT EXISTS idx_preference_events_created ON preference_events(created_at);
```

说明：

- `target_type='candidate_gif'` 是主要路径。
- `target_type='media'` 兼容主库审核反馈。
- `score_snapshot_json` 保存反馈发生时的分数，便于后续分析“为什么当时推荐了它”。
- `target_vector_json` 是反馈时目标向量的快照。这样即使以后 embedding 模型变化，也能复盘旧偏好。

### 4.4 新增 `preference_profiles`

用途：保存从事件流聚合出的偏好画像。

建议 schema：

```sql
CREATE TABLE IF NOT EXISTS preference_profiles (
    profile_id TEXT PRIMARY KEY,

    scope TEXT NOT NULL
        CHECK(scope IN ('global','scenario')),
    scenario_key TEXT NOT NULL,

    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,

    liked_centroid_json TEXT,
    disliked_centroid_json TEXT,

    tag_weights_json TEXT,
    emotion_weights_json TEXT,
    scene_type_weights_json TEXT,

    sample_count_like INTEGER DEFAULT 0,
    sample_count_dislike INTEGER DEFAULT 0,
    sample_count_neutral INTEGER DEFAULT 0,

    confidence REAL DEFAULT 0,
    decay_config_json TEXT,

    source_event_count INTEGER DEFAULT 0,
    source_event_max_created_at TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

唯一约束建议：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_preference_profiles_scope_key_model
ON preference_profiles(scope, scenario_key, embedding_model);
```

`scenario_key` 约定：

```text
global
emotion:intimacy
emotion:tension
scene_type:close-up
scene_type:dialogue
tag:warm_lighting
tag:low_light
```

第一版必须有：

```text
global
emotion:<emotional_core>
scene_type:<scene_type>
tag:<top tag>
```

### 4.5 新增可选表 `candidate_review_queue`

第一版可以不建，直接通过 `candidate_gifs.status` 和 `final_score` 查询。如果 UI 需要更稳定的审核顺序，再建。

建议 schema：

```sql
CREATE TABLE IF NOT EXISTS candidate_review_queue (
    queue_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    priority REAL DEFAULT 0,
    reason TEXT,
    status TEXT DEFAULT 'pending'
        CHECK(status IN ('pending','shown','completed','skipped')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidate_gifs(candidate_id)
);
```

## 5. 场景 key 设计

长期偏好要同时支持稳定主审美和分场景记忆。场景 key 是分场景画像的边界。

### 5.1 生成规则

新增服务：

```text
app/services/scenario.py
```

建议函数：

```python
def build_scenario_keys(annotation: dict) -> list[str]:
    ...
```

输入来源：

```text
emotional_core
scene_type
tags_json
aesthetic_notes_json
```

输出示例：

```json
[
  "global",
  "emotion:intimacy",
  "scene_type:close-up",
  "tag:warm_lighting",
  "tag:soft_focus"
]
```

### 5.2 规则

1. 永远包含 `global`。
2. 有 `emotional_core` 时加入 `emotion:<value>`。
3. 有 `scene_type` 时加入 `scene_type:<value>`。
4. tags 最多取前 3 个，加入 `tag:<normalized_tag>`。
5. tag 归一化：
   - 小写。
   - 空格转 `_`。
   - 去除逗号、句号、斜杠等符号。
6. 不要从文件名、人物名直接生成场景 key。第一版先避免过拟合。

### 5.3 画像启用阈值

场景画像不能因为 1 个 like 就强影响排序。建议：

```text
min_like_samples = 3
min_total_samples = 5
```

不满足阈值时：

```text
只使用 global profile，不使用该 scenario profile。
```

## 6. 偏好画像构建算法

新增服务：

```text
app/services/preference_memory.py
```

建议公开函数：

```python
def record_preference_event(...):
    ...

def rebuild_preference_profiles(embedding_model: str | None = None) -> dict:
    ...

def get_applicable_profiles(scenario_keys: list[str]) -> list[dict]:
    ...

def score_with_preference_profiles(candidate_id: str) -> dict:
    ...
```

### 6.1 centroid 计算

对每个 profile：

```text
liked_centroid = mean(vectors of like events)
disliked_centroid = mean(vectors of dislike events)
```

所有向量在计算前 L2 normalize。

如果希望近期反馈有轻微更高权重，可以使用时间衰减，但第一版建议先不启用。稳定性优先。

未来可选：

```text
weight = exp(-age_days / half_life_days)
half_life_days = 180
```

### 6.2 tag 和 emotion 权重

从事件里聚合：

```text
like_count(tag) - dislike_count(tag) * 1.5
like_count(emotion) - dislike_count(emotion) * 1.5
```

归一化到 `[-1, 1]`。

建议计算：

```text
raw_weight = (likes - 1.5 * dislikes) / max(likes + dislikes, 1)
```

解释：

- dislike 权重略高，因为负样本更能防止“看起来相似但不想要”的误召回。
- neutral 不直接参与正负权重，但可以用于降低 confidence。

### 6.3 profile confidence

建议：

```text
confidence = min(1.0, (like_count + dislike_count) / 20)
```

低 confidence 的 scenario profile 只给很小权重，避免过拟合。

### 6.4 rebuild 策略

不要每次反馈都重建。建议：

```text
每次反馈 -> 只写 preference_events
手动触发或累计 N 条事件 -> rebuild preference_profiles
```

第一版实现：

```text
POST /api/preference/rebuild
uv run python scripts/preference_memory.py rebuild
```

后续可加自动触发：

```text
每 10 条新事件重建一次
```

## 7. 候选重排算法

新增服务：

```text
app/services/reranker.py
```

### 7.1 输入

候选记录：

```text
candidate_gifs
candidate_vectors
scenario_keys_json
```

基础 RAG：

```text
base_rag_score
```

偏好画像：

```text
global profile
matching scenario profiles
```

### 7.2 推荐评分公式

第一版：

```text
final_score =
  0.45 * base_rag_similarity
  0.20 * global_like_similarity
  0.15 * scenario_like_similarity
  0.15 * (1 - dislike_similarity)
  0.05 * diversity_bonus
```

定义：

```text
base_rag_similarity:
  候选与主收藏库 FAISS top-k 的最高或加权平均相似度

global_like_similarity:
  候选向量与 global liked_centroid 的 cosine similarity

scenario_like_similarity:
  候选向量与匹配 scenario liked_centroid 的加权平均 cosine similarity

dislike_similarity:
  max(global disliked similarity, scenario disliked similarity)

diversity_bonus:
  防止输出一堆几乎重复片段，第一版可先固定为 0
```

如果某个画像不存在：

```text
对应项记为 0
权重可以归一化，也可以保持为 0
```

推荐第一版保持为 0，这样基础 RAG 仍主导，符合“稳定个人审美”。

### 7.3 dislike 排斥项

dislike 不应该只是“不要加分”，而应该主动降权。

建议：

```text
if dislike_similarity >= 0.85:
    final_score *= 0.4
elif dislike_similarity >= 0.75:
    final_score *= 0.7
```

这样可以处理：

```text
候选与收藏库很像，但也和用户讨厌过的新样本很像
```

### 7.4 scenario profile 权重

匹配多个 scenario 时：

```text
scenario_like_similarity =
  weighted_average(profile_similarity * profile_confidence)
```

只使用 confidence >= 0.25 的 profile。

如果没有满足条件的 scenario profile：

```text
scenario_like_similarity = 0
```

## 8. 候选 GIF 生命周期

### 8.1 创建

新视频发现流程生成候选：

```text
status = candidate
export_status = not_exported 或 exported
base_rag_score = 初筛分数
final_score = 初始重排分数
```

创建时必须写：

```text
candidate_gifs
candidate_vectors
```

如果 GIF 文件已导出：

```text
exported_gif_path
export_status = exported
```

### 8.2 审核

用户在 UI 中看到候选，做：

```text
like
dislike
neutral
```

系统动作：

```text
1. 写 preference_events
2. 更新 candidate_gifs.status
3. 不写 media
4. 不更新主 FAISS
```

状态对应：

```text
like    -> candidate_gifs.status = liked
dislike -> candidate_gifs.status = disliked
neutral -> candidate_gifs.status = neutral
```

### 8.3 晋升

只有用户明确点击“加入收藏”或 API 调用 promote 时，才晋升。

晋升动作：

```text
1. 将 candidate_gif 的 exported_gif_path 或源视频片段作为新媒体写入 media
2. 写 frames 或复用候选帧
3. 写 annotations
4. 计算 media embedding
5. 写主 FAISS 和 vector_refs
6. candidate_gifs.status = promoted
7. candidate_gifs.promoted_media_id = 新 media_id
```

晋升前必须检查：

```text
quality_status = passed
exported_gif_path 存在
不是近重复
用户明确确认
```

近重复检查：

```text
1. sha256 完全重复
2. pHash 距离小于配置阈值
3. embedding similarity 大于 0.97
```

## 9. API 设计

### 9.1 候选查询

```text
GET /api/candidates/next
```

Query 参数：

```text
status=candidate
strategy=highest_score | random | uncertain
scenario_key=<optional>
```

返回：

```json
{
  "candidate": {
    "candidate_id": "...",
    "source_video_path": "...",
    "start": 12.3,
    "end": 15.8,
    "duration": 3.5,
    "exported_gif_path": "...",
    "caption": "...",
    "summary": "...",
    "emotional_core": "intimacy",
    "tags": ["warm_lighting"],
    "scene_type": "close-up",
    "scenario_keys": ["global", "emotion:intimacy"]
  },
  "score": {
    "base_rag_similarity": 0.72,
    "global_like_similarity": 0.66,
    "scenario_like_similarity": 0.61,
    "dislike_similarity": 0.12,
    "final_score": 0.69
  },
  "similar_base_items": [],
  "matching_profiles": []
}
```

### 9.2 候选详情

```text
GET /api/candidates/{candidate_id}
```

返回候选完整记录、反馈历史、相似主库样本、命中的 profile。

### 9.3 候选反馈

```text
POST /api/candidates/{candidate_id}/feedback
```

Body：

```json
{
  "rating": "like",
  "reason": "喜欢这个近景情绪和暖色光线",
  "corrected_tags": ["close-up", "warm_lighting"],
  "rebuild_profile": false
}
```

行为：

```text
1. 插入 preference_events
2. 更新 candidate_gifs.status
3. 返回当前 event_id
4. 如果 rebuild_profile=true，触发 rebuild
```

### 9.4 重建偏好画像

```text
POST /api/preference/rebuild
```

返回：

```json
{
  "status": "ok",
  "profiles_built": 12,
  "events_used": 57,
  "global_profile": {
    "sample_count_like": 31,
    "sample_count_dislike": 12,
    "confidence": 1.0
  }
}
```

### 9.5 查看画像

```text
GET /api/preference/profiles
```

Query：

```text
scope=global|scenario
scenario_key=<optional>
```

### 9.6 重排候选

```text
POST /api/candidates/rerank
```

Body：

```json
{
  "source_video_sha256": "...",
  "limit": 100,
  "write_scores": true
}
```

行为：

```text
重新计算候选 final_score，并写回 candidate_gifs。
```

### 9.7 晋升候选

```text
POST /api/candidates/{candidate_id}/promote
```

Body：

```json
{
  "confirm": true,
  "film": "optional",
  "tags": ["optional"]
}
```

行为：

```text
候选 GIF 正式加入主 media / annotations / FAISS。
```

## 10. UI 设计

当前 `app/ui/review.py` 已能审核主库媒体。v2 建议新增候选审核模式。

### 10.1 UI 模式

顶部增加模式切换：

```text
Library Review
Candidate Review
Preference Profiles
```

第一版可以只做 `Candidate Review`。

### 10.2 Candidate Review 显示内容

左侧：

```text
候选 GIF 预览
源视频路径
时间段 start/end/duration
```

右侧：

```text
summary
caption
emotional_core
scene_type
tags
scenario_keys
base_rag_score
global_like_similarity
scenario_like_similarity
dislike_penalty
final_score
```

操作按钮：

```text
Like
Neutral
Dislike
Promote to Library
Skip
Rebuild Profiles
```

提交反馈后：

```text
自动加载下一条 candidate
```

### 10.3 Profile 可解释性

每条候选需要展示为什么推荐：

```text
Top base matches:
  - media_id / score / tags / emotion

Matched preference profiles:
  - global confidence=...
  - emotion:intimacy confidence=...

Penalty:
  - disliked centroid similarity=...
```

这能帮助用户判断是 RAG 命中，还是近期反馈带来的变化。

## 11. CLI 设计

建议新增或扩展：

```text
scripts/preference_memory.py
```

命令：

```powershell
uv run python scripts/preference_memory.py rebuild
uv run python scripts/preference_memory.py status
uv run python scripts/preference_memory.py rerank --source-video-sha256 <hash>
uv run python scripts/preference_memory.py export-report
```

如果已有 `scripts/pipeline.py`，也可以合并：

```powershell
uv run python scripts/pipeline.py preference rebuild
uv run python scripts/pipeline.py candidates rerank --source-video-sha256 <hash>
uv run python scripts/pipeline.py candidates promote <candidate_id>
```

要求：

- 所有命令默认 dry-run 或打印即将修改的数量。
- 写操作需要明确参数，例如 `--apply`。
- 大批量 rerank 需要 `--limit`。

## 12. 服务模块拆分

建议新增：

```text
app/services/candidates.py
app/services/scenario.py
app/services/preference_memory.py
app/services/reranker.py
app/services/promotion.py
```

职责：

```text
candidates.py:
  创建候选、查询候选、更新状态、保存候选向量

scenario.py:
  从 annotation/tag/emotion 生成 scenario_keys

preference_memory.py:
  记录 feedback event、重建 profiles、读取 profiles

reranker.py:
  计算 final_score，写回 score_json

promotion.py:
  将 candidate 明确晋升到主 media 和主 FAISS
```

不要把这些逻辑塞进 `app/main.py`。`app/main.py` 只做 API 编排。

## 13. 与现有表的关系

### 13.1 `media`

保持主库含义：

```text
用户原始收藏 GIF
用户明确 promote 的候选 GIF
```

不要让普通候选自动进入 `media`。

### 13.2 `feedback`

保留现有表，继续用于主库媒体审核。

不要强行替换。新增 `preference_events` 用于统一偏好学习。

可选兼容策略：

```text
当 /api/feedback 写入主库 feedback 时，也同步写一条 preference_events(target_type='media')。
```

这样主库审核反馈也能参与画像。

### 13.3 `video_clips`

现有 `video_clips` 可保留。v2 有两种选择：

方案 A：继续使用 `video_clips` 表作为候选片段基础，再新增 `candidate_gifs` 保存审核和偏好字段。

方案 B：直接用 `candidate_gifs` 覆盖候选片段和导出 GIF 的完整生命周期。

推荐方案 B。理由是 `video_clips` 当前字段偏少，状态也偏导出流程，不足以承载偏好记忆。保留 `video_clips` 不动，新增 `candidate_gifs` 更安全。

## 14. 配置建议

在 `configs/models.yaml` 增加：

```yaml
preference_memory:
  enabled: true
  min_like_samples_for_scenario: 3
  min_total_samples_for_scenario: 5
  dislike_hard_penalty_threshold: 0.85
  dislike_soft_penalty_threshold: 0.75
  auto_rebuild_every_events: 10
  weights:
    base_rag_similarity: 0.45
    global_like_similarity: 0.20
    scenario_like_similarity: 0.15
    dislike_penalty: 0.15
    diversity_bonus: 0.05
```

注意：

- 第一版不要启用复杂时间衰减。
- 如果要加时间衰减，默认关闭。

## 15. 测试计划

新增测试：

```text
tests/test_scenario.py
tests/test_preference_memory.py
tests/test_candidate_feedback.py
tests/test_reranker.py
tests/test_candidate_promotion.py
```

### 15.1 `test_scenario.py`

覆盖：

- 永远包含 `global`。
- `emotional_core=intimacy` 生成 `emotion:intimacy`。
- `scene_type=close-up` 生成 `scene_type:close-up`。
- tag 归一化为空格转下划线。
- tag 数量最多 3 个。

### 15.2 `test_preference_memory.py`

覆盖：

- like/dislike/neutral 能写入 `preference_events`。
- rebuild 后生成 global profile。
- 样本不足时不生成 scenario profile 或 confidence 很低。
- liked centroid 和 disliked centroid 维度正确。

### 15.3 `test_candidate_feedback.py`

覆盖：

- candidate like 后状态变 `liked`。
- candidate dislike 后状态变 `disliked`。
- feedback 不写入主 `media`。
- feedback 事件包含 `score_snapshot_json`。

### 15.4 `test_reranker.py`

覆盖：

- 有 global liked profile 时相似候选加分。
- 有 matching scenario profile 时候选加分。
- dislike similarity 超过阈值时显著降权。
- 没有 profile 时只使用 base RAG score。

### 15.5 `test_candidate_promotion.py`

覆盖：

- 未确认 `confirm=true` 不晋升。
- 无 exported GIF 不晋升。
- promote 后写入 `media`。
- promote 后 `candidate_gifs.status=promoted`。
- promote 后 `promoted_media_id` 不为空。

## 16. 迁移实施顺序

### Phase 1: Schema 和基础服务

任务：

1. 在 `app/db.py:init_db()` 增加新表。
2. 在 `_migrate()` 中为旧库幂等创建索引。
3. 新增 `app/services/scenario.py`。
4. 新增 `app/services/candidates.py`。
5. 新增测试 `test_scenario.py`。

验收：

```powershell
uv run python -c "from app.db import init_db; init_db(); print('db ok')"
uv run pytest tests/test_scenario.py
```

### Phase 2: Preference events 和 profiles

任务：

1. 新增 `app/services/preference_memory.py`。
2. 实现 `record_preference_event()`。
3. 实现 `rebuild_preference_profiles()`。
4. 新增 API：
   - `POST /api/preference/rebuild`
   - `GET /api/preference/profiles`
5. 新增 CLI：
   - `scripts/preference_memory.py rebuild`
   - `scripts/preference_memory.py status`

验收：

```powershell
uv run pytest tests/test_preference_memory.py
```

### Phase 3: Candidate feedback API

任务：

1. 新增：
   - `GET /api/candidates/next`
   - `GET /api/candidates/{candidate_id}`
   - `POST /api/candidates/{candidate_id}/feedback`
2. feedback 写入 `preference_events`。
3. feedback 更新 `candidate_gifs.status`。
4. 不写入主 `media`。

验收：

```powershell
uv run pytest tests/test_candidate_feedback.py
```

### Phase 4: Reranker

任务：

1. 新增 `app/services/reranker.py`。
2. 实现评分公式。
3. 实现 dislike hard penalty。
4. 新增：
   - `POST /api/candidates/rerank`
5. 将 rerank 分数写回 `candidate_gifs.score_json` 和 `final_score`。

验收：

```powershell
uv run pytest tests/test_reranker.py
```

### Phase 5: Candidate UI

任务：

1. 扩展 `app/ui/review.py` 或新增 `app/ui/candidate_review.py`。
2. 展示候选 GIF、分数拆解、匹配 profile。
3. 支持 like/dislike/neutral。
4. 支持 promote。

验收：

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
uv run python app/ui/candidate_review.py
```

手动验证：

```text
加载候选 -> like -> preference_events 增加 -> candidate 状态变 liked
加载候选 -> dislike -> rerank 后相似候选降权
```

### Phase 6: Promotion

任务：

1. 新增 `app/services/promotion.py`。
2. 新增 `POST /api/candidates/{candidate_id}/promote`。
3. promote 写入 `media`、`annotations`、主 FAISS。
4. promote 前做重复检测。

验收：

```powershell
uv run pytest tests/test_candidate_promotion.py
```

## 17. 最小可用版本范围

如果时间有限，MVP 只做：

```text
candidate_gifs
candidate_vectors
preference_events
preference_profiles
scenario.py
preference_memory.py
reranker.py
candidate feedback API
preference rebuild API
```

暂缓：

```text
promotion.py
candidate_review_queue
candidate FAISS
自动 rebuild
时间衰减
训练 reranker
```

MVP 仍然可以完成核心闭环：

```text
新 GIF 入候选表 -> 用户反馈 -> 重建画像 -> 下一次 rerank 更准
```

## 18. 关键边界和风险

### 18.1 主库污染风险

风险：

```text
把所有 liked candidate 自动写入 media，会让主库越来越偏向近期视频。
```

处理：

```text
只有 promote 才写主库。
```

### 18.2 过拟合风险

风险：

```text
少量 feedback 就生成强 scenario profile，导致后续候选被单次偏好牵引。
```

处理：

```text
scenario profile 设置 min sample 和 confidence。
```

### 18.3 dislike 误伤风险

风险：

```text
用户 dislike 的可能是某个片段质量差，而不是讨厌这个风格。
```

处理：

```text
dislike penalty 只在高相似时强降权。
UI 允许填写 reason 和 corrected_tags。
后续可支持 dislike_reason_type。
```

未来可扩展：

```text
dislike_reason_type = bad_quality | wrong_style | duplicate | too_explicit | boring | other
```

第一版可以先不加，但 schema 的 `reason` 要保留。

### 18.4 embedding 模型变化风险

风险：

```text
preference_events 里的向量和当前 embedding 模型不一致。
```

处理：

```text
preference_events 和 preference_profiles 都保存 embedding_model 和 embedding_dim。
rebuild 时只使用当前模型匹配的事件，或重新计算候选向量。
```

## 19. 验收 SQL

候选不会自动进主库：

```sql
SELECT COUNT(*) FROM candidate_gifs;
SELECT COUNT(*) FROM media;
```

对候选做 like 后：

```sql
SELECT status FROM candidate_gifs WHERE candidate_id = ?;
SELECT rating FROM preference_events WHERE target_type='candidate_gif' AND target_id = ?;
```

重建画像后：

```sql
SELECT scope, scenario_key, sample_count_like, sample_count_dislike, confidence
FROM preference_profiles
ORDER BY scope, scenario_key;
```

promote 后：

```sql
SELECT status, promoted_media_id FROM candidate_gifs WHERE candidate_id = ?;
SELECT * FROM media WHERE media_id = ?;
```

## 20. 交付物清单

新增文件：

```text
app/services/candidates.py
app/services/scenario.py
app/services/preference_memory.py
app/services/reranker.py
app/services/promotion.py
scripts/preference_memory.py
tests/test_scenario.py
tests/test_preference_memory.py
tests/test_candidate_feedback.py
tests/test_reranker.py
tests/test_candidate_promotion.py
```

修改文件：

```text
app/db.py
app/main.py
app/ui/review.py 或新增 app/ui/candidate_review.py
configs/models.yaml
```

可选修改：

```text
README.md
使用手册.md
version1.md
```

## 21. 推荐提交拆分

```text
1. db: add candidate and preference memory tables
2. feat: record candidate feedback events
3. feat: rebuild global and scenario preference profiles
4. feat: rerank candidates with preference memory
5. feat: add candidate review API and UI
6. feat: promote selected candidates into library
```

每个提交都要能独立通过对应测试。

## 22. 给执行 Agent 的注意事项

1. 当前可能正在全量跑 RAG 入库。不要动 `data/library.db-wal`、`data/library.db-shm`、`data/vlm_loop.log`。
2. 不要重置主库。
3. 不要在全量任务运行时做破坏性 schema 重建。新增表和新增索引通常可以做，但要注意 SQLite 锁。
4. 如果数据库正在被长任务写入，优先先实现代码和测试，用临时测试库跑测试。
5. 所有 schema 迁移必须幂等。
6. 所有新增 API 必须不破坏现有 `/api/feedback` 和 `/api/review/next`。
7. candidate feedback 是新路径，不要强行复用旧 `feedback(media_id)` 表。

## 23. 一句话设计结论

主数据库是稳定收藏库，新视频 GIF 是候选样本；候选样本的 like/dislike 先进入偏好事件流，再汇总成全局和分场景偏好画像，用于下一次候选重排。只有用户明确 promote 的候选，才进入主 `media` 和主 FAISS。
