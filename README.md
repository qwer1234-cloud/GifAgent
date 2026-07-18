# GifAgent

> 本地影视 GIF 自动标注与偏好挖掘 Agent —— 基于 Ollama + FAISS + RAG 的智能片段发现系统

## 概述

GifAgent 是一个运行在本地的影视片段智能管理工具。它自动扫描 GIF/视频文件，调用本地 VLM 分析每一帧的审美特征和情感表达，通过质量门禁过滤低质量输出，建立 FAISS 向量索引支持相似检索，并在新视频中自动发现符合偏好的经典片段。

### 核心能力

- **自动打标**：VLM（llava:13b）逐帧分析审美特征 → LLM（DeepSeek-V4-Flash）综合生成标签，所有输出经过质量门禁校验
- **质量门禁**：统一的 JSON 解析器 + placeholder 检测 + Pydantic 模型校验，placeholder 率从 89% 降至 <1%
- **向量索引**：基于 nomic-embed-text 的 FAISS 语义向量库，支持文本到 GIF 的跨模态检索（8109 向量 / 9221 媒体）
- **RAG 增强**：两阶段自适应 GIF 提取，per-frame VLM 评分 + 时域 clip 合并 + FAISS 相似 GIF 检索
- **智能采样**：15s 间隔粗采样 → 高分区域 10s 细采样，merge_gap=6s 自动合并相邻高分帧
- **断点恢复**：VLM 处理循环自动恢复、per-frame DB commit、每 50 batch 模型重启防降速
- **Preference Memory**：候选 GIF 物化 → 人工反馈收集 → 偏好画像构建 → 重排序，含 holdout 评估门禁
- **批量处理**：视频目录批量处理 + checkpoint 断点续跑，支持 200+ 视频无人值守处理
- **视频去重**：基于时长 + 关键帧 pHash 的内容指纹，即使文件名不同也能识别重复视频（抗重编码/换容器）
- **9 宫格缩略图**：每个视频自动选 9 张评分最高且视觉不重复的帧，生成 3x3 网格缩略图 + 9 张独立帧图

### 数据流

```
E:\data\originals\（8000+ GIF）
  → SHA256 + pHash 去重 → SQLite 入库
  → ffmpeg GIF 抽帧（6-12 帧/张）
  → llava:13b 逐帧审美分析（~4.7s/帧）
  → json_guard 统一解析 → quality 门禁校验
  → DeepSeek-V4-Flash 综合标注（tags + emotional_core + aesthetic_notes）
  → nomic-embed-text 向量化 → FAISS 索引
  → 新视频：I-frame → VLM 裸分析 → 每帧 caption FAISS 检索 → RAG 增强合成
  → 导出候选 GIF（ffmpeg palette 二段式）
```

---

## 快速开始

### 环境要求

| 组件 | 要求 | 说明 |
|------|------|------|
| Python | 3.11+ | uv 自动管理 |
| uv | 最新版 | `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| ffmpeg | 任意版本 | PATH 中可用 |
| Ollama | 最新版 | WSL 或 Windows，监听 localhost:11434 |
| GPU | 16GB+ VRAM | 本地运行 llava:13b；LLM 文本合成走云端 DeepSeek-V4-Flash |

### 必需模型

```bash
ollama pull llava:13b                                    # VLM 视觉分析
ollama pull nomic-embed-text:latest                      # Embedding 向量化
```

### 安装

```bash
git clone https://github.com/qwer1234-cloud/GifAgent.git
cd GifAgent

# 一键安装（自动下载 Python 3.11+，创建隔离 venv，安装依赖）
uv sync

# 验证
uv run python -c "from app.main import app; print('OK')"
```

### 配置

编辑 `configs/models.yaml`：

```yaml
media:
  source_dir: "E:/data/originals"     # GIF/视频源目录

vlm:
  provider: "ollama"
  model: "llava:13b"                  # 视觉模型
  base_url: "http://127.0.0.1:11434"
  manage_lifecycle: true              # 启动/停止模型（true/false）
  launch_mode: "wsl"                  # none（不管理）| native（ollama）| wsl（wsl ollama）
  # 注意：launch_mode 不根据 URL 推断，必须显式设置。

llm:
  provider: "anthropic_compatible"
  model: "DeepSeek-V4-Flash"          # 云端文本模型
  api_key_env: "ANTHROPIC_API_KEY"
  base_url: ""                        # 默认使用 ANTHROPIC_BASE_URL 或 Anthropic-compatible /v1/messages
  temperature: 0.3
  max_tokens: 2048

embedding:
  provider: "ollama"
  text_model: "nomic-embed-text:latest"

preference_memory:
  enabled: false                      # 偏好记忆功能开关

database:
  path: "data/library.db"

paths:
  faiss_dir: "data/faiss"
  frames_dir: "data/frames"
  exports_dir: "data/exports"
```

---

## 使用流程

### 第一步：媒体入库 + 去重

```bash
# 扫描源目录，SHA256/pHash 自动去重
curl -X POST http://127.0.0.1:8000/api/scan
```

或命令行：
```bash
uv run python -c "
from app.db import init_db; init_db()
from app.services.scanner import scan_and_register
stats = scan_and_register('E:/data/originals')
print(stats)
"
```

### 第二步：构建 RAG 向量库

```bash
# Phase 1: pHash 聚类 + 代表帧选取
uv run python scripts/cluster_and_select.py

# Phase 2: VLM + LLM 标注（最长耗时，支持断点恢复）
uv run python scripts/process_representatives.py

# 或使用稳定的 VLM 处理循环（自动恢复 + 模型重启）
uv run python scripts/vlm_loop.py

# 查看进度
uv run python scripts/process_representatives.py --status

# Phase 3: 簇内标签继承 + FAISS 索引构建
uv run python scripts/inherit_and_index.py
```

### 第三步：自适应 GIF 提取（RAG 增强）

```bash
# 单视频处理：15s 粗采样 → VLM 评分 → 10s 细采样 → clip 合并
uv run python scripts/test_video_adaptive.py --video <path/to/video.mp4>

# 批量处理（带 checkpoint 断点续跑）
uv run python scripts/test_video_batch.py --dir "C:/path/to/videos" --limit 5

