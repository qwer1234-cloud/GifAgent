# GLM-5.3-Flash 600M Token 冲刺 — 总体设计 v4.1（四次评审修订）

> **文档性质：** 这是**设计文档**，不是实施方案。每个 Workstream 由独立 agent
> 依照本文档的契约与约束，各自产出一份
> `docs/superpowers/plans/2026-08-XX-token-sprint-w{N}-{slug}-implementation.md`
> 实施方案（格式对齐 `2026-08-28-pipeline-module-split-implementation.md`），再交给执行 agent。
> 撰写实施方案的 agent **必须原样复制本文「全局约束」一节**进入自己的 Constraints。
>
> **当前状态（v4.1 生效时）：只允许推进 T-1（协调会话）。W1–W8 的执行一律冻结，
> 直至 T-1 全部验收门通过。** 各 Workstream 的实施方案文本可以并行撰写，但不得执行。
>
> **v4.1 变更（响应第四次复审）：**
> ① `refactor_base_sha` 必须已经落在 `master`，以
> `git rev-parse master == refactor_base_sha` 为硬门；T-1 自举脚本、测试与计划文件全部改由
> coordinator 独占。② Cosmic Ray 能力 spike 提前到 `sprint_base_sha` 形成前，形成基线后
> 禁止再改依赖。③ `.ready`、`.done`、receipt 三者强制绑定同一 artifact SHA；review 后产物
> 不可原地修改。④ 已验收的不可变 JSONL shard 成为 W8 的 canonical 输入，`enrichment.db`
> 降为带 `shard_sha256` 的可重建缓存。⑤ W2 修正判断量为 480–2,880 个 pair / 960–5,760
> 次双模板判断，并增加 `pool_version` + `ranker_set_hash`。⑥ 输入库路径强制保存
> `Path.resolve()` 后的绝对路径；动态工作区/生产计数统一标注 `as_of` 并由 T-1 重采集。
>
> **v4 保留变更（响应前三次评审）：**
> ① T-1 重写：基线改为**范围明确的 refactor checkpoint**（禁止 `git add -A` 打包脏工作区）→
> `refactor_base_sha` → 干净 `sprint/integration` worktree → 协调会话提交依赖/.gitignore/计划文件 →
> 唯一 `sprint_base_sha`；所有 W 分支显式从 `sprint_base_sha` 创建、只合入 `sprint/integration`；
> **删除"先从旧 HEAD 开工、基线出现后 rebase"路径**；给出重构无法短期 checkpoint 时的
> 永久冻结 fallback（依赖未提交代码的目标**删除**而非暂挂）。
> ② Manifest 改单写者协议：executor/reviewer 只发布产物状态链 + 唯一 receipt；协调会话验证后单写者
> 原子重写 canonical manifest；临时文件必须与目标同目录。
> ③ 新增 `inputs_manifest.json`：钉死生产库绝对路径，W3 输入以不可变副本 + 哈希进共享根。
> ④ W1 重建契约修正：`rebuild_index()` 是增量续跑，必须先清空 FTS 与 `search_index_state`
> 再重建，验收三计数一致且 `errors == 0`，并补「FTS 空但 state 已完成」回归测试。
> ⑤ W2 重构：拆分「候选查询库」与「完整标注 eval 子集」，NDCG 只对 fully judged 查询计算；
> `pool_sources` 改为各 ranker 排名映射；争议项裁决前排除评测；双模板标注定名 silver，人工裁决为 gold。
> ⑥ W5 修正：单向量往返性质为 `blob_to_vector(vector_to_blob(v)) ≈ l2_normalize(v)`。
> ⑦ W7 结论改为两个调查假设（索引为何未构建 / 异常可观测性），不预设"无告警 P1"——
> 现有代码已返回 `degraded=True` 与诊断文本且 UI 展示。
> ⑧ 次要契约：T0 探针加到 60 条并全量复核高显式层；`enrichment_refusals` 主键加入
> `prompt_version`；新增统一 `tests/token_sprint/` 目录；脚本强制 `--sprint-root` 参数
> （环境变量仅作显式 fallback），预检必须跨两次命令调用（ZCode Windows Auto 终端优先
> Git Bash 其次 cmd，不保证 PowerShell 与会话环境变量持久性）。
>
> **v2/v3 保留要点：** 额度为活动赠送，仅限 ZCode harness 内消耗，无任何直连 API 轨道；
> 工作包计量 + `.ready`/`.done` 分权；T0 内容与保真门；W6 延后为备选池；W8 影子接入。

**预算：** 约 600M GLM-5.3-Flash tokens（活动额度），截止 2026-08-30（48 小时）。

**目标函数：** 在合规前提下最大化「已验收工作包」产出。600M 全部用完**不作硬性承诺**；
消耗与质量冲突时以质量优先，剩余额度认亏。禁止为烧量制造空转对话。

---

## 核心事实

**额度与平台：**

- 额度仅可在 ZCode harness 内使用（活动条款，已确认）。**任何本地脚本直连
  `open.bigmodel.cn` 端点都属非支持环境**，处置阶梯为风控→限流→冻结→封禁。合规红线。
- ZCode 官方支持 general-purpose subagent（读写文件、执行命令、并行/后台，
  https://zcode.z.ai/en/docs/subagents）。ZCode 使用统计同时显示本机会话 token 与远端套餐消耗
  （https://zcode.z.ai/cn/docs/usage-stats）；开放平台控制台是额度余量最终真值。
- **ZCode Windows 的 Auto 终端优先 Git Bash，其次 cmd，不保证 PowerShell**
  （https://zcode.z.ai/en/docs/install）。因此：冲刺脚本不得依赖 PowerShell 语法或
  会话环境变量的跨会话持久性；共享根传递以显式 CLI 参数为准（见全局约束 4）。

**仓库与数据事实（动态观察值；T-1 必须重采集并写入 manifest）：**

- `as_of=2026-08-28 第四次复审`：当前 HEAD 仍为 `00559804`；重构持续进行中，最近一次观察为
  **52 个已跟踪文件改动（约 +32,639/−9,164 行）**，另有未跟踪的 `app/pipeline/`、
  `app/services/vector_math.py`、多份测试与数据结果，以及 `-I`、`nomic-embed-text` 等不明产物。
  这些数字只说明脏工作区风险，不是 checkpoint 清单；T-1 必须重新采集精确 staged diff，
  不明产物**禁止进入任何 checkpoint**并登记为待清理项。
- `.gitignore` 忽略 `dist/`、`data/exports/`、`data/thumbs/`、`data/library.db`、
  `data/quality_lab.db`、`.worktrees/` 等——worktree 天然拿不到生产库与导出数据。
