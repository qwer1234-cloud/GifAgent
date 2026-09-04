# 人物语义动作连续性与身体曲线评估设计

日期：2026-09-04

修订日期：2026-09-05

状态：第二轮 review 修订完成，待实施里程碑 0A

范围：新增素材的强制连续性门禁、动作幅度判定、人脸身份补强与面部动态咨询证据、历史 GIF 的有限回刷、身体曲线偏好评分

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

> 同一镜头内持续锁定同一主角集合；姿态、接触关系和运动方向不能出现无法解释的突跳；截取窗口必须覆盖完整动作单元。只有所有首版必需证据均可用且通过时才允许自动导出。首版轮廓/掩码是咨询证据：缺失本身不阻断自动通过，但一旦检测到未解释异常就转人工复核。必需证据缺失、组件失败或结论冲突时，拒绝自动导出并转人工复核。

强保证只适用于能够访问原视频及候选窗口前后上下文的新增素材。仅有成品 GIF 的历史内容不能证明 GIF 外部的起势、收势或隐藏转场，因此不能获得等价的完整自动通过状态。

### 2.2 身体曲线的两层含义

身体曲线必须拆成彼此隔离的两层：

1. **解剖时序稳定性**属于硬门。它以检测、跟踪和姿态检查人体拓扑、比例和运动是否连续，不评价人物本身是否“好看”；首版轮廓/掩码只负责咨询和升级复核，待资格评测后再决定是否成为必需证据。
2. **曲线审美偏好**属于软评分。它学习用户对姿态舒展度、轮廓可读性、构图和视觉节奏的偏好，只影响已经通过硬门的候选排序。

软评分不得覆盖身份切换、动作不完整、解剖异常或证据不足。

### 2.3 主体范围

- 主体可以是单一主角，也可以是最多两人的“共同主角集合”；双人紧密交互时不得为了满足 schema 强行选出唯一主角。画面中需要共同建模的主体达到三人或以上时，首版以 `primary_subject_ambiguous` 进入 `REVIEW_REQUIRED`。
- 主角集合中的人物接受完整的人体检测、身份跟踪、姿态、动作阶段和曲线评估；轮廓/掩码在首版仅作为咨询证据。
- 其他人物至少接受稳定跟踪以及与主角集合的相对位置、接触建立、维持和解除关系评估。
- 无法稳定确定主角集合，或集合成员身份无法持续关联时，结果为 `REVIEW_REQUIRED`，不得依据画面中心或面积猜测后自动放行。

## 3. 目标与非目标

### 3.1 目标

1. 在 GIF 编码前阻断人物身份跳变、人体结构突变、不可解释的轮廓跳变和语义动作断裂。
2. 对新增素材验证候选窗口是否包含完整动作单元。
3. 所有自动放行结论都具备可追溯的输入、模型、配置、逐项证据和原因码。
4. 组件不可用时 fail closed，不再静默回退到固定窗口自动导出。
5. 为身体曲线审美建立独立、可关闭、可评估的 Preference Memory 特征通道。
6. 对历史 GIF 做可恢复的内部连续性回刷，同时诚实标记无法验证的外部上下文。
7. 允许 GLM-5.3-Flash 在 Harness 中辅助开发、标注和离线复核，但生产流水线不依赖 Coding Plan。
8. 以相机运动补偿后的归一化时序曲线判断动作幅度和动作阶段，不把低幅动作简单判坏。
9. 当画面中存在可用人脸时，以人脸外观嵌入补强身份连续性，并把面部关键点/表情动态作为咨询和偏好候选特征；没有可用人脸时明确记为不适用，而不是猜测情绪。

### 3.2 非目标

- 不把审美评分包装成客观人体价值判断。
- 不使用单个 VLM 或单一总分作为强门禁。
- 不为历史 GIF 推断不存在的原视频上下文。
- 不自动删除、覆盖、取消收藏或下架历史 GIF。
- 不把 Harness 包装成隐藏 API 或无人值守的生产模型通道。
- 不在首版训练端到端视频动作基础模型。
- 不在本设计中绑定单一第三方模型品牌。后端是否拥有自动放行资格由验证集结果决定。

## 4. 总体方案

采用“确定性视觉证据 + 时序关系分析 + 本地 VLM 语义复核 + fail-closed 策略”的分层架构。生产插入点严格对齐现有 `rank_dedup`：动作物化和 boundary snap 完成后生成稳定窗口，随后形成去重组并执行 Continuity Gate；只有自动或人工放行项才能进入 Quality MoE、Preference 排序和 `gif_clip` 编码。

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
Boundary Snap 固定最终待评估窗口
        │
        ▼
Embedding / Temporal Dedup 分组
        │
        ▼
Person Evidence Extractor
        │
        ├── 人体检测与主角跟踪
        ├── 姿态关键点
        ├── 人体分割轮廓
        ├── 动作幅度时序
        ├── 人脸身份与面部动态咨询证据
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
        ├── AUTO_PASS ───────────────────────┐
        ├── REVIEW_REQUIRED ─► 人工复核 ─► MANUAL_PASS
        └── HARD_REJECT ─────► 可申诉复核 ─► MANUAL_PASS
                                             │
                                             ▼
                         Quality MoE ─► Preference 排序 ─► GIF 编码