# 恢复中断的批量任务（自动跳过已完成视频）
uv run python scripts/test_video_batch.py --dir "C:/path/to/videos"
```

输出：
- `data/exports/adaptive_test/{video_name}/`：每个视频的候选 GIF 片段（merge_gap=6s 合并）
- `data/exports/adaptive_test/{video_name}/Sample/`：9 宫格缩略图（`{video_name}_grid.jpg`）+ 9 张独立帧图（`{video_name}_sample_*.jpg`）
- GIF 命名格式：`{video_name}@@@{seq}_{start_ms}ms-{end_ms}ms.gif`（旧的秒格式仅用于兼容读取）
- `data/batch_checkpoint.json`：批量处理断点文件（含视频指纹，用于跨文件名去重）

**视频去重**：批量处理时自动计算每个视频的内容指纹（时长 + 5 个关键帧 pHash）。如果新视频与已处理视频指纹匹配（Hamming distance ≤ 5），自动跳过并记录 `duplicate_of`。抗重编码、换容器、改名，不抗裁剪/水印。

### 第四步：Stage 2 流水线（LLM 合成 + FAISS 重建）

VLM 处理完成后自动触发，也可手动运行：

```bash
# LLM 综合标注 + FAISS 索引重建
uv run python scripts/pipeline_stage2.py

# 查看日志
tail -f data/pipeline_stage2.log
```

### 第五步：候选 GIF 导入

```bash
# 将导出的候选 GIF 导入 preference memory 系统
uv run python scripts/import_adaptive_candidates.py

# 指定导出目录
uv run python scripts/import_adaptive_candidates.py --export-dir data/exports/adaptive_test
```

### 第六步：质量数据重置

```bash
# 预览（安全，不修改任何数据）
uv run python scripts/reset_derived_quality_data.py --dry-run

# 执行重置（自动备份数据库到 data/backups/）
uv run python scripts/reset_derived_quality_data.py --apply
```

### 启动服务

```bash
# FastAPI 服务
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# Gradio 审核界面（port 7860，原始 GIF 审核）
uv run python app/ui/review.py

# Gradio 候选审核界面（port 7861，候选 GIF 评分 + 批量控制面板）
uv run python app/ui/candidate_review.py
```

### Candidate review performance fix (2026-07-04)

- `GET /api/candidates` now supports server-side pagination and filtering:
  `status`, `limit`, `offset`, and optional exact `folder`. The default status
  is `candidate`.
- `GET /api/candidates/folders` discovers recursive candidate folders below a
  selected root directory and returns per-folder counts/status counts. It also
  includes folders that contain `.gif` files not yet materialized into
  `candidate_gifs`; these are shown as new candidates.
- The Review tab no longer loads candidates at page open. Choose a data root,
  click `Load Folders`, then choose the specific folder to review from the
  recursive folder list.
- When a folder contains GIF files but no `candidate_gifs` rows yet, selecting
  that exact folder materializes only that folder's direct GIF files on demand.
- The candidate review UI loads only one small page at a time (`PAGE_SIZE=12`)
  instead of pulling every candidate GIF on each refresh.
- Gallery cells use cached static thumbnails in `data/thumbs/candidates/`.
  The full animated GIF is loaded only for the currently selected candidate.
- Selection is bound to the current page state, so `Like` / `Dislike` is sent
  to the exact candidate the user clicked, even after filtering or paging.
- Candidate display and feedback now validate that the GIF file still exists at
  the original `artifact_path`; moved or missing files return a path-integrity
  error instead of silently reviewing stale data.
- The Windows GUI bundle was rebuilt with:
  `uv run pyinstaller --noconfirm build_exe.spec`.
  Output: `dist/GifAgentUI/GifAgentUI.exe`.

For an in-place release, use `bash scripts/rebuild_exe.sh`. The script preserves
both `dist/GifAgentUI/data/` and the writable
`dist/GifAgentUI/configs/models.yaml`, so rebuilding does not reset the task
history, GIF exports, labels, Preference Memory, databases, or settings edited
through the UI. Do not replace only `GifAgentUI.exe`; the matching `_internal/`
runtime must be released with it.

### Adaptive duplicate reduction tuning (2026-07-05)

- Adaptive export now clears generated artifacts in the target video output
  folder before reprocessing, preventing stale GIFs from earlier runs from
  mixing with the new run.
- The default adaptive config is stricter: `worthiness_threshold=0.50`,
  `refine_threshold=0.65`, `output_ratio=0.45`, `max_output=40`.
- Embedding dedup is enabled at `embedding_dedup_threshold=0.90`, then a
  temporal dedup pass keeps only the highest-scored clip within a 12s peak-time
  window.
- Result JSON records both `embedding_deduped_clips` and final `deduped_clips`
  so each run shows how much was removed.

### PotPlayer bookmark export (2026-07-08)

- Adaptive GIF export now writes `{video_name}.pbf` beside the generated GIFs
  when `adaptive.potplayer_pbf_enabled=true`.
- The `.pbf` file contains one bookmark per successfully exported GIF, using
  the same screenshot interval used for GIF generation. Bookmark titles include
  rank, interval, score, merge type, and caption summary.
- Reprocessing the same video clears the previous generated `.pbf` together
  with old GIFs and palette files.

---

## API 参考

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 总览统计（media/frames/annotations/vectors） |
| POST | `/api/scan` | 扫描源目录，注册新文件 |
| POST | `/api/preprocess` | 对未处理的 GIF 抽帧 |
| POST | `/api/process-frames` | 启动 VLM+LLM 处理（后台线程） |
| GET | `/api/processing-progress` | VLM 处理进度 |
| POST | `/api/build-index` | 构建 FAISS 向量索引 |
| POST | `/api/score-all` | 批量偏好评分 |
| GET | `/api/media/{id}` | 获取媒体详情、标注、帧列表 |
| GET | `/api/media/{id}/score` | 获取媒体偏好评分详情 |
| GET | `/api/review/next` | 获取下一条待审核媒体 |
| GET | `/api/review/{id}` | 获取指定媒体审核数据 |
| POST | `/api/feedback` | 提交人工反馈（like/dislike/neutral） |
| GET | `/api/candidates/folders` | 递归列出指定根目录下包含候选 GIF 的文件夹，返回数量、缺失数和状态计数 |
| GET | `/api/candidates` | 分页列出候选 GIF，支持 `status` / `limit` / `offset` / `folder`，返回状态计数 |
| POST | `/api/candidates/{candidate_id}/feedback` | 提交候选 GIF 评分（like/neutral/dislike/skip） |
| GET | `/api/preference/profiles` | 获取偏好画像列表及当前生效版本 |
| POST | `/api/preference/profiles/build` | 构建新的偏好画像 |
| POST | `/api/preference/profiles/{version}/publish` | 发布指定版本的偏好画像 |
| POST | `/api/preference/evaluate` | 偏好画像发布门禁评估（holdout 评估） |
| POST | `/api/tasks/commands` | (Phase 1) 任务引擎：下发控制命令（cancel/pause/resume） |
| GET | `/api/tasks/commands/pending` | (Phase 1) 轮询待处理命令 |
| GET | `/api/tasks/jobs` | (Phase 1) 列出所有任务及其状态统计 |
| GET | `/api/tasks/jobs/{job_id}` | (Phase 1) 查看任务详情（含视频和阶段） |
| GET | `/api/tasks/stages` | (Phase 1) 按状态/工作者/视频查询阶段 |
| POST | `/api/tasks/export-candidates` | (Phase 1) 打包候选 GIF |
| POST | `/api/tasks/import-legacy` | (Phase 1) 导入旧版队列/检查点状态 |

---

## 架构概览

### 质量门禁系统（app/services/）

所有 VLM/LLM 输出必须通过三层验证才能写入数据库：

| 模块 | 职责 |
|------|------|
| `json_guard.py` | 统一 JSON 解析：处理 markdown fence、`<think>` 标签、平衡括号提取 |
| `quality.py` | Placeholder 检测（12 种模板短语）+ emotional_core 归一化 + 字段校验 |
| `schemas.py` | Pydantic 模型定义（FrameAnalysis、ClipScore、MediaAnnotation）及 15 种规范情感枚举 |

关键指标：**placeholder 率从 89% 降至 <1%**。每次 VLM 调用最多重试 3 次，quality 失败自动重新生成。

### RAG 两阶段流水线

v2 架构采用两阶段设计避免 RAG 回音壁效应：

1. **Pass 1**：VLM 裸分析（无 RAG 上下文）→ 生成每帧的 caption + emotional_core
2. **Pass 2**：每帧 caption → nomic-embed-text 向量化 → FAISS 检索 top-5 相似收藏 → 作为 RAG 上下文注入 LLM 综合

自适应提取扩展（`test_video_adaptive.py`）：
- 15s 间隔粗采样全片 → per-frame VLM 评分
- 高分区域（>0.5）±15s 范围内 10s 细采样
- 相邻高分帧按 merge_gap=6s 合并为连贯片段
- FAISS embedding 去重（cosine > 0.95）避免重复导出

### 断点恢复机制

`vlm_loop.py` 实现生产级 VLM 处理循环：

- 每批 200 帧，per-frame DB commit 保证进度不丢失
- 所有进度写入 `data/vlm_loop.log`（带时间戳）
- 每 50 batch 自动重启 llava:13b 模型，防止推理速度衰减
- 连续 3 batch 零处理量自动终止
- 重启后从 `frames.vlm_status='pending'` 自动恢复

### 向量索引

- FAISS IndexFlatIP（内积）归一化为余弦相似度
- 8109 个向量索引，覆盖 9221 个 GIF 媒体
- `manifest.json` 记录 schema 版本、embedding 模型、维度
- `verify_index()` 交叉校验 FAISS 与 SQL vector_refs 记录一致性

---

## 项目结构

```
GifAgent/
├── app/
│   ├── main.py                       # FastAPI 应用（35+ 个端点）
│   ├── config.py                     # YAML 配置加载
│   ├── db.py                         # SQLite 连接 + 迁移 + Checkpoint
│   ├── routers/
│   │   ├── candidates.py             # 候选 GIF API（list + feedback）
│   │   ├── preference.py             # 偏好画像 API（build + publish + evaluate）
│   │   ├── tasks.py                  # 任务引擎命令/状态 API（7 个端点）
│   │   └── quality_lab.py            # 质量实验室 API（9 个端点）
│   ├── services/
│   │   ├── scanner.py                # 文件扫描、SHA256/pHash 去重
│   │   ├── preprocess.py             # ffmpeg GIF 抽帧、缩略图
│   │   ├── scheduler.py              # Ollama 模型切换调度
│   │   ├── vision.py                 # llava:13b 逐帧审美分析
│   │   ├── llm.py                    # LLM 综合标注（含 think 标签处理）
│   │   ├── embedding.py              # Ollama Embedding API
│   │   ├── indexer.py                # FAISS 索引管理（余弦相似）
│   │   ├── scorer.py                 # 四因子偏好评分
│   │   ├── json_guard.py             # 统一 JSON 解析器
│   │   ├── quality.py                # Placeholder 检测 + 质量校验
│   │   ├── schemas.py                # Pydantic 模型（FrameAnalysis 等）
│   │   ├── llm_client.py             # LLM 客户端（Anthropic-compatible）
│   │   ├── scenario.py               # 场景标签管理
│   │   ├── video_fingerprint.py      # 视频指纹（时长+关键帧pHash，去重用）
│   │   ├── candidates.py             # 候选 GIF 物化服务
│   │   ├── preference_schema.py      # Preference Memory 数据库 DDL
│   │   ├── preference_events.py      # 反馈事件记录服务
│   │   ├── preference_memory.py      # 偏好画像构建 + 发布服务
│   │   ├── preference_types.py       # 偏好系统类型定义
│   │   ├── preference_evaluation.py  # Holdout 评估服务
│   │   ├── provenance.py             # Provenance 数据类
│   │   └── reranker.py               # 偏好加权重排序器
│   ├── quality_lab/                  # Phase 2: 质量实验室（benchmark 评估系统）
│   │   ├── __init__.py               # 公共导出
│   │   ├── models.py                 # 数据类（ExperimentConfig, ABSession 等）
│   │   ├── schema.py                 # quality_lab.db DDL（10 张表）
│   │   ├── manifests.py             # 不可变 benchmark manifest 管理
│   │   ├── runner.py                 # ExperimentRunner 运行编排
│   │   ├── metrics.py                # NumPy 质量指标（4 种）
│   │   ├── calibration.py            # VLM 分数校准（PAV 等渗回归）
│   │   ├── ab_review.py              # 盲测 A/B 评审服务
│   │   └── promotion.py             # 冠军配置晋级/回滚
│   ├── task_engine/                  # Phase 1: 可靠任务引擎
│   │   ├── __init__.py               # 公共导出
│   │   ├── models.py                 # 数据类
│   │   ├── schema.py                 # DDL + 迁移
│   │   ├── repository.py             # TaskRepository CRUD
│   │   ├── fingerprints.py           # SHA-256 / 哈希工具
│   │   ├── artifacts.py              # 制品提交与校验
│   │   ├── legacy_import.py          # 旧版状态导入
│   │   ├── stages.py                 # 阶段适配器协议
│   │   ├── adaptive_adapter.py       # 自适应适配器
│   │   └── worker.py                 # 单写入者工作循环
│   └── ui/
│       ├── review.py                 # Gradio 原始 GIF 审核界面（port 7860）
│       ├── candidate_review.py       # 候选 GIF 评分 + 批量控制面板（port 7861）
│       └── quality_lab_tab.py        # 质量实验室标签页（盲测 A/B、晋级、回滚）
├── configs/
│   └── models.yaml                   # 主配置：模型、路径、阈值、偏好开关、任务引擎
├── scripts/
│   ├── setup.bat                     # Windows 一键安装脚本
│   ├── index_library.py              # 全量索引流水线（5 阶段）
│   ├── cluster_and_select.py         # pHash 聚类 + 代表选取
│   ├── process_representatives.py    # VLM+LLM 标注（带 checkpoint）
│   ├── vlm_loop.py                   # VLM 处理循环（自动恢复 + 模型重启）
│   ├── vlm_quick_200.py              # VLM 批量处理（200 帧/批）
│   ├── vlm_continuous.py             # VLM 持续处理（外部进程）
│   ├── vlm_continuous_inproc.py      # VLM 持续处理（进程内）
│   ├── llm_synth_resume.py           # LLM 合成断点恢复
│   ├── pipeline_stage2.py            # Stage 2 流水线（LLM 合成 + FAISS 重建）
│   ├── inherit_and_index.py          # 簇内标签继承 + FAISS 索引
│   ├── test_video_rag.py             # RAG v1 测试
│   ├── test_video_rag_v2.py          # RAG v2 测试（两阶段流水线）
│   ├── test_video_adaptive.py        # 自适应 GIF 提取
│   ├── test_video_batch.py           # 批量视频处理（checkpoint 断点续跑）
│   ├── import_adaptive_candidates.py # 候选 GIF 导入 preference memory
│   ├── reset_derived_quality_data.py # 质量数据重置（--dry-run / --apply）
│   ├── rag_synth_recover.py          # RAG 合成恢复
│   ├── rag_100_batch.py              # RAG 批量处理（100 帧）
│   ├── test_jur639.py                # JUR-639 专用测试
│   ├── preference_memory.py          # 偏好记忆 CLI（status/build/publish）
│   ├── evaluate_preference.py        # 偏好画像发布门禁评估
│   ├── export_gifs.py               # GIF 批量导出
│   ├── task_worker.py                # 单写入者任务引擎工作进程
│   ├── import_legacy_task_state.py   # 旧版队列/检查点状态导入
│   ├── write_version_manifest.py     # 版本清单生成
│   ├── smoke_active_preference.py    # 偏好学习冒烟测试（6 种反馈 + 构建 + 发布 + 评估 + 回滚）
│   ├── smoke_task_engine.py          # 任务引擎冒烟测试
│   └── smoke_quality_lab.py          # 质量实验室冒烟测试
├── tests/
│   ├── test_json_guard.py            # JSON 解析测试
│   ├── test_quality.py               # 质量校验测试
│   ├── test_indexer_manifest.py      # FAISS manifest 验证
│   ├── test_reset_derived_quality_data.py  # Reset 安全性测试
│   ├── test_preference_preflight.py   # 偏好记忆迁移预检测试
│   ├── test_candidate_schema.py       # 候选表 schema 测试
│   ├── test_candidate_materialization.py  # 候选物化测试
│   ├── test_preference_events.py      # 反馈事件测试
│   ├── test_preference_profiles.py    # 偏好画像测试
│   ├── test_preference_evaluation.py  # Holdout 评估测试
│   ├── test_preference_reranker.py    # 重排序器测试
│   ├── test_version_manifest.py       # 版本清单和冒烟测试
│   ├── test_quality_lab_api.py        # 质量实验室 API 测试
│   └── task_engine/                   # 任务引擎测试套件
│       ├── test_repository.py
│       ├── test_artifacts.py
│       ├── test_legacy_import.py
│       ├── test_stage_adapter.py
│       ├── test_fault_injection.py
│       └── test_worker.py
├── data/                             # 运行时数据（gitignore）
│   ├── library.db                    # SQLite 数据库
│   ├── faiss/                        # FAISS 向量索引
│   ├── frames/                       # 抽帧 JPEG
│   ├── thumbs/                       # 缩略图
│   ├── exports/                      # GIF 导出
│   ├── backups/                      # 数据库备份
│   └── vlm_loop.log                  # VLM 处理日志
├── docs/
│   └── runbook-rag-workbench.md      # RAG 工作台运维手册
├── pyproject.toml                    # uv 项目配置
└── README.md
```

---

## 技术要点

### 统一 JSON 解析（json_guard.py）

替代了分散在 vision.py、llm.py、各个脚本中的 5 份 `_parse_json_response()` 副本。统一处理流程：

1. 剥离 `<think>...</think>` 和 `</think>` 后缀
2. 去除 markdown code fence（```` ```json ````）
3. 尝试 `json.loads` 严格解析
4. 失败时回退到平衡括号提取（提取第一个完整 `{...}` 对象）
5. 返回 `JsonParseResult(ok, data, raw, error)` 统一结构

### Placeholder 检测（quality.py）

检测 12 种常见的 VLM 模板输出短语：
`"what you see"`, `"one reason"`, `"2-3 observations"`, `"3-5 keywords"`, `"describe what you actually see"` 等。

检测到 placeholder 时：
- `caption` 置空，标记 `quality_failed`
- `aesthetic_notes` 逐条检查，过滤含 placeholder 的条目
- `emotional_core` 多值（管道分隔）归一化为首个有效值
- `gif_worthiness` 范围校验（0.0-1.0）

### LLM 思考模式处理

`json_guard.parse_json_response()` 会自动剥离 `<think>...</think>` 标签，并提取实际 JSON 输出。

### 质量数据重置

`reset_derived_quality_data.py` 用于在模型升级或 prompt 改进后全量重跑标注：

- `--dry-run`：预览将要清除的数据，不做任何修改
- `--apply`：自动备份数据库到 `data/backups/`，清除 frame_annotations / annotations / vector_refs / checkpoints，重置 frame 状态为 pending，删除 FAISS 索引文件
- 保留 media、frames、feedback 数据不受影响

### 偏好记忆迁移安全预检

在修改生产数据库前运行：
```bash
uv run python scripts/preference_memory.py status --json
```
详见 `docs/runbook-rag-workbench.md` 和上方 Preference Memory 系统章节。

### 运行测试

```bash
uv run pytest tests/ -v
# Current suite: 400+ tests.
# 400+ tests: JSON 解析、placeholder 检测、emotional_core 归一化、
# FAISS manifest 验证、reset 安全性、候选物化、反馈事件、偏好画像、Holdout 评估、重排序、
# 质量实验室 API、盲测 A/B、晋级/回滚
```

---

## 模型栈

| 角色 | 模型 | 说明 |
|------|------|------|
| VLM | `llava:13b` | 逐帧视觉分析，~5.9s/帧 |
| LLM | `DeepSeek-V4-Flash` | 云端 Anthropic-compatible 综合标注 + 标签生成 |
| Embedding | `nomic-embed-text:latest` | 文本/帧向量化（FAISS 索引） |

---

## Preference Memory 系统

Preference Memory 是 GifAgent 的偏好学习子系统，从候选 GIF 的人工反馈中学习用户偏好，构建可发布的偏好画像，并用于重排序候选结果。

### 架构

```
候选 GIF 物化 → 人工评分（like/dislike/neutral/skip/quality_reject/favorite）
     ↓
反馈事件记录（append-only，不可变事件日志，支持 correction 撤销）
     ↓
偏好画像构建（7 道门禁 → 质心向量计算 → 确定性版本号）
     │    ├─ 可配置 recency 衰减（指数半衰期，默认 90 天）
     │    ├─ 6 种反馈含义（favorite = 2x like weight）
     │    └─ scenario 级别子画像（按情感/标签分群）
     ↓
Holdout 评估门禁（Like@20 / Dislike@20 / NDCG@20）
     ↓
Source-grouped 评估（Phase 3）:
     ├─ base-vs-preference NDCG 对比
     ├─ pairwise win rate（preference 胜率）
     ├─ exploration diversity（源视频/场景多样性）
     ├─ vector coverage（向量覆盖率）
     └─ inactive fallback 分析
     ↓
画像发布（explicit publish，非自动覆盖）
     ↓
重排序（0.55×RAG + 0.25×global_like + 0.15×scenario_like - 0.20×global_dislike）
```

### 数据库表

| 表名 | 说明 |
|------|------|
| `candidate_gifs` | 候选 GIF（来源、时间、路径、状态、评分） |
| `candidate_vectors` | 候选向量索引 |
| `candidate_vector_exclusions` | 向量排除记录（格式不支持或空帧等原因无法向量化） |
| `favorite_gifs` | 收藏 GIF（绑定候选） |
| `preference_events` | 反馈事件日志（append-only，支持 correction 撤销） |
| `preference_profile_builds` | 画像构建历史记录 |
| `preference_profiles` | 偏好画像内容（global + scenario 级别） |
| `preference_profile_current` | 当前生效的画像版本 |
| `preference_profile_publications` | 发布历史（含回滚记录） |

### 反馈含义

| Rating | 含义 | 在 Profile 中的处理 |
|--------|------|---------------------|
| `like` | 正面：符合用户偏好 | 计入 liked centroid，weight = 1.0 |
| `dislike` | 负面：不符合用户偏好 | 计入 disliked centroid，weight = 1.0 |
| `neutral` | 中性：无明显偏好 | 不计入画像构建 |
| `skip` | 跳过：用户未评分 | 不计入画像构建 |
| `quality_reject` | 质量否决：视觉/技术缺陷 | 不计入画像构建 |
| `favorite` | 强烈正面：特别偏好 | 计入 liked centroid，weight = 2.0 |

### 功能开关

在 `configs/models.yaml` 中通过 `preference_memory.enabled` 控制：

```yaml
preference_memory:
  enabled: false   # 设为 true 启用重排序等功能
```

### CLI 工具

```bash
# 查看偏好记忆系统状态
uv run python scripts/preference_memory.py status --json

# 构建偏好画像
uv run python scripts/preference_memory.py build

# Backfill candidate vectors required by profile builds.
# Default scope is effective like/dislike feedback targets.
uv run python scripts/backfill_candidate_vectors.py --db dist/GifAgentUI/data/library.db

# 发布画像
uv run python scripts/preference_memory.py publish --profile-version <version>
```

GUI 发布入口：Candidate Review 页面的 `Profile` 区域中，先点击
`Refresh Profiles`，选择已完成构建的 profile version，再点击
`Publish Selected Profile`。发布后会写入 `preference_profile_current`，
后续重排序只使用已发布版本。

发布接口会为每次请求使用独立数据库连接，并设置 30s SQLite
`busy_timeout`。如果 GUI 或后台任务正在占用数据库，接口会返回可重试的
503，而不是把锁表冒泡成 500。

构建画像前需要确保所有有效 like/dislike 反馈目标都有对应
`candidate_vectors`；如果向量覆盖不完整，Build Profile 会被门禁拦截。

### Holdout 评估

```bash
# 门禁评估（需要 30+ 标定判断，训练/holdout 源视频不重叠）
uv run python scripts/evaluate_preference.py --profile-version <version> --holdout data/holdout.jsonl
```

### Source-grouped 评估（Phase 3）

Phase 3 新增 `evaluate_source_grouped()` 方法，按 `source_video_sha256` 分组检查训练集和
holdout 集的源视频隔离，并报告多维度的学习质量指标：

| 指标 | 说明 |
|------|------|
| `source_video_integrity` | 训练/holdout 源视频重叠检测 |
| `base_ndcg_at_20` | 纯 RAG 基线（`base_rag_similarity`）的 NDCG@20 |
| `preference_ndcg_at_20` | 偏好增强后（`final_score`）的 NDCG@20 |
| `ndcg_delta` | preference NDCG - base NDCG（正值表示偏好提升了排序质量） |
| `pairwise_win_rate` | 偏好排名胜过基线排名的 liked 候选占比 |
| `exploration_diversity` | 源视频和场景标签的多样性统计 |
| `vector_coverage` | holdout 候选的向量覆盖率 |
| `inactive_fallbacks` | 因缺少偏好分数而回退到 RAG 基线的候选占比 |

### 配置文件参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `preference_memory.enabled` | `false` | 功能开关 |
| `preference_memory.recency_enabled` | `true` | 启用 recency 衰减 |
| `preference_memory.recency_half_life_days` | `90.0` | 半衰期（天） |
| `preference_memory.favorite_weight` | `2.0` | favorite 评级权重 |
| `preference_memory.like_weight` | `1.0` | like 评级权重 |
| `preference_memory.dislike_weight` | `1.0` | dislike 评级权重 |
| `preference_memory.scenario_min_feedback` | `8` | scenario 画像最低反馈数 |

### 冒烟测试

```bash
# 验证完整的偏好学习生命周期
uv run python scripts/smoke_active_preference.py
```

操作：创建候选数据集 → 记录全部 6 种反馈含义 → 构建并发布画像 → 展示解释 → 回滚。
所有操作在内存 SQLite 中完成，不修改生产数据。

---

## Phase 1: Reliable Task Engine

Phase 1 adds a production-grade task processing engine for adaptive GIF extraction and pipeline stages.

### Architecture

| Component | File | Role |
|-----------|------|------|
| Schema | `app/task_engine/schema.py` | 7 tables (task_jobs, task_videos, task_stages, task_artifacts, task_events, task_commands, task_migrations) |
| Repository | `app/task_engine/repository.py` | `TaskRepository` — transactional CRUD, stage leasing (90s lease with heartbeat), cancellation |
| Fingerprints | `app/task_engine/fingerprints.py` | `sha256_file()`, `canonical_hash()`, `canonical_json()` |
| Artifacts | `app/task_engine/artifacts.py` | `commit_artifact()` with path-existence + SHA-256 validation |
| Stages | `app/task_engine/stages.py` | `StageAdapter` protocol, `StageContext`, `StageResult` |
| Adapter | `app/task_engine/adaptive_adapter.py` | Wraps existing adaptive pipeline as a stage adapter |
| Worker | `app/task_engine/worker.py` | `TaskWorker` — single-writer lease loop with heartbeat, retry, cancellation |
| Legacy Import | `app/task_engine/legacy_import.py` | One-shot migration from batch_queue_state.json + checkpoint |
| Provenance | `app/services/provenance.py` | Captures git commit, config hash, model versions, prompt hashes |

### Production Eight-Stage Pipeline

Every video advances through real, independently leased stages:
`discover -> sample -> vlm -> refine -> rank_dedup -> synthesize -> gif_clip -> materialize`.
Each stage reads immutable upstream artifacts and atomically commits its own
versioned manifest. `gif_clip` fans out to one stage per clip, so a failed GIF
can be retried without repeating successful GIFs or earlier video stages.
`materialize` starts only after all clip stages are terminal and reports partial
output as `needs_attention` instead of silently marking the video successful.

The full production-path release gate covers success, VLM outage, invalid VLM
payload, and valid zero-clip execution. The 2026-07-18 baseline is
`940 passed, 2 skipped, 3 warnings`; the warnings are dependency deprecations.

### Task API Endpoints (7 new)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/tasks/commands` | Enqueue a command (cancel/pause/resume) |
| GET | `/api/tasks/commands/pending` | Poll pending commands |
| GET | `/api/tasks/jobs` | List all jobs with status counts |
| GET | `/api/tasks/jobs/{job_id}` | Job detail with videos and stages |
| GET | `/api/tasks/stages` | Query stages by status/worker/video |
| POST | `/api/tasks/export-candidates` | Package candidate GIFs for export |
| POST | `/api/tasks/import-legacy` | Import legacy queue/checkpoint state |

### New Scripts

```bash
# Run the task worker (single-writer loop)
uv run python scripts/task_worker.py [--once] [--poll 1.0] [--db data/task_state.db]

# Import legacy batch queue state into task engine
uv run python scripts/import_legacy_task_state.py \
    --queue data/batch_queue_state.json \
    --state data/batch_state.json \
    --checkpoint data/batch_checkpoint.json \
    --db data/task_state.db

# Generate version manifest for packaged builds
uv run python scripts/write_version_manifest.py --dist dist/GifAgentUI

# Smoke test the task engine (requires temp dir, rejects production data)
uv run python scripts/smoke_task_engine.py --data-dir /tmp/smoke-test
```

### Control Tab Cutover

The Gradio Control tab now uses the task API instead of the legacy batch queue.
Set `GIFAGENT_LEGACY_QUEUE_UI=1` to restore the old queue-based control panel.

### Config

```yaml
task_engine:
  enabled: true
  db_path: "data/task_state.db"
  poll_seconds: 1.0
  lease_seconds: 90
  max_attempts: 3
  base_delay_seconds: 5
  max_delay_seconds: 300
```

### Backup & Rollback

Legacy import creates timestamped backups before any write transaction.
Migration tracking via `task_migrations` table with SHA-256 migration IDs
ensures idempotent re-import.

Historical queues do not need to be force-rerun for a release. Preserve their
databases and checkpoints, then use a small new-video smoke run when validating
a rebuilt package.

---

## Phase 2: Quality Lab

Phase 2 adds a systematic quality evaluation framework (`app/quality_lab/`) for
comparing experiment configurations through frozen benchmark manifests, automated
metric scorecards, blind A/B review, and champion promotion with rollback.

### Architecture

```
app/quality_lab/
├── __init__.py       # Public exports
├── models.py         # Dataclasses (ExperimentConfig, ExperimentRun, BenchmarkItem, etc.)
├── schema.py         # quality_lab.db DDL (10 tables) + connect_quality_db()
├── manifests.py      # Immutable JSON manifest creation + loading
├── runner.py         # ExperimentRunner — submits items as task jobs
├── metrics.py        # NumPy metrics: ndcg_at_k, temporal_coverage, diversity_score, export_integrity
├── calibration.py    # VLM score calibration: reliability diagram bins + PAV isotonic regression
├── ab_review.py      # BlindReviewService — blind A/B session lifecycle
└── promotion.py      # Champion promotion (6 gates) + rollback + history
```

### 24-Video Manifest Procedure

To create a frozen benchmark manifest from a set of source videos:

```bash
# 1. Collect a diverse set of videos (e.g., 24) covering different
#    duration buckets, resolutions, and content paces.
# 2. For each video, compute its content fingerprint using
#    app.services.video_fingerprint.
# 3. Create BenchmarkItem objects with assigned splits.
# 4. Freeze the manifest to an immutable JSON file.

uv run python -c "
import json, uuid
from pathlib import Path
from app.quality_lab.manifests import freeze_manifest, assign_splits
from app.quality_lab.models import BenchmarkItem

items = [
    BenchmarkItem(
        item_id=uuid.uuid4().hex,
        source_path=str(Path('videos') / f'{name}.mp4'),
        video_fingerprint=fingerprint,
        duration_bucket=duration,
        resolution_bucket=resolution,
        pace_bucket=pace,
        difficulty_tags=('action', 'dialog'),
        split='tune',  # will be reassigned by assign_splits
    )
    for name, fingerprint, duration, resolution, pace in [
        ('clip01', 'fp001', 'short', '720p', 'medium'),
        # ... add all 24 clips
    ]
]

# Deterministic tune/holdout split (70/30, seeded by content)
split_items = assign_splits(items)

# Freeze to immutable JSON (manifest_id = SHA-256 of content)
manifest_id = freeze_manifest(split_items, Path('manifest.json'), version=1)
print(f'Created manifest {manifest_id} with {len(split_items)} items')
"
```

### Tune/Holdout Boundary

- Items are assigned splits deterministically using a seed derived from all
  items' content (fingerprint + buckets). Items sharing the same fingerprint
  always receive the same split.
- Default ratio: **70% tune / 30% holdout**.
- Tune runs drive champion promotion; holdout runs guard against overfitting.
- Both splits must have completed runs before promotion gates pass.
- Holdout results appear in the scorecard but never influence promotion decisions.

### Scorecard Definitions

| Metric | Range | Description |
|--------|-------|-------------|
| `export_integrity` | [0, 1] | `succeeded / max(1, attempted)` — fraction of successful exports |
| `temporal_coverage` | [0, 1] | Fraction of video timeline covered by union of exported clip intervals |
| `ndcg_at_k` | [0, 1] | NDCG at position k for ranked relevance scores |
| `diversity_score` | [0, 1] | Average pairwise cosine distance of exported clip vectors |

