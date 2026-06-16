# GifAgent VLM 输出质量与 RAG 管线整改方案 v1

本文档给后续执行 Agent 使用。目标不是重写项目，而是把当前能跑的实验管线整改成可恢复、可验证、可持续迭代的偏好 RAG 系统。

当前仓库：`C:\Users\sunhao\Desktop\code\GifAgent`

当前分支：`master`

远端：`https://github.com/qwer1234-cloud/GifAgent.git`

## 1. 背景和问题判断

GifAgent 的目标是把个人喜欢的 GIF 导入 RAG，再用视觉模型从新视频中采样出符合个人偏好的片段。现在项目已经有基本链路：

```text
媒体扫描 -> SQLite 入库 -> GIF 抽帧 -> VLM 帧级描述 -> LLM 媒体级合成 -> Embedding -> FAISS -> 新视频采样/检索/导出
```

当前主要问题不是“链路不存在”，而是 VLM 输出质量污染了下游数据。被污染的字段会继续进入 LLM 合成、Embedding、FAISS 和候选排序，导致后续 RAG 越跑越偏。

本地数据库在 2026-06-16 的观察值：

```text
media:
  gif: 8114
  image: 111
  video: 996

frames:
  done: 28026
  pending: 93651

annotations: 1005
frame_annotations: 28805
vector_refs(media_global): 1005

templateish_frame_annotations: 25662
empty_captions: 395

representatives:
  gif is_representative=0: 105
  gif is_representative=1: 8009
```

`templateish_frame_annotations` 的判定包含以下模板残留：

- `caption = "what you see"`
- `aesthetic_notes_json` 包含 `"2-3 observations"`
- `why_i_like_it = "one reason"`
- `why_i_like_it` 以 `"why this works"` 开头

这说明 VLM 输出不能直接入库。必须先加质量闸门，再清理和重跑派生数据。

## 2. 总体整改原则

1. 不要重置整库。
   保留 `media`、`frames`、`feedback`。这些是原始文件、抽帧结果和人工偏好反馈。

2. 重建所有由 VLM 输出派生的数据。
   `frame_annotations`、`annotations`、`vector_refs`、`data/faiss/*` 都应在质量闸门上线后重建。

3. 先修质量闸门，再重跑 VLM。
   如果先清表重跑，而 prompt 和校验没改，污染会再次出现。

4. 实验脚本不能继续成为正式管线。
   `scripts/` 里硬编码路径、重复 JSON 解析、重复 Ollama 调度的逻辑需要收敛到 `app/services/` 和正式 CLI。

5. 每个阶段都要有验证命令。
   不要靠肉眼看日志判断成功。

## 3. 不应删除和可以重建的数据

### 3.1 保留

保留以下表和文件：

```text
media
frames
feedback
data/library.db 本身
data/frames/
data/thumbs/
```

理由：

- `media` 保存文件扫描、SHA256、pHash、路径、媒体类型。
- `frames` 保存已经抽好的 JPEG 帧，重抽成本高。
- `feedback` 是人工偏好数据，价值最高。
- `data/frames/` 可复用，不应因 VLM 质量问题删除。

### 3.2 重建

重建以下表和文件：

```text
frame_annotations
annotations
vector_refs
processing_checkpoint
data/faiss/media_index.faiss
data/faiss/id_map.json
data/faiss/manifest.json  # 整改后新增
```

理由：

- `frame_annotations` 是污染源。
- `annotations` 依赖污染的帧级输出。
- `vector_refs` 和 FAISS 索引依赖污染的媒体级标注。
- checkpoint 会引用旧阶段进度，质量闸门变更后应重新计算。

### 3.3 建议新增安全脚本

新增脚本：

```text
scripts/reset_derived_quality_data.py
```

要求：

- 默认 dry-run，不做删除。
- 必须显式传入 `--apply` 才执行。
- 执行前自动备份 `data/library.db` 到 `data/backups/library.before-quality-reset.<timestamp>.db`。
- 只清派生表，不删 `media`、`frames`、`feedback`。
- 清表后把 `frames.vlm_status` 重置为 `pending`。
- 删除 FAISS 文件时只删除 `data/faiss/` 下已知文件，不递归删除用户路径。

建议 SQL：

```sql
DELETE FROM vector_refs;
DELETE FROM annotations;
DELETE FROM frame_annotations;
DELETE FROM processing_checkpoint;
UPDATE frames SET vlm_status = 'pending';
```

建议 PowerShell 验证：

```powershell
uv run python scripts/reset_derived_quality_data.py --dry-run
uv run python scripts/reset_derived_quality_data.py --apply
```

验收标准：

```text
frame_annotations = 0
annotations = 0
vector_refs = 0
frames.pending = frames.total
feedback 数量不变
media 数量不变
```

## 4. 需要优先修复的代码点

### 4.1 LLM 重试路径缺少 `time` 导入

文件：

```text
app/services/llm.py
```

现象：

`synthesize_media_annotation()` 在异常重试路径调用 `time.sleep(5)`，但文件没有 `import time`。

位置：

```text
app/services/llm.py:114
```

整改：

- 添加 `import time`。
- 同时把异常日志保留到结构化错误字段，避免只 print。

验收：

- 用 fake Ollama 或 monkeypatch 模拟第一次请求失败、第二次成功。
- 测试应证明函数不会因为 `NameError: time is not defined` 中断。

### 4.2 GIF 抽帧配置算了但没用

文件：

```text
app/services/preprocess.py
```

现象：

`sample_count` 被计算：

```text
app/services/preprocess.py:22
app/services/preprocess.py:23
```

但实际 ffmpeg 使用固定：

```text
app/services/preprocess.py:31
```

```text
fps=2,scale=640:-1
```

这会导致长 GIF 抽出过多帧，直接放大 VLM 成本。

整改：

- 改成按 `media.gif_sample_frames` 和 `media.gif_max_sample_frames` 控制抽帧数量。
- 对 GIF 使用均匀采样，而不是固定 fps。
- 抽帧前清理同一 `media_id` 的旧帧文件，避免重复发现历史帧。
- 插入 `frames` 前确保同一 `media_id` 不重复插入同一批帧。

建议方案：

```text
1. 读取 GIF duration 和 frame_count。
2. 目标帧数 target = min(max_sample_frames, max(sample_frames, 可用帧数上限))。
3. 用 ffmpeg select 或 fps 表达式均匀抽 target 张。
4. 若 ffmpeg 失败，保存 stderr 到错误信息。
```

验收：

- 配置 `gif_sample_frames=8`、`gif_max_sample_frames=12`。
- 对一个 100 帧 GIF 抽帧数量不超过 12。
- 对一个 5 帧 GIF 不报错，抽出的帧数不超过原始帧数。

### 4.3 调度器失败帧不落库

文件：

```text
app/services/scheduler.py
```

现象：

API 管线 `process_pending_frames()` 捕获 VLM 异常时只把失败放入内存 `vlm_results`，没有把 `frames.vlm_status` 更新为 `failed`。如果服务中断，失败状态丢失。

位置：

```text
app/services/scheduler.py:91
app/services/scheduler.py:106
```

整改：

- 在 VLM 开始前把帧状态更新为 `vlm_processing`。
- 成功后写 `frame_annotations` 并更新 `done`。
- 失败后更新 `failed`，记录 `error_message` 和 `attempt_count`。
- LLM 合成阶段失败时，不应把已经成功的帧重跑；应记录媒体级合成失败状态。

建议 schema 扩展：

```sql
ALTER TABLE frames ADD COLUMN vlm_attempts INTEGER DEFAULT 0;
ALTER TABLE frames ADD COLUMN vlm_error TEXT;
ALTER TABLE frame_annotations ADD COLUMN quality_status TEXT DEFAULT 'unchecked';
ALTER TABLE frame_annotations ADD COLUMN quality_errors_json TEXT;
ALTER TABLE annotations ADD COLUMN quality_status TEXT DEFAULT 'unchecked';
ALTER TABLE annotations ADD COLUMN quality_errors_json TEXT;
```