```

Continuity Gate 位于 `_apply_boundary_snaps` 之后、`_evaluate_quality_pipeline_candidates` 之前。分阶段流水线和 direct 流水线必须调用同一个领域服务，不能各自维护一套判断逻辑。人工放行只覆盖 Continuity Gate，不绕过 Quality MoE，也不直接生成正式 GIF。

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
- 相机运动补偿后的全身/局部动作幅度曲线；
- 可见人脸框、面部关键点、质量和表情动态；
- 其他人物、相关物体和接触候选；
- 解码、检测、姿态、分割、人脸身份和面部动态咨询证据覆盖率。

首版采用基础 8 FPS、疑难区间最高 16 FPS 的自适应采样。burst 只由 `temporal_evidence.py` 已有的运动残差、光流幅度和方向变化触发，不由姿态模型自身的低置信度触发，避免“低置信度—加密采样—仍低置信度”的循环。加密后仍不能把连续证据缺口压到 0.25 秒以内时，结果进入人工复核。

后端通过 `DetectorBackend`、`PoseBackend`、`MaskBackend`、`ReIdBackend`、`FaceBackend` 和 `ExpressionBackend` 协议隔离。参考模型组合优先评测 RTMDet/RTMPose 类检测姿态后端、具备视频掩码传播能力的分割后端、独立外观嵌入以及轻量人脸关键点/表情动态后端；任何具体组合在通过第 13 节资格门禁前都只能运行影子模式。

### 5.2 `PrimarySubjectSetTracker`

主体由候选锚点附近的语义关系确定，然后向前、向后双向跟踪。输出是包含一人或两人的 `PrimarySubjectSet`：单人场景保持一个主体；对称双人、紧密接触或无法合理指定唯一主角的场景把两人作为共同主体，避免强行选择单一主角。

跟踪不能只依赖框面积或画面中心，至少融合：

- 时序位置和运动预测；
- 人体外观嵌入；
- 姿态和躯干尺度；
- 分割掩码传播一致性，首版仅作咨询；
- 遮挡前后的再识别置信度。

单个跟踪器报告的身份切换只触发 `REVIEW_REQUIRED`。只有镜头边界证据，或至少两个独立证据族共同确认主体集合成员发生变化，才可自动判为 `HARD_REJECT`。跟踪丢失、轨迹竞争或遮挡后再识别不足属于证据不完整。

### 5.3 `BodyGeometryAnalyzer`

姿态坐标先转换到以躯干中心、肩胯轴和躯干尺度为基准的归一化坐标系，降低镜头平移、缩放和人物整体旋转的影响。

首版要求检测、主体集合跟踪和姿态的覆盖率达标；由姿态派生的下列几何异常判定仍属于咨询证据：

- 肢体连接拓扑是否突然改变；
- 左右侧是否出现无法解释的交换；
- 归一化骨段长度是否发生异常跳变；
- 关节角速度、加速度和 jerk 是否出现非物理离群；
- 肩、躯干、腰、胯和四肢轮廓是否在运动补偿后突然断裂；
- 相邻人体掩码的时序 IoU、轮廓距离和光流传播残差；
- 身体被裁切、人物过小、重度遮挡时证据是否仍满足覆盖要求。

左右侧交换、骨段比例和 jerk 都容易受到姿态误检与 8 FPS 采样混叠影响，因此单一几何异常只能触发 `REVIEW_REQUIRED`，不能独立触发 `HARD_REJECT`。“身体曲线硬门”只检查曲线随时间是否保持可解释的连续变化，不使用固定腰臀比例、性别模板或单帧审美标准。

人体分割和轮廓在 continuity-v1 中是咨询证据：它们参与解释、可视化和软特征，但不属于 `AUTO_PASS` 的必需证据，也不能触发 `HARD_REJECT`。只有资格评测证明其在目标素材上带来稳定净增益后，后续评估版本才可以把该族升级为必需证据。

软特征包括姿态舒展度、轮廓可读性、曲线节奏、主体构图和遮挡程度。这些特征单独版本化并送入 Preference Memory。

### 5.4 `MotionAmplitudeAnalyzer`

动作幅度不是单帧位移或固定阈值，而是相机运动补偿后的多信号时序曲线。组件融合：

- 以躯干尺度归一化的关键点位移、关节角变化和有效肢体活动范围；
- 主体框/掩码相对相机背景运动的残差；
- `temporal_evidence.py` 已有光流幅度、运动残差和方向变化；
- 主体集合成员之间的相对位移、接近/分离和接触变化。

输出至少包括 `subtle`、`moderate`、`large` 三档描述，归一化峰值、持续时间、上升/峰值/恢复阶段、周期振幅稳定度和噪声底。幅度较小本身不是拒绝理由：只要信号高于自适应噪声底、动作阶段完整且语义可解释，细微动作仍可通过。低于可观察噪声底、幅度曲线突然塌缩/爆发或窗口只截到峰值一侧时进入 `REVIEW_REQUIRED`；单一幅度异常不能独立触发 `HARD_REJECT`。

自适应噪声底不得从候选动作幅度的总体分布反推。它只使用与待测动作相对独立的参照：上下文 2 秒内被确定性规则识别为静止的主体关键点抖动，以及排除主体区域后的背景运动补偿残差。初始要求背景有效像素覆盖至少 30%、全局仿射估计内点率至少 50%；任一条件不足时标记 `camera_compensation_low_confidence`，幅度证据为 `UNAVAILABLE` 并进入 `REVIEW_REQUIRED`。找不到可信静止段且背景参照也不足时不得用候选自身峰值估计噪声底。

该组件是首版必需证据，也是 16 FPS burst 的触发来源之一。burst 仍只依赖评估前可取得的运动残差/光流，而不以本组件最终结论循环触发自身重算。

### 5.5 `FaceEvidenceAnalyzer`

人脸在 continuity-v1 中不作为条件式必需门禁，而拆成两种用途：

1. 可用人脸的外观嵌入是 `appearance_identity` 独立性分组中的额外身份连续性证据，可与人体外观 ReID 互补，但两者同属一个分组，不能被重复计数成两个独立确认。
2. 面部关键点与可观察表情动态是咨询证据。它跟踪眉眼、眼睑、嘴部等几何变化是否平滑，供复核可视化和 Preference Memory 候选特征使用，不判断“动作阶段应该有什么表情”。

初始可用门槛为至少 3 个采样帧、脸框短边至少 64 px、面部关键点平均置信度至少 0.70，且在人脸可见区间中的可用覆盖率至少 60%。该门槛基于独立高分辨率人脸支路的实际输入尺寸；若只使用当前 320 px 分析帧，必须在 spike 报告中单独标记，不能把低激活率解释为素材没有人脸。是否增加高分辨率解码由成本测量决定。

- 没检测到人脸时为 `NOT_APPLICABLE`，不影响 `AUTO_PASS`；
- 检测到人脸但未达到可用门槛时为 `DETECTED_UNUSABLE`，只记录覆盖率；
- 可用人脸嵌入发生不连续时，只作为 `appearance_identity` 分组中的一个证据原子；
- 表情模型不可用、`ABSTAIN`、覆盖中断或单一表情突跳均不改变 continuity-v1 的自动决策，只生成咨询证据；
- 删除 `facial_expression_semantic_conflict`，不推断真实心理状态、性格、同意与否或敏感属性；
- 表情动态特征只有在独立 source-grouped holdout 证明排序增益后才进入 Preference Memory。

### 5.6 `ActionSemanticAnalyzer`

该组件把人物、肢体、相关物体和接触关系组织成时间图：

- 节点：人物、身体区域、相关物体、动作阶段；
- 边：接近、接触、分离、相对方向、遮挡和共同运动；
- 时间状态：准备、发展、峰值、恢复、稳定或循环周期边界。

规则证据先判断接触建立、维持和解除是否存在合理的前后状态，再由本地 VLM 查看彩色故事板、姿态叠图、轮廓叠图和结构化证据。VLM 输出必须符合封闭 JSON Schema，不能自行创造时间戳或跳过证据字段。

VLM 只提供一个独立语义证据族：

- 它可以把候选转为 `REVIEW_REQUIRED` 或参与确认 `HARD_REJECT`；
- 它不能越过缺失的必需姿态或跟踪证据单独产生 `AUTO_PASS`；
- 拒答、超时、非结构化输出或低置信度均视为 `ABSTAIN`。

### 5.7 `ContinuityGate`

门禁不计算一个能够相互抵消的总分。continuity-v1 的自动通过条件是所有首版必需断言的合取：

```text
AUTO_PASS =
    镜头连续
    AND 主体集合连续
    AND 姿态证据覆盖达标
    AND 动作幅度可观察且阶段连续
    AND 无未解决的几何异常
    AND 接触关系变化可解释
    AND 动作阶段完整
    AND 语义复核无冲突
