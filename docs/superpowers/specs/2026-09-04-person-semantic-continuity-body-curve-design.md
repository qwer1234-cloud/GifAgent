# 人物语义动作连续性与身体曲线评估设计

日期：2026-09-04

状态：待用户书面复核

范围：新增素材的强制连续性门禁、历史 GIF 的有限回刷、身体曲线偏好评分

## 1. 背景

GifAgent 当前已经具备转场检测、基于运动残差的动作边界分析、稀疏接触图 VLM 复核、循环评分和 Quality MoE。现有实现能够降低跨镜头、明显跳变和不完整窗口的概率，但不能对“同一人物的语义动作是否自然连贯”给出强保证，主要原因是：

- 没有稳定的人体检测、主角跟踪和身份连续性证据；
- 没有逐帧姿态关键点、人体轮廓和接触关系；
- 当前动作 VLM 只查看 6–8 帧灰度接触图；
- 低置信度或组件失败时仍可能退回固定窗口；
- `action_completeness_score` 不能证明真实人体运动、接触语义和动作阶段均连续。

本设计增加一条 fail-closed 的人物连续性评估链。它优先降低错误自动放行，而不是维持现有产量。

## 2. 已确认的产品边界

### 2.1 强保证的工程定义

“强保证”不是宣称模型永远正确，而是以下可审计契约：

> 同一镜头内持续锁定同一主角；姿态、人体轮廓、接触关系和运动方向不能出现无法解释的突跳；截取窗口必须覆盖完整动作单元。只有所有必需证据均可用且通过时才允许自动导出。证据缺失、组件失败或结论冲突时，拒绝自动导出并转人工复核。

强保证只适用于能够访问原视频及候选窗口前后上下文的新增素材。仅有成品 GIF 的历史内容不能证明 GIF 外部的起势、收势或隐藏转场，因此不能获得等价的完整自动通过状态。

### 2.2 身体曲线的两层含义

身体曲线必须拆成彼此隔离的两层：

1. **解剖和轮廓时序稳定性**属于硬门。它检查人体拓扑、比例、轮廓和运动是否连续，不评价人物本身是否“好看”。
2. **曲线审美偏好**属于软评分。它学习用户对姿态舒展度、轮廓可读性、构图和视觉节奏的偏好，只影响已经通过硬门的候选排序。

软评分不得覆盖身份切换、动作不完整、解剖异常或证据不足。

### 2.3 主体范围

- 主角接受完整的人体检测、身份跟踪、姿态、轮廓、动作阶段和曲线评估。
- 其他人物至少接受稳定跟踪以及与主角的相对位置、接触建立、维持和解除关系评估。
- 无法稳定确定主角时，结果为 `REVIEW_REQUIRED`，不得依据画面中心或面积猜测后自动放行。

## 3. 目标与非目标

### 3.1 目标

1. 在 GIF 编码前阻断人物身份跳变、人体结构突变、不可解释的轮廓跳变和语义动作断裂。
2. 对新增素材验证候选窗口是否包含完整动作单元。
3. 所有自动放行结论都具备可追溯的输入、模型、配置、逐项证据和原因码。
4. 组件不可用时 fail closed，不再静默回退到固定窗口自动导出。
5. 为身体曲线审美建立独立、可关闭、可评估的 Preference Memory 特征通道。
6. 对历史 GIF 做可恢复的内部连续性回刷，同时诚实标记无法验证的外部上下文。
7. 允许 GLM-5.3-Flash 在 Harness 中辅助开发、标注和离线复核，但生产流水线不依赖 Coding Plan。

### 3.2 非目标

- 不把审美评分包装成客观人体价值判断。
- 不使用单个 VLM 或单一总分作为强门禁。
- 不为历史 GIF 推断不存在的原视频上下文。
- 不自动删除、覆盖、取消收藏或下架历史 GIF。
- 不把 Harness 包装成隐藏 API 或无人值守的生产模型通道。
- 不在首版训练端到端视频动作基础模型。
- 不在本设计中绑定单一第三方模型品牌。后端是否拥有自动放行资格由验证集结果决定。

## 4. 总体方案

采用“确定性视觉证据 + 时序关系分析 + 本地 VLM 语义复核 + fail-closed 策略”的分层架构。

