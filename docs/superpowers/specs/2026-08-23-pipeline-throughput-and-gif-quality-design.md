# 流水线吞吐与 GIF 导出质量改造设计

> Implementation status (2026-08-23): **设计阶段，尚未实现**。本文只描述目标状态、
> 约束与验收口径，不代表任何代码已经修改。落地按
> `docs/superpowers/plans/2026-08-23-pipeline-throughput-and-gif-quality.md` 分期执行。

## 1. 文档状态

- 日期：2026-08-23
- 状态：设计完成，实现待定
- 适用对象：自适应 GIF 抽取链路（Direct 与 Staged 两条路径）、任务引擎调度
- 目标版本：第一版吞吐改造 + GIF 编码正确性修复 + 打分确定性
- 依赖设计：
  - `2026-07-29-transition-aware-gif-extraction-design.md`
  - `2026-07-29-action-completeness-design.md`
  - `2026-08-09-moe-aesthetic-quality-and-repairability-design.md`
- 硬件前提：GPU 显存 16GB；VLM 为 `Qwen3.6-35B-A3B-Uncensored:IQ2_M`（约 12GB）

## 2. 背景与问题

当前自适应链路已经具备粗采样、VLM 打分、细采样、区域 merge、动作完整性、
转场保护、Quality MoE 和偏好重排等完整能力，但存在两类互相独立的系统性问题。

### 2.1 吞吐问题

以 90 分钟视频、`sample_interval=7` 为例，单视频需要：

| 环节 | 现状实现 | 量级 |
|------|----------|------|
| 粗采样抽帧 | 每个时间戳独立 `subprocess.run(["ffmpeg", ...])`，串行 | 约 771 次进程启动 |
| 粗采样打分 | `_score_vlm_frame` 串行 HTTP，无并发 | 约 771 次调用 |
| 细采样 | 同上，`max_refine_frames` 封顶 | ≤ 120 次调用 |
| 阶段调度 | `TaskWorker.run_once()` 一次只认领一个 stage | 8 + N 个子进程 |
| `gif_clip` | 每 clip 一个子进程（含 `init_db()`、manifest SHA 校验、ffprobe、2 次 ffmpeg） | N ≤ 100 |

四个可量化的浪费：

1. **打分输出被大量丢弃。** 粗采样阶段唯一被消费的字段是 `gif_worthiness` 与
   `sex_act`，但 `SCORE_PROMPT_ADULT`（`scripts/test_video_adaptive.py:109-132`）
   同时要求 `caption`、`emotional_core`、两条 `aesthetic_notes` 和 `reason`，
   每帧约 150–200 个输出 token。对一个解码速度受限的本地模型，这意味着约
   85% 的 GPU 时间用于生成后续会被丢弃的文本。

2. **抽帧完全串行且不占用 GPU。** 抽帧只消耗磁盘 I/O 与 CPU，与 VLM 打分
   互不冲突，却排在同一条串行链路上。

3. **阶段级并发缺失。** `claim_stage`（`app/task_engine/repository.py:262-268`）
   一次只返回一行，导致 `gif_clip`（纯 CPU/ffmpeg）运行时 GPU 完全空转，
   反之亦然。

4. **模型生命周期开销。** `wait_model`（`scripts/test_video_adaptive.py:630-675`）
   的探活分支 POST `/api/generate` 时未设 `num_predict`，模型已加载时会对
   `"ping"` 生成一段完整回复才返回；`_score_vlm_frame` 未发送 `keep_alive`，
   阶段子进程之间存在 12GB 权重被逐出重载的风险。

### 2.2 质量问题

1. **GIF 帧率不可整除。** GIF 的 Graphic Control Extension 延迟字段以 1/100 秒
   为单位，FFmpeg 的 GIF 复用器时间基同为 1/100。`gif_fps: 24` 对应
   100/24 = 4.1667 厘秒，取整后产生 4/5 厘秒交替的帧间隔，所有导出 GIF 都带有
   固有的节奏抖动与轻微速度偏差。25 fps（4 厘秒）或 20 fps（5 厘秒）可整除。

2. **调色板参数使用默认值。** `build_ffmpeg_filter`
   （`app/quality_moe/repair.py:629-639`）之后拼接的是裸 `palettegen` 与裸
   `paletteuse`。默认 `stats_mode=full` 让静止背景与运动主体等权竞争 256 色配额；
   默认 `diff_mode=none` 让抖动噪点在静止区域逐帧变化，产生"沙沙爬动"观感并
   显著增大体积。

