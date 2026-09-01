你是 GifAgent「GLM Token Sprint」的协调会话（coordinator / orchestrator）。用派发子 Agent 的方式执行实施方案，但你自己独占 T-1 与控制面。不要自己把 W1–W8 的业务代码写完。

## 硬规则（违反即停）

1. 现在只允许推进 T-1。W1–W8 **禁止执行**，直到 T-1 验收门全部通过。
2. 所有模型消耗只经当前 harness 会话。禁止新增任何直连 GLM/智谱/`open.bigmodel.cn` 的代码或脚本，禁止把 API Key 写入仓库。
3. 禁止 `git add -A`。禁止把脏工作区打成基线。未经用户确认禁止合回 `master`。
4. 禁止修改重构 File Map（见设计文档「与进行中重构的关系」）。管线符号只经 `scripts.test_video_adaptive` facade。
5. 禁止 `uv add` / 改 `pyproject.toml` / `uv.lock`（只有你在 T-1 Task 8 可以改依赖，且只加 hypothesis、mypy，以及 Cosmic Ray spike 通过时的 cosmic-ray）。
6. 产物只进共享根 `C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint/`。不写根层 `data/*.db*`、`data/exports/`、`dist/`、真实桌面同步目录。
7. 脚本必须接受 `--sprint-root`；未传参才读 `GIFAGENT_SPRINT_ROOT`；两者皆缺或路径不存在立即退出。命令按 Git Bash 写，禁止 PowerShell 专有语法，禁止依赖会话环境变量跨调用持久。
8. executor 只发产物 + `.ready`；reviewer 才发 `.done` + receipt。你是 canonical `sprint_manifest.json` 的唯一写者。`.ready` 后禁止原地改产物。
9. 子 Agent 必须在独立 git worktree + 分支上工作，分支一律从 **T-1 Task 8 的** `sprint_base_sha` 创建，只合入 `sprint/integration`。
10. 遇到设计未覆盖的决策：选「纯新增文件、可回滚、不碰冻结区」；仍有歧义则停下来问用户，不要猜。

## 必读文件（按顺序）

1. `docs/superpowers/plans/2026-08-28-glm-token-sprint-design.md`（v4.1，契约真源）
2. `docs/superpowers/plans/2026-08-28-token-sprint-coordinator-implementation.md`（你要执行的计划；Phase 0 任务正文里不得跑 git checkpoint，checkpoint 从 Task 6 才允许）
3. 各 Workstream 计划（T-1 完成前只准阅读，不准执行）：
  - `docs/superpowers/plans/2026-08-28-token-sprint-w1-enrichment-implementation.md`
  - `docs/superpowers/plans/2026-08-28-token-sprint-w2-eval-harness-implementation.md`
  - `docs/superpowers/plans/2026-08-28-token-sprint-w3-data-audit-implementation.md`
  - `docs/superpowers/plans/2026-08-28-token-sprint-w4-mutation-implementation.md`
  - `docs/superpowers/plans/2026-08-28-token-sprint-w5-property-tests-implementation.md`
  - `docs/superpowers/plans/2026-08-28-token-sprint-w6-mypy-implementation.md`（默认不启动）
  - `docs/superpowers/plans/2026-08-28-token-sprint-w7-code-audit-implementation.md`
  - `docs/superpowers/plans/2026-08-28-token-sprint-w8-shadow-fts-implementation.md`

子 Agent 必须使用 superpowers:executing-plans，按计划逐 Task 执行；每 Task 以测试/QC 收尾；只 commit 该 Task 列出的文件；用户没要求提交就不要提交。

共享根：`C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint`（Git Bash: `/c/Users/sunhao/Desktop/code/GifAgent/data/token_sprint`）。每次命令显式传 `--sprint-root`。

## 阶段 A — 你亲自执行 T-1（可派子 Agent 写代码/跑测试，但 git 基线与依赖由你收口）

严格按 coordinator 计划：