```text
候选时间窗与前后上下文
        │
        ▼
Transition Guard
        │
        ▼
Action Boundary 产生候选窗口
        │
        ▼
Person Evidence Extractor
        │
        ├── 人体检测与主角跟踪
        ├── 姿态关键点
        ├── 人体分割轮廓
        └── 接触与相对位置证据
        │
        ▼
Body Geometry + Action Semantic Analyzer
        │
        ├── 硬门证据
        └── 曲线审美软特征
        │
        ▼
本地 VLM 语义复核
        │
        ▼
Continuity Gate
        │
        ├── AUTO_PASS ───────► Quality MoE ─► Preference 排序 ─► GIF 导出
        ├── REVIEW_REQUIRED ─► 人工复核队列
        └── HARD_REJECT ─────► 拒绝队列
```

Continuity Gate 位于最终 GIF 编码之前。分阶段流水线和 direct 流水线必须调用同一个领域服务，不能各自维护一套判断逻辑。

## 5. 核心组件

### 5.1 `PersonEvidenceExtractor`

职责是从原视频窗口和前后上下文生成标准化的逐帧人体证据，不做最终决策。

输入：

- 源视频路径和 SHA-256；
- 候选 `start_s`、`end_s`、锚点；
- 转场和动作边界证据；
- 冻结的采样、模型和设备配置。

输出：

- 每帧时间戳、实际解码时间和帧哈希；
- 人体框、跟踪 ID、检测置信度；
- 姿态关键点、关键点置信度和可见性；
- 主角人体掩码及轮廓；
- 其他人物、相关物体和接触候选；
- 解码、检测、姿态和分割覆盖率。

首版采用基础 8 FPS、疑难区间最高 16 FPS 的自适应采样。若在动作快速、置信度下降或异常附近仍不能把连续证据缺口压到 0.25 秒以内，结果必须进入人工复核。

后端通过 `DetectorBackend`、`PoseBackend`、`MaskBackend` 和 `ReIdBackend` 协议隔离。参考模型组合优先评测 RTMDet/RTMPose 类检测姿态后端、具备视频掩码传播能力的分割后端以及独立外观嵌入；任何具体组合在通过第 13 节资格门禁前都只能运行影子模式。

### 5.2 `PrimarySubjectTracker`

主角由候选锚点附近的语义主体确定，然后向前、向后双向跟踪。跟踪不能只依赖框面积或画面中心，至少融合：

- 时序位置和运动预测；
- 人体外观嵌入；
- 姿态和躯干尺度；
- 分割掩码传播一致性；
- 遮挡前后的再识别置信度。

主角身份切换是不可抵消的硬失败。跟踪丢失、两条轨迹竞争或遮挡后再识别不足属于证据不完整，进入 `REVIEW_REQUIRED`。

### 5.3 `BodyGeometryAnalyzer`

姿态坐标先转换到以躯干中心、肩胯轴和躯干尺度为基准的归一化坐标系，降低镜头平移、缩放和人物整体旋转的影响。

硬门特征包括：

- 肢体连接拓扑是否突然改变；
- 左右侧是否出现无法解释的交换；
- 归一化骨段长度是否发生异常跳变；
- 关节角速度、加速度和 jerk 是否出现非物理离群；
- 肩、躯干、腰、胯和四肢轮廓是否在运动补偿后突然断裂；
- 相邻人体掩码的时序 IoU、轮廓距离和光流传播残差；
- 身体被裁切、人物过小、重度遮挡时证据是否仍满足覆盖要求。

“身体曲线硬门”只检查曲线随时间是否保持可解释的连续变化。它不使用固定腰臀比例、性别模板或单帧审美标准。

软特征包括姿态舒展度、轮廓可读性、曲线节奏、主体构图和遮挡程度。这些特征单独版本化并送入 Preference Memory。

### 5.4 `ActionSemanticAnalyzer`

该组件把人物、肢体、相关物体和接触关系组织成时间图：

- 节点：人物、身体区域、相关物体、动作阶段；
- 边：接近、接触、分离、相对方向、遮挡和共同运动；
- 时间状态：准备、发展、峰值、恢复、稳定或循环周期边界。

规则证据先判断接触建立、维持和解除是否存在合理的前后状态，再由本地 VLM 查看彩色故事板、姿态叠图、轮廓叠图和结构化证据。VLM 输出必须符合封闭 JSON Schema，不能自行创造时间戳或跳过证据字段。

VLM 只提供一个独立语义证据族：

- 它可以把候选转为 `REVIEW_REQUIRED` 或参与确认 `HARD_REJECT`；
- 它不能越过缺失的姿态、跟踪或轮廓证据单独产生 `AUTO_PASS`；
- 拒答、超时、非结构化输出或低置信度均视为 `ABSTAIN`。