```

所有硬门证据原子必须声明一个 `independence_group`，首版固定为：

- `scene_boundary`：镜头切换和转场检测；
- `pose_geometry`：人体检测、姿态覆盖和从人体姿态派生的身体几何；面部关键点动态属于 `record_only`，不进入该分组的硬确认计数；
- `appearance_identity`：人体 ReID 与人脸外观嵌入；
- `photometric_motion`：光流、运动残差、动作边界和从这些信号派生的幅度/阶段；
- `semantic_vlm`：VLM 对接触、主体和动作语义的结构化判断。

“两个独立证据族”在实现中必须解释为两个不同 `independence_group`，不是两个字段或两个分析器。`amplitude_phase_incomplete` 与基于同一运动曲线的 `action_phase_incomplete` 都属于 `photometric_motion`，合计仍只算一个确认；人体 ReID 与人脸嵌入都属于 `appearance_identity`，合计也只算一个。一个派生结论融合多个原始分组时，证据清单必须分别保留各原子和哈希，门禁按实际可用的不同分组计数。

决策优先级：

1. `scene_boundary` 确认镜头断裂，或至少两个不同 `independence_group` 确认同一身份/动作断裂时为 `HARD_REJECT`；
2. 无确认异常但任一必需证据缺失、冲突或不可用时为 `REVIEW_REQUIRED`；
3. 只有全部断言均有有效证据且通过时才为 `AUTO_PASS`。

Boundary snap 是门禁前的窗口规范化，不占用修正次数。Continuity Analyzer 在 snap 后最多再建议一次语义边界修正；修正后必须从头重跑完整评估，不能复用与新窗口不一致的旧结果，也不能循环调整直到偶然通过。

### 5.8 `CurvePreferenceScorer`

该组件只处理已通过硬门的候选。它从版本化数值特征训练独立的 L2 正则逻辑回归：like/favorite 是正样本，dislike 是负样本，neutral 不参与拟合。训练只使用 NumPy，避免为该轻量学习器引入新的运行时依赖；模型产物冻结特征均值、标准差、系数、截距、训练事件水位和源视频集合。

`PreferenceReranker` 增加名为 `curve_preference` 的可选分量，名义权重为 0.10。为保持总权重可解释，`base_rag` 从 0.55 调整为 0.45，`global_like=0.25`、`scenario_like=0.15` 和 `global_dislike=0.20` 保持不变。实际曲线权重像现有 `global_like` 一样乘以模型置信度：`effective_curve_weight = 0.10 * curve_model_confidence`。其中 `curve_model_confidence = min(data_confidence, holdout_margin_factor)`，`data_confidence = min(1, effective_feedback_count / 100)`，`holdout_margin_factor = clamp(pairwise_delta_lcb95 / 0.05, 0, 1)`；若 source-group bootstrap 的 pairwise delta 95% 下界不大于 0，则模型不得发布。曲线特征不可用时，正向分量按现有缺失重归一化规则恢复，不制造默认中位分。

- 特征缺失时仅停用曲线偏好分量，并对其余有效分量重新归一化；
- 硬门未通过时不运行偏好重排；
- 历史 like、favorite、dislike 可以训练审美偏好，但不能成为连续性硬门标签；
- 画像训练和 holdout 必须按源视频隔离，防止相邻 GIF 泄漏；
- 直接复用 `PreferenceEvaluationService` 的至少 30 条 holdout、训练/holdout 源视频零重叠和 `evaluate_source_grouped()` 报告，并新增曲线模型相对当前基线的 NDCG 与 pairwise 指标。

### 5.9 `ContinuityReviewQueue`

待复核候选在正式 GIF 产生前就必须持久化。该服务保存冻结窗口、源视频身份、代理文件、评估引用、去重组身份和复核状态，不依赖 `candidate_gifs`。

人工操作包括 `MANUAL_PASS`、`MANUAL_REJECT`、撤销和 correction。人工通过后，由任务引擎创建幂等的 post-continuity resume 任务：重新校验源视频和窗口身份，只执行 Quality MoE 硬门和该单候选的 `gif_clip` 编码，不重新参与候选排序或数量截断。若源视频已经丢失或变更，人工通过记录仍保留，但导出任务进入 `needs_attention`。

若人工通过在 GIF 已发布后被撤销，系统只追加 correction 事件并把已发布候选标记为需要关注，不自动删除文件、取消收藏或更改既有反馈；后续业务处置必须由用户明确执行。

模型版本升级不会静默覆盖队列。只有尚未开始人工复核、源视频仍可用且哈希匹配的 pending 项可以自动生成新版评估；旧评估保留并标记 superseded。已有人工作结论的项目必须由用户显式选择“用当前模型重评”。

## 6. 决策状态与原因码

### 6.1 自动判断与有效处置分离

自动模型只产生三种不可变判断：

- `AUTO_PASS`：全部必需证据通过，可进入后续质量和排序链；
- `REVIEW_REQUIRED`：不能证明安全自动导出，但也没有足够独立证据确认异常；
- `HARD_REJECT`：镜头边界已确认，或至少两个不同 `independence_group` 中具备硬确认资格的证据原子共同确认身份切换/语义断裂；`record_only` 咨询证据不计数。

业务处置另行计算，不回写模型判断：

- `AUTO_PASS`：自动判断通过；
- `MANUAL_PASS`：人工看过冻结证据后放行；
- `PENDING_REVIEW`：等待人工复核；
- `REJECTED`：人工拒绝，或仍未被人工推翻的 `HARD_REJECT`。

`HARD_REJECT` 不是不可申诉的删除状态。检测、跟踪和 ReID 在遮挡、局部身体和双人紧密接触场景中都可能误判，因此它必须进入可检索的复核队列，允许人工 `MANUAL_PASS` 或 `MANUAL_REJECT`。人工通过仅覆盖 Continuity Gate，仍必须经过 Quality MoE；任何人工结论都不能直接写出 GIF。

### 6.2 历史 GIF

- `GIF_ONLY_PASS`：GIF 内部身份、姿态、幅度、咨询式面部动态、轮廓、接触和循环检查通过，但外部动作上下文未验证。
- `GIF_ONLY_REVIEW`：GIF 内部证据缺失或冲突。
- `GIF_ONLY_REJECT`：GIF 内部已确认存在异常。

`GIF_ONLY_PASS` 永远不能转换为 `AUTO_PASS`。即使 GLM/Harness 人工辅助认为动作自然，也只能改善 GIF 内部复核状态，不能补造缺失的原视频上下文。首版不会对全部历史库批量计算这些状态；只有存在明确 UI/人工复核消费方的按需任务才计算。

### 6.3 首版原因码

原因码使用稳定的 snake_case 值，至少包含：

- `source_unavailable`、`source_hash_mismatch`、`decode_failed`；
- `model_unavailable`、`vram_exhausted`、`continuity_budget_exhausted`；
- `shot_boundary_conflict`、`fixed_window_fallback`；
- `context_truncated_media_edge`、`duplicate_group_exhausted`；
- `primary_subject_ambiguous`、`identity_switch`、`tracking_gap`；
- `pose_coverage_low`、`critical_joint_clipped`、`subject_too_small`；
- `limb_topology_jump`、`body_ratio_jump`、`joint_motion_jump`；
- `motion_amplitude_below_observable_floor`、`motion_amplitude_discontinuity`、`amplitude_phase_incomplete`、`camera_compensation_low_confidence`；
- `face_embedding_identity_conflict`、`facial_landmark_temporal_jump`；
- `silhouette_jump`、`mask_propagation_conflict`；
- `contact_transition_unexplained`、`action_phase_incomplete`；
- `vlm_abstained`、`evidence_family_conflict`；
- `historical_gif_without_source_context`。

界面文案可以本地化，但数据库和清单中的原因码保持稳定。

## 7. 初始安全阈值

以下参数是影子模式的初始值，不因上线而自动获得生产资格：

- 主角集合中未解释的身份切换允许次数：0；
- 有效姿态证据覆盖率：至少 95%；
- 最大连续证据缺口：0.25 秒；
- 未解释的肢体拓扑或接触突变：0；
- 动作幅度：使用相机补偿和躯干尺度归一化的自适应噪声底，不设置跨人物固定最小幅度；低于可观察噪声底时待复核；
- 非循环动作：必须包含准备或可解释的进入状态，以及恢复或稳定结束状态；
- 循环动作：至少包含一个完整周期；
- 人脸：外观嵌入只增强 `appearance_identity`；面部关键点和表情动态始终是咨询/偏好候选证据，其缺失、拒答或单一突跳不改变 continuity-v1 自动决策；
- VLM：不得存在高置信度冲突或拒绝证据，且 `required_for_auto_pass=true` 时 `ABSTAIN` 不能自动通过；
- 首版必需证据族：镜头/转场、检测与跟踪、姿态覆盖、动作幅度、接触语义、动作阶段和本地 VLM；
- 首版咨询证据分两级：分割、掩码传播、轮廓和身体几何属于 `review_on_anomaly`，缺失不阻断，但检测到未解释异常时进入 `REVIEW_REQUIRED`；面部关键点/表情动态属于 `record_only`，缺失或单一异常均不改变自动决策。两级咨询证据都不能单独导致 `HARD_REJECT`，`record_only` 证据也不得计入独立分组确认数。

单个左右侧交换、关节 jerk 或轮廓突跳很可能来自姿态采样和遮挡误差，只能导出 `REVIEW_REQUIRED`。`HARD_REJECT` 只允许由已确认镜头边界，或至少两个不同 `independence_group` 中具备硬确认资格的证据原子共同确认。

候选靠近真实媒体首尾时，上下文只能截到文件边缘，系统记录 `context_truncated_media_edge`，但不把“文件之外不存在的 2 秒”视为模型故障。若首帧可证明处于静止/准备态且尾帧已恢复/稳定，可以继续判断；若首帧已经处于进行态或尾帧仍在进行态，则为 `REVIEW_REQUIRED`。

安全门之外还设置首版可用性门。代表性、按源视频隔离且经人工认可的正样本 holdout 上，`AUTO_PASS` 覆盖率必须至少 40%；代表性全量 holdout 的 `REVIEW_REQUIRED` 比例必须不高于 60%。任一指标未达标时不得启用 `enforce`，而应重新设计必需证据集。这两个值是首版产品门槛，不替代第 13 节的错误放行安全上界。

阈值必须从冻结验证集校准。配置变化、模型变化或提示词变化都会生成新的 `config_hash`，旧结果不得自动冒充新版本结果。

## 8. 数据流与现有流水线集成

### 8.1 新增素材

目标顺序严格落在 `rank_dedup` 内，不能写成 Quality MoE 或 GIF 编码之后：

1. `materialize_action_candidates` 在 Transition Guard 保护下产生动作候选。
2. `_apply_boundary_snaps` 完成现有边界吸附；snap 后的窗口才是评估输入。
3. 为 snap 后窗口生成唯一候选身份。`pre_export_candidate_key = SHA256(canonical_json(source_sha256, start_us, end_us, anchor_us))`，其中时间统一四舍五入为整数微秒、JSON 键排序且无多余空白。`_assign_candidate_identities` 直接采用该 key 的前 16 位作为现有 `candidate_id`/`clip_id`，不再把列表 `index` 写入身份，避免 staged/direct 或组内替补改变 ID。
4. embedding dedup 与 temporal dedup 只形成 duplicate group，不立即不可逆地丢弃组内替补项；temporal dedup 需要像 embedding dedup 一样返回组关系。分组保持现有贪心语义：按分数降序、同分按 pre-export key 排序；每个被删除项只归入第一个直接满足阈值的 keeper，不做传递闭包。`duplicate_group_id` 是排序后成员 key 与去重策略哈希的 canonical JSON SHA-256。
5. `_assign_candidate_identities` 位于 dedup 分组之后，但只执行第 3 步的稳定映射，不再产生第二套身份。
6. 每组先评估当前排序最高的候选。若 `AUTO_PASS`，该组停止，并把尚未人工处理的兄弟项标为 `superseded_by_group_pass`，不进入普通 pending 队列；若为 `REVIEW_REQUIRED` 或 `HARD_REJECT`，则继续评估下一近重复候选，直到找到通过项或组内耗尽。只有整组无通过项时，组内可复核项才进入 pending。
7. Continuity Analyzer 读取候选窗口及默认前后各 2 秒上下文，但上下文不得越过 Transition Guard 已确认的镜头边界。若确定性证据在 VLM 运行前建议唯一一次语义边界修正，则生成新窗口、重新计算 pre-export key 和 duplicate group，并从头重跑评估；旧评估和旧组引用标为 superseded。boundary snap 是门禁前规范化，不占用这次语义修正。VLM 发现边界疑点时只进入 `REVIEW_REQUIRED`，不触发第二轮模型换入换出。
8. 只有 `AUTO_PASS` 或 `MANUAL_PASS` 才进入 `_evaluate_quality_pipeline_candidates`；随后执行 `_rank_pipeline_clips`，最后由下游 `gif_clip` 编码。
9. `REVIEW_REQUIRED` 和 `HARD_REJECT` 不生成正式 GIF，但生成小型复核代理文件和持久化 review item。

组内替补策略同时避免两种失败：不为每个必然被去重的候选无条件运行重模型，也不会因为先保留的代表项需要复核而丢掉一个可自动通过的近重复项。组内都未通过时记录 `duplicate_group_exhausted`。

整视频 1,200 秒预算耗尽后，剩余候选仍按 fail-closed 进入 `REVIEW_REQUIRED`，并记录候选在原视频中的归一化位置（0–20%、20–40%、40–60%、60–80%、80–100%）。影子报告必须分别给出各位置桶的预算耗尽率，暴露视频后半段的顺序偏差。

现有 `fallback_fixed`、转场无法确认时保留原窗口等降级路径，在 `continuity_gate.mode=enforce` 下必须转成 `REVIEW_REQUIRED`，不能继续自动导出。

人工 `MANUAL_PASS` 后不要求原始 pipeline task 仍在运行。里程碑 5 必须实现专用 `continuity_resume` job：以 review item、人工事件 revision 和 assessment hash 组成幂等键；重新校验源路径、源哈希和冻结窗口；只对该候选运行 Quality MoE 硬门；通过后生成带 `manual_resume=true` 的单候选、rank_dedup-compatible 不可变 manifest，再复用 `gif_clip`。该路径不调用 `_rank_pipeline_clips`，也不受 `_planned_output_count`、`OUTPUT_RATIO` 或 `MAX_OUTPUT` 截断。

resume job 的 materialize 使用追加语义：按稳定 candidate ID 幂等合并 PBF/result JSON，只入库本次成功 GIF，不清空或重写已完成视频的既有产物；若 `continuity_publication_links` 已存在则视为成功重放。源视频丢失/变化、Quality MoE 拒绝或追加发布冲突时任务进入 `needs_attention`，不得使用代理文件冒充源视频编码。

Quality MoE 现有 `action_completeness_score < 0.50` 的 `action_incomplete` 硬门，在 Continuity Gate 为 `disabled` 或 `shadow` 时保留。当前 continuity 配置取得 `enforce` 资格后，该规则退役为诊断/排序信号，避免同一动作完整性被两个不同门禁重复否决；Quality MoE 其余质量门保持不变。

### 8.2 历史 GIF

1. 批量首版只从 `candidate_gifs` 提取曲线软特征，不运行完整连续性链；曲线提取仍需检测、姿态及可选掩码，因此不是“无模型成本”的元数据回填，其有效率和耗时必须先由里程碑 0 的 200 项样本实测。
2. GIF-only 连续性仅用于人工点选、收藏/高价值项复核或抽样校准；任务直接解码 GIF，可以评估 GIF 内动作幅度和可用人脸的表情动态，但不尝试伪造源视频窗口。
3. 按需任务可以写入 GIF-only 评估并在 UI 显示内部连续性徽标/复核入口，但不能改变业务状态。
4. 不修改原有候选状态、收藏关系、反馈事件或 GIF 文件。

## 9. 数据模型

数据沿用 Quality MoE 的版本、输入哈希、配置哈希和外部证据校验思路，但不复用其只放在 `work_dir/quality_moe/{input_hash}/` 的生命周期。Continuity 的复核与回刷必须跨任务存活，因此使用 SQLite 索引加内容寻址的外部证据目录，也不把新字段不断堆进 `candidate_gifs`。

### 9.1 `continuity_assessments`

每次评估一行，核心字段：

- `assessment_id`；
- 可空的已发布 `candidate_id`，以及必填的运行期 `source_run_candidate_id` 或 pre-export candidate key；
- `source_kind`：`source_video` 或 `gif_only`；
- `analyzed_input_sha256`：实际被分析的原视频或 GIF 的哈希；
- `source_video_sha256_snapshot`、`source_path_snapshot`：候选中保存的原视频身份快照，GIF-only 场景允许只有快照而没有可访问文件；
- `window_start_sec`、`window_end_sec`、`context_start_sec`、`context_end_sec`；
- `evaluation_version`、`config_hash`、`model_bundle_hash`、`prompt_hash`；
- `decision`、`reason_codes_json`；
- `identity_status`、`pose_status`、`silhouette_status`、`contact_status`、`action_phase_status`、`vlm_status`；
- `amplitude_status`、`amplitude_class`、`normalized_amplitude_peak`、`amplitude_phase_complete`；
- `face_presence_status`、`face_identity_status`、`face_usable_frame_count`、`face_usable_coverage`、`facial_landmark_status`、`facial_expression_status`；
- `pose_coverage`、`max_evidence_gap_sec`、`minimum_confidence`；
- `created_at`、`completed_at`。

幂等键由实际分析输入哈希、窗口、评估版本和配置哈希构成。模型或阈值升级产生新行，旧行保留。GIF-only 评估不得把候选中遗留的原视频哈希冒充为本次实际分析输入哈希。

### 9.2 `continuity_evidence`

保存每个证据族的状态、摘要、极值和异常时间点。大型逐帧关键点、轮廓和叠图写入外部证据目录，数据库保存：

- `independence_group`：固定为第 5.7 节定义的一个分组；
- `artifact_path`；
- `artifact_sha256`；
- `mime_type`；
- `frame_count`；
- `input_hash`。

证据文件损坏或哈希不符时，对应评估不能用于自动放行。

### 9.3 `continuity_review_items`

正式 GIF 生成前的持久化复核载体，每项至少保存：

- `review_item_id`、`current_assessment_id`、`source_run_candidate_id`、`preexport_candidate_key`、`duplicate_group_id`；
- 冻结的窗口、上下文、源路径快照、源视频哈希和代理文件引用；
- `status`：`pending`、`manual_pass`、`manual_reject`、`superseded`、`superseded_by_group_pass` 或 `expired`；
- 当前评估版本、创建/更新时间和幂等 resume task key。

该表不依赖 `candidate_gifs`，因为后者只在 GIF 成功导出并被 `ingest_succeeded_gifs_into_library` 接纳后才存在。现有带 CHECK 约束的 `candidate_gifs.status` 及其 schema 保持不变。升级模型时，旧 review item 与 assessment 都保留。

### 9.4 `continuity_review_events`

人工复核采用追加式事件流，支持 correction 和撤销：

- 目标 review item、评估和运行期候选；
- 人工结论和问题标签；
- 备注、创建时间、撤销时间；
- 复核时看到的评估版本和证据哈希。

人工事件可以用于模型评测和阈值校准，但不回写原始模型输出。

### 9.5 `continuity_publication_links`

在 GIF 成功发布后，把 `candidate_gifs.candidate_id` 与实际使用的 assessment/review item 关联起来。该链接只在发布事务成功后写入，使 pre-export 数据不必伪造 `candidate_gifs` 外键。

### 9.6 `curve_features` 与 `curve_preference_models`

`curve_features` 保存版本化软特征、可用性和特征提取器哈希。`curve_preference_models` 保存标准化参数、逻辑回归系数/截距、训练事件水位、训练源视频集合、feature/model version、holdout 指标和发布状态。Preference Memory 只读取已发布、通过 holdout 门禁的模型和特征版本。

### 9.7 `continuity_qualification_manifests`

保存资格清单的内容哈希、`config_hash`、`model_bundle_hash`、`prompt_hash`、冻结数据集哈希、各项安全/可用性指标和签发时间。只有与当前运行配置完全匹配且外部清单文件 SHA-256 校验通过的记录才能解锁 `enforce`。

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
  required_evidence_families:
    - transition
    - subject_tracking
    - pose
    - motion_amplitude
    - contact_semantics
    - action_phase
    - local_vlm
  independence_groups:
    scene_boundary:
      - transition
    pose_geometry:
      - detection
      - pose
      - body_geometry
    appearance_identity:
      - body_reid
      - face_embedding
    photometric_motion:
      - optical_flow
      - motion_residual
      - motion_amplitude
      - action_boundary
      - action_phase
    semantic_vlm:
      - contact_semantics
      - local_vlm
  advisory_evidence_families:
    review_on_anomaly:
      - silhouette
      - mask_propagation
      - body_geometry
    record_only:
      - facial_landmarks
      - facial_expression
  face_evidence:
    identity_embedding_enabled: true
    landmark_dynamics_mode: advisory
    expression_mode: preference_candidate
    min_face_short_side_px: 64
    min_landmark_confidence: 0.70
    min_usable_frames: 3
    min_usable_coverage: 0.60
  motion_amplitude:
    normalization: torso_scale
    camera_motion_compensation: true
    noise_floor: adaptive
    min_background_coverage: 0.30
    min_affine_inlier_ratio: 0.50
  runtime_vlm:
    source: pipeline_vlm_snapshot
    required_for_auto_pass: true
  inference_runtime:
    launch_mode: wsl_sidecar
    gpu_policy: exclusive
    max_candidate_wall_clock_s: 30
    max_video_wall_clock_s: 1200
  qualification_manifest_path: null
  qualification_manifest_sha256: null
  historical:
    enabled: false
    batch_size: 25
    bulk_scope: curve_features_only
    priority: scored_feedback_first
  curve_preference:
    enabled: false
    feature_version: curve-v1
    model_weight: 0.10
```

`runtime_vlm.source: pipeline_vlm_snapshot` 表示读取运行清单中已经冻结的现有 `vlm:` 配置；它不是新的 provider 名，也不复用 Quality MoE 中仅用于 `base_url` 的 `inherit_vlm` 字符串约定。

`subject_tracking` 是由位置、姿态、光流和外观原子组合的必需结论，不被强行塞入单一分组；发生冲突时必须回溯并保存实际支持结论的原子及其分组，不能把同一个跟踪器的融合输出复制成多个独立确认。

`shadow` 下资格清单字段允许为 null。`enforce` 下两者必须非空：加载器读取清单文件、校验声明的 SHA-256，重算当前 `config_hash`、模型包、提示词和验证集身份，并查到相同的已签发资格记录；任一项不匹配即拒绝启动 `enforce`，而不是悄悄退回 shadow。这里的“签发”仅指保存经人工批准的内容哈希和审计记录，不要求公私钥签名体系。配置加载器还必须拒绝未知模式、非法范围、缺失模型身份和被标记过期的清单。

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

首版批量回刷只提取身体曲线软特征，因为它们有明确的 Preference Memory 消费方；不对约 16.9k 个降采样、调色板化 GIF 全量运行昂贵且无法补齐外部上下文的连续性链。训练样本顺序：