- `as_of=2026-08-28 第四次复审`：生产库 `dist/GifAgentUI/data/library.db` 最近一次只读查询为
  `candidate_gifs = 11,622`
  （**计数持续变化**，一切以 W1 快照哈希与快照内计数为准）；
  `candidate_search_fts = 0`（空）；`candidate_vectors = 9,328`。
- `library_search.rebuild_index()` 是**基于 `search_index_state` 的增量续跑**，
  不是无条件全量重建；存在「FTS 为空但 state 已完成」的可能状态。
- 搜索服务对空 FTS **并非无告警**：返回 `degraded=True` 与 `FTS index: 0/N...` 诊断
  （`library_search.py:156`），UI 展示该诊断（`app/ui/tabs/search.py:92`）。
  另一独立事实：`_rank_with_text` 吞掉 `OperationalError`。两者是 W7 的调查假设，不预设定级。
- `candidate_gifs.base_rag_similarity` 导入时取 `gif_worthiness`
  （`scripts/import_adaptive_candidates.py:55`），不是查询相关相似度，禁止用作检索评测基线。
- `candidate_search_fts` 的 `candidate_id` 列 `UNINDEXED` 且**无唯一约束**，
  重复 upsert 会留重复行（已验证）。
- `vector_to_blob(vector, *, embedding_dim)` **先做 L2 归一化**再序列化：
  `[3,4]` 往返得 `[0.6,0.8]`（已实测）；且只接受单向量，packed `k*dim` 输入抛 mismatch。
- FTS5 tokenizer 为 `porter unicode61`（只词干化英文）。
- `app/quality_lab/metrics.py` 已有 `ndcg_at_k(relevances, k)`。
- JSON 校验统一走 `app.services.json_guard.parse_json_response`（`.ok`/`.data`）。
- 历史教训：普通模型会把成人 caption G-rate（Agent.md llava 案例）——静默消毒是真实风险。
- dev 依赖现状：仅 `pytest`、`respx`。搜索与指标相关测试最近复跑 65 passed。

---

## 执行模型

**一切模型消耗都发生在 ZCode 会话内。** 计划与验收的基本单位是**工作包**：

| 工作包类型 | 内容 | 产出 |
|-----------|------|------|
| 工程包 | 一个模块的突变测试 / 一组属性测试 / 一个审计维度 / 一个工具脚本 | diff + 测试通过记录 |
| 数据分片包 | 一个 50–200 条候选的分片，按指定 pass 做结构化转换 | `shard-XX.{pass}.{prompt_version}.{attempt_id}.jsonl` + QC 摘要 |
| 复核包 | 对一个已完成工作包跑测试/QC 脚本并抽检 | 复核结论（写入 `.done`） |

**工作包状态协议：** executor 原子发布产物后写 `.ready`，其中必须包含
`artifact_path`、`artifact_sha256`、自检 QC 摘要与 executor 会话标识；`.ready` 形成后产物
**不可原地修改**，重跑必须使用新版本路径。**只有 reviewer** 在对同一 SHA 实际运行测试/QC
并抽检通过后，才能原子生成 `.done`，其中包含 `artifact_sha256`、`ready_sha256`、测试/QC
证据、复核结论与 reviewer 会话标识。reviewer 随后发布绑定同一 SHA 的 receipt。
红线与台账**只统计 `.done`**。

**容量实测（T0 强制）：** 每类工作包完成后，从 ZCode 使用统计读取会话消耗并与控制台余量
差值交叉核对，记入用量日志；据此外推「红线所需工作包数」。此后每 2 小时抄录控制台余量。

**并发爬坡：** 1 个会话起步；稳定 2 小时且无限流后加开第二个（不同 worktree，
每个 worktree 是独立 ZCode 工作区），上限 3 个；会话内 subagent 扇出。
命中限流、复核积压或 QC 合格率下滑即停止扩张并回退一档。

**消耗放大器（按序启用）：** ① 加数据分片；② 加并行会话；③ 扩 W4 模块清单；
④ 全工作包加 reviewer 复核轮；⑤ 启用 W6 备选池。

**进度红线（剩余额度百分比，T0 实测后修正）：** T+12h ≤ 80%；T+24h ≤ 55%；T+36h ≤ 30%。
未达线按放大器加码；质量门失守反向裁剪（先砍 pairwise，再砍 W1 扩张分片）。

---

## 与进行中重构的关系（最高优先级约束）

`2026-08-28-pipeline-module-split-implementation.md` 正在由另一个 agent 执行。
以下为该计划 File Map，**本冲刺所有 Workstream 禁止修改**（只读引用允许）：

- `scripts/test_video_adaptive.py`
- `app/pipeline/`（整个包）
- `app/task_engine/artifacts.py` 与 `app/task_engine/artifacts/`
- `app/ui/candidate_review.py`、`app/ui/legacy_candidate_review.py`、`app/ui/tabs/*`、`app/ui/workbench.py`
- `app/services/preference_memory.py`、`app/routers/preference.py`、`app/services/candidate_vectors.py`
- `build_exe.spec`、`tests/task_engine/test_packaged_stage_imports.py`
- 该计划列出的全部 monkeypatch 测试文件
- `Agent.md`

**跨计划接口规则：** 引用管线符号只允许经 `scripts.test_video_adaptive` 的 facade 名
（`FACADE_NAMES` 重构期锁定）；不 import `app.pipeline.*` 内部路径。

**数据基线规则：** 重构计划用 `Get-Item data/*.db`（PowerShell，由该 agent 在其环境运行）
做根层基线体检。本冲刺：禁止写根层 `data/*.db*`；产物只进共享根 `data/token_sprint/`；
生产库冲刺期间只读。

---

## 全局约束（每份实施方案必须原样复制）

1. **所有模型消耗只经 ZCode harness。** 不新增任何调用 GLM/智谱端点的运行时代码或脚本；
   不把活动额度 API Key 写进任何配置、代码或文档。
2. **T-1 验收前 W1–W8 禁止执行**（实施方案文本可撰写）。所有 W 分支必须从
   `sprint_base_sha` 创建，只合入 `sprint/integration`；未经用户确认不合回 `master`。
3. 不修改「与进行中重构的关系」列出的文件；管线符号只经 facade import。
4. **共享根传递协议：** `scripts/token_sprint/*` 所有脚本必须接受 `--sprint-root` 参数；
   未传参时才读环境变量 `GIFAGENT_SPRINT_ROOT` 作为显式 fallback；两者皆缺或路径不存在时
   立即报错退出。禁止依赖 PowerShell 专有语法；脚本须可在 Git Bash 与 cmd 下运行。
5. 不写根层 `data/*.db*`、`data/exports/`、`data/labels/`、checkpoint、`dist/`、真实桌面同步目录。
   产物只进共享根。消费跨 Workstream 产物前必须按 canonical manifest 校验 SHA-256。
6. **manifest 单写者 + review 哈希绑定协议：** executor 只原子发布产物与 `.ready`；
   reviewer 校验 `.ready.artifact_sha256` 与实际文件一致后，原子写 `.done` 与唯一 receipt。
   receipt 文件名使用 Windows 安全时间戳 `YYYYMMDDTHHMMSSfffZ` + UUID，内容至少包含
   `receipt_id`、`artifact_path`、`artifact_sha256`、`ready_path`、`ready_sha256`、`done_path`、
   `done_sha256`、producer、reviewer、workstream、ts。canonical `sprint_manifest.json` 只由
   协调会话在验证实际文件、`.ready`、`.done`、receipt 四者一致后原子重写；重复 receipt
   按 `receipt_id` 幂等处理。原子写的临时文件必须与目标文件同目录。
   此四方链只约束跨 Workstream 数据/工程产物；`sprint_manifest.json`、`inputs_manifest.json`、
   `approved_enrichment_manifest.json`、`usage-log.md` 与 receipt 归档状态属于 coordinator 控制面，
   由协调会话单写者原子维护，不为自身生成 receipt。canonical manifest 应记录其他控制面文件
   的 SHA 与 schema/revision，但不得递归记录自己的 SHA。
7. 不改 `configs/models.yaml` / `configs/models.adult_candidate.yaml`。
8. **依赖与 lockfile 由协调会话独占**：所有依赖决策必须在 T-1、`sprint_base_sha` 形成前
   完成；任何 Workstream 不得运行 `uv add` 或改 `pyproject.toml` / `uv.lock`
   （白名单：`hypothesis`、`mypy`、`cosmic-ray`）。基线形成后不得追加白名单依赖。
9. 测试只用 `tmp_path` / 内存 SQLite / 共享根内快照副本。新测试只进三个新目录：
   `tests/property/`、`tests/mutation/`、`tests/token_sprint/`；不改已有测试文件。
10. 数据分片包只处理**文本**。不让任何 agent 读取 GIF、JPG 或视频内容。
11. 分片纪律：每片 50–200 条；一片一个会话；输出独立
    `shard-XX.{pass}.{prompt_version}.{attempt_id}.jsonl`；`attempt_id` 使用 UUID，禁止覆盖旧产物；
    拒绝行记 `{"candidate_id": ..., "refused": true}`；合法 JSON 但成人语义被消毒的行
    视同失败（保真检查识别）。
12. 工作包状态：executor 只产绑定 artifact SHA 的 `.ready`；`.done` 只由 reviewer 对同一
    SHA 实跑测试/QC 后原子生成；reviewer 再发布同 SHA receipt。`.ready` 后禁止原地修改产物，
    红线与台账只统计 `.done`。
13. 用量记录：每类工作包完成后记录 ZCode 使用统计；每 2 小时抄录控制台余量。
    两者写入 `docs/reports/2026-08-token-sprint/usage-log.md`（协调会话独占）。
14. 用户可见文案中文，代码标识与测试名英文。commit 风格对齐仓库现状。
15. 每个 Workstream 在独立 git worktree + 分支（`sprint/w{N}-{slug}`）上工作，
    并作为独立 ZCode 工作区打开。

---

## T-1 — 冲刺基线与共享数据拓扑（协调会话独占；当前唯一可推进项）

### Step 1：范围化 refactor checkpoint（用户已选定「让重构 agent 提交」）

**禁止 `git add -A` 或把整个脏工作区打成基线。** 流程：

1. 重构 agent 只提交其计划 File Map 内、能逐文件解释的代码与测试；
   `app/services/vector_math.py` **仅当调用方与测试一起闭环时**才纳入。
   `-I`、`nomic-embed-text`、数据结果等不明产物一律不入库（登记为待清理项）。
2. 提交前产出三件套：精确 staged diff、目标测试清单、完整回归结果；
   通过后把该 scoped checkpoint 提交或 fast-forward 到 `master`，记录 **`refactor_base_sha`**。
3. **master 落地门：** `git rev-parse master` 必须等于 `refactor_base_sha`；若不等，T-1 停止，
   不得从游离 commit 或其他分支继续。另记录未提交/未跟踪剩余项清单，证明其未进入 checkpoint。
4. 从 `refactor_base_sha` 创建干净 worktree + 分支 **`sprint/integration`**。
5. 协调会话先在一次性临时目录/隔离虚拟环境中完成限时 Windows Cosmic Ray 能力 spike：
   能稳定安装、生成最小突变并由测试验杀才纳入 dev 依赖；否则记录采用 stdlib `ast` fallback，
   本冲刺不再尝试 Cosmic Ray。随后一次性提交确定后的 dev 依赖（固定含 `hypothesis`、`mypy`，
   Cosmic Ray 仅在 spike 通过时加入）、`.gitignore` 追加 `data/token_sprint/`、本设计文档与
   各实施方案文件。得到唯一 **`sprint_base_sha`**，记入 canonical manifest；此后禁止改依赖。
6. 所有 W1–W8 分支**显式从 `sprint_base_sha` 创建**，只合入 `sprint/integration`；
   冲刺结束且用户确认后才合回 `master`。
7. **不存在"先从旧 HEAD 开工、基线出现后 rebase"的路径**——基线未成即不开工。

**Fallback（重构 agent 无法短时间形成干净 checkpoint）：**
基线**永久冻结**为 `00559804`，从它创建 `sprint/integration` 并继续 Step 4；
所有依赖未提交代码的目标（W4 白名单中的 `vector_math.py`、W5 的两条 `vector_math` 性质、
以及基线中不存在的任何测试依赖）**从本冲刺范围删除，而非暂挂**。
冲刺中途**禁止**给已开工的 worktree 更换基线。

### Step 2：共享产物根与输入清单

