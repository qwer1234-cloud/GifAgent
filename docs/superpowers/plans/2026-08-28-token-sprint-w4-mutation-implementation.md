# Token Sprint W4 — Mutation Testing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).
>
> **FROZEN until T-1 gates pass.** Mutation **tool path is frozen at T-1**: if canonical manifest `cosmic_ray.status == "included"` use Cosmic Ray already in `sprint_base_sha` dev deps; if `"fallback"` use `scripts/token_sprint/mutation_harness.py` (stdlib `ast`). **Do not `uv add`. Do not re-spike Cosmic Ray.** Mutants run only on copies under `tmp_path`. `git status --porcelain --untracked-files=all -- app/` must stay empty.

**Goal:** 对重构 File Map **之外**且存在于 `sprint_base_sha` 的白名单模块做突变测试：现有测试验杀 → 存活突变由 subagent 写 `tests/mutation/test_mut_{module}.py` 杀死 → reviewer 确认「突变死、原模块全绿」。发现的真实 bug 登记 W7，**不修产品代码**。

**Architecture:** 模块拷到临时目录 → 突变 → `PYTHONPATH`/`sys.path` 指向副本跑 pytest。用 sentinel mutant 证明 pytest 导入的是副本 `__file__`，不是 worktree。

**Tech Stack:** 已冻结的 dev 依赖 + 现有 pytest。fallback harness：stdlib `ast` 四类算子 + `subprocess`。

**Branch:** `sprint/w4-mutation`.

**模块白名单（仅当文件存在于 `sprint_base_sha`）：**

| Module | Path |
|--------|------|
| clip_merge | `app/services/clip_merge.py` |
| export_ranking | `app/services/export_ranking.py` |
| grid_select | `app/services/grid_select.py` |
| timeline | `app/services/timeline.py` |
| taste_map | `app/services/taste_map.py` |
| narrative_curation | `app/services/narrative_curation.py` |
| collections | `app/services/collections.py` |
| desktop_export_sync | `app/services/desktop_export_sync.py` |
| metrics | `app/quality_lab/metrics.py` |
| calibration | `app/quality_lab/calibration.py` |
| promotion | `app/quality_lab/promotion.py` |
| repository | `app/task_engine/repository.py` |
| fingerprints | `app/task_engine/fingerprints.py` |

**条件项：** `app/services/vector_math.py` 仅当 manifest `vector_math_in_baseline: true`。fallback 冻结 `00559804` 或未闭环 → **从范围删除**（不暂挂，不写「稍后」）。

现有测试（只跑，不改这些文件）：`tests/test_clip_merge.py`、`tests/test_export_ranking.py`、`tests/test_grid_select.py`、`tests/test_timeline.py`、`tests/quality_lab/test_metrics.py`、`tests/quality_lab/test_calibration.py`、`tests/quality_lab/test_promotion.py` 等。`taste_map` 可能尚无测试——存活突变多，杀伤测试全部新建在 `tests/mutation/`。

---

## Constraints

### 全局约束（从设计文档原样复制）

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

### W4 额外禁区

- 禁止就地改写 worktree 里的 `app/` 源文件（含「测完再还原」）。突变只在临时副本。
- 禁止修改已有测试文件；杀伤测试只新建 `tests/mutation/test_mut_*.py`。
- 禁止修产品 bug；登记 `docs/reports/2026-08-token-sprint/w4-surviving.md` 供 W7。
- 禁止 import `app.pipeline.*` 内部。
- 计划中不得出现 `uv add`。不得在开工后再申请 Cosmic Ray。

---

## File Map

**Create**

- `scripts/token_sprint/mutation_harness.py` — **即使 Cosmic Ray included 也要写 fallback 入口**，但 included 时默认 `--engine cosmic-ray`；fallback 时 `--engine ast` 是唯一路径。两引擎都必须走临时副本。
- `tests/mutation/test_harness_sentinel.py` — 证明导入副本
- `tests/mutation/test_mut_{module}.py` — 按模块增量添加
- `docs/reports/2026-08-token-sprint/w4-mutation-scores.md`

**Do not modify:** 任何 `app/**`、已有 `tests/test_*.py`、`pyproject.toml`。

---

## AST operators (fallback engine)

四类，全部在副本 AST 上操作：

1. **AOR** — `+ - * / // %` 换成同类另一算子（跳过字符串加法若无法静态区分则只改 `ast.BinOp` 且两边都是 `ast.Constant` 数字，或全部 BinOp 数字上下文；宁可漏不可改 SQL 字符串）。
2. **ROR** — `Eq NotEq Lt LtE Gt GtE` 互换。
3. **UOI** — 对 `ast.UnaryOp` 的 `Not/USub` 删除或翻转。
4. **LCR** — `And/Or` 互换。

每个突变体 = 源文件一份拷贝 + 单一算子单点修改。命名 `mut_{module}_{op}_{lineno}_{col}`。

---

## Task 1: Sentinel — pytest imports the copy

**Files:** `scripts/token_sprint/mutation_harness.py`, `tests/mutation/test_harness_sentinel.py`

无论最终引擎是什么，**第一条验收**必须用已知可杀 sentinel：

```python
# copied module app/services/_sprint_sentinel.py is NOT in repo.
# Harness writes tmp/app/services/clip_merge.py copy AND a sibling
# tmp/sentinel_mod.py with:
def sentinel_one():
    return 1
```

测试 `assert sentinel_one() == 1`。Harness 把 `return 1` 改为 `return 0` 后跑 subprocess pytest。

