# 转场感知 GIF 截取设计

## 状态

- 日期：2026-07-29
- 状态：已通过分段设计确认，等待规格复核
- 目标验证视频：`C:\Users\sunhao\Desktop\ToWatch\现代爱情故事.1991.BD1080p.国英双语中字.mp4`

## 背景与问题

当前自适应 GIF 链路主要依据离散静帧的 VLM `gif_worthiness` 和时间距离合并候选。`app/services/clip_merge.py` 只检查相邻时间差、分数、最大合并跨度和峰值分数，不检查候选窗口内部的画面连续性。当前默认 `merge_gap=15s`，因此两个不同镜头只要都被打高分，就可能合并为一个候选。

导出阶段还会围绕最佳帧扩张窗口；即使最佳帧本身没有问题，窗口也可能吞入相邻硬切或明显叠化。分阶段 `gif_clip` 路径还需要统一应用 `max_duration`，否则仅修改配置页的最大时长不能保证生产任务实际遵守该限制。

用户需要的行为是：明显硬转场或大幅叠化不得出现在最终 GIF 中；类似缓慢上移、平移、缩放的连续运镜应保留；候选跨越多个镜头时，优先裁剪到最佳镜头，必要时拆成多个独立 GIF；拆分后单段少于 2 秒则丢弃。

## 目标与非目标

### 目标

1. 检测候选窗口内部的硬切、明显叠化和渐隐/渐显边界。
2. 通过全局运动补偿区分连续运镜与镜头切换，保留慢速上移等好片段。
3. 将跨边界候选裁剪或拆分为镜头内片段，任何最终 GIF 不跨越已确认边界。
4. 统一 direct、旧批处理和 task-engine staged 路径的窗口计算与转场处理。
5. 记录可复盘的检测指标、动作和错误原因。
6. 在指定的《现代爱情故事》视频上完成真实管线验证。

### 非目标

- 不重写现有 VLM 内容评分、embedding 去重或 Preference Memory。
- 不把单帧 `scene_type=transition` 的 LLM 输出当作主要判定依据。
- 不在第一版引入 PySceneDetect、深度光流模型或其他重量级依赖。
- 不要求所有轻微镜头变化都被删除；无法确认且未达到硬边界置信度的变化可以保留并降权。

## 用户可见行为与候选生命周期

候选处理顺序固定为：

```text
VLM 帧评分
  -> 时间候选合并
  -> 计算真实导出窗口
  -> transition_guard 检测
  -> 裁剪/拆分/丢弃
  -> 片段级评分与最佳帧选择
  -> embedding/temporal 去重
  -> 全局排序与 max_output
  -> GIF 导出
```

当一个候选跨越多个镜头时：

1. 找到包含原最佳帧的镜头，作为优先片段。
2. 其他满足最小时长的镜头也创建为独立候选。
3. 每个片段的时间两端向内收缩 `transition_boundary_margin_s`，默认 0.25 秒。
4. 每个片段少于 `transition_min_duration_s`，默认 2 秒，直接丢弃。
5. 片段内优先复用已有评分帧；没有评分帧时抽取中点并调用现有 VLM 评分一次。VLM 失败时只丢弃该片段，不使整批任务失败。
6. 拆分片段按普通候选参与后续排序；所有片段合计继续受 `max_output` 限制。

## 共享服务边界

新增 `app/services/transition_guard.py`。服务不负责 VLM、embedding 或 GIF 编码，只负责对一个真实视频时间窗返回镜头边界和清理后的片段。

建议的逻辑接口：

```text
guard_candidate_window(
    video_path,
    original_start_s,
    original_end_s,
    anchor_ts_s,
    config,
) -> TransitionGuardResult
```

`TransitionGuardResult` 至少包含：

- `original_start_s`、`original_end_s`、`anchor_ts_s`；
- `boundaries`：边界时间、类型、置信度和组成指标；
- `segments`：清理后的起止时间、是否包含最佳帧、动作；
- `hard_cut_count`、`soft_transition_count`；
- `motion_type`：`coherent_camera_motion`、`hard_cut`、`dissolve`、`flash_or_exposure`、`unknown`；
- `transition_action`：`keep`、`trim`、`split`、`drop`、`unverified`；
- `guard_error`（可空）。

服务应保持纯函数式边界：输入为视频和窗口，输出为可序列化结果；不得修改源视频，不得删除已有导出文件。

## 检测算法

### 第一层：快速变化筛查

候选窗口按约 8 fps、宽度约 320 px 解码。对相邻帧计算低成本特征：

- HSV 和亮度直方图距离；
- 灰度边缘变化；
- 帧间结构差异。

该层只负责标出疑似变化区间，不单独决定删除。这样可以避免将快速动作、闪光或局部主体移动直接判为硬切。

### 第二层：全局运动补偿

对疑似区间使用 OpenCV 特征点跟踪和 RANSAC 全局仿射模型；必要时使用 ECC 作为回退。记录：

- 匹配内点比例；
- 平移/缩放/旋转参数的连续性；
- 仿射对齐后的残差；
- 对齐前后的直方图和边缘变化。

判定规则：

- 单次大突变、匹配内点骤降、补偿后残差持续很高：硬切。
- 连续多个采样点中等变化、无法由同一全局运动解释：叠化或渐隐。
- 全局位移方向稳定、仿射拟合良好、补偿后残差低：连续运镜，允许保留。
- 只有亮度短暂异常、结构仍可对齐且下一帧恢复：闪光/曝光变化，不切断。