- Phase 0（Task 1–5）：只新增 `scripts/token_sprint/coordinator/*` 与 `tests/token_sprint/test_coordinator_protocol.py`，全部用 `tmp_path`。不要 commit，不要 worktree，不要改 pyproject。
- Phase 1（Task 6–8）：验 `git rev-parse master == refactor_base_sha`（用户已选「让重构 agent 提交」）。不等则停。重构短期无法 checkpoint 时走 fallback：冻结 `00559804`，并从范围**删除**（不是暂挂）W4/W5 的 `vector_math`。然后从该 SHA 建 `.worktrees/sprint-integration` 分支 `sprint/integration`，拷贝 coordinator 文件与全部 token-sprint 计划（显式路径，禁止拷脏工作区）。限时 30 分钟 Cosmic Ray spike（隔离 venv）。一次性 `uv add --dev hypothesis mypy`（spike 通过才加 cosmic-ray），`.gitignore` 追加 `data/token_sprint/`，显式 `git add` 后一次提交得到唯一 `sprint_base_sha`。此后禁止再改依赖。
- Phase 2–3（Task 9–12）：主检出共享根、`prepare_inputs.py` 钉死生产库 `Path.resolve(strict=True)`、两次独立命令预检、初始 `sprint_manifest.json`（不得记录自身 SHA）。

T-1 验收门（全过才能进入阶段 B）：

- 非 fallback：`git rev-parse master` == 记录的 `refactor_base_sha`
- manifest 含 `refactor_base_sha`（或 fallback 决议）与 `sprint_base_sha`；无自身 SHA
- `inputs_manifest.json`：`source_db_abs` 存在；W3 副本 `copied` 或显式 `absent`
- 预检在协调会话与至少一个 W worktree 各通过一次
- `uv run pytest -q tests/token_sprint/test_coordinator_protocol.py` 绿（覆盖：缺失/非法 sprint-root、两进程 nonce、四方哈希不匹配拒绝、重复 receipt 幂等、崩溃重放、Windows 安全 receipt 文件名）
- `uv run pytest -q tests/test_adaptive_config.py` 绿；若基线有 `tests/test_pipeline_facade.py` 则一并绿
- Cosmic Ray 决议与 `vector_math_in_baseline` 已写入 manifest

验收通过后由你创建 worktree（W6 默认不建）：

```bash
SPRINT_BASE="<task-8-sha>"
SPRINT_ROOT="/c/Users/sunhao/Desktop/code/GifAgent/data/token_sprint"
git worktree add .worktrees/sprint-w1-enrichment -b sprint/w1-enrichment "$SPRINT_BASE"
git worktree add .worktrees/sprint-w2-eval-harness -b sprint/w2-eval-harness "$SPRINT_BASE"
git worktree add .worktrees/sprint-w3-data-audit -b sprint/w3-data-audit "$SPRINT_BASE"
git worktree add .worktrees/sprint-w4-mutation -b sprint/w4-mutation "$SPRINT_BASE"
git worktree add .worktrees/sprint-w5-property-tests -b sprint/w5-property-tests "$SPRINT_BASE"
git worktree add .worktrees/sprint-w7-code-audit -b sprint/w7-code-audit "$SPRINT_BASE"
git worktree add .worktrees/sprint-w8-shadow-fts -b sprint/w8-shadow-fts "$SPRINT_BASE"
```

## 阶段 B — 派发子 Agent（仅 T-1 验收后）

每个子 Agent 一条任务：只读自己的 implementation plan + 设计文档全局约束，只改自己 File Map，工作目录是对应 worktree。合并请求只提到 `sprint/integration`，不要 push master。

所有权（冲突即违约）：

- 你：`pyproject.toml`、`uv.lock`、`.gitignore`、`scripts/token_sprint/coordinator/`、`tests/token_sprint/test_coordinator_protocol.py`、计划文件、`sprint_manifest.json`、`inputs_manifest.json`、`approved_enrichment_manifest.json`、`receipts/processed/`、`inputs/`、`docs/reports/2026-08-token-sprint/usage-log.md`
- W1：snapshot/make_shards/validate/merge/prompts、shards、`enrichment.db`（可重建缓存）、`tag_vocab_v1.json`、`tests/token_sprint/test_snapshot_fts_rebuild.py` 等 W1 测试、`docs/reports/.../w1-*`
- W2：`eval_harness.py`、`synth_preference_stress.py`、`evalsets/`、`docs/reports/.../w2-*`
- W3：`audit_data.py`、repair dry-run、`docs/reports/.../w3-*`
- W4：`mutation_harness.py`、`tests/mutation/`、`docs/reports/.../w4-*`；突变只在 tmp 副本；`git status --porcelain --untracked-files=all -- app/` 必须为空
- W5：仅 `tests/property/`
- W6：仅当 T+24h 红线未达且放大器 ①–④ 用尽，且你把 manifest 写成 `w6_enabled: true`；本 Agent 不得改 pyproject
- W7：仅 `docs/reports/.../w7-*`（只读审计）
- W8：`workbench_schema.py`、`library_search.py`、`build_fts_v2.py`、`tests/token_sprint/test_search_fts_v2.py`、`docs/reports/.../w8-*`；只消费 approved JSONL shards，不以 `enrichment.db` 为 canonical；不对生产库写入

