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
  base_url: "http://localhost:11434"

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
- GIF 命名格式：`{video_name}@@@{seq}_{start_s}s-{end_s}s.gif`
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
  `status`, `limit`, and `offset`. The default status is `candidate`.
- The candidate review UI loads only one small page at a time (`PAGE_SIZE=12`)
  instead of pulling every candidate GIF on each refresh.
- Gallery cells use cached static thumbnails in `data/thumbs/candidates/`.
  The full animated GIF is loaded only for the currently selected candidate.
- Selection is bound to the current page state, so `Like` / `Dislike` is sent
  to the exact candidate the user clicked, even after filtering or paging.
- The Windows GUI bundle was rebuilt with:
  `uv run pyinstaller --noconfirm build_exe.spec`.
  Output: `dist/GifAgentUI/GifAgentUI.exe`.

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
| GET | `/api/candidates` | 分页列出候选 GIF，支持 `status` / `limit` / `offset`，返回状态计数 |
| POST | `/api/candidates/{candidate_id}/feedback` | 提交候选 GIF 评分（like/neutral/dislike/skip） |
| GET | `/api/preference/profiles` | 获取偏好画像列表及当前生效版本 |
| POST | `/api/preference/profiles/build` | 构建新的偏好画像 |
| POST | `/api/preference/profiles/{version}/publish` | 发布指定版本的偏好画像 |
| POST | `/api/preference/evaluate` | 偏好画像发布门禁评估（holdout 评估） |

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
│   ├── main.py                       # FastAPI 应用（18 个端点）
│   ├── config.py                     # YAML 配置加载
│   ├── db.py                         # SQLite 连接 + 迁移 + Checkpoint
│   ├── routers/
│   │   ├── candidates.py             # 候选 GIF API（list + feedback）
│   │   └── preference.py             # 偏好画像 API（build + publish + evaluate）
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
│   │   └── reranker.py               # 偏好加权重排序器
│   └── ui/
│       ├── review.py                 # Gradio 原始 GIF 审核界面（port 7860）
│       └── candidate_review.py       # 候选 GIF 评分 + 批量控制面板（port 7861）
├── configs/
│   └── models.yaml                   # 主配置：模型、路径、阈值、偏好开关
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
│   └── export_gifs.py               # GIF 批量导出
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
│   └── test_preference_reranker.py    # 重排序器测试
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
# Current suite: 91 tests, 1 skipped.
# 91 tests（1 skipped）: JSON 解析、placeholder 检测、emotional_core 归一化、
# FAISS manifest 验证、reset 安全性、候选物化、反馈事件、偏好画像、Holdout 评估、重排序
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
候选 GIF 物化 → 人工评分（like/dislike/neutral/skip）
     ↓
反馈事件记录（append-only，不可变事件日志）
     ↓
偏好画像构建（7 道门禁 → 质心向量计算 → 确定性版本号）
     ↓
Holdout 评估门禁（Like@20 / Dislike@20 / NDCG@20）
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
| `preference_events` | 反馈事件日志（append-only） |
| `preference_profile_builds` | 画像构建历史记录 |
| `preference_profiles` | 偏好画像内容 |
| `preference_profile_current` | 当前生效的画像版本 |

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

# 发布画像
uv run python scripts/preference_memory.py publish --profile-version <version>
```

### Holdout 评估

```bash
# 门禁评估（需要 30+ 标定判断，训练/holdout 源视频不重叠）
uv run python scripts/evaluate_preference.py --profile-version <version> --holdout data/holdout.jsonl
```

---

## License

MIT

## Links

- GitHub: [https://github.com/qwer1234-cloud/GifAgent](https://github.com/qwer1234-cloud/GifAgent)