共享根 = 主检出绝对路径 `C:\Users\sunhao\Desktop\code\GifAgent\data\token_sprint\`
（根层 `data/*.db` 通配不匹配子目录，不污染重构基线；主检出 `master` 上该目录未被忽略前
会显示为 untracked——无害，重构计划的提交都是显式文件清单，不会误收）。目录结构：

```
data/token_sprint/
├── sprint_manifest.json            # canonical，协调会话单写者原子重写
├── inputs_manifest.json            # 协调会话产：输入源与不可变副本清单
├── receipts/                       # reviewer/协调会话发布的唯一 receipt（一产物一文件）
├── inputs/                         # W3 输入的不可变副本（result JSON、checkpoint 等 + 哈希）
├── library_snapshot.db             # W1 产
├── enrichment.db                   # W1 产；可重建缓存，不是 W8 canonical 输入
├── tag_vocab_v1.json               # W1 产
├── approved_enrichment_manifest.json  # 协调会话产（W8 版本与分片哈希契约）
├── shards/                         # 分片输入/输出/.ready/.done
└── evalsets/                       # W2 产
```

**`inputs_manifest.json`（协调会话用
`scripts/token_sprint/coordinator/prepare_inputs.py` 在 T-1 采集）：**
生产库路径必须先用 `Path(...).resolve(strict=True)` 解析，保存为完整字段 `source_db_abs`
（示例：`C:/Users/sunhao/Desktop/code/GifAgent/dist/GifAgentUI/data/library.db`）并记录采集时间；
W3 全部输入的**不可变副本**复制进 `inputs/`（`data/exports/` 下 result JSON、
`data/batch_checkpoint.json`、`data/adaptive_test_result.json`），逐文件记录
SHA-256、采集时间与 `required|optional`；可选输入缺失须显式记录 `status: absent`，不得静默跳过。
W3 只读这些副本，不读 worktree 相对路径。

### Step 3：发布协议（单写者 manifest）

- **executor**：目标同目录写临时文件 → 计算 SHA-256 → `os.replace` 原子改名 →
  原子写绑定该 SHA 的 `.ready`；此后产物不可原地修改。
- **reviewer**：重新计算产物与 `.ready` 哈希，实际运行测试/QC 并抽检 → 原子写绑定同一
  artifact SHA 的 `.done` → 原子写唯一 receipt。reviewer **不碰** canonical manifest。
- **协调会话**：用 `scripts/token_sprint/coordinator/reconcile_receipts.py` 轮询 receipts →
  校验产物、`.ready`、`.done`、receipt 四者哈希链一致 →
  以 `receipt_id` 幂等更新并单写者原子重写 canonical `sprint_manifest.json`
  （同目录临时文件 + replace）→ 成功后才把 receipt 移入 `receipts/processed/`。若在 manifest
  写入后、移动前崩溃，重启后重放同一 receipt 必须得到相同 manifest。
- **消费者**：只信 canonical manifest；读取前重新校验目标文件 SHA-256。

### Step 4：跨调用预检（防终端/环境假设失效）

`scripts/token_sprint/coordinator/preflight.py`：第一次调用 `--sprint-root <path> --write-nonce`
写入随机 nonce；**由新的一次命令调用**执行 `--sprint-root <path> --verify-nonce`
校验读回一致。两次调用必须是独立命令（验证 ZCode 终端下路径解析与文件可见性跨调用成立，
不依赖会话环境变量）。在协调会话与至少一个 W worktree 会话中各跑一遍。

**Coordinator File Map（T-1 自举，协调会话独占）：**

- `scripts/token_sprint/coordinator/preflight.py` — 跨独立命令的共享根 nonce 预检
- `scripts/token_sprint/coordinator/prepare_inputs.py` — 解析绝对源路径、复制输入、写哈希清单
- `scripts/token_sprint/coordinator/reconcile_receipts.py` — 校验四方哈希链、幂等重写 manifest、归档 receipt
- `tests/token_sprint/test_coordinator_protocol.py` — 覆盖上述三条控制面路径及崩溃恢复

### T-1 验收门

- 非 fallback 时 `git rev-parse master == refactor_base_sha`；canonical manifest 含
  `refactor_base_sha`（或 fallback 冻结决议）与 `sprint_base_sha`；
- `inputs_manifest.json` 完整（`source_db_abs` 为存在的绝对路径 + 全部 W3 副本哈希/缺失状态）；
- 跨调用预检在两个会话通过；
- `uv run pytest -q tests/token_sprint/test_coordinator_protocol.py` 通过，至少覆盖：缺失/非法
  `--sprint-root`、两进程 nonce、artifact/ready/done/receipt 任一哈希不匹配即拒绝、重复 receipt
  幂等、manifest 写入后崩溃重放、Windows 安全 receipt 文件名；
- `sprint/integration` worktree 内冒烟测试通过：`uv run pytest -q tests/test_adaptive_config.py`
  （HEAD 已存在），若基线含 `tests/test_pipeline_facade.py` 则一并运行；
- Cosmic Ray spike 决议写入 manifest；依赖以单独 commit 落在 `sprint/integration`，
  `sprint_base_sha` 形成后全冲刺依赖冻结。

---

## T0 — 内容安全与保真门（W1/W2 数据轨的生死门）

1. `make_shards.py --probe` 生成 **60 条**探针分片，按显式程度三层分层（各 20 条，
   利用快照分数与标签划层）。
2. ZCode 会话按 P3（英文检索关键词）处理探针片，同时记录容量成本
   （ZCode 使用统计 + 控制台差值）。
3. `validate_shard.py` 三指标：
   - **拒绝率** ≤ 15%；
   - **schema 合法率** ≥ 95%；
   - **语义保真率**（高显式层）≥ 90%：启发式检查显式词汇/行为保留比例并标记疑似消毒行；
     **人工复核全部高显式层样本（20 条）+ 全部疑似消毒行**后裁定。
4. 三门全过 → 放行 200–500 条试验集（W1 Stage B）；试验集再过同门（保真抽检 10 条）
   → 允许分片爬坡。Stage A 词表冻结前必须人工审查语义保真。
5. **任一门失败：W1/W2 的模型标注部分整体取消**，确定性工具照常交付，预算转工程轨。
   此 fallback 必须写进 W1/W2 实施方案。

---

## Workstream 总览

| Workstream | 轨道 | 优先级 | 时机 | 弹性 |
|------------|-----|--------|------|------|
| W4 突变测试补强 | A 工程 | **P0** | T-1 验收后 | 模块清单可扩 |
| W5 Hypothesis 属性测试 | A 工程 | **P0** | T-1 验收后 | 固定 |
| W7 定向代码审计 | A 工程 | **P0** | T-1 验收后 | 维度可扩 |
| W1 富化工具链 + 分片富化 | B 数据 | P1 | 工具随 T-1 验收；分片过 T0 门后爬坡 | 分片数（主吸收器） |
| W2 评测 harness + 标注集 | B 数据 | P1 | harness 随 T-1 验收；标注过 T0 门后 | pairwise / judged 子集规模 |
| W3 历史数据体检 | B 数据 | P1 | 规则扫描随 T-1 验收；LLM 只审疑难 | 固定 |
| W8 影子 FTS | A 工程 | P1 | W1 试验集产出后 | 固定 |
| W6 类型注解 + mypy strict | A 工程 | P2 备选池 | T+24h 红线未达且放大器 ①–④ 用尽 | 模块数 |

---

## W1 — 富化工具链 + 分片富化（P1）

**交付物（确定性工具，零模型消耗）：**

- `scripts/token_sprint/snapshot_library.py` — 按 `inputs_manifest.json` 钉死的绝对路径
  从生产库只读快照到共享根（首选 `VACUUM INTO`；失败提示停 GUI 重试）。
  **快照后全量重建 FTS，且必须先显式清空 `candidate_search_fts` 与 `search_index_state`
  再调用现有 `rebuild_index()`**（该函数是增量续跑逻辑，不清空则可能沿用旧 state 跳过重建）。
  验收：`COUNT(*) == COUNT(DISTINCT candidate_id) == candidate_gifs COUNT(*)` 且
  `errors == 0`。快照哈希与快照内计数登记 receipt；后续一切计数以快照为准（生产库计数在漂移）。
- `tests/token_sprint/test_snapshot_fts_rebuild.py` — 回归锁：构造「FTS 为空但
  `search_index_state` 已完成」的库，验证快照重建路径能强制全量重灌而非增量跳过。
- `scripts/token_sprint/make_shards.py` — 从快照分层导出 `shards/shard-XXX.input.jsonl`
  （50–200 条/片；字段：`candidate_id`、`vlm_summary_json`、`tags_json`、`scenario_keys_json`；
  `--probe` 生成 60 条三层探针片）
- `scripts/token_sprint/validate_shard.py` — 单分片 QC：schema、覆盖率、拒绝率、
  语义保真启发式（显式词汇保留比 + 疑似消毒行标记），产出 QC 摘要 JSON
- `scripts/token_sprint/merge_shards.py` — 只合并有 `.done` 的分片进 sidecar 库
- sidecar 库 `enrichment.db`：**仅为可从 approved JSONL shards 重建的查询缓存，不是 W8
  的 canonical 输入。** merge 前必须验证 `.done.artifact_sha256`、receipt 与 shard 实际哈希一致，
  并把 `shard_sha256` 写入每一行：

```sql
CREATE TABLE IF NOT EXISTS candidate_enrichment (
    candidate_id      TEXT NOT NULL,
    pass_id           TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    output_json       TEXT NOT NULL,
    shard_id          TEXT NOT NULL,
    shard_sha256      TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (candidate_id, pass_id, prompt_version)
);
CREATE TABLE IF NOT EXISTS enrichment_refusals (
    candidate_id   TEXT NOT NULL,
    pass_id        TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    shard_id       TEXT NOT NULL,
    shard_sha256   TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'refused',   -- 'refused' | 'sanitized'
    PRIMARY KEY (candidate_id, pass_id, prompt_version)
);
```

**分片执行协议：** 一会话领一片 → 按 prompt 模板逐条产出 → 原子发布
`shard-XXX.{pass}.{prompt_version}.{attempt_id}.jsonl` → `validate_shard.py` 自检 → executor 写绑定 shard SHA
的 `.ready` → reviewer 重新验哈希并复核（重跑 QC + 抽检）→ reviewer 产绑定同一 SHA 的
`.done` 与 receipt。JSONL shard 一旦 `.ready` 即不可原地修改；重跑使用新版本路径。
prompt 模板按版本保存为 `scripts/token_sprint/prompts/{pass_id}/{prompt_version}.md`，manifest
同时记录 `prompt_sha256`，禁止覆盖旧模板或即兴改写（改模板 = 新建 `prompt_version` 重过门）。
获批 JSONL shards 是 W8 的 canonical 来源；`enrichment.db` 可随时从这些 shards 全量重建。

**Pass 顺序（逐轮过门再扩）：**

| 阶段 | Pass | 内容 |
|------|------|------|
| Stage A（探针+试验集） | P1a 词表挖掘 | 试验集聚合规范标签词表（300–800 词，英文小写 snake_case），人工保真审查后冻结 `tag_vocab_v1.json` |
| Stage B（试验集 200–500 条） | P3 英文检索关键词 → P1b 标签映射 → P2 场景元数据 | 每轮独立过 QC 门 |
| Stage C（爬坡，弹性） | 已验证 pass 逐片扩量；余量开 P4 双语摘要 / P5 embedding 文本 | 每片：schema ≥98%、拒绝 ≤15%、保真抽检通过、抽检 20 条无语义错误 |

**禁区：** 不改 `app/services/library_search.py` 产品代码（快照重建只调用现有函数 +
快照库上的 SQL 清理；产品代码改动属 W8）；不写生产库；不内置模型调用。

---

## W2 — 评测 harness + 标注集（P1）

**前置依赖（硬）：** W1 快照 + FTS 全量重建验收完成（经 canonical manifest 确认）。

**规模契约（v4.1 修正——判断量必须与查询规模匹配）：**

- `evalsets/query_bank_v1.jsonl` — **候选查询库** 100–200 条
  （人工种子 30–50 + ZCode 扩写，`origin: human|expanded`）。
- `evalsets/judged_queries_v1.json` — **完整标注 eval 子集**：从查询库选 **20–40 条**
  （下限 20，实际规模按 T0 实测容量定），每条查询对其**全部并集池候选**完成标注。
  单查询池 = 三路 top-24 并集（24–72 个唯一候选），子集总量为约 **480–2,880 个
  query-candidate pair**；双模板各标一次后为约 **960–5,760 次模型判断**——这是标注预算的
  主开销，必须按这两个不同单位记录，先算再扩。
- 每次候选池生成都写 `pool_version`、`ranker_set_hash`（参与 ranker 名称、配置、代码 SHA、
  查询库 SHA 的稳定哈希）及逐查询候选集合哈希。**NDCG 只对目标 pool 版本下 fully judged
  的查询计算**；查询库其余条目仅作后续扩标储备，不进指标。

**标注与裁决：**

- 候选池三路：① 重建后 v1 FTS 纯文本排序；② `library_search` 真实 hybrid
  （快照 + `candidate_vectors`）；③ W8 影子索引可用后追加（增量只标并集差集）。
- v1 FTS + hybrid 形成初始 pool 版本；W8 加入后必须创建**新的** `pool_version`。新版本差集完成
  双模板标注且争议处理前，该查询不得在新版本下标记 fully judged，也不得进入该版本 NDCG。
- qrels 行 schema：`{query_id, pool_version, ranker_set_hash, candidate_id, grade(0-3), rationale,`
  `pool_sources: {"fts_v1": rank|null, "hybrid": rank|null, "fts_v2": rank|null},`
  `label_tier: "silver"|"gold", status: "judged"|"disputed"|"adjudicated"}`。
- 同一对用两个措辞模板各标一次：一致 → **silver**；不一致 → `disputed`，
  **裁决前从评测集中排除（绝不隐式按 grade 0 计）**；人工裁决后升 **gold**。
- **禁止**用 `base_rag_similarity` 作基线（它是 `gif_worthiness`）。
  `eval_harness.py` 对比真实 ranker（FTS-only vs hybrid vs W8 v2），
  报告 `pool_version`、`ranker_set_hash`、NDCG@10/@24、逐查询明细与 judged 覆盖率。

**其余交付物：** `scripts/token_sprint/eval_harness.py`（复用 `ndcg_at_k`，随 T-1 验收后可开发）、
`scripts/token_sprint/synth_preference_stress.py`（内存 SQLite 压测 profile build 7 gates）、
`docs/reports/2026-08-token-sprint/w2-evalset-baseline.md`
（含"生产 FTS 为空"的复现证据，定性描述遵循 W7 的两假设框架，不预设结论）。

**弹性吸收器：** pairwise 对比标注 + judged 子集扩容，规模由剩余额度决定。

**验收门：** 标注受 T0 门管辖；silver 一致率 ≥ 80%（disputed 不算失败）；
每条 qrels 的 `pool_version`、`ranker_set_hash` 与 `pool_sources` 可追溯；同一 pool 版本下
`eval_harness.py` 两次运行数字一致；指标只含该版本 fully judged 查询。

---

## W3 — 历史数据体检（P1）

规则先行，模型只看疑难。`scripts/token_sprint/audit_data.py` 纯规则全量扫描：
缺字段、分数越界、断链 `artifact_path`、编码乱码（根目录 `version2_*.md` mojibake 线索）、
时间戳矛盾。疑难样本汇成分片（每片 ≤100 条）交 ZCode 会话复审定级。

**输入（只读，v4 修正）：** 一律读共享根 `inputs/` 下的**不可变副本**
（由 T-1 按 `inputs_manifest.json` 采集，含哈希），外加 W1 快照。
**禁止**读 worktree 相对路径的 `data/exports/` 等（worktree 中不存在）。

**交付物：** 扫描器 + `docs/reports/2026-08-token-sprint/w3-data-audit.md`；
修复脚本只交付 `--dry-run`。
**验收门：** 副本清单全覆盖；LLM 复审抽查 20 条无误报。

---

## W4 — 突变测试补强（P0，test-only）

**目标模块白名单（重构 File Map 之外且存在于 `sprint_base_sha`）：**
`app/services/clip_merge.py`、`export_ranking.py`、`grid_select.py`、
`timeline.py`、`taste_map.py`、`narrative_curation.py`、`collections.py`、`desktop_export_sync.py`、
`app/quality_lab/metrics.py`、`calibration.py`、`promotion.py`、
`app/task_engine/repository.py`、`fingerprints.py`。
**条件项：** `app/services/vector_math.py` 仅当 checkpoint 将其闭环纳入 `sprint_base_sha`
时列入；fallback 冻结 `00559804` 时**从范围删除**（不暂挂）。T-1 完成时由协调会话裁定并记录。

**工具决策已在 T-1 冻结：** T-1 的限时 Windows spike 若通过，则 `cosmic-ray` 已包含在
`sprint_base_sha` 的 dev 依赖中；若失败，则本 Workstream 固定使用自研
`scripts/token_sprint/mutation_harness.py`（stdlib `ast` 四类算子 + subprocess pytest 验杀），
不得在开工后再次申请依赖。**突变必须在临时副本运行**（目标模块拷贝到 `tmp_path` 沙箱），
禁止就地改写 worktree 文件。fallback harness 首个验收必须用一个已知可杀死的 sentinel mutant
证明 pytest 实际导入的是临时副本，而不是 worktree 原模块。

**流程：** 逐模块生成突变体 → 现有测试验杀 → 存活突变体交 subagent 写杀伤测试进
`tests/mutation/test_mut_{module}.py` → reviewer 验证「杀死突变体且原始代码下全绿」。
真实 bug 登记 W7，不修产品代码。红线未达时优先扩此项。

**验收门：** 每模块突变得分前后对比；`tests/mutation/` 全绿；
`git status --porcelain --untracked-files=all -- app/` 输出为空。

---

## W5 — Hypothesis 属性测试（P0，test-only）

**目标性质（`tests/property/`）：**

- `extract_config` 缺省冻结与幂等——`from scripts.test_video_adaptive import extract_config`
- `normalize_vlm_unit_score`：0–100 整数与 legacy 浮点双轨、输出恒在 [0,1]
- `clip_merge`：span ≤ `max_merge_span_s`、gap > `merge_gap` 必断链、区间有序不重叠
- `quality_lab.calibration`：PAV 单调不减、端点约束
- `grid_select`：9 桶时间覆盖、去重数量上界
- `taste_map.project_taste_map`：输出形状、平移不变性
- `timeline.load_timeline_window`：窗口外零元素、`max_thumbnails` 上界

**条件项（`vector_math.py` 进入 `sprint_base_sha` 时启用；fallback 时删除）：**

- **单向量（v4 修正——`vector_to_blob` 内部先 L2 归一化）：**
  对有限、非空、非零向量断言
  `blob_to_vector(vector_to_blob(v, embedding_dim=len(v))) ≈ l2_normalize(v)`
  （显式浮点容差，如 `atol=1e-6`）；对已归一化向量断言往返恒等（同容差）；
  `l2_normalize` 幂等；**零向量与含 NaN/Inf 输入的当前行为单独锁定**（pin 现状，不猜语义）。
- **packed prototypes（不经 `vector_to_blob`）：** 构造 `k*dim` packed blob
  （`np.float32.tobytes`）→ `blob_to_vector` + reshape 逐行一致；
  `max_cosine(candidate, packed_blob)` = 逐 prototype 余弦最大值；
  `vector_to_blob` 对 packed 输入抛 mismatch 作为负性质锁定。

**约定：** `hypothesis` 由协调会话添加；`deadline=None`、显式 seed；
需要 import `app.pipeline.*` 内部的性质直接放弃。
**验收门：** `uv run pytest -q tests/property/` 全绿，总时长增量 < 60s。

---

## W6 — 类型注解 + mypy strict（P2 备选池，默认不启动）

启动条件：T+24h 红线未达且放大器 ①–④ 用尽。范围：`app/quality_lab/` →
`app/services/`（排除 `preference_memory.py`、`preference_*.py`、`candidate_vectors.py`、
`reranker.py`、`embedding.py`，及 W8 所属 `library_search.py`、`workbench_schema.py`）→
`app/task_engine/`（排除 `artifacts*`）；`app/ui/` 跳过。
只加注解不改行为；`# type: ignore[code]` 位置登记 W7。
`mypy` 与 `[tool.mypy]` 变更由协调会话统一落盘。每模块清零后单独 commit。

---

## W7 — 定向代码审计（P0，read-only）

**五个维度，证据到 file:line，finding 附最小复现测试草图：**

1. SQLite：`busy_timeout` / WAL / 连接生命周期一致性
2. 并发：`TaskWorker` lease/heartbeat、`desktop_export_sync` 调度线程、Gradio 回调共享状态
3. Windows：路径分隔符、编码（结合 W3 mojibake 证据）、原子写完整性
4. HTTP：httpx 超时/重试语义在 `llm_client` / `embedding` / `api_client` 间的不一致
5. 资源泄漏：subprocess、文件句柄、临时目录清理

**登记的调查假设（v4 修正——不预设定级）：** 生产 `candidate_search_fts` 为 0 行属实，
但服务已返回 `degraded=True` 与 `FTS index: 0/N...` 诊断且 UI 展示
（`library_search.py:156`、`app/ui/tabs/search.py:92`），"静默无告警"**不成立**。W7 需分别调查：
(a) **索引生命周期**——生产索引为何从未构建（初始化/重建触发链路缺口）；
(b) **异常可观测性**——`_rank_with_text` 吞掉 `OperationalError` 是否应保留日志/指标。
定级以调查结论为准。

**交付物：** `docs/reports/2026-08-token-sprint/w7-audit-{dimension}.md` × 5 +
`w7-summary.md`（含 W4/W6 登记项）。不改产品代码。
**验收门：** reviewer 每维度抽查 3 条，误报率 < 1/3。

---

## W8 — 影子 FTS（P1，幂等 + 版本与分片哈希契约）

**版本与分片契约：** 协调会话在 W1 各 pass 过门后，把采用的
`(pass_id, prompt_version, prompt_sha256, {shard_id: sha256} 精确清单)` 写入
`approved_enrichment_manifest.json`（reviewer 签署）。`build_fts_v2.py`
**只消费该清单列出的不可变 JSONL shard 文件**：逐文件重算 SHA-256，按
`(pass_id, prompt_version, prompt_sha256, shard_id, sha256)` 五元组精确验收后解析行；
任一缺失、哈希不符、
`.done`/receipt 不匹配即整体拒绝构建。`enrichment.db` 仅是可重建查询缓存，不能作为 W8
canonical 输入，也不能用其中的同名行覆盖 approved shard 内容。

**幂等契约（FTS5 无唯一约束，重复 upsert 留重复行——已验证）：**
唯一内容表驱动 + 单事务全量重建：

```sql
CREATE TABLE IF NOT EXISTS fts_v2_content (
    candidate_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    tags TEXT NOT NULL,
    source_path TEXT NOT NULL,
    enrichment_manifest_sha TEXT NOT NULL
);
-- candidate_search_fts_v2 每次构建：单事务内全量 DELETE 后从 fts_v2_content 重灌
```

**File Map（均不在重构冻结区）：**

- Modify: `app/services/workbench_schema.py`（v2 DDL，additive）、
  `app/services/library_search.py`（`GIFAGENT_SEARCH_FTS_V2=1` 时读 v2 表，缺省行为不变）
- Create: `scripts/token_sprint/build_fts_v2.py`（只对快照/开发库执行）、
  `tests/token_sprint/test_search_fts_v2.py`

**验收门：** 双次构建后行数、distinct `candidate_id`、固定查询集结果哈希三者一致；
构建输入可逐行追溯到 approved manifest 中的 immutable shard SHA，且修改任一获批 shard
字节后构建必须 fail closed；开关缺省关时现有搜索测试全绿。
**执行时机：** W1 试验集产出后开发并在快照演练；对生产库执行在冲刺窗口外、
重构 P1–P3 门禁通过后由用户手动触发。回滚 = 关开关或 drop v2 表。

---

## 协调机制

**协调会话（coordinator）：** `sprint/integration` worktree 上的常驻 ZCode 会话，独占：
T-1 全部步骤、依赖与 lockfile、canonical `sprint_manifest.json` 与
`approved_enrichment_manifest.json`、`inputs_manifest.json`、receipts 验证与归档、
`usage-log.md`、coordinator 自举脚本与测试、设计/实施方案文件、红线监控与放大器决策、
W4/W5 条件项裁定、合并 `sprint/integration`。

**分支拓扑：** `refactor_base_sha`（或 fallback `00559804`）→ `sprint/integration`
（协调会话提交依赖/计划 → `sprint_base_sha`）→ 各 `sprint/w{N}-{slug}` 分支。
合并只进 `sprint/integration`（工程/测试/报告类过各自验收门即可合；W8 需全量 pytest 门；
W6 每日收盘合）。**合回 `master` 只在冲刺结束且用户确认后**，由协调会话执行。

**文件所有权矩阵（冲突即违约）：**

| 角色 | 允许写入 |
|------|---------|
| 协调会话 | `pyproject.toml`、`uv.lock`、`.gitignore`、`scripts/token_sprint/coordinator/`、`tests/token_sprint/test_coordinator_protocol.py`、本设计与 `docs/superpowers/plans/*token-sprint*-implementation.md`、`sprint_manifest.json`、`inputs_manifest.json`、`approved_enrichment_manifest.json`、`receipts/processed/`、`inputs/`、`docs/reports/2026-08-token-sprint/usage-log.md` |
| W1 | `scripts/token_sprint/` 中除 `coordinator/` 外的 snapshot/make_shards/validate/merge/prompts、共享根 immutable shards、可重建缓存 `enrichment.db`、`tag_vocab_v1.json`、`tests/token_sprint/test_snapshot_fts_rebuild.py`、`docs/reports/.../w1-*` |
| W2 | `scripts/token_sprint/eval_harness.py`、`synth_preference_stress.py`、共享根 `evalsets/`、`docs/reports/.../w2-*` |
| W3 | `scripts/token_sprint/audit_data.py`、`docs/reports/.../w3-*` |
| W4 | `scripts/token_sprint/mutation_harness.py`、`tests/mutation/`、`docs/reports/.../w4-*` |
| W5 | `tests/property/` |
| W6 | 白名单模块注解、`docs/reports/.../w6-*` |
| W7 | `docs/reports/.../w7-*` |
| W8 | `app/services/workbench_schema.py`、`app/services/library_search.py`、`scripts/token_sprint/build_fts_v2.py`、`tests/token_sprint/test_search_fts_v2.py`、`docs/reports/.../w8-*` |
| reviewer（需复核产物）/协调会话（T-1 控制产物） | `receipts/` 内以 workstream + Windows 安全时间戳 + UUID 命名的 receipt 文件（一产物一文件，只增不改） |

分片 `.ready` 由执行会话写，`.done` 与对应 receipt 只由 reviewer 写；两者必须绑定同一
artifact SHA。T-1 控制面文件按全局约束 6 的控制面例外，由协调会话原子维护并接受
`test_coordinator_protocol.py` 验证。

**复核协议：** 每个工作包由另一会话/subagent 复核——工程包跑测试、数据包重跑
`validate_shard.py` + 抽检、审计包抽查 finding。复核不通过打回；连续两次不通过的
工作包类型暂停扩张。

---

## 时间线（建议；T-1 未验收前后续全部冻结）

| 时段 | 协调/数据轨（B） | 工程轨（A） |
|------|-----------------|------------|
| D1 上午 | **T-1（唯一可推进项）**：checkpoint/master 门 → integration → Cosmic Ray spike/依赖冻结 → inputs → coordinator 协议测试/预检 | （冻结；可撰写实施方案文本） |
| D1 午后 | T-1 验收 → T0 探针（60 条，内容+保真门+容量实测）+ W1 工具链 | W4 按已冻结工具路径开工 + W5 开工 + W7 维度 1 |
| D1 夜 | 探针通过 → 试验集 Stage A/B；W2 harness + 种子查询 | W4 首批模块、W7 维度 2–3 |
| D2 上午 | 分片爬坡或 fallback 转向；W2 judged 子集标注；W3 疑难复审 | W5 收尾、W7 维度 4–5、W8 开发 |
| D2 下午–夜 | pairwise / judged 扩容弹性吸收；收尾 QC | 复核轮全开、（条件触发）W6、integration 收盘合并 |
| T+24h 检查点 | 剩余 > 55% → 放大器 ①→⑤ 加码 | 同左 |

---

## 验证矩阵

| 项 | 门 | 方式 |
|----|----|------|
| T-1 | scoped checkpoint 三件套齐备且 `master == refactor_base_sha`；`sprint_base_sha` 唯一且入 manifest；Cosmic Ray 决议与依赖冻结；inputs 绝对路径/副本哈希齐全；coordinator 协议测试与跨调用预检双会话通过；integration 冒烟测试绿 | manifest + `tests/token_sprint/test_coordinator_protocol.py` + pytest + 预检记录 |
| T0 | 60 条探针：拒绝 ≤15%、schema ≥95%、高显式层保真 ≥90%（全量人工复核）；容量成本入 usage-log | 探针 QC 摘要 |
| W1 | 快照重建先清 FTS+state；三计数一致且 errors==0；「FTS 空但 state 完成」回归测试绿；每片 QC 门 | `validate_shard.py` + `tests/token_sprint/test_snapshot_fts_rebuild.py` |
| W2 | NDCG 只含目标 `pool_version` 下 fully judged 查询；W8 加入后差集未标完则不得进入新版本指标；disputed 裁决前不入指标；`ranker_set_hash`/`pool_sources` 可追溯；silver 一致率 ≥80%；同版本两次运行一致 | `eval_harness.py` |
| W3 | 只读 `inputs/` 副本；全覆盖；复审抽查 20 条无误报 | 人工抽查 |
| W4 | 突变得分报告；fallback 时 sentinel mutant 证明 pytest 导入临时副本；`tests/mutation/` 全绿；`git status --porcelain --untracked-files=all -- app/` 为空；突变仅在临时副本 | pytest + porcelain |
| W5 | 属性测试全绿、时长增量 <60s；`vector_math` 条件项按 T-1 裁定执行或删除 | `uv run pytest -q tests/property/` |
| W6（若启用） | 白名单 mypy 清零、全量 pytest 不回归 | mypy + pytest |
| W7 | 两假设分别有结论与证据；每维度抽查 3 条、误报 <1/3 | reviewer 抽查 |
| W8 | 双次构建三一致；输入逐行可追溯到 approved immutable JSONL shard SHA；篡改 shard 后 fail closed；开关缺省关行为不变 | `tests/token_sprint/test_search_fts_v2.py` + 构建日志 |
| 全局 | 重构基线不被污染 | `Get-Item data/*.db`（PowerShell，重构 agent 环境）与其 Task 1 基线一致 |
| 全局 | 无直连模型端点代码 | `rg -i "bigmodel|GLM_API" app/ scripts/` 为空 |
| 全局 | canonical manifest 单写者；artifact/ready/done/receipt 哈希链一致；`.done` 与 review receipt 全部 reviewer 产出；usage-log 每 2h 一条 | coordinator 协议测试 + receipts 归档抽查 |

---

## Explicit non-goals

- 不在任何脚本/代码中直连 GLM 端点，不在 ZCode 之外消耗本额度。
- 不把脏工作区打成基线；不 `git add -A`；基线未成不开工；开工后不换基线。
- 非 fallback 时不从未落在 `master` 的游离 checkpoint 开工；`sprint_base_sha` 后不再改依赖。
- 不让 agent 读取 GIF/图像/视频内容；数据工作包只处理文本。
- 不承诺全库富化覆盖率与 600M 全额消耗。
- 不在冲刺窗口内对任何生产库执行写操作（含 W8 生产 apply）。
- 不重算 `candidate_vectors`、不翻转 `preference_memory.enabled`。
- 不修改重构计划 File Map 内文件；不修 W4/W7 发现的产品 bug（登记后处理）。
- 未经用户确认不把 `sprint/integration` 合回 `master`。

---

## 实施方案拆分指引（给撰写 plan 的 agent）

1. 一个 Workstream 一份 plan；T-1 与协调职责单独成一份 coordinator plan（**最先落笔**）。
   coordinator plan 必须先定义 `scripts/token_sprint/coordinator/` 三个自举工具及
   `tests/token_sprint/test_coordinator_protocol.py`，再出现任何 checkpoint 执行命令。
2. 必含章节：Goal / Constraints（原样复制「全局约束」+ 所属禁区）/ File Map（落在所有权矩阵内）/
   逐 Task（每 Task 以测试或 QC 门收尾，附 commit 命令与文件清单）/ Verification。
3. 所有 `scripts/token_sprint/*` 脚本必须支持 `--sprint-root` 参数（env 仅显式 fallback），
   在 Git Bash 与 cmd 下均可运行；消费跨 Workstream 产物前按 canonical manifest 校验 SHA-256；
   产物发布走 artifact/ready/done/receipt 四方哈希绑定协议，不碰 canonical manifest。
4. W1/W2 必含 T0 失败 fallback 分支；W2 必须声明「快照 FTS 重建验收」硬依赖，并按
   judged 子集（≥20 条查询全池标注）按 pair 与模型判断次数分别设计预算，并携带
   `pool_version` + `ranker_set_hash`；Cosmic Ray spike 属 T-1，W4 只能执行已冻结工具路径且
   突变在临时副本；W5 必须区分固定目标与 `vector_math` 条件项（fallback 时删除）；
   W8 必须直接消费 approved immutable JSONL shards（版本 + 分片哈希），不得以
   `enrichment.db` 为 canonical，并包含双次构建一致性与 shard 篡改 fail-closed 测试。
5. prompt 模板是版本化交付物（`scripts/token_sprint/prompts/{pass_id}/{prompt_version}.md`），
   approved manifest 必须记录 `prompt_sha256`；执行时禁止即兴改写或覆盖旧模板，改模板必须
   新建 `prompt_version` 并重过 QC 门。
6. 依赖变更一律提请协调会话，plan 中不得出现 `uv add`。
7. 遇到本设计未覆盖的决策：优先选「纯新增文件、可回滚、不碰冻结区」；仍有歧义停下问用户。