注意：

SQLite 迁移要写成幂等逻辑。先查 `PRAGMA table_info(table)`，缺列才 `ALTER TABLE`。

验收：

- 模拟一帧 VLM 请求失败三次，该帧最终为 `failed`。
- 模拟服务中断后重启，`done` 的帧不重跑，`pending` 的帧继续跑。

### 4.4 FAISS 索引缺 manifest 和原子写入

文件：

```text
app/services/indexer.py
```

现象：

- `MediaIndex.__init__(dim=768)` 写死维度。
- `idx.add()` 先 `faiss.write_index()`，再写 `id_map.json`，再写 SQLite `vector_refs`。
- 中途失败可能导致 FAISS、id_map、SQLite 三者不一致。

位置：

```text
app/services/indexer.py:33
app/services/indexer.py:60
app/services/indexer.py:62
app/services/indexer.py:70
```

整改：

- 新增 `data/faiss/manifest.json`。
- manifest 至少包含：

```json
{
  "index_name": "media_index",
  "embedding_model": "nomic-embed-text:latest",
  "dim": 768,
  "metric": "cosine",
  "created_at": "...",
  "vector_count": 0,
  "schema_version": 1
}
```

- 首次 embedding 后用实际向量长度初始化索引。
- 加载索引时校验 manifest 里的模型和维度。
- 写文件使用临时文件，再 rename：

```text
media_index.faiss.tmp -> media_index.faiss
id_map.json.tmp -> id_map.json
manifest.json.tmp -> manifest.json
```

- 新增 verify 命令，检查：

```text
FAISS ntotal == len(id_map) == SELECT COUNT(*) FROM vector_refs WHERE index_name='media_index'
```

验收：

- 删除 FAISS 文件后可完整 rebuild。
- 手动破坏 `id_map.json` 后 verify 命令能报错。
- embedding 模型改名后不会静默复用旧索引。

### 4.5 Review UI 未形成反馈闭环

文件：

```text
app/ui/review.py
app/main.py
```

现象：

`load_next_for_review()` 只请求 `/api/status`，没有查询下一条待审核媒体，也没有展示相似 GIF。

位置：

```text
app/ui/review.py:9
app/ui/review.py:66
```

整改：

- 新增 API：

```text
GET /api/review/next
GET /api/review/{media_id}
POST /api/feedback
```

- `/api/review/next` 返回：

```json
{
  "media_id": "...",
  "preview_path": "...",
  "summary": "...",
  "emotional_core": "...",
  "aesthetic_notes": [],
  "why_i_like_it": "...",
  "tags": [],
  "score": {},
  "similar": []
}
```

- UI 应展示 preview、标注、相似样本、评分、反馈输入。
- 用户点击 like/dislike/neutral 后自动加载下一条。

验收：

- 启动 FastAPI 和 Gradio 后，点击 Next 能看到真实媒体。
- 提交反馈后 `feedback` 表新增记录。
- 同一媒体已反馈后不再作为默认 next 候选返回。

## 5. VLM 输出质量整改设计

### 5.1 新增统一 schema

新增文件：

```text
app/services/schemas.py
```

定义以下 Pydantic 模型：

```python
class FrameAnalysis(BaseModel):
    caption: str
    emotional_core: Literal[
        "tension", "melancholy", "awe", "joy", "sadness", "catharsis",
        "serenity", "excitement", "dread", "nostalgia", "admiration",
        "intimacy", "vulnerability", "longing", "desire", "other"
    ]
    aesthetic_notes: list[str]
    why_i_like_it: str

class ClipScore(FrameAnalysis):
    gif_worthiness: float
    reason: str

class MediaAnnotation(BaseModel):
    summary: str
    emotional_core: str
    aesthetic_notes: list[str]
    why_i_like_it: str
    tags: list[str]
    scene_type: str | None = None
```