### Calibration Command

VLM score calibration produces reliability-diagram bins and fits a monotonic
calibrator using the pool-adjacent-violators (PAV) algorithm:

```bash
uv run python -c "
from app.quality_lab.calibration import calibration_curve, fit_monotonic_calibrator

# Example: raw VLM scores vs binary ground-truth labels
scores = [0.1, 0.3, 0.5, 0.7, 0.9]
labels = [0, 0, 1, 1, 1]

# Reliability curve
bins = calibration_curve(scores, labels, bins=5)
for b in bins:
    print(f'  [{b.lower:.1f}, {b.upper:.1f}): mean_score={b.mean_score:.3f}, pos_rate={b.positive_rate:.3f}, count={b.count}')

# PAV isotonic regression
cal = fit_monotonic_calibrator(scores, labels)
print(f'Thresholds: {cal.thresholds}')
print(f'Values:     {cal.values}')
"
```

### Blind A/B Review

The `BlindReviewService` creates blind review sessions between two experiment
runs. Clips are paired by source-video fingerprint and temporal proximity.
Each reviewer sees opaque side tokens instead of config IDs.

```bash
uv run python -c "
from app.quality_lab import connect_quality_db, BlindReviewService

db = connect_quality_db()  # uses data/quality_lab.db
service = BlindReviewService(db)

# Create a session between two runs
session = service.create_session(
    run_a='<tune-run-id-a>', run_b='<tune-run-id-b>', seed=42
)

# Walk through unjudged pairs
pair = service.next_pair(session.session_id)
while pair:
    print(f'Pair {pair.pair_index}: left={pair.left_token[:8]}... right={pair.right_token[:8]}...')
    choice = input('Your choice (left/right/tie/both_bad): ')
    service.record(session.session_id, str(pair.pair_index), choice)
    pair = service.next_pair(session.session_id)

# Reveal which config won
result = service.reveal(session.session_id)
print(f'Config A wins: {result.run_a_wins}, Config B wins: {result.run_b_wins}')
"
```

### Champion Promotion

Promotion gates a config through 6 checks before it becomes the champion:

1. Config exists in `experiment_configs`
2. Confirmation string matches config ID
3. At least one completed tune run
4. At least one completed holdout run
5. At least one completed blind A/B session involving any of the config's runs
6. Average `export_integrity` >= 0.9

CLI promotion:

```bash
uv run python -c "
from app.quality_lab import connect_quality_db
from app.quality_lab.promotion import promote_config, list_champion_history

db = connect_quality_db()
result = promote_config('<config-id>', db_conn=db, confirmation='<config-id>')
print(result['message'])
print(f'Scorecard: {result[\"scorecard\"]}')
"
```

### Rollback

Rollback reverts to the previous champion config by finding the most recent
promote event in `champion_history`:

```bash
uv run python -c "
from app.quality_lab import connect_quality_db
from app.quality_lab.promotion import rollback, list_champion_history

db = connect_quality_db()
result = rollback(db_conn=db)
print(result['message'])

# Verify history
history = list_champion_history(db_conn=db)
for event in history:
    print(f'  {event[\"action\"]}: {event[\"config_id\"]} ({event[\"created_at\"]})')
"
```

### Provenance Lookup

Every experiment config records its provenance — git commit, config hash, model
versions, and prompt hashes — enabling full reproducibility:

```bash
uv run python -c "
import json
from app.quality_lab import connect_quality_db

db = connect_quality_db()
rows = db.execute(
    'SELECT config_id, provenance_json, created_at FROM experiment_configs ORDER BY created_at'
).fetchall()
for r in rows:
    prov = json.loads(r['provenance_json'])
    print(f'Config {r[\"config_id\"]}:')
    print(f'  Git commit: {prov.get(\"git_commit\", \"unknown\")}')
    print(f'  Config hash: {prov.get(\"config_hash\", \"unknown\")}')
    print(f'  Created: {r[\"created_at\"]}')
"
```

### Smoke Test

A standalone smoke test validates the full quality-lab lifecycle without
running VLM or creating real GIFs:

```bash
uv run python scripts/smoke_quality_lab.py --data-dir /tmp/quality-smoke
```

Operations: create two configs, create a 4-item manifest, complete runs with
injected fake results, create a blind A/B session, record judgments, promote
one config, roll back, and verify no source files changed.

### Quality Lab API Endpoints (9 new)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/quality/runs` | List all experiment runs |
| GET | `/api/quality/runs/{run_id}` | Get a single run |
| GET | `/api/quality/runs/{run_id}/scorecard` | Run metric scorecard |
| POST | `/api/quality/ab-sessions` | Create blind A/B session |
| POST | `/api/quality/ab-sessions/{session_id}/judgments` | Record judgment |
| POST | `/api/quality/champions/{config_id}/promote` | Promote config to champion |
| POST | `/api/quality/champions/rollback` | Rollback to previous champion |
| GET | `/api/quality/champions/history` | Champion history events |
| GET | `/api/quality/champions/current` | Current champion config |

---

## Phase 4: Library Workbench

Phase 4 adds the **Library Workbench** (`app/ui/workbench.py`), a comprehensive Gradio-based
management UI with 7 tabs for browsing, searching, reviewing, and curating the GIF library.
The workbench replaces the separate candidate-review and control-panel UIs with a unified
interface backed by a suite of new services.

### Architecture

```
app/ui/
├── workbench.py              # Shell: gr.Blocks with all 7 tabs
├── api_client.py             # GifAgentApiClient (HTTP to FastAPI)
├── components/
│   ├── __init__.py
│   ├── common.py             # Shared Gradio components
│   └── timeline.py           # Timeline renderer (PotPlayer targets)
└── tabs/
    ├── __init__.py
    ├── today.py              # Today / Attention Inbox
    ├── control.py            # Queue / task control
    ├── review.py             # Candidate review
    ├── search.py             # Semantic + filtered search
    ├── collections.py        # Smart collections + exports
    ├── lab.py                # Quality Lab
    ├── settings.py           # Config editor
    └── profile.py            # Profile management

app/services/
├── workbench_schema.py       # FTS5 DDL, SearchQuery / SearchPage / CollectionSpec models
├── library_search.py         # LibrarySearchService — FTS5 + vector search
├── timeline.py               # load_timeline_window + potplayer_target
├── media_relink.py           # propose_relinks / apply_relink by fingerprint
├── collections.py            # CollectionService — create / refresh / freeze / export
├── taste_map.py              # project_taste_map — 2D SVD projection
├── narrative_curation.py     # curate_narrative — beat-based selection
└── attention.py              # list_attention_items — cross-DB inbox
```