3. **单帧证据生成长片段。** `build_export_window`
   （`app/services/gif_windows.py:52-53`）对 `frame_count == 1` 的候选使用
   `min_duration + (max_duration - min_duration) * worthiness`。在
   `max_duration: 20` 下，一个孤立高分帧会生成约 19 秒 GIF，而我们对该窗口内
   其余时间发生了什么没有任何证据。多帧分支（`:46-51`）使用
   `min(max_duration, span + 3.0)`，是有证据支撑的，不在此列。

4. **时间轴锁在整秒网格上。** 采样在整秒、merge 端点是整秒、导出窗口按
   `anchor - 0.4 * duration` 平移，最终起止点几乎不可能落在动作的自然边界，
   导致片段常从动作中途开始、在动作中途结束。

5. **打分不可复现。** `vlm_temperature: 0.25`、`vlm_top_p: 0.90`、
   `vlm_top_k: 40` 使同一帧重跑得到不同分数，而
   `worthiness_threshold=0.62`、`refine_threshold=0.70`、
   `merge_score_threshold=0.58` 全部建立在该分数之上。这同时使 A/B 对比、
   Quality Lab 冠军晋级和阈值调参失去可比基线。

6. **已有校准器未接入。** `app/quality_lab/calibration.py` 的可靠性分箱与
   PAV 保序回归已经实现并有测试覆盖，但生产打分路径直接使用原始
   `gif_worthiness`，阈值语义随模型/提示词漂移。

## 3. 目标

1. 在不改变候选发现语义的前提下，把单视频端到端耗时降低到当前的 25%–40%。
2. 消除 GIF 编码层面的确定性缺陷（帧率取整、调色板配额、抖动爬动）。
3. 让 VLM 打分在同一输入、同一配置下完全可复现。
4. 把导出时长与其证据强度绑定：长片段必须有多帧证据。
5. 所有新行为由冻结任务快照控制，Direct 与 Staged 两条路径行为一致。
6. 全过程可测量：改造前先建立分阶段耗时基线，改造后用同一基准集对比。
7. 不删除、不覆盖任何历史导出 GIF、任务记录、标注或偏好数据。

## 4. 非目标

第一版不包含以下内容：

- 更换 VLM 模型本身（模型 A/B 作为独立实验项，走 Quality Lab 流程，不在本次
  改造的必经路径上）。
- 改变八阶段任务图：`discover -> sample -> vlm -> refine -> synthesize ->
  rank_dedup -> gif_clip -> materialize` 保持不变。
- 改变 `gif_clip` 的按 clip 扇出契约（每 clip 独立可重试的语义必须保留）。
- 引入新的重量级依赖（不新增 PySceneDetect、光流模型、推理框架）。
- 改变 Quality MoE 的裁决语义、软拒绝门槛或可挽救性边界。
- 改变 Preference Memory 的画像构建、发布门禁或重排公式。
- 用生成式手段修补画面内容。

## 5. 已确认的约束

### 5.1 数据不可变

- 历史 `data/*.db`（`library.db`、`task_state.db`、`quality_lab.db`）、历史导出
  目录、标注、检查点和可写配置在任何测试中都不得被修改。
- 所有测试使用 `tmp_path` 与临时 SQLite。
- **运维警告**：`adaptive.clear_output_dir` 默认为 `true`，重跑同一视频会清空
  该视频的输出目录。验证新配置必须在新建目录或素材副本上进行，不得就地重跑
  历史视频。

### 5.2 冻结快照优先

- 所有新增配置键都必须经由 `extract_config()`
  （`scripts/test_video_adaptive.py:725-822`）进入任务快照。
- 运行时不得读取环境变量来选择行为（与 `score_prompt_mode` 现有约定一致）。
- `POST /api/tasks/jobs/{id}/retry` 不重写 `config_json`，因此已存在的任务在
  重试时自动沿用旧行为。这是本次改造的向后兼容主要保障。

### 5.3 默认值即当前行为

- 每个新增开关的默认值必须复现当前行为，使得"不改 YAML"的用户完全无感。
- 行为变更通过显式修改 `configs/models.yaml` 与
  `configs/models.adult_candidate.yaml` 生效。