断言 subprocess 失败（突变被杀）**并且** `sys.modules['sentinel_mod'].__file__` 打印在 tmp 下（在被测测试里写入 `tmp/imported_from.txt`）。若 pytest 仍加载 worktree 模块 → harness bug，不得继续。

CLI：

```text
python scripts/token_sprint/mutation_harness.py --sprint-root <path> \
  --engine ast|cosmic-ray --module clip_merge --dry-run-sentinel
```

`--sprint-root` 用于把得分 JSON 写到 `<sprint-root>/w4/`（可选）以及协议一致性；得分报告最终进 git 的 `docs/reports/...`。

- [ ] **Step 1: 实现 sentinel 路径。**
- [ ] **Step 2:**

```bash
uv run pytest -q tests/mutation/test_harness_sentinel.py
```

Expected: PASS（harness 自测）。再手动：

```bash
python scripts/token_sprint/mutation_harness.py --sprint-root "$SPRINT_ROOT" --engine ast --dry-run-sentinel
```

Expected: 报告 killed=1，imported_from 含临时目录。

- [ ] **Step 3: Commit**

```bash
git add -- scripts/token_sprint/mutation_harness.py tests/mutation/test_harness_sentinel.py
git commit -m "feat: mutation harness runs pytest against tmp copies with sentinel proof"
```

---

## Task 2: Engine dispatch from frozen T-1 decision

读 `<sprint-root>/sprint_manifest.json` 的 `cosmic_ray.status`。

- `included`：`--engine cosmic-ray` 调已安装的 cosmic-ray，**工作目录仍是模块临时副本**（把副本树做成 mini 包）。若 cosmic-ray 在 worktree 内执行失败：记录并 **停止**，不要私自改依赖；向 coordinator 报告。不要静默切 ast，除非 manifest 已是 fallback。
- `fallback`：只允许 `--engine ast`。

测试：manifest 为 fallback 时传入 cosmic-ray → exit 2。

- [ ] **Step 1+2:** pytest + commit `fix: refuse cosmic-ray engine when T-1 froze ast fallback`（若需要）。

---

## Task 3: First module — `clip_merge` (known tests exist)

对 `merge_scored_frames_into_clips` 生成突变，跑 `tests/test_clip_merge.py`（通过 `PYTHONPATH=tmp_copy` 让 `from app.services.clip_merge import ...` 解析到副本——副本目录结构必须是 `tmp/app/services/clip_merge.py` + 空 `__init__.py`）。

产出 `docs/reports/2026-08-token-sprint/w4-mutation-scores.md` 表格：module, mutants, killed, survived, score。

存活突变：每个一条，subagent 在 `tests/mutation/test_mut_clip_merge.py` 写杀伤测试。杀伤测试必须在**原始**模块下也通过（不能只在杀突变时过、原码失败）。

Reviewer：跑 `uv run pytest -q tests/test_clip_merge.py tests/mutation/test_mut_clip_merge.py` 全绿，且 harness 对该存活点现在 killed。

- [ ] **Step 1: 跑 harness。**
- [ ] **Step 2: 写杀伤测试直到该模块得分报告更新。**
- [ ] **Step 3:**

```bash
uv run pytest -q tests/test_clip_merge.py tests/mutation/
git status --porcelain --untracked-files=all -- app/
```

Expected: tests PASS；porcelain **空**。

- [ ] **Step 4: Commit** 仅 `tests/mutation/test_mut_clip_merge.py` + 报告。

```bash
git add -- tests/mutation/test_mut_clip_merge.py docs/reports/2026-08-token-sprint/w4-mutation-scores.md
git commit -m "test: kill clip_merge survivors without touching product code"
```

---

## Task 4–N: Remaining whitelist modules

每个模块一个 Task，同一流程。建议顺序（现有测试多的优先）：`export_ranking` → `calibration` → `metrics` → `grid_select` → `promotion` → `timeline` → `fingerprints` → `repository` → `collections` → `taste_map` → `narrative_curation` → `desktop_export_sync` →（条件）`vector_math`。

红线未达时优先扩本列表的突变数量（更多算子实例），而不是改产品代码。

每个模块提交独立：`test: kill {module} mutation survivors`。

`desktop_export_sync` 含线程/调度：突变跑超时 60s kill subprocess，记 `timeout` 为 survived 并登记 W7，不要挂死会话。

---

## Task last: Reviewer package + porcelain

- [ ] `uv run pytest -q tests/mutation/`
- [ ] `uv run pytest -q tests/test_clip_merge.py tests/test_export_ranking.py tests/test_grid_select.py tests/test_timeline.py tests/quality_lab/test_metrics.py tests/quality_lab/test_calibration.py tests/quality_lab/test_promotion.py`
- [ ] `git status --porcelain --untracked-files=all -- app/` 为空
- [ ] 得分报告前后对比表完整
- [ ] 工程包 `.ready` / reviewer `.done` + receipt（报告文件作为 artifact）

---

## Verification matrix

| Gate | Pass |
|------|------|
| Sentinel | pytest `__file__` 在 tmp |
| Engine freeze | 与 manifest 一致；不得私自 uv add |
| Porcelain | `app/` 无改动、无未跟踪产品文件 |
| tests/mutation | 全绿 |
| 原测试 | 全绿 |
| Bug | 只登记不修复 |

---

## Done looks like

- 白名单（经 T-1 裁定）每模块有得分。
- 杀伤测试只在 `tests/mutation/`。
- 产品树干净。

---

## Explicit non-goals

- 修 clip_merge / ranking / 任何产品逻辑。
- 对冻结区或 `library_search.py` 做突变。
- 就地改源再还原。