### 7 Tabs

| Tab | Module | Purpose |
|-----|--------|---------|
| 今日 (Today) | `tabs/today.py` | Attention inbox: task failures, migration conflicts, profile publishes, high-value reviews, champion promotions |
| 队列 (Queue) | `tabs/control.py` | Task engine job control: start/pause/resume/cancel batch processing |
| 审核 (Review) | `tabs/review.py` | Candidate GIF review: paginated gallery, like/dislike/skip/favorite/quality_reject feedback |
| 搜索 (Search) | `tabs/search.py` | Semantic + filtered search: full-text, tags, folder, duration, status, date ranges |
| 合集 (Collections) | `tabs/collections.py` | Smart collections: generate, refresh, freeze, export (JSON manifest + PBF) |
| 实验室 (Lab) | `tabs/lab.py` | Quality Lab: benchmark runs, blind A/B, champion promotion/rollback |
| 设置 (Settings) | `tabs/settings.py` | Config editor + profile management + publish controls |

### New Services

#### LibrarySearchService (`library_search.py`)

FTS5 + vector similarity search over `candidate_gifs`. Supports:
- **Exact filters**: tags (JSON array), folder (substring), duration range, status list, date range
- **Text search**: FTS5 BM25 ranking combined with cosine similarity against nomic-embed-text embeddings
- **Pagination**: stable offset/limit, max 24 items per page
- **Index rebuild**: resumable, per-batch commit, state tracked in `search_index_state`

```python
from app.services.library_search import LibrarySearchService, SearchQuery

page = search_service.search(
    SearchQuery(text="explosion", tags=("action",), min_duration=1.0),
    limit=24, offset=0,
)
```

#### Timeline (`timeline.py`)

Loads scenes, candidates, and generated GIFs overlapping a viewport window.
Thumbnail cap of 60 prevents memory blowout. Each `TimelineSpan` carries
`base_score`, `preference_score`, and `thumbnail_path`.

```python
from app.services.timeline import load_timeline_window, potplayer_target

window = load_timeline_window(conn, video_id="vid-001", start_sec=0, end_sec=120)
url = potplayer_target("C:/videos/clip.mp4", 30.5)  # → potplayer://...?seek=30.5
```

#### Media Relink (`media_relink.py`)

Detects candidates whose source video moved (fingerprint match, path mismatch).
`propose_relinks()` returns `RelinkProposal` objects; `apply_relink()` updates
paths atomically.

#### CollectionService (`collections.py`)

Create, refresh (search + farthest-first diversity selection), freeze (lock version),
and export (JSON manifest + PBF binary) smart collections.

```python
from app.services.workbench_schema import CollectionSpec
from app.services.collections import CollectionService

service = CollectionService(conn, search_service)
spec = CollectionSpec(name="Best Action", query=SearchQuery(tags=("action",)), target_count=20)
collection = service.create(spec)
version = service.refresh(collection.collection_id)
report = service.export(collection.collection_id, Path("data/exports"))
```

#### Taste Map (`taste_map.py`)

2D projection of candidate embedding vectors via centred SVD (no scikit-learn
dependency). Returns `TastePoint(candidate_id, x, y)` list.

```python
from app.services.taste_map import project_taste_map

points = project_taste_map(vectors_np, candidate_ids)
```

#### Narrative Curation (`narrative_curation.py`)

Greedy beat-based candidate selection: assigns the best-fitting candidate to
each narrative beat (opening, development, climax, ending) with diversity bonus
for unused source videos.

```python
from app.services.narrative_curation import curate_narrative, CurationCandidate

beats = curate_narrative(candidates, beats=("opening", "development", "climax", "ending"))
```

#### Attention Inbox (`attention.py`)

Cross-DB aggregation of actionable items: task failures, SHA256 conflicts,
profile publishes, high-value review candidates, champion promotions.
Read-only; catches per-source connection errors so one locked DB never fails
the whole inbox.

```python
from app.services.attention import list_attention_items

items = list_attention_items(task_repo=repo, library_conn=lib, quality_conn=qual)
```

### Workbench Service Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/workbench/search` | Search candidates (text, tags, folder, duration, status, dates) |
| GET | `/api/workbench/timeline` | Load timeline window (scenes + candidates + generated GIFs) |
| GET | `/api/workbench/attention` | Attention inbox items |
| POST | `/api/workbench/relinks/scan` | Scan for media relink opportunities |
| POST | `/api/workbench/relinks/apply` | Apply a relink proposal |
| POST | `/api/workbench/collections` | Create a new collection |
| POST | `/api/workbench/collections/{id}/refresh` | Refresh collection (search + diversity) |
| POST | `/api/workbench/collections/{id}/freeze` | Freeze collection version |
| POST | `/api/workbench/collections/{id}/export` | Export collection (JSON + PBF) |

### Performance Characteristics

- Search handles 10,000+ candidate rows in under 5 seconds per page.
- Timeline caps thumbnails at 60 per viewport window.
- Search results return at most 24 items per page (stable pagination).
- Result payloads use static `preview_path` strings; full GIF bytes are never
  embedded in API responses.
- The search → select → create-collection workflow requires 3 primary UI actions.

### Smoke Test

```bash
uv run python scripts/smoke_library_workbench.py
```

Validates: search, timeline, relink, collections (create/refresh/freeze/export),
taste map projection, narrative curation, and attention inbox. Runs entirely
in an in-memory SQLite database with synthetic vectors.

### New Test File

```
tests/test_workbench_performance.py   # 10k-row performance, 60-thumbnail cap, UI action count
```

### Phase 4 Tasks (1-8) Output Summary

| Task | Output |
|------|--------|
| 1: Workbench Shell | `workbench.py`, `api_client.py`, 7-tab `gr.Blocks`, modular UI boundary |
| 2: Attention Inbox | `attention.py` — cross-DB aggregation, per-source error isolation |
| 3: Semantic Search | `library_search.py`, `workbench_schema.py`, FTS5 + vector search |
| 4: Moment Timeline | `timeline.py` — viewport window, 60-thumbnail cap, PotPlayer URLs |
| 5: Media Relink | `media_relink.py` — fingerprint-based path correction |
| 6: Collections | `collections.py` — create/refresh/freeze/export (JSON + PBF) |
| 7: Taste Map + Narrative | `taste_map.py` (SVD projection), `narrative_curation.py` (beat selection) |
| 8: Performance + Smoke | Performance tests (10k rows, 5s), smoke test, final docs |

---

## License

MIT

## Links

- GitHub: [https://github.com/qwer1234-cloud/GifAgent](https://github.com/qwer1234-cloud/GifAgent)