### 5.4 显存预算

- 目标机器 16GB，VLM 约 12GB，留出约 4GB。
- 并发打分的 KV cache 增量必须在该预算内；并发度是可配置项而非硬编码。
- 在 16GB 上不再需要为加载 VLM 而强制卸载 `nomic-embed-text`（约 275MB）。

## 6. 现状测量（改造的前置条件）

代码中**当前不存在端到端计时**：`run_pipeline` 没有总计时器，粗/细采样循环只
打印帧计数不打印耗时，只有 `action_pipeline` 记录了 `cv_ms` / `vlm_ms` /
`total_ms`。

因此第一步必须是埋点，否则后续所有优化只能靠估算。需要采集的指标：

| 指标 | 位置 | 用途 |
|------|------|------|
| `extract_ms_total` / `extract_ms_p50` | 抽帧循环 | 验证并行抽帧收益 |
| `vlm_ms_total` / `vlm_ms_p50` / `vlm_calls` | `_score_vlm_frame` 调用点 | 验证两级 prompt 与并发收益 |
| `vlm_output_tokens_total` | Ollama 响应的 `eval_count` | 直接量化"被丢弃的 token" |
| `stage_wall_ms` | 每个 stage 子进程 | 验证阶段并发收益 |
| `model_wait_ms` | `wait_model` / `stop_model` | 验证生命周期修复收益 |

指标写入各阶段 manifest 的 `timings` 字段与最终 `result_*.json`，不新建数据库表。

## 7. 质量改造设计

### 7.1 GIF 编码正确性

新增三个冻结配置键，默认值保持当前行为：

```yaml
adaptive:
  gif_fps: 25                      # 由 24 改为可整除 100 的帧率
  gif_palette_stats_mode: diff     # full（当前默认） | diff
  gif_dither: sierra2_4a           # none | bayer | floyd_steinberg | sierra2_4a
  gif_diff_mode: rectangle         # none（当前默认） | rectangle
```

`build_ffmpeg_filter` 保持只负责 `fps` / 修复配方 / `scale` 前缀不变；新增一个
`build_palette_filters()` 返回 `(palettegen_args, paletteuse_args)`，由 Direct 与
Staged 两处导出点共用，保证两条路径生成完全相同的命令。

参数取值必须白名单校验后再拼进命令行，禁止把配置字符串直接插入 filtergraph。

### 7.2 导出时长与证据强度绑定

```yaml
adaptive:
  max_duration: 8                  # 由 20 收紧
  single_frame_max_duration_s: 5   # 新增：单帧证据的独立上限
```

`build_export_window()` 增加一个 `single_frame_max_duration_s` 参数：
`frame_count == 1` 时用它替代 `max_duration` 参与插值；多帧路径的
`min(max_duration, span + 3.0)` 逻辑不变。参数缺省时回退到 `max_duration`，
保持现有调用点与测试的行为。

### 7.3 亚秒级边界吸附

```yaml
adaptive:
  boundary_snap_enabled: false     # 第一版默认关闭，A/B 通过后再开
  boundary_snap_radius_s: 0.6
```

在转场保护之后、去重之前，对每个候选窗口的起止点做一次局部搜索：复用
`app/services/temporal_evidence.py` 的 `TemporalEvidenceCache`（已支持按区间
批量 ffmpeg 解码，`transition_scan_fps=8`），在 ±`boundary_snap_radius_s` 内寻找
帧间差分的局部极小点作为新起止点。

硬约束：

- 吸附不得跨越 `transition_guard` 已确认的边界，也不得侵入
  `transition_boundary_margin_s` 安全区。
- 吸附后时长仍须满足 `transition_min_duration_s` 与 `max_duration`。
- 已标记 `guarded_export_window=True` 的动作窗口不参与吸附（动作完整性优先）。
- 吸附失败或证据不足时保持原窗口，不得丢弃候选。

### 7.4 打分确定性

```yaml
adaptive:
  vlm_temperature: 0.0
  vlm_top_p: 1.0
  vlm_top_k: 1
  vlm_seed: 20260823               # 新增，进入快照与 config_hash
```

打分是回归任务而非生成任务，采样噪声只带来不可复现性。`vlm_seed` 进入冻结快照
后，同一 `(视频指纹, 时间戳, 模型, 提示词模式, seed)` 组合的分数可复现，现有的
`scored_checkpoint.json` 恢复机制语义也随之变得严格。