### 5.5 `ContinuityGate`

门禁不计算一个能够相互抵消的总分。自动通过条件是所有必需断言的合取：

```text
AUTO_PASS =
    镜头连续
    AND 主角身份连续
    AND 姿态证据覆盖达标
    AND 无确认的解剖或轮廓突变
    AND 接触关系变化可解释
    AND 动作阶段完整
    AND 语义复核无冲突
```

决策优先级：

1. 有高置信度确认异常时为 `HARD_REJECT`；
2. 无确认异常但任一必需证据缺失、冲突或不可用时为 `REVIEW_REQUIRED`；
3. 只有全部断言均有有效证据且通过时才为 `AUTO_PASS`。

动作边界最多允许修正一次。修正后必须从头重跑完整评估，不能复用与新窗口不一致的旧通过结果，也不能循环调整直到偶然通过。

### 5.6 `CurvePreferenceScorer`

该组件只处理已通过硬门的候选。它将曲线软特征与现有 caption embedding、标签、全局画像和场景画像共同输入 Preference Memory。

- 特征缺失时仅停用曲线偏好分量，并对其余有效分量重新归一化；
- 硬门未通过时不运行偏好重排；
- 历史 like、favorite、dislike 可以训练审美偏好，但不能成为连续性硬门标签；
- 画像训练和 holdout 必须按源视频隔离，防止相邻 GIF 泄漏。

## 6. 决策状态与原因码

### 6.1 新增素材

- `AUTO_PASS`：全部必需证据通过，可进入后续质量和排序链。
- `REVIEW_REQUIRED`：不能证明安全自动导出，但也没有足够证据确认异常。
- `HARD_REJECT`：一个高置信度物理异常，或两个相互独立的证据族共同确认语义断裂。

### 6.2 历史 GIF

- `GIF_ONLY_PASS`：GIF 内部身份、姿态、轮廓、接触和循环检查通过，但外部动作上下文未验证。
- `GIF_ONLY_REVIEW`：GIF 内部证据缺失或冲突。
- `GIF_ONLY_REJECT`：GIF 内部已确认存在异常。

`GIF_ONLY_PASS` 永远不能转换为 `AUTO_PASS`。即使 GLM/Harness 人工辅助认为动作自然，也只能改善 GIF 内部复核状态，不能补造缺失的原视频上下文。

### 6.3 首版原因码

原因码使用稳定的 snake_case 值，至少包含：

- `source_unavailable`、`source_hash_mismatch`、`decode_failed`；
- `shot_boundary_conflict`、`fixed_window_fallback`；
- `primary_subject_ambiguous`、`identity_switch`、`tracking_gap`；
- `pose_coverage_low`、`critical_joint_clipped`、`subject_too_small`；
- `limb_topology_jump`、`body_ratio_jump`、`joint_motion_jump`；
- `silhouette_jump`、`mask_propagation_conflict`；
- `contact_transition_unexplained`、`action_phase_incomplete`；
- `vlm_abstained`、`evidence_family_conflict`；
- `historical_gif_without_source_context`。

界面文案可以本地化，但数据库和清单中的原因码保持稳定。

## 7. 初始安全阈值

以下参数是影子模式的初始值，不因上线而自动获得生产资格：

- 主角身份切换允许次数：0；
- 有效姿态证据覆盖率：至少 95%；
- 最大连续证据缺口：0.25 秒；
- 未解释的肢体拓扑、轮廓或接触突变：0；
- 非循环动作：必须包含准备或可解释的进入状态，以及恢复或稳定结束状态；
- 循环动作：至少包含一个完整周期；
- VLM：不得存在高置信度冲突或拒绝证据。

阈值必须从冻结验证集校准。配置变化、模型变化或提示词变化都会生成新的 `config_hash`，旧结果不得自动冒充新版本结果。

## 8. 数据流与现有流水线集成

### 8.1 新增素材

1. Transition Guard 先移除或拆分确认的镜头边界。
2. Action Boundary 在安全镜头段内产生候选动作窗口。
3. Continuity 服务读取候选窗口以及默认前后各 2 秒上下文，但不越过已确认镜头边界。
4. 人体证据提取、主角跟踪、几何分析和语义分析生成不可变评估。
5. 若建议一次边界修正，则生成新窗口并重新执行步骤 3–4。
6. Continuity Gate 只把 `AUTO_PASS` 送入 GIF 编码及后续 Quality MoE。
7. `REVIEW_REQUIRED` 和 `HARD_REJECT` 不生成正式导出，但可以生成小型复核代理文件。
8. 物化候选时写入评估引用和版本，不覆盖原动作或转场证据。