字段规则：

- `caption` 不能为空。
- `caption` 最少 8 个字符。
- `aesthetic_notes` 数量为 2 到 4。
- 每条 note 最少 8 个字符。
- `why_i_like_it` 最少 12 个字符。
- `gif_worthiness` 范围为 0.0 到 1.0。
- 所有字符串需要去除首尾空白。

### 5.2 新增占位词检测

新增文件：

```text
app/services/quality.py
```

建议函数：

```python
def detect_placeholder_text(value: str) -> list[str]:
    ...

def validate_frame_analysis(payload: dict) -> tuple[FrameAnalysis | None, list[str]]:
    ...

def validate_media_annotation(payload: dict) -> tuple[MediaAnnotation | None, list[str]]:
    ...
```

占位词黑名单至少包含：

```text
what you see
one word
one reason
2-3 observations
2-4 qualities
3-5 keywords
why this works as a gif
describe what you actually see
concise description
what you actually observe
```

检测应大小写不敏感。

如果任意关键字段命中占位词：

- 不写入 `done`。
- 记录为质量失败。
- 可重试最多 2 次。
- 重试 prompt 必须带上具体失败原因。

### 5.3 新增统一 JSON 解析器

新增文件：

```text
app/services/json_guard.py
```

替换以下重复函数：

```text
app/services/vision.py:_parse_json_response
app/services/llm.py:_parse_json_response
scripts/*:parse_json / parse_json_response
```

要求：

- 去除 Markdown fence。
- 去除 `<think>...</think>`。
- 支持从响应中提取第一个完整 JSON object。
- 对解析失败返回结构化错误，不只截断字符串。
- 不吞掉异常原因。

建议返回类型：

```python
@dataclass
class JsonParseResult:
    ok: bool
    data: dict | None
    raw: str
    error: str | None
```

验收：

- 单测覆盖：
  - 纯 JSON。
  - ```json fenced JSON。
  - `<think>...</think>{...}`。
  - 前后有自然语言的 JSON。
  - 非法 JSON。

### 5.4 修改 prompt，禁止模板值进入示例

当前多个脚本把占位 JSON 直接放在 prompt 里，例如：

```text
scripts/vlm_loop.py:33
scripts/vlm_quick_200.py:22
scripts/vlm_continuous_inproc.py:17
scripts/rag_100_batch.py:48
scripts/test_video_adaptive.py:74
```

整改方式：

- 不要用 `"caption": "what you see"` 作为示例值。
- 改用字段说明，不提供可复制的占位短语。
- 如果必须给例子，例子要足够具体，并明确“不要照抄示例”。

建议 prompt 结构：

```text
Return one JSON object only.

Fields:
- caption: 18-40 words describing the visible subjects, composition, lighting, and action in this exact frame.
- emotional_core: one lowercase value from the allowed list.
- aesthetic_notes: 2-4 concrete visual observations from this exact image.
- why_i_like_it: one sentence explaining why this exact frame may match a saved GIF preference.