1. favorite、like 作为正样本；
2. dislike 作为负样本，必须与正样本在同一首版训练批次中处理；
3. neutral/未评分候选可以预计算特征供在线排序，但不参与监督拟合；
4. rejected、archived 只有在其反馈语义明确且用户主动纳入时处理，不能把业务状态自动当作审美标签。

历史 GIF 的内部连续性检查仅按需触发：用户主动复核 favorite/高价值项、抽样校准或 UI 打开“检查内部连续性”。它的消费方是 GIF-only 徽标和人工复核队列；在这些 UI/任务入口完成前不启动该计算。

里程碑 0 另取至少 200 个按分辨率、时长、反馈类型和主体大小分层的历史 GIF，测量仅提取曲线软特征所需的检测/姿态/可选掩码 wall-clock、峰值 VRAM和有效特征率。报告必须给出因分辨率、裁切、调色板和姿态失败导致的缺失比例，并估算全量约 16.9k GIF 的总 GPU 小时；有效训练样本不足时不得启动逻辑回归训练。

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
- 动作幅度归一化、相机运动补偿、噪声底、阶段完整性和周期幅度稳定度；
- 无人脸、不可用人脸、可用人脸、人脸嵌入身份冲突，以及面部关键点突跳只作咨询的策略；
- 合取门禁不能被高软分覆盖；
- 同一 `independence_group` 内多个异常仍只算一个确认，两个不同分组才能确认硬拒绝；
- VLM 拒答、超时、畸形 JSON 和低置信度；
- 一次边界修正上限和重新评估；
- boundary snap 后窗口身份冻结，窗口变化不能复用旧证据；
- duplicate group 在首选项未通过时尝试下一项，并在找到通过项时停止；
- GIF-only 状态永远不能升级为 `AUTO_PASS`；
- 配置、输入和模型哈希漂移导致旧证据失效；
- qualification manifest 缺失、篡改或与当前配置不匹配时拒绝 `enforce`；
- 回刷幂等、暂停恢复、批次提交和失败隔离。