### 7.5 分数校准接入

```yaml
adaptive:
  score_calibration_enabled: false
  score_calibration_path: ""       # 指向冻结的校准器 JSON
```

新增 `scripts/fit_score_calibration.py`：从 `preference_events` 读取有效
like/dislike 标注，与候选对应的原始 `gif_worthiness` 配对，调用
`app/quality_lab/calibration.py` 的 `calibration_curve()` 与
`fit_monotonic_calibrator()`，输出带 `model_id` / `prompt_mode` / `sample_count`
溯源信息的冻结 JSON。

运行时在阈值判断**之前**应用校准，原始分数与校准分数都写入 manifest。校准器的
`model_id` 与 `prompt_mode` 与当前任务快照不匹配时拒绝加载并回退到原始分数，
避免跨模型误用。

## 8. 吞吐改造设计

### 8.1 两级打分提示词（最大单点收益）

```yaml
adaptive:
  score_schema_mode: legacy        # legacy（当前行为） | two_tier
  caption_backfill_max_frames: 150
  vlm_num_predict_score: 48
  vlm_num_predict_caption: 320
```

`two_tier` 模式下：

- **粗采样与细采样**使用精简 schema，只要求
  `{"gif_worthiness": 0.0, "sex_act": 0.0}`（`default` 模式下无 `sex_act`），
  约 20 个输出 token。评分刻度说明保留在提示词中，因为它决定分数分布；被删除的
  只有输出字段。
- **caption 回填**在 merge 产生 clip 之后进行，只对每个 clip 的 `best_frame`
  用完整 schema 重跑一次，数量受 `caption_backfill_max_frames` 约束。

`_score_vlm_frame` 增加 `schema: Literal["score", "full"]` 参数。`score` 模式下
跳过 `parse_vlm_response` 的 caption 质量门禁（无 caption 可门禁），但
`gif_worthiness` 的严格校验（有限、落在 [0,1]、不接受 bool、不回退 0.5）
与三次重试逻辑完全保留。

回填位置：

- **Direct 路径**：`merge_scored_frames_into_clips` 之后、动作/转场处理之前。
- **Staged 路径**：`_stage_refine` 的尾部，而**不是** `_stage_synthesize`。

选择 `_stage_refine` 的理由是产物血缘约束。`STAGE_INPUT_KINDS["synthesize"]`
只包含 `refine_manifest`；`_stage_refine` 的产物只有 `refine_manifest`，它抽取的
细采样 JPEG 从未注册为 artifact；`_stage_synthesize(work_dir, cfg, inputs)` 也没有
`config_data` 参数因而拿不到 VLM 运行时配置。若在 synthesize 里回填，就必须跨
stage work_dir 读取未经 SHA 校验的文件，违反"阶段只读已校验上游产物"的既有不变量。

`_stage_refine` 则同时具备三个条件：已持有 `config_data` 与 VLM 运行时、已调用
`wait_model`、帧文件就在本阶段 work_dir 内。

实现方式：refine 在写出 manifest 之前，对**即将写出的** `scored_frames` 调用
`merge_scored_frames_into_clips`（与 synthesize 完全相同的纯函数与冻结 merge 配置），
得到与 synthesize 将要计算的完全一致的分组，只对各组 `best_frame` 回填 caption，
再把 caption 写回 manifest 的 `frames` 条目。

这样 `_stage_synthesize` **无需任何修改**——它已经在
`clips_data.append({... "caption": sf.get("caption", "")})` 处读取该字段。

回填失败必须**非致命**：clip 保留空 caption，embedding 去重跳过该 clip，
LLM 合成照常降级。这与现有"LLM 失败不阻断导出"的约定一致。若 refine 的预演
merge 与 synthesize 的最终 merge 因任何原因不一致，后果也只是某个 `best_frame`
没有 caption，落入同一条非致命路径。

下游消费者兼容性：

| 消费者 | 依赖字段 | two_tier 下是否满足 |
|--------|----------|---------------------|
| 阈值/merge/去重排序 | `gif_worthiness`、`sex_act` | 粗采样即产出 |
| embedding 去重 | clip caption | 回填产出 |
| LLM 合成 | clip caption / notes | 回填产出 |
| 9 宫格缩略图 | 分数 + pHash | 不依赖 caption |
| Quality MoE | 像素证据 + 分数 | 不依赖 caption |