Do not use generic placeholders. Do not write "what you see", "one reason", "2-3 observations", or any wording from this instruction as field values.
```

### 5.5 入库策略

`analyze_frame()` 不应把未通过质量检查的数据作为正常标注写入。

建议状态：

```text
pending
vlm_processing
quality_failed
done
failed
```

如果不想改 CHECK 约束，可短期用现有状态：

- 质量失败但可重试：`pending`，并增加 `vlm_attempts`。
- 达到重试上限：`failed`，`vlm_error` 写入质量错误。

推荐长期修改 CHECK 约束，加入 `quality_failed`。SQLite 修改 CHECK 约束需要重建表，工作量较大。第一版可以先不改 CHECK，只新增错误字段。

## 6. RAG 和采样整改

### 6.1 从 media 级索引升级到多索引

当前 `compute_media_embedding()` 优先使用媒体级文本摘要：

```text
app/services/embedding.py:77
```

但 `score_media()` 把最近邻分数称为 `visual_similarity`：

```text
app/services/scorer.py:54
```

这会混淆文本相似和视觉相似。

整改：

新增向量类型：

```text
media_global_text
frame_caption_text
clip_candidate_text
```

可后续扩展：

```text
frame_visual
clip_visual
```

第一版先做文本多索引即可。原因是 Ollama 当前使用 `nomic-embed-text`，直接视觉 embedding 并不稳定。先把名字、类型和检索语义理清。

### 6.2 RAG 检索结果要带可解释上下文

当前新视频脚本只把相似 GIF 的 emotion 和 tags 注入。建议 RAG context 包含：

```text
media_id
score
summary
emotional_core
tags
why_i_like_it
source_file basename
```

不要把完整路径发给模型。

### 6.3 候选排序不要只按 aesthetic_notes 数量

`scripts/test_video_rag_v2.py` 当前按 `len(aesthetic_notes)` 排序导出候选。这很容易奖励模型废话。

建议候选分数：

```text
final_score =
  0.35 * gif_worthiness
  0.25 * rag_similarity
  0.15 * emotional_match
  0.15 * caption_quality
  0.10 * motion_or_boundary_confidence
```

第一版可先实现：

- `gif_worthiness`
- `rag_similarity`
- `caption_quality`
- dark-frame filter
- duplicate caption filter

## 7. 实验脚本收敛计划

### 7.1 保留但降级的脚本

以下脚本可以保留为历史实验，但不应作为主入口：

```text
scripts/test_jur639.py
scripts/test_video_rag.py
scripts/test_video_rag_v2.py
scripts/test_video_adaptive.py
scripts/rag_synth_recover.py
scripts/export_gifs.py
scripts/vlm_quick_200.py
scripts/vlm_continuous.py
scripts/vlm_continuous_inproc.py
```

给这些脚本顶部加注释：

```python
# Deprecated experiment script. Prefer scripts/pipeline.py.
```

### 7.2 新增正式 CLI

新增：

```text
scripts/pipeline.py
```

建议命令：

```powershell
uv run python scripts/pipeline.py scan
uv run python scripts/pipeline.py preprocess
uv run python scripts/pipeline.py annotate-frames --limit 200
uv run python scripts/pipeline.py synthesize-media --limit 100
uv run python scripts/pipeline.py build-index
uv run python scripts/pipeline.py verify-index
uv run python scripts/pipeline.py reset-derived --dry-run
uv run python scripts/pipeline.py discover-video --video "C:/path/to/video.mp4" --output data/exports/run1
```

CLI 必须从 `configs/models.yaml` 和命令行参数读取路径，不允许写死：

```text
C:/Users/sunhao/Desktop/ToWatch/JUR-639.mp4
E:/data/originals
```

`E:/data/originals` 可以作为默认配置存在，但脚本内部不要硬编码。

## 8. 数据库迁移计划

### 8.1 短期迁移

在 `app/db.py:_migrate()` 中新增幂等迁移：

```text
frames.vlm_attempts INTEGER DEFAULT 0
frames.vlm_error TEXT
frame_annotations.quality_status TEXT DEFAULT 'unchecked'
frame_annotations.quality_errors_json TEXT
annotations.quality_status TEXT DEFAULT 'unchecked'
annotations.quality_errors_json TEXT
vector_refs.embedding_model TEXT
vector_refs.embedding_dim INTEGER
vector_refs.source_hash TEXT
```

### 8.2 唯一性约束

当前表结构没有防止同一帧重复插入多条 VLM 标注。SQLite 对已有表添加 UNIQUE 比较麻烦。第一版建议先加逻辑防重：

```sql
SELECT annotation_id FROM frame_annotations WHERE frame_id=? AND model_name=?
```

存在则更新或跳过，不再插入新行。

长期建议重建表并加入：

```sql
UNIQUE(frame_id, model_name)
UNIQUE(media_id, model_name)
```

## 9. 测试计划

新增 dev 依赖：

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "respx>=0.21",
]
```