集成测试覆盖：

- staged 与 direct 路径使用同一门禁并产生等价结果；
- `REVIEW_REQUIRED` 和 `HARD_REJECT` 不能到达正式 GIF 导出；
- `MANUAL_PASS` 创建独立、幂等的 late-resume 任务，且仍经过 Quality MoE；
- 源视频在人工通过后丢失或哈希变化时进入 `needs_attention`；
- Quality MoE 和 Preference Memory 只能接收 `AUTO_PASS` 或 `MANUAL_PASS`；
- continuity enforce 启用时，Quality MoE 的旧 `action_incomplete` 硬门退役而其余策略不变；
- 数据库迁移对现有候选、反馈和收藏零破坏；
- EXE 只携带 sidecar 客户端，不捆绑 PyTorch/姿态/分割模型；sidecar 发现、版本校验、离线运行和错误呈现正常。

真实媒体集覆盖：

- 正常、快速、慢速和周期动作；
- 遮挡、出画再入画、多人交互和身份交换；
- 平移、缩放、手持抖动、低光和模糊；
- 人体裁切、无头/身体局部、人物过小和复杂衣物；
- 单人、共同主角、双人紧密接触和主体集合歧义；
- 细微、中等、大幅、周期性动作，以及相机运动大但主体相对动作小的反例；
- 无脸、脸部过小、遮挡脸、可用人脸、快速表情变化和表情分类器拒答；
- 真实复杂姿态与人工构造的肢体、轮廓跳变；
- GIF 首尾连续但动作边界不完整的反例。