初始配置建议使用归一化指标：快速筛查阈值 0.40、硬边界阈值 0.65、连续软变化至少 3 个采样点；阈值必须通过合成测试和指定实片复核校准，并最终记录在任务配置中。阈值不是按单个原始像素差硬编码，避免误杀缓慢上移和快速主体动作。

### 边界与片段生成

确认边界后在边界两侧各留 0.25 秒安全余量。若边界附近仍无法确定最佳安全位置，优先保证片段不跨边界；若安全窗口不足 2 秒，丢弃该片段。

未达到硬边界置信度的变化只产生 `transition_risk` 和排序扣分，不自动切断。已确认的硬切、明显叠化和渐隐必须切断，不能只降低一点分数后继续导出原窗口。

## 两条运行路径的接入

### Task-engine staged

生产阶段仍保持：

```text
discover -> sample -> vlm -> refine -> synthesize -> rank_dedup -> gif_clip -> materialize
```

在 `rank_dedup` 生成 clip ID、执行 embedding/temporal 去重之前调用 `transition_guard`。`rank_dedup` 必须接收源视频路径，先更新清理后的 `start_ts/end_ts`、片段最佳帧和评分，再生成稳定 clip ID。这样 `gif_clip` fan-out 和文件名会对应真实清理窗口。

`gif_clip` 仍只负责导出单个已通过的片段，不在该阶段首次发现转场后抛错；否则会把可裁剪的质量问题变成整条任务的部分失败。

### Direct、旧批处理与独立脚本

在 direct 路径计算真实导出窗口后、embedding/temporal 去重和 FFmpeg 导出前调用同一服务。当前 direct 多帧和单帧窗口计算逻辑不同，必须先抽取共享的窗口计算函数，并在两条路径统一应用 `max_duration`、最小时长和边界安全余量。

## 配置、快照与 UI

`configs/models.yaml` 新增：

```yaml
adaptive:
  transition_guard_enabled: true
  transition_min_duration_s: 2.0
  transition_boundary_margin_s: 0.25
  transition_scan_fps: 8
  transition_scan_width: 320
  transition_motion_compensation: true
  transition_hard_threshold: 0.65
  transition_soft_threshold: 0.40
  transition_soft_run_frames: 3
  transition_rescore_split_segments: true
```

前三个字段在设置页提供中文 `?` 帮助；其余字段作为高级配置保留在 YAML 中，避免普通用户调坏算法。Task API 深合并后将完整配置冻结到任务快照和 config hash；禁止使用未冻结的环境变量决定转场行为。

## 错误处理与可观测性

- 单候选解码失败：重试一次；仍失败则跳过候选并记录 `unverified`/错误详情。
- VLM 补评分失败：跳过没有已有评分帧的拆分片段；已有评分片段继续处理。
- 所有候选都被跳过：任务可完成，但视频结果标记 `needs_attention` 并保留失败统计。
- 源视频不可读或 ffmpeg/解码器故障：按现有任务错误分类上报，不删除源数据或历史候选。

`adaptive_test_result.json`、阶段 manifest 和候选记录新增转场统计：

- 输入候选数、检测候选数、清理片段数；
- 确认硬切数、软转场数、闪光数；
- trim/split/drop/unverified 数量；
- 清理前后窗口和最终时长；
- motion type、置信度、算法版本和配置 hash。

## 测试与验收

### 单元测试

新增 `tests/test_transition_guard.py`，使用可重复的合成视频覆盖：

1. 静止/轻微噪声：不得产生边界。
2. 连续向上平移：必须保留为 `coherent_camera_motion`。
3. 两个完全不同画面硬切：在相邻帧附近检出并拆分。
4. 多帧渐隐/渐显：检出软边界并拆分。
5. 单帧闪光/曝光突变：不得误切。
6. 固定背景上的快速主体移动：不得仅因局部运动误切。

现有 `tests/test_clip_merge.py` 增加 shot boundary 不得跨界合并、拆分片段最小时长和清理窗口测试；`tests/test_adaptive_config.py`、配置帮助测试和 task API 测试覆盖新增字段及快照冻结。

### 管线测试

task-engine 生产测试和 direct 测试分别验证：

- 清理发生在去重、clip ID 和 GIF fan-out 之前；
- `max_duration` 和最小时长都生效；
- direct/staged 对相同窗口得到一致清理结果；
- 拆分后可从后续候选补足输出，且总数不超过 `max_output`；
- 转场 guard 单候选失败不会使整批任务崩溃；
- manifest 中包含清理理由和统计。

### 指定实片验收

对以下文件运行完整流程：

`C:\Users\sunhao\Desktop\ToWatch\现代爱情故事.1991.BD1080p.国英双语中字.mp4`

验收证据包括：

- 运行命令、配置快照、阶段日志和结果 JSON；
- 处理前后候选数、硬切/软转场数、trim/split/drop 统计；
- 所有最终 GIF 的清理窗口和边界检查结果；
- 抽查硬转场附近的 GIF，确认明显跨切镜头现象缓解；
- 抽查慢速上移/平移等高分 GIF，确认没有被系统性删除；
- 最终 GIF 时长均不超过配置的 `max_duration`，有效拆分片段均不少于 2 秒。

## 版本与回滚

转场算法、配置和输出 manifest 使用独立 schema/version 字段。`transition_guard_enabled=false` 可回退到旧候选行为，但默认开启。回滚只影响新运行，不删除历史 GIF、数据库记录或已有结果文件。