### 8.2 抽帧并行化

```yaml
adaptive:
  frame_extract_workers: 1         # 1 = 当前串行行为
```

六处 `subprocess.run(["ffmpeg", ..., "-vframes", "1", ...])` 调用点收敛到一个
`app/services/frame_extract.py` 中的 `extract_frames()`，内部用
`ThreadPoolExecutor` 按 `frame_extract_workers` 并发。抽帧只占 I/O 与 CPU，
与 GPU 打分不冲突。

同时补齐当前缺失的 ffmpeg 参数：`-an -sn`（不解复用音频与字幕流）、
`-q:v 3`（当前未指定 JPEG 质量，依赖编码器默认值）。

`extract_frames()` 必须保持结果顺序确定、逐帧错误可归因（哪个时间戳失败、
ffmpeg 退出码是多少），与现有 `refine_extraction_failed` 计数语义一致。

### 8.3 VLM 打分并发

```yaml
adaptive:
  vlm_score_workers: 1             # 1 = 当前串行行为
```

打分循环改为 `ThreadPoolExecutor(max_workers=vlm_score_workers)`，配合 Ollama
侧的 `OLLAMA_NUM_PARALLEL`。

- 结果按时间戳排序后写入 manifest，保证 manifest 字节级可复现。
- 失败计数、重试与错误分类语义不变。
- 16GB 显存下建议从 `2` 起步实测，`3` 为上限候选；配置必须允许回落到 `1`。
- 并发只在同一阶段内部生效，不改变阶段间的顺序契约。

### 8.4 阶段类别并发

```yaml
task_engine:
  gpu_stage_workers: 1
  cpu_stage_workers: 1             # 1 = 当前行为
```

阶段分类：

| 类别 | 阶段 |
|------|------|
| GPU | `vlm`、`refine`、`rank_dedup` |
| CPU | `discover`、`sample`、`synthesize`、`gif_clip`、`materialize` |

`synthesize` 归入 CPU 类：caption 回填在 `_stage_refine` 完成（见 8.1），
synthesize 只做纯函数 merge 与云端 LLM 调用，属网络 I/O 而非 GPU 占用。
`rank_dedup` 归入 GPU 类，因为它会调用 embedding 并在动作复核时惰性使用 VLM。

`claim_stage()` 增加可选的 `stage_names` 过滤参数（默认 `None` = 当前行为），
`scripts/task_worker.py` 与 `app/ui/launcher.py` 按配置启动 1 个 GPU worker
线程与 K 个 CPU worker 线程。

这条改动**不修改**八阶段任务图，也**不修改** `gif_clip` 的按 clip 扇出契约——
并行度来自多个 worker 各自认领不同的 `gif_clip` 行，每个 clip 仍然独立可重试。

必须验证的并发安全点：

1. `claim_stage` 的 `BEGIN IMMEDIATE` 事务在多 worker 下不会重复认领同一行。
2. `orchestrator.advance_job()` 被并发调用时，`ensure_stage()` 的幂等键
   （如 `from:rank_dedup:clip:{cid}`）不产生重复阶段行。
3. `materialize` 只在所有 `gif_clip` 到达终态后创建，且并发下只创建一次。
4. 心跳线程使用独立 SQLite 连接，多 worker 下续租不互相干扰。
5. SQLite `busy_timeout` 足以覆盖多 worker 争用。

在这五点全部有回归测试覆盖之前，`cpu_stage_workers` 必须保持默认 `1`。

### 8.5 模型生命周期修复

三处独立修复：

1. `wait_model` 探活分支补 `{"num_predict": 1}`，与其下方触发加载的分支
   （`scripts/test_video_adaptive.py:661-670`）保持一致。
2. `_score_vlm_frame` 的请求体加入 `"keep_alive": vlm_keep_alive`
   （新增配置键，默认 `"30m"`，与 embedding 侧现有约定对齐）。
3. `_stage_vlm` 中为腾显存而卸载 `nomic-embed-text` 的逻辑改为按预算判断：
   新增 `vlm.free_vram_before_load: false`（16GB 下无需卸载），
   `true` 时保持当前行为。