### 13.2 统计资格门禁

正式 `enforce` 同时需要安全资格和可用性资格，且安全指标优先：

- 按源视频分组切分训练、校准和 holdout；
- 冻结 holdout 后不得用其调参；
- 对真实、已确认不合格且按源视频隔离的样本计算错误自动放行率精确 95% 二项置信上界；
- 只有上界不超过 0.5% 时，当前模型组合和配置哈希才获得自动放行资格；598 是零误放时的最低样本量，不是固定门槛，观察到 1 次误放时约需 950 个，更多误放按精确 Clopper-Pearson 上界继续增加；
- 合成负样本只用于回归和定向压力测试，不能计入上述 598 个统计样本；
- 按身份切换、动作不完整、镜头冲突、姿态/拓扑异常等失效族分别报告，并要求每个声明为 critical 的失效族至少 50 个真实样本；任一 critical 族样本量不足时标为“未取得该族统计资格”且不能启用 `enforce`；
- 在未积累到 598 个真实不合格样本前，允许采用 150 个零误放样本对应约 2% 上界作为开发阶段 checkpoint，但只能保持 shadow 或人工确认后导出，不能据此开启全自动 `enforce`；
- 代表性人工认可正样本上的 `AUTO_PASS >= 40%`，代表性全量 holdout 上 `REVIEW_REQUIRED <= 60%`；
- 自动通过覆盖率、误杀率、人工复核率、VLM `ABSTAIN`/JSON 失败率和吞吐量分别报告，不能与安全指标混合。

### 13.3 五阶段上线

0. **可行性 spike**：在约 50 个代表性真实视频候选和至少 200 个分层历史 GIF 上离线验证领域覆盖、软特征提取率、资源和耗时；未达第 15 节门槛则停止进入生产实现。
1. **基础设施与影子模式**：实现 sidecar 协议、证据、数据库、复核队列、可视化和门禁，但不改变正式导出集合。
2. **模型资格与阈值冻结**：建立人工标注集，比较候选后端，冻结同时通过安全与可用性门禁的模型和配置。
3. **新增素材强制门禁**：先小批量 canary 并持续抽样人工审计，再启用 `enforce`；任何版本升级重新回到影子模式。
4. **历史软特征回刷**：首版只批量生成曲线特征；GIF-only 连续性保留为按需功能，不追认完整动作强保证。

## 14. 可观测性与人工复核

每次运行至少记录：

- 输入、自动通过、待复核和确认拒绝数量；
- 各原因码计数；
- 跟踪、姿态、分割、本地 VLM 的覆盖率、失败率和耗时；
- 动作幅度分档、归一化峰值、噪声底、阶段不完整率和幅度异常复核率；
- 人脸检测率、可用率、320 px 与候选高分辨率支路的覆盖/耗时差异、人脸嵌入身份增益、面部关键点/表情咨询证据覆盖率；
- 面部证据边际捕获率：其他必需分组均通过、仅面部证据告警且人工确认真实失效的比例；该值不用于 continuity-v1 门禁，只决定后续版本是否升级；
- VLM `ABSTAIN`、JSON 解析失败、拒答、超时分别计数；
- 边界修正尝试与成功数量；
- duplicate group 的评估数量、替补通过率和组内耗尽率；
- `superseded_by_group_pass` 数量，以及按候选视频位置五分位统计的预算耗尽/复核率；
- 人工通过、拒绝、撤销、hard-reject 申诉通过率以及 late-resume 结果；
- `fixed_window_fallback` 等旧降级原因；
- 按人物尺寸、遮挡、多人数、运动速度和镜头类型分桶的通过率；
- sidecar/Ollama 模型加载和卸载耗时、峰值 VRAM、候选及整视频 p50/p95 wall-clock；
- 当前 `evaluation_version`、`config_hash` 和 `model_bundle_hash`。

人工复核界面显示原片段、上下文、姿态/轮廓叠图、异常时间点、组件状态和原因码。复核者无需阅读日志即可知道系统缺少什么证据。

## 15. 性能与资源约束

### 15.1 里程碑 0：可行性 spike

在生产 schema 和流水线接入前，先选约 50 个真实视频候选，并另取至少 200 个分层历史 GIF。视频候选覆盖单人、共同主角、局部/无头、双人紧密接触、遮挡、细微/中等/大幅动作、快速运动、无脸/可用脸/遮挡脸、低光和画面边缘。离线运行候选检测、姿态、跟踪、动作幅度、面部身份/咨询动态以及可选分割/ReID，报告：

- 每种场景的检测/姿态覆盖率、最大缺口、跟踪丢失和主角集合歧义分布；
- 动作幅度分档、静止/背景参照的噪声底分离度、背景覆盖/仿射内点率、阶段完整性和相机运动补偿前后差异；
- 人脸外观嵌入对身份失效的边际捕获率，320 px 与候选高分辨率解码的人脸可用率/耗时，以及面部关键点/表情咨询证据覆盖率；
- 历史 GIF 曲线特征有效率、缺失原因、单项成本和约 16.9k 项的估算 GPU 小时；
- 必需证据可用率、`REVIEW_REQUIRED` 预测比例和不同证据族的边际增益；
- 候选 wall-clock p50/p95、按视频估算 p95、峰值 RAM/VRAM；
- 本地 VLM 的 `ABSTAIN`、JSON 失败和语义冲突率；
- Ollama 当前驻留状态，以 `/api/ps` 实测为准，不能仅根据配置推断显存占用。

进入完整实现的建议硬门是：候选 p50 不高于 10 秒、p95 不高于 30 秒，去重后的代表性视频 p95 不高于 20 分钟，峰值 VRAM 不高于 14 GB，并且第 7 节 40%/60% 可用性目标有达到可能。spike 未达门槛时先调整证据集/运行时，不继续堆叠八个生产里程碑。