现有 `fallback_fixed`、转场无法确认时保留原窗口等降级路径，在 `continuity_gate.mode=enforce` 下必须转成 `REVIEW_REQUIRED`，不能继续自动导出。

### 8.2 历史 GIF

1. 从 `candidate_gifs` 枚举现有 GIF，并验证 GIF 文件身份。
2. 直接解码 GIF 的全部或自适应选取帧，不尝试伪造源视频窗口。
3. 运行 GIF 内部身份、姿态、轮廓、接触和首尾循环评估。
4. 写入 GIF-only 评估及曲线软特征。
5. 不修改原有候选状态、收藏关系、反馈事件或 GIF 文件。

## 9. 数据模型

数据沿用 Quality MoE 的版本、输入哈希、配置哈希和外部证据校验思路，但不把新字段不断堆进 `candidate_gifs`。

### 9.1 `continuity_assessments`

每次评估一行，核心字段：

- `assessment_id`；
- `candidate_id` 或运行期 `source_run_candidate_id`；
- `source_kind`：`source_video` 或 `gif_only`；
- `analyzed_input_sha256`：实际被分析的原视频或 GIF 的哈希；
- `source_video_sha256_snapshot`、`source_path_snapshot`：候选中保存的原视频身份快照，GIF-only 场景允许只有快照而没有可访问文件；
- `window_start_sec`、`window_end_sec`、`context_start_sec`、`context_end_sec`；
- `evaluation_version`、`config_hash`、`model_bundle_hash`、`prompt_hash`；
- `decision`、`reason_codes_json`；
- `identity_status`、`pose_status`、`silhouette_status`、`contact_status`、`action_phase_status`、`vlm_status`；
- `pose_coverage`、`max_evidence_gap_sec`、`minimum_confidence`；
- `created_at`、`completed_at`。

幂等键由实际分析输入哈希、窗口、评估版本和配置哈希构成。模型或阈值升级产生新行，旧行保留。GIF-only 评估不得把候选中遗留的原视频哈希冒充为本次实际分析输入哈希。

### 9.2 `continuity_evidence`

保存每个证据族的状态、摘要、极值和异常时间点。大型逐帧关键点、轮廓和叠图写入外部证据目录，数据库保存：

- `artifact_path`；
- `artifact_sha256`；
- `mime_type`；
- `frame_count`；
- `input_hash`。

证据文件损坏或哈希不符时，对应评估不能用于自动放行。

### 9.3 `continuity_review_events`

人工复核采用追加式事件流，支持 correction 和撤销：

- 目标评估和候选；
- 人工结论和问题标签；
- 备注、创建时间、撤销时间；
- 复核时看到的评估版本和证据哈希。

人工事件可以用于模型评测和阈值校准，但不回写原始模型输出。

### 9.4 `curve_features`

保存版本化软特征、可用性和特征提取器哈希。Preference Memory 只读取已发布、通过 holdout 门禁的特征版本。

## 10. 配置契约

新增顶层配置建议如下：

```yaml
continuity_gate:
  enabled: true
  mode: shadow                # shadow | enforce
  evaluation_version: continuity-v1
  context_margin_s: 2.0
  base_sample_fps: 8
  burst_sample_fps: 16
  min_pose_coverage: 0.95
  max_evidence_gap_s: 0.25
  max_identity_switches: 0
  allow_single_boundary_repair: true
  evidence_dir: data/continuity_evidence
  runtime_vlm:
    provider: inherit_vlm
    required_for_auto_pass: true
  historical:
    enabled: false
    batch_size: 25
    priority: feedback_first
  curve_preference:
    enabled: false
    feature_version: curve-v1
```

`enforce` 只有在当前模型组合、配置哈希和验证报告满足第 13 节门禁时才能启动。配置加载器必须拒绝未知模式、非法范围和缺失模型身份。

## 11. GLM-5.3-Flash 与 Harness 边界

GLM-5.3-Flash 原生支持视频、图像、文本和结构化输出，但用户现有额度属于只能在支持工具内使用的 Coding Plan/Harness 配额，不能作为 GifAgent 自建应用 API 使用。

因此第一版：