`stop_model` 的三轮重试（每轮 `sleep(5)` + `sleep(5..10)`）改为先查 `/api/ps`，
确认目标模型未加载时立即返回，避免无谓等待。

### 8.6 明确不做的项

- **`gif_clip` 批量化**（多 clip 合并进一个 stage）：会改变扇出契约与重试粒度，
  收益已被 8.4 的 CPU 并发覆盖大部分。列为后续可选项。
- **单趟 `fps=1/N` 解码替代逐帧 seek**：对 90 分钟 1080p 素材需要全量解码约
  13 万帧，相对 771 次 keyframe seek 没有稳定优势，且在 4K 素材上更差。
- **降低送入 VLM 的图像分辨率**（当前 `scale=640:-1`）：属于质量变量，
  应走 Quality Lab A/B 而非并入性能改造。

## 9. 配置与快照契约

新增配置键汇总，全部经 `extract_config()` 进入冻结快照：

| 键 | 默认值 | 默认是否等价当前行为 |
|----|--------|----------------------|
| `adaptive.gif_palette_stats_mode` | `full` | 是 |
| `adaptive.gif_dither` | `sierra2_4a` | 是 |
| `adaptive.gif_diff_mode` | `none` | 是 |
| `adaptive.single_frame_max_duration_s` | 回退到 `max_duration` | 是 |
| `adaptive.vlm_seed` | `null`（不发送 seed） | 是 |
| `adaptive.vlm_keep_alive` | `"30m"` | 否（行为改善，无语义变化） |
| `adaptive.vlm_num_predict_score` | `null` | 是 |
| `adaptive.vlm_num_predict_caption` | `null` | 是 |
| `adaptive.score_schema_mode` | `legacy` | 是 |
| `adaptive.caption_backfill_max_frames` | `150` | 仅 `two_tier` 下生效 |
| `adaptive.frame_extract_workers` | `1` | 是 |
| `adaptive.vlm_score_workers` | `1` | 是 |
| `adaptive.boundary_snap_enabled` | `false` | 是 |
| `adaptive.boundary_snap_radius_s` | `0.6` | 仅开启时生效 |
| `adaptive.score_calibration_enabled` | `false` | 是 |
| `adaptive.score_calibration_path` | `""` | 是 |
| `vlm.free_vram_before_load` | `true` | 是 |
| `task_engine.gpu_stage_workers` | `1` | 是 |
| `task_engine.cpu_stage_workers` | `1` | 是 |

Settings 页只暴露用户需要日常调整的子集：`gif_fps`、`max_duration`、
`single_frame_max_duration_s`、`score_schema_mode`、`frame_extract_workers`、
`vlm_score_workers`。其余保留在 YAML 与冻结快照中，与转场保护现有做法一致。

所有键必须同时出现在 `configs/models.yaml` 与
`configs/models.adult_candidate.yaml`，且新暴露的 UI 字段必须在
`tests/test_config_help_annotations.py` 中有中文帮助文案。

**已发现的既有偏差（本次一并修正）**：`README.md` 与 `Agent.md` 都称
`configs/models.adult_candidate.yaml` 是主配置的镜像，但它实际已经落后：
`worthiness_threshold: 0.42`（主配置 0.62）、`refine_threshold: 0.55`（0.70）、
`merge_score_threshold: 0.50`（0.58）、`vlm_temperature: 0.50`（0.25）、
缺少 `max_refine_frames`（因而回落到 `extract_config()` 的 120）。
本次改造不得假定两份 YAML 已经同步，每个键都要分别显式写入；文档描述也应
从"镜像"改为"预设"。

`CONFIG_FIELD_KEYS` / `CONFIG_FIELD_HELP` 的权威定义在
`app/ui/tabs/settings.py`，`app/ui/candidate_review.py` 只是再导出，但
`app/ui/legacy_candidate_review.py` 另有一份独立副本，新增 UI 字段时两处都要改。

## 10. 兼容性与数据安全

1. **历史任务自动兼容。** Retry 不重写 `config_json`，历史任务重试时读到的是
   旧快照，行为不变。
2. **默认值即旧行为。** 未修改 YAML 的部署升级后行为完全不变。
3. **`quality_moe_config_hash` 与 `action_config_hash` 语义不变。** 新增的性能类
   键（`*_workers`、`num_predict`、`keep_alive`）不参与质量哈希，避免纯性能改动
   使历史 Quality Lab 结果失效；影响输出的键（`gif_*`、`score_schema_mode`、
   `vlm_seed`、`single_frame_max_duration_s`、`boundary_snap_*`、
   `score_calibration_*`）必须参与。