### 15.2 运行时与打包边界

检测、姿态、跟踪、动作幅度、人脸身份/面部动态及可选分割/ReID 以版本化的 WSL2/CUDA sidecar 运行；PyInstaller EXE 只包含客户端、协议类型、健康检查和错误呈现，不把当前仓库尚不存在的 Torch/ONNXRuntime 与模型权重直接打进 EXE。模型权重外置、内容寻址，并由 `model_bundle_hash` 和 manifest 校验。sidecar 至少提供 health、capabilities、evaluate、cancel 和版本查询接口。

当前主 VLM 配置有较长 `keep_alive` 且不会在每次加载前主动释放显存，因此不能假设 Ollama 与视觉 sidecar 可无条件共存。`gpu_policy: exclusive` 以整段源视频为调度单位，每视频最多执行一次 sidecar → VLM 切换，而不是每候选切换：

1. 先完成 embedding dedup，此时允许现有 Ollama embedding 模型驻留；
2. 查询 Ollama `/api/ps`、记录实际驻留模型并释放 GifAgent 可管理的 Ollama 模型；
3. sidecar 对该视频的所有候选共享解码、轨迹和确定性证据；所有语义边界修正也必须在此阶段完成并重算组；
4. sidecar 释放 GPU 后，只恢复一次主 VLM，批量执行该视频所有候选的语义复核；
5. VLM 只能给出通过/冲突/`ABSTAIN`，不得触发边界修正或第二次 sidecar 换入；
6. 若 spike 证明共存稳定且不超过显存门槛，才允许显式切换到 coexist 配置。

运行期间还必须满足：

- 检测、姿态和咨询分割按批次调度并复用已解码帧；
- 同一视频重叠候选共享人物轨迹和帧级证据缓存；
- 先运行低成本检测和覆盖检查，明显不支持的窗口不再调用昂贵语义复核；
- 缓存键包含源哈希、时间戳、解码参数和模型哈希；
- direct GUI 只提交异步任务并显示阶段、进度、预计耗时和取消入口，不能阻塞 UI 线程；
- 显存不足时缩小批次并重试一次，仍失败则以 `vram_exhausted` 进入 `REVIEW_REQUIRED`；
- 超过候选或整视频 wall-clock 预算时以 `continuity_budget_exhausted` 待复核，不得降低证据要求。

吞吐量是次级指标。不能通过降低采样率、忽略遮挡或跳过必需模型来维持产量。

## 16. 故障处理

- 解码失败、模型未加载、显存不足、预算耗尽、VLM 超时和证据损坏都产生结构化失败记录；
- 可重试故障与永久输入故障分开；
- 日志不得写入 API Key、完整环境变量或未脱敏凭据；
- 模型或外部证据目录不可用时，新增素材不得绕过门禁；
- 数据库写入失败时不得发布没有评估记录的正式 GIF；
- 人工复核导入失败时保持原状态并给出字段级原因；
- `HARD_REJECT` 被人工推翻时保留原判断、申诉证据和人工事件，不删除失败样本；
- 关闭 `continuity_gate.enabled` 是显式回退到旧行为，界面和运行清单必须醒目标记“强保证未启用”。

## 17. 实施边界与预期文件

后续实施计划应按以下独立里程碑组织，避免在一个任务中混合全部风险：

0. 可丢弃的 50 个视频候选 + 200 个历史 GIF 可行性 spike、资源实测和 go/no-go 报告；
1. 领域类型、配置、资格清单、数据表和纯门禁策略；
2. sidecar 协议、安装/doctor、参考检测/姿态/跟踪/人脸身份与面部动态后端和合成媒体测试；
3. 主角集合、身体几何、动作幅度、人脸身份补强、面部动态咨询、动作语义和本地 VLM 复核；
4. 稳定 pre-export key、非传递 dedup group、组通过 supersede、重分组、staged/direct 接入、按位置预算观测和影子模式；
5. 持久化人工复核、`continuity_resume` job、单候选 manifest、跳过输出截断、追加 materialize、review bundle 和可观测性；
6. 资格评测工具、阈值冻结、manifest 签发与 canary；
7. 历史曲线软特征回刷及按需 GIF-only 复核；
8. 曲线逻辑回归与 Preference Memory source-grouped holdout 发布。

预计新增模块位于 `app/services/continuity/`，避免继续扩张现有 `action_boundary.py` 和 `action_pipeline.py`。流水线只通过一个稳定 facade 调用该子系统；重模型实现位于独立 sidecar 项目/目录，通过版本化协议连接。数据库迁移沿用现有幂等 schema 入口；测试分别位于 `tests/continuity/`、流水线集成测试和真实媒体验证脚本。里程碑 0 的 go/no-go 报告经用户复核后，才编写其余生产实施计划。

## 18. 验收标准

设计实现完成必须同时满足：

1. 新增素材在 enforce 模式下只有 `AUTO_PASS` 才能自动导出；`MANUAL_PASS` 可以恢复导出，但不能绕过 Quality MoE。
2. 任一必需组件不可用时没有固定窗口或旧 VLM 降级旁路。
3. 主角集合身份、姿态、动作幅度、接触和动作阶段都有独立、可审计结果；首版轮廓/掩码明确标为咨询证据。
4. 没有可用人脸时明确为不适用；可用人脸嵌入只增强身份分组，面部关键点/表情动态保持咨询证据，不因缺失或单一异常降低自动通过率。
5. 身体曲线硬门与审美软评分在代码、数据和策略上隔离。
6. staged 与 direct 路径行为一致。
7. 所有评估均冻结输入、配置、模型和提示词身份。
8. 未生成 GIF 的候选也有独立、可恢复的 review item；人工放行后能从 Quality MoE 开始幂等续跑。
9. 历史批量首版只生成曲线软特征；按需 GIF-only 状态不修改原文件、业务状态或反馈历史，并同时使用 like/favorite/dislike 训练曲线偏好。
10. GLM-5.3-Flash 不作为生产运行时依赖，Harness 复核是显式人工流程。
11. `HARD_REJECT` 除镜头断裂外必须由两个不同 `independence_group` 确认，同源派生信号不得重复计数。
12. 当前模型组合同时通过安全与可用性门禁、且资格 manifest 与当前哈希完全匹配后才能切换到 enforce。
13. PyInstaller EXE 不捆绑重模型栈，sidecar 与 Ollama 以视频为单位的显存调度、耗时预算和失败路径已经过里程碑 0 实测。
14. continuity enforce 启用时 Quality MoE 不再重复执行旧 `action_incomplete` 硬拒绝。
15. 回填和数据库迁移有 dry-run、备份、幂等、恢复和零数据丢失证据。

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