如果暂时不调整 uv dependency group，也可以放到：

```toml
[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "respx>=0.21",
]
```

新增测试目录：

```text
tests/
  test_json_guard.py
  test_quality.py
  test_preprocess_sampling.py
  test_indexer_manifest.py
  test_scheduler_status.py
  test_reset_derived_quality_data.py
```

### 9.1 必测项

`test_json_guard.py`

- fenced JSON 能解析。
- Qwen think tag 能剥离。
- 非法 JSON 返回错误而不是抛出未处理异常。

`test_quality.py`

- `"what you see"` 被拒绝。
- `"2-3 observations"` 被拒绝。
- 合法 caption 和 notes 通过。
- emotion 多值 `"joy|sadness"` 被拒绝或归一成单值，但不能静默接受原始值。

`test_preprocess_sampling.py`

- `gif_max_sample_frames=12` 时不抽出超过 12 帧。
- ffmpeg 失败时返回失败状态。

`test_indexer_manifest.py`

- manifest 维度和模型不匹配时报错。
- `verify-index` 能发现 id_map 和 SQLite 数量不一致。

`test_scheduler_status.py`

- VLM 请求失败时帧状态进入 `failed`，错误信息落库。
- VLM 质量失败时不会写入正常 `done` 标注。

`test_reset_derived_quality_data.py`

- dry-run 不改库。
- apply 后只清派生表。
- `feedback` 不变。

## 10. 执行顺序

### Phase 0: 加测试骨架和安全脚本

目标：

- 项目能运行 `pytest`。
- 有 dry-run reset 脚本。
- 不改变现有数据。

任务：

1. 更新 `pyproject.toml` 添加 dev 依赖。
2. 新建 `tests/`。
3. 新建 `scripts/reset_derived_quality_data.py`。
4. 为 reset 脚本写测试。

验收：

```powershell
uv run pytest tests/test_reset_derived_quality_data.py
uv run python scripts/reset_derived_quality_data.py --dry-run
```

### Phase 1: JSON 和质量闸门

目标：

- 所有 VLM/LLM 输出必须经过统一解析和质量检查。

任务：

1. 新建 `app/services/json_guard.py`。
2. 新建 `app/services/schemas.py`。
3. 新建 `app/services/quality.py`。
4. 改 `app/services/vision.py` 使用质量闸门。
5. 改 `app/services/llm.py` 使用质量闸门。
6. 修复 `app/services/llm.py` 缺 `import time`。

验收：

```powershell
uv run pytest tests/test_json_guard.py tests/test_quality.py
```

### Phase 2: 抽帧和调度状态机

目标：

- 抽帧数量受配置控制。
- 调度中断后可恢复。
- 失败状态落库。

任务：

1. 修 `app/services/preprocess.py` 的 `sample_count` 未使用问题。
2. 扩展 `app/db.py:_migrate()`。
3. 改 `app/services/scheduler.py` 状态更新。
4. 增加 CLI limit 参数，避免误跑全量。

验收：

```powershell
uv run pytest tests/test_preprocess_sampling.py tests/test_scheduler_status.py
```

### Phase 3: FAISS manifest 和 verify

目标：

- 索引可验证，可重建，不静默复用错误模型。

任务：

1. 改 `app/services/indexer.py`。
2. 增加 `manifest.json`。
3. 增加 `verify_index()`。
4. 在 CLI 暴露 `build-index` 和 `verify-index`。

验收：

```powershell
uv run pytest tests/test_indexer_manifest.py
uv run python scripts/pipeline.py verify-index
```

### Phase 4: Review UI 闭环

目标：

- 人工反馈能直接进入下一轮排序和审核。

任务：

1. 在 `app/main.py` 新增 review API。
2. 改 `app/ui/review.py` 加载真实候选。
3. 提交反馈后自动加载下一条。
4. 已反馈媒体默认排除。