4. **测试隔离。** 所有新测试使用 `tmp_path`；发布门禁前后用
   `Get-Item data/*.db` 核对历史数据库未被改动。
5. **打包同步。** 若新增模块（`app/services/frame_extract.py` 等）未被
   `build_exe.spec` 的 `collect_submodules("app")` 收集，必须补进 hidden imports，
   并同步 `tests/task_engine/test_packaged_stage_imports.py`。

## 11. 验收标准

### 11.1 性能

在冻结的 3 视频基准集上，对比改造前后（同一素材副本，非就地重跑）：

| 项 | 目标 |
|----|------|
| 单视频端到端墙钟时间 | ≤ 基线的 40% |
| `vlm` + `refine` 阶段合计耗时 | ≤ 基线的 30% |
| VLM 输出 token 总数 | ≤ 基线的 25% |
| 抽帧阶段耗时 | ≤ 基线的 35%（`frame_extract_workers=6`） |
| `gif_clip` 全部 clip 合计耗时 | ≤ 基线的 45%（`cpu_stage_workers=3`） |

### 11.2 质量

| 项 | 目标 |
|----|------|
| 导出 GIF 的帧延迟 | 全部为同一整数厘秒值 |
| 平均 GIF 体积 | 相对基线下降（同分辨率同时长） |
| 打分可复现性 | 同一帧两次运行 `gif_worthiness` 完全相同 |
| 盲测 A/B | 新配置对旧配置不劣（`both_bad` 与 `tie` 之外的胜率 ≥ 50%） |
| `export_integrity` | ≥ 0.9 |

### 11.3 正确性

- `two_tier` 与 `legacy` 在同一素材上产出的 clip 时间区间集合一致
  （caption 内容允许不同，时间边界不允许）。
- Direct 与 Staged 在同一冻结配置下产出相同的导出窗口。
- 现有生产发布门禁四个 E2E 场景（成功链路、VLM 宕机、非法载荷、
  合法零 clip）全部通过。

### 11.4 回归门禁

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine/test_full_production_stage_chain.py -s
.\.venv\Scripts\python.exe -m pytest -q tests/task_engine tests/quality_lab
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

## 12. 风险与回滚

| 风险 | 影响 | 缓解 |
|------|------|------|
| 精简 schema 改变分数分布 | 阈值失准，候选数量剧变 | 保留完整刻度说明；上线前在基准集上对比分数直方图；`score_schema_mode` 可一键回落 `legacy` |
| VLM 并发导致显存溢出到 CPU | 反而变慢 | `vlm_score_workers` 可配置；埋点直接暴露 p50 变化；默认 `1` |
| 多 worker 破坏单写入者不变量 | 重复阶段、租约错乱、数据损坏 | 五项并发安全点全部要有回归测试；未覆盖前 `cpu_stage_workers` 保持 `1` |
| 边界吸附把动作切碎 | 观感变差 | 默认关闭；不吸附 `guarded_export_window`；不跨转场边界；需盲测 A/B 通过 |
| `max_duration` 收紧丢失长镜头 | 部分素材候选变短 | 属预期行为变更；由盲测 A/B 判定；可单独回调 |
| 打包遗漏新模块 | 冻结 EXE 阶段子进程 `ModuleNotFoundError` | `test_packaged_stage_imports.py` 同步更新 |

回滚方式：所有行为变更都是配置键，把 YAML 恢复为默认值即可回到当前行为，
无需回滚代码，也不需要重跑或删除任何历史数据。

## 13. 分期路线

| 阶段 | 内容 | 可独立交付 |
|------|------|-----------|
| A | 耗时埋点 + GIF 编码修正 + 时长收紧 + 打分确定性 | 是 |
| B | 生命周期修复 + 抽帧并行 + 两级 prompt | 是 |
| C | VLM 并发 + 阶段类别并发 | 是 |
| D | 边界吸附 + 分数校准 + 模型 A/B | 是 |

A 阶段必须先落地：没有基线数据，B/C 的收益无法验证，D 的 A/B 也没有对照。
每个阶段结束后都应保持完整回归门禁通过，可以在任意阶段停止。