- 不实现 GLM 生产 provider；
- 不让生产门禁依赖 GLM 可用性；
- 保留通用远程多模态 provider 接口，未来只有获得合规标准 API 配额后才能接入；
- GLM 仅用于 Harness 内的代码开发、评审、测试设计、样本分析和离线人工辅助复核。

系统可以为疑难候选导出人工选择的 review bundle：

```text
candidate.mp4 或 candidate.gif
context_before_after.mp4        # 仅新增素材提供
pose_overlay.mp4 或 .gif
silhouette_overlay.mp4 或 .gif
evidence.json
review_prompt.md
bundle_manifest.json
```

从 Harness 导回的 `manual_review.json` 必须校验候选 ID、输入哈希、窗口、评估版本和 bundle 哈希。该过程是显式人工操作，不提供自动轮询、批量调用或伪 API。

## 12. 历史回刷与数据安全

2026-09-04 的只读快照显示生产库约有 16,894 条 `candidate_gifs`、16,233 条 `preference_events` 和 2,392 条收藏记录。数字会继续变化，因此执行前必须重新 dry-run。

回刷顺序：

1. favorite、like、promoted；
2. 其他可用普通候选；
3. dislike、rejected、archived，仅在校准负样本或用户主动要求时处理。

执行约束：

- 首次运行只输出扫描量、GIF 可用率、预计计算量和跳过原因；
- 写入前制作 SQLite 一致性备份并记录 SHA-256；
- 小批次提交，每批保存检查点；
- 重跑只处理缺失的当前版本结果；
- 单条失败不回滚已完成批次，也不永久排除可重试错误；
- 不删除、覆盖或重新编码历史 GIF；
- 不修改 `candidate_gifs.status`、`favorite_gifs` 或既有 `preference_events`；
- 只有人工明确操作才可改变历史内容的业务状态。

## 13. 验证、统计门禁与上线

### 13.1 自动化测试

单元测试覆盖：

- 坐标归一化、轨迹关联、覆盖率和最大缺口计算；
- 肢体比例、关节运动、轮廓传播和接触关系异常；
- 合取门禁不能被高软分覆盖；
- VLM 拒答、超时、畸形 JSON 和低置信度；
- 一次边界修正上限和重新评估；
- GIF-only 状态永远不能升级为 `AUTO_PASS`；
- 配置、输入和模型哈希漂移导致旧证据失效；
- 回刷幂等、暂停恢复、批次提交和失败隔离。

集成测试覆盖：

- staged 与 direct 路径使用同一门禁并产生等价结果；
- `REVIEW_REQUIRED` 和 `HARD_REJECT` 不能到达正式 GIF 导出；
- Quality MoE 和 Preference Memory 只能接收 `AUTO_PASS`；
- 数据库迁移对现有候选、反馈和收藏零破坏；
- EXE 打包后模型资源发现、离线运行和错误呈现正常。

真实媒体集覆盖：

- 正常、快速、慢速和周期动作；
- 遮挡、出画再入画、多人交互和身份交换；
- 平移、缩放、手持抖动、低光和模糊；
- 人体裁切、人物过小和复杂衣物；
- 真实复杂姿态与人工构造的肢体、轮廓跳变；
- GIF 首尾连续但动作边界不完整的反例。

### 13.2 统计资格门禁

正式 `enforce` 的首要指标是错误自动放行率，而不是总体准确率：

- 按源视频分组切分训练、校准和 holdout；
- 冻结 holdout 后不得用其调参；
- 对已确认不合格样本计算错误自动放行率的精确 95% 二项分布置信上界；
- 只有上界不超过 0.5% 时，当前模型组合和配置哈希才获得自动放行资格；
- 若观察到零次误放，至少约 600 个独立不合格样本才能支持约 0.5% 的 95% 上界；
- 自动通过覆盖率、误杀率、人工复核率和吞吐量单独报告，不能与安全指标混合。

### 13.3 四阶段上线

1. **基础设施与影子模式**：实现后端协议、证据、数据库、可视化和门禁，但不改变正式导出集合。
2. **模型资格与阈值冻结**：建立人工标注集，比较候选后端，冻结通过统计门禁的模型和配置。
3. **新增素材强制门禁**：先小批量 canary，再启用 `enforce`；任何版本升级重新回到影子模式。
4. **历史 GIF 回刷**：只产生 GIF-only 状态和软特征，不追认完整动作强保证。

## 14. 可观测性与人工复核

每次运行至少记录：