验收：

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
uv run python app/ui/review.py
```

手动验证：

- 点击 Next 有媒体。
- Like/Neutral/Dislike 写入 `feedback`。
- 刷新后状态统计正常。

### Phase 5: 清理派生数据并小批量重跑

目标：

- 使用新质量闸门重建少量干净数据。

执行前：

```powershell
uv run python scripts/reset_derived_quality_data.py --dry-run
```

确认无误后：

```powershell
uv run python scripts/reset_derived_quality_data.py --apply
```

小批量重跑：

```powershell
uv run python scripts/pipeline.py annotate-frames --limit 100
uv run python scripts/pipeline.py synthesize-media --limit 20
uv run python scripts/pipeline.py build-index --limit 20
uv run python scripts/pipeline.py verify-index
```

质量验收 SQL：

```sql
SELECT COUNT(*) FROM frame_annotations
WHERE lower(caption) IN ('what you see', 'one reason')
   OR aesthetic_notes_json LIKE '%2-3 observations%';
```

期望结果：

```text
0
```

## 11. 交付物清单

必须交付：

```text
app/services/json_guard.py
app/services/schemas.py
app/services/quality.py
scripts/reset_derived_quality_data.py
scripts/pipeline.py
tests/test_json_guard.py
tests/test_quality.py
tests/test_reset_derived_quality_data.py
```

必须修改：

```text
app/services/vision.py
app/services/llm.py
app/services/preprocess.py
app/services/scheduler.py
app/services/indexer.py
app/services/embedding.py
app/services/scorer.py
app/db.py
app/main.py
app/ui/review.py
pyproject.toml
```

可选修改：

```text
README.md
使用手册.md
scripts/test_*.py
```

## 12. 明确不要做的事

不要做：

- 不要删除 `media` 表。
- 不要删除 `frames` 表。
- 不要删除 `feedback` 表。
- 不要递归删除整个 `data/`。
- 不要在没有 `--limit` 的情况下直接全量跑 VLM。
- 不要继续把 `"what you see"`、`"one reason"` 这种占位值写入 prompt 示例。
- 不要把 `JUR-639.mp4` 或本机绝对路径写死在正式管线里。
- 不要把旧 FAISS 索引和新 embedding 模型混用。
- 不要用 `git reset --hard` 或 revert 用户已有改动。

## 13. 后续 Agent 开始前检查

执行 Agent 开始前先运行：

```powershell
git status --short --branch
Test-Path data/library.db
uv run python -c "from app.db import init_db; init_db(); print('db ok')"
```

如果工作区已有用户改动，必须保留并绕开，不要回滚。

当前已知未提交改动：

```text
app/services/preprocess.py
data/adaptive_test_result.json
data/vlm_loop.log
version1.md
```

其中 `version1.md` 是本整改文档。`app/services/preprocess.py` 的现有改动是把 `subprocess.run(..., text=True)` 改为 `encoding="utf-8", errors="replace"`，用于避免 Windows GBK 输出问题。后续修改 `preprocess.py` 时应保留这个意图。

## 14. 最终验收标准

完成后应满足：

1. `pytest` 通过。
2. 小批量 VLM 重跑后，占位输出计数为 0。
3. `frames` 的失败状态和错误原因可查询。
4. `annotations` 和 `frame_annotations` 不再静默接受 parse error。
5. FAISS `verify-index` 通过。
6. Review UI 能加载真实候选并提交反馈。
7. 新视频发现脚本不依赖硬编码视频路径。
8. 所有正式入口支持 `--limit`，避免误触发 100 小时级全量任务。

## 15. 推荐提交顺序

建议拆成 5 个提交，便于 review：

```text
1. test: add quality gate and reset safety tests
2. fix: validate VLM and LLM structured outputs
3. fix: make frame processing resumable and bounded
4. fix: add FAISS manifest and index verification
5. feat: wire review API and feedback UI
```

每个提交都应能独立通过相关测试。
