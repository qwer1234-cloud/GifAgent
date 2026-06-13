# GifAgent

> 本地影视 GIF 自动标注与偏好挖掘 Agent —— 基于 Ollama + FAISS + RAG 的智能片段发现系统

## 概述

GifAgent 是一个运行在本地的影视片段智能管理工具。它能够自动扫描你收藏的 GIF/视频文件，调用本地大模型分析每一帧的**审美特征**和**情感表达**，建立向量索引支持相似检索，并在新视频中自动发现符合你偏好的经典片段。

### 核心能力

- **自动打标**：VLM（llava:13b）逐帧分析审美特征 → LLM（Qwen3-14B）综合生成标签
- **向量索引**：基于 Ollama Embedding 的 FAISS 语义向量库，支持文本到 GIF 的跨模态检索
- **RAG 增强**：新视频的场景帧通过 FAISS 检索已有收藏，注入相似上下文提升分析一致性
- **智能采样**：I-frame + 暗帧过滤 + 全片均匀采样，避免盲采浪费
- **断点恢复**：Checkpoint 机制保证长时间 VLM 处理中服务器重启不丢失进度

### 数据流

```
E:\data\originals\（9000+ GIF）
  → SHA256 + pHash 去重 → SQLite 入库
  → ffmpeg GIF 抽帧（6-12 帧/张）
  → llava:13b 逐帧审美分析（~4.7s/帧）
  → Qwen3-14B 综合标注（tags + emotional_core + aesthetic_notes）
  → nomic-embed-text 向量化 → FAISS 索引
  → 新视频：I-frame → VLM 裸分析 → 每帧 caption 的 FAISS 检索 → RAG 增强合成
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
| GPU | 16GB+ VRAM | 需交替运行 llava:13b (~8GB) 和 Qwen3-14B (~9GB) |

### 必需模型

```bash
ollama pull llava:13b                    # VLM 视觉分析
ollama pull hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M  # LLM 文本合成
ollama pull nomic-embed-text             # Embedding 向量化
```

### 安装

```bash
git clone https://github.com/<your-username>/GifAgent.git
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
  source_dir: "E:/data/originals"   # 媒体源目录

vlm:
  model: "llava:13b"                # 视觉模型

llm:
  model: "hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M"  # 文本模型

embedding:
  text_model: "nomic-embed-text:latest"  # Embedding 模型
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
# 完整流水线（分阶段执行）：
# Phase 1: 抽帧
uv run python scripts/cluster_and_select.py

# Phase 2: VLM + LLM 标注（耗时最长，支持断点恢复）
uv run python scripts/process_representatives.py

# 查看进度
uv run python scripts/process_representatives.py --status

# Phase 3: 标签继承 + FAISS 索引
uv run python scripts/inherit_and_index.py
```

### 第三步：新视频场景发现（RAG 增强）

```bash
uv run python scripts/test_video_rag_v2.py
```

输出：
- `data/test_jur639_rag_v2_result.json`：完整的分析结果
- `data/exports/rag_v2_test/`：导出的候选 GIF 片段

### 启动 FastAPI 服务

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### 启动 Gradio 审核界面

```bash
uv run python app/ui/review.py
# 浏览器打开 http://127.0.0.1:7860
# 快捷键：A 喜欢 | S 一般 | D 不喜欢 | E 编辑标签
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

## 项目结构

```
GifAgent/
├── app/
│   ├── main.py                  # FastAPI 应用（10 个端点）
│   ├── config.py                # YAML 配置加载
│   ├── db.py                    # SQLite 连接 + 迁移 + Checkpoint
│   ├── services/
│   │   ├── scanner.py           # 文件扫描、SHA256/pHash 去重
│   │   ├── preprocess.py        # ffmpeg GIF 抽帧、缩略图
│   │   ├── scheduler.py         # Ollama 模型切换调度
│   │   ├── vision.py            # llava:13b 逐帧审美分析
│   │   ├── llm.py               # LLM 综合标注（含 think 标签处理）
│   │   ├── embedding.py         # Ollama Embedding API（文本优先）
│   │   ├── indexer.py           # FAISS 索引管理（余弦相似）
│   │   └── scorer.py            # 四因子偏好评分
│   └── ui/
│       └── review.py            # Gradio 审核界面（A/S/D 快捷键）
├── configs/
│   └── models.yaml              # 主配置：模型、路径、阈值
├── scripts/
│   ├── index_library.py         # 全量索引流水线（5 阶段）
│   ├── cluster_and_select.py    # pHash 聚类 + 代表选取
│   ├── process_representatives.py # VLM+LLM 标注（带 checkpoint）
│   ├── vlm_quick_200.py         # VLM 批量处理（200 帧/次）
│   ├── llm_synth_resume.py      # LLM 合成断点恢复
│   ├── inherit_and_index.py     # 簇内标签继承 + FAISS 索引
│   └── test_video_rag_v2.py    # RAG v2 测试（两阶段流水线）
├── data/                        # 运行时数据（gitignore）
│   ├── library.db               # SQLite 数据库
│   ├── faiss/                   # FAISS 向量索引
│   ├── frames/                  # 抽帧 JPEG
│   └── exports/                 # GIF 导出
├── pyproject.toml               # uv 项目配置
├── 初版构建方案.md               # 设计文档
├── 使用手册.md                   # 详细使用手册（663 行）
└── README.md
```

---

## 技术要点

### RAG 两阶段流水线

v2 版本采用两阶段设计避免 RAG 回音壁效应：

1. **Pass 1**：VLM 裸分析（无 RAG 上下文）→ 生成每帧的 caption + emotional_core
2. **Pass 2**：每帧 caption → nomic-embed-text 向量化 → FAISS 检索 top-5 相似收藏 → 作为 RAG 上下文注入 LLM 综合

这确保了每帧的 RAG 上下文是基于**实际视觉内容**匹配的，而不是固定查询词的重复结果。

### 断点恢复机制

`processing_checkpoint` 表记录每个阶段的进度：
- `phase`：当前阶段标识（如 `vlm_llm_representatives`）
- `last_media_id`：上次处理到的媒体 ID
- `batch_index`：批次索引
- `extra_json`：附加元数据（如批次耗时）

脚本通过 `save_checkpoint()` 和 `load_checkpoint()` 实现自动恢复。

### LLM 思考模式处理

Qwen3-14B 使用 `<think>...</think>` 标签包裹推理过程。`_parse_json_response()` 自动剥离 think 标签，提取实际 JSON 输出。

### VLM 输出清洗

`analyze_frame()` 包含后处理逻辑：
- emotional_core 管道分隔符拆分 → 取第一个有效值
- caption 模板文本检测 → 置空标记

---

## License

MIT