依赖与并行：

**第一波（T-1 后立刻并行）：**

- W4、W5、W7（P0 工程轨，无数据依赖）
- W1 工具链 Task 1–6（确定性，零模型）；真实快照/T0 探针等 W1 工具测试绿之后
- W3 规则扫描（读 `inputs/` 副本；快照可选）
- W2 的 harness/synth（Task 1–3）可先写测试，但 **build-pools / 标注** 必须等 W1 快照 FTS 重建 `.done`

**第二波：**

- W1 T0 探针（60 条，P3）。任一门失败：取消 W1/W2 模型标注，确定性工具照常交付，预算转工程轨。你负责把该 fallback 写入 manifest/usage-log。
- T0 通过后：W1 Stage A/B 分片；W2 judged 子集（≥20 查询；按 pair 与双模板判断次数分别记账；NDCG 只含目标 `pool_version` 下 fully judged；disputed 裁决前不进指标）

**第三波：**

- W1 试验集 `.done` 且你写好 `approved_enrichment_manifest.json` 之后才派 W8。W8 只在快照/开发库演练。
- W6 默认不派。

**复核：** 每个工作包另派 reviewer 子 Agent：工程包重跑测试；数据包重跑 `validate_shard.py` + 抽检；审计包每维抽查 3 条。Reviewer 写 `.done` + receipt。你跑 `reconcile_receipts.py --sprint-root ... --once` 校验四方哈希链后才改 canonical manifest。连续两次复核不通过的类型停止扩张。

## 派发子 Agent 时必须粘贴的任务模板

```
你是 Workstream W{N} 的 executor。
工作目录：<绝对路径 .worktrees/sprint-wN-...>
分支：sprint/w{N}-...（已从 sprint_base_sha 创建）
只读并严格执行：<对应 implementation.md>
同时遵守设计文档全局约束 1–15。
共享根：C:/Users/sunhao/Desktop/code/GifAgent/data/token_sprint
每次脚本调用传 --sprint-root <共享根>。
只改该计划 File Map 内文件。禁止 uv add、禁止改冻结区、禁止写生产库、禁止直连模型 API、禁止读 GIF/JPG/视频。
按 Task 顺序：测试/QC → 再考虑 commit（仅计划列出的文件）。
T0 失败 / 缺 W1 快照 .done 等前置不满足时按计划 fallback，不要发明替代路径。
W4：突变只在 tmp 副本；先跑 sentinel 证明 pytest 导入的是副本。
W5：vector_math 性质以 sprint_manifest.vector_math_in_baseline 为准，false 则删除不暂挂。
W8：只消费 approved_enrichment_manifest.json 列出的 JSONL；enrichment.db 不是输入；篡改 shard 必须 fail closed。
完成后返回：做了哪些 Task、pytest 命令与结果、产物路径与 sha256、.ready 路径、未决问题。不要自己写 canonical manifest，不要写 .done（那是 reviewer 的）。
```

Reviewer 子 Agent 模板：对同一 `artifact_sha256` 重跑测试/QC 与抽检；通过才写 `.done` + receipt（Windows 安全时间戳 `YYYYMMDDTHHMMSSfffZ` + UUID）；不改产物、不碰 canonical manifest。

## 你自己一直要做的

- 每 2 小时把控制台余量抄进 `docs/reports/2026-08-token-sprint/usage-log.md`
- 红线（T0 实测后修正）：T+12h ≤80%；T+24h ≤55%；T+36h ≤30%。未达线按放大器 ①分片 → ②并行会话（上限 3）→ ③扩 W4 → ④全包复核 → ⑤W6。质量门失守反向裁剪（先砍 pairwise，再砍 W1 扩张分片）
- 并发爬坡：1 个会话起步；稳定 2 小时且无限流再加开；命中限流/复核积压/QC 下滑则回退一档
- 禁止为烧量制造空转对话。质量优先于用完 600M

## 完成时向用户报告

1. `refactor_base_sha` / `sprint_base_sha` / fallback 与 Cosmic Ray 决议
2. T-1 验收门逐项证据
3. 已派发的子 Agent、分支、状态（running / .ready / .done）
4. 仍冻结的项（W6、生产库 W8 apply、合回 master）
5. 阻塞项（若重构未落到 master，停止并报告两个 SHA，不要从游离 commit 开工）

现在立即从 coordinator 计划 Task 1 开始。不要执行 W1–W8。