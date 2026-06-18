# GifAgent

> 本地影视 GIF 自动标注与偏好挖掘 Agent —— 基于 Ollama + FAISS + RAG 的智能片段发现系统

## 概述

GifAgent 是一个运行在本地的影视片段智能管理工具。它自动扫描 GIF/视频文件，调用本地 VLM 分析每一帧的审美特征和情感表达，通过质量门禁过滤低质量输出，建立 FAISS 向量索引支持相似检索，并在新视频中自动发现符合偏好的经典片段。

### 核心能力

- **自动打标**：VLM（llava:13b）逐帧分析审美特征 → LLM（Qwen3-14B）综合生成标签，所有输出经过质量门禁校验
- **质量门禁**：统一的 JSON 解析器 + placeholder 检测 + Pydantic 模型校验，placeholder 率从 89% 降至 <1%
- **向量索引**：基于 nomic-embed-text 的 FAISS 语义向量库，支持文本到 GIF 的跨模态检索（1062 向量 / 8114 媒体）
- **RAG 增强**：两阶段自适应 GIF 提取，per-frame VLM 评分 + 时域 clip 合并 + FAISS 相似 GIF 检索
- **智能采样**：20s 间隔粗采样 → 高分区域 10s 细采样，merge_gap=10s 自动合并相邻高分帧
- **断点恢复**：VLM 处理循环自动恢复、per-frame DB commit、每 50 batch 模型重启防降速

### 数据流

```
E:\data\originals\（8000+ GIF）
  → SHA256 + pHash 去重 → SQLite 入库
  → ffmpeg GIF 抽帧（6-12 帧/张）
  → llava:13b 逐帧审美分析（~4.7s/帧）
  → json_guard 统一解析 → quality 门禁校验
  → Qwen3-14B 综合标注（tags + emotional_core + aesthetic_notes）
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
| GPU | 16GB+ VRAM | 交替运行 llava:13b (~8GB) 和 Qwen3-14B (~9GB) |

### 必需模型

```bash
ollama pull llava:13b                                    # VLM 视觉分析
ollama pull hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M          # LLM 文本合成
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
  model: "llava:13b"                  # 视觉模型

llm:
  model: "hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M"  # 文本模型

embedding:
  text_model: "nomic-embed-text:latest"  # Embedding 模型

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
# 两阶段自适应提取：20s 粗采样 → VLM 评分 → 10s 细采样 → clip 合并
uv run python scripts/test_video_adaptive.py
```

输出：
- `data/exports/adaptive_test/`：导出的候选 GIF 片段（merge_gap=10s 合并）
- 控制台输出每帧的 gif_worthiness 分数和处理统计

### 第四步：质量数据重置

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

# Gradio 审核界面（A=喜欢 S=一般 D=不喜欢 E=编辑标签）
uv run python app/ui/review.py
# 浏览器打开 http://127.0.0.1:7860
```

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
| POST | `/api/feedback` | 提交人工反馈（like/dislike/neutral） |

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
- 20s 间隔粗采样全片 → per-frame VLM 评分
- 高分区域（>0.5）±20s 范围内 10s 细采样
- 相邻高分帧按 merge_gap=10s 合并为连贯片段
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
- 1062 个向量索引，覆盖 8114 个 GIF 媒体
- `manifest.json` 记录 schema 版本、embedding 模型、维度
- `verify_index()` 交叉校验 FAISS 与 SQL vector_refs 记录一致性

---

## 项目结构

```
GifAgent/
├── app/
│   ├── main.py                       # FastAPI 应用（10 个端点）
│   ├── config.py                     # YAML 配置加载
│   ├── db.py                         # SQLite 连接 + 迁移 + Checkpoint
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
│   │   └── schemas.py                # Pydantic 模型（FrameAnalysis 等）
│   └── ui/
│       └── review.py                 # Gradio 审核界面（A/S/D 快捷键）
├── configs/
│   └── models.yaml                   # 主配置：模型、路径、阈值
├── scripts/
│   ├── index_library.py              # 全量索引流水线（5 阶段）
│   ├── cluster_and_select.py         # pHash 聚类 + 代表选取
│   ├── process_representatives.py    # VLM+LLM 标注（带 checkpoint）
│   ├── vlm_loop.py                   # VLM 处理循环（自动恢复 + 模型重启）
│   ├── vlm_quick_200.py              # VLM 批量处理（200 帧/批）
│   ├── vlm_continuous.py             # VLM 持续处理（外部进程）
│   ├── vlm_continuous_inproc.py      # VLM 持续处理（进程内）
│   ├── llm_synth_resume.py           # LLM 合成断点恢复
│   ├── inherit_and_index.py          # 簇内标签继承 + FAISS 索引
│   ├── test_video_rag.py             # RAG v1 测试
│   ├── test_video_rag_v2.py          # RAG v2 测试（两阶段流水线）
│   ├── test_video_adaptive.py        # 自适应 GIF 提取（20s/10s 两阶段）
│   ├── reset_derived_quality_data.py # 质量数据重置（--dry-run / --apply）
│   ├── rag_synth_recover.py          # RAG 合成恢复
│   ├── rag_100_batch.py              # RAG 批量处理（100 帧）
│   ├── test_jur639.py                # JUR-639 专用测试
│   └── export_gifs.py               # GIF 批量导出
├── tests/
│   ├── test_json_guard.py            # JSON 解析测试（9 tests）
│   ├── test_quality.py               # 质量校验测试（19 tests）
│   ├── test_indexer_manifest.py      # FAISS manifest 验证（3 tests）
│   └── test_reset_derived_quality_data.py  # Reset 安全性测试（3 tests）
├── data/                             # 运行时数据（gitignore）
│   ├── library.db                    # SQLite 数据库
│   ├── faiss/                        # FAISS 向量索引
│   ├── frames/                       # 抽帧 JPEG
│   ├── exports/                      # GIF 导出
│   ├── backups/                      # 数据库备份
│   └── vlm_loop.log                  # VLM 处理日志
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

Qwen3-14B 使用 `<think>...</think>` 标签包裹推理过程。`json_guard.parse_json_response()` 自动剥离 think 标签，提取实际 JSON 输出。

### 质量数据重置

`reset_derived_quality_data.py` 用于在模型升级或 prompt 改进后全量重跑标注：

- `--dry-run`：预览将要清除的数据，不做任何修改
- `--apply`：自动备份数据库到 `data/backups/`，清除 frame_annotations / annotations / vector_refs / checkpoints，重置 frame 状态为 pending，删除 FAISS 索引文件
- 保留 media、frames、feedback 数据不受影响

### 运行测试

```bash
uv run pytest tests/ -v
# 34 tests: JSON 解析、placeholder 检测、emotional_core 归一化、
# FAISS manifest 验证、reset 安全性
```

---

## 模型栈

| 角色 | 模型 | 说明 |
|------|------|------|
| VLM | `llava:13b` | 逐帧视觉分析，~4.7s/帧 |
| LLM | `hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M` | 综合标注 + 标签生成 |
| Embedding | `nomic-embed-text:latest` | 文本/帧向量化（FAISS 索引） |

---

## License

MIT

## Links

- GitHub: [https://github.com/qwer1234-cloud/GifAgent](https://github.com/qwer1234-cloud/GifAgent)