- 输入、自动通过、待复核和确认拒绝数量；
- 各原因码计数；
- 跟踪、姿态、分割、本地 VLM 的覆盖率、失败率和耗时；
- 边界修正尝试与成功数量；
- `fixed_window_fallback` 等旧降级原因；
- 按人物尺寸、遮挡、多人数、运动速度和镜头类型分桶的通过率；
- 当前 `evaluation_version`、`config_hash` 和 `model_bundle_hash`。

人工复核界面显示原片段、上下文、姿态/轮廓叠图、异常时间点、组件状态和原因码。复核者无需阅读日志即可知道系统缺少什么证据。

## 15. 性能与资源约束

- 目标环境按当前 16 GB VRAM 配置设计，默认同时只运行一个重型视觉模型阶段；
- 检测、姿态和分割按批次调度，并复用已解码帧；
- 同一视频重叠候选共享人物轨迹和帧级证据缓存；
- 先运行低成本检测和覆盖检查，明显不支持的窗口不再调用昂贵语义复核；
- 缓存键包含源哈希、时间戳、解码参数和模型哈希；
- 显存不足时缩小批次并重试一次，仍失败则进入 `REVIEW_REQUIRED`，不得降低证据要求。

吞吐量是次级指标。不能通过降低采样率、忽略遮挡或跳过必需模型来维持产量。

## 16. 故障处理

- 解码失败、模型未加载、显存不足、VLM 超时和证据损坏都产生结构化失败记录；
- 可重试故障与永久输入故障分开；
- 日志不得写入 API Key、完整环境变量或未脱敏凭据；
- 模型或外部证据目录不可用时，新增素材不得绕过门禁；
- 数据库写入失败时不得发布没有评估记录的正式 GIF；
- 人工复核导入失败时保持原状态并给出字段级原因；
- 关闭 `continuity_gate.enabled` 是显式回退到旧行为，界面和运行清单必须醒目标记“强保证未启用”。

## 17. 实施边界与预期文件

后续实施计划应按以下独立里程碑组织，避免在一个任务中混合全部风险：

1. 领域类型、配置、数据表和纯门禁策略；
2. 人体证据后端协议、参考后端和合成媒体测试；
3. 跟踪、身体几何、动作语义和本地 VLM 复核；
4. staged/direct 流水线接入与影子模式；
5. 人工复核、review bundle 和可观测性；
6. 资格评测工具、阈值冻结与 canary；
7. 历史 GIF-only 回刷；
8. 曲线软特征与 Preference Memory holdout 发布。

预计新增模块位于 `app/services/continuity/`，避免继续扩张现有 `action_boundary.py` 和 `action_pipeline.py`。流水线只通过一个稳定 facade 调用该子系统。数据库迁移沿用现有幂等 schema 入口；测试分别位于 `tests/continuity/`、流水线集成测试和真实媒体验证脚本。

## 18. 验收标准

设计实现完成必须同时满足：

1. 新增素材在 enforce 模式下只有全部证据通过才能自动导出。
2. 任一必需组件不可用时没有固定窗口或旧 VLM 降级旁路。
3. 主角身份、姿态、轮廓、接触和动作阶段都有独立、可审计结果。
4. 身体曲线硬门与审美软评分在代码、数据和策略上隔离。
5. staged 与 direct 路径行为一致。
6. 所有评估均冻结输入、配置、模型和提示词身份。
7. 历史 GIF 只产生 GIF-only 状态，不修改原文件、业务状态或反馈历史。
8. GLM-5.3-Flash 不作为生产运行时依赖，Harness 复核是显式人工流程。
9. 当前模型组合通过第 13 节统计资格门禁后才能切换到 enforce。
10. 回填和数据库迁移有 dry-run、备份、幂等、恢复和零数据丢失证据。

## 19. 参考现状

- `app/services/temporal_evidence.py`：当前帧间运动与残差证据；
- `app/services/transition_guard.py`：当前镜头边界保护；
- `app/services/action_boundary.py`：当前动作候选和循环评分；
- `app/services/action_vlm.py`：当前 6–8 帧接触图语义复核；
- `app/services/action_pipeline.py`：转场优先的动作物化流程；
- `app/pipeline/stages/rank_dedup.py`：staged 流水线候选物化入口；
- `app/quality_moe/`：版本化多证据质量判断模式；
- `app/services/preference_schema.py` 与 `app/services/preference_memory.py`：候选、反馈和偏好画像。

GLM 能力与配额边界参考：

- https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash
- https://docs.bigmodel.cn/cn/coding-plan/faq
