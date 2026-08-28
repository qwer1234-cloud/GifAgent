# Token Sprint W5 — Hypothesis Property Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).
>
> **FROZEN until T-1 gates pass.** `hypothesis` 已由协调会话写入 `sprint_base_sha`。**禁止 `uv add`。** 需要 `import app.pipeline.*` 内部路径的性质直接放弃。`extract_config` 只经 facade：`from scripts.test_video_adaptive import extract_config`。`vector_math` 两条性质为条件项：manifest `vector_math_in_baseline: false` 时**删除**（不暂挂、不写 skip 占位当未完成）。

**Goal:** `tests/property/` 全绿，总时长增量 < 60s。性质锁定现有行为，不改产品代码。

**Architecture:** 每条性质一个测试文件或清晰 class。`@settings(deadline=None, seed=20260828)`（或模块级 `settings.register_profile`）。显式 seed，不用默认 deadline。

**Tech Stack:** 已冻结的 `hypothesis`、pytest、numpy。

**Branch:** `sprint/w5-property-tests`.

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

### W5 额外禁区

- 不改被测产品模块。失败 = 缩小 generator 或 pin 现状，不要「顺便修」。
- 不 import `app.pipeline.config` 等内部路径测 `extract_config`。
- 本 W 不新增 `scripts/token_sprint/*`（无脚本则无 `--sprint-root` 要求）。
- 计划中不得出现 `uv add`。

---

## File Map

**Create only**

- `tests/property/conftest.py` — `settings.register_profile("token_sprint", deadline=None)` 并 `settings.load_profile("token_sprint")`；seed 在各测试 `@settings(seed=20260828)` 再锁一次
- `tests/property/test_extract_config.py`
- `tests/property/test_normalize_vlm_unit_score.py`
- `tests/property/test_clip_merge.py`
- `tests/property/test_calibration.py`
- `tests/property/test_grid_select.py`
- `tests/property/test_taste_map.py`
- `tests/property/test_timeline.py`
- `tests/property/test_vector_math.py` — **仅条件项为 true 时创建**

**Do not modify:** 产品代码、已有测试、`pyproject.toml`。

---

## Shared hypothesis settings

```python
from hypothesis import given, settings, strategies as st, assume
from hypothesis import HealthCheck

SETTINGS = dict(deadline=None, seed=20260828, max_examples=80,
                suppress_health_check=[HealthCheck.too_slow])
```

`max_examples=80` 是为时长增量 <60s；若单文件超时，先降该文件 examples，不要关性质。

---

### Task 1: `extract_config` freeze + idempotence

**Files:** `tests/property/conftest.py`, `tests/property/test_extract_config.py`

```python
from scripts.test_video_adaptive import extract_config
```

性质：

- `extract_config({})` 与 `extract_config({"adaptive": {}})` 对已知冻结键相等（至少 `min_duration==2.0`、`max_duration==20.0`、`sex_act_threshold` 与 `tests/test_adaptive_config.py` 一致）。
- 对随机 `adaptive` 子集（只生成**已存在键**的合法类型，不要发明新键语义）：`extract_config({"adaptive": d})` 两次调用 `==`。
- 省略的键必须等于空 dict 基线，而不是 None 漂移。

不要走 `app.pipeline.config`。

- [ ] **Step 1: 写测试。**
- [ ] **Step 2:** `uv run pytest -q tests/property/test_extract_config.py`
- [ ] **Step 3: Commit** `test: property freeze extract_config defaults via script facade`

---

### Task 2: `normalize_vlm_unit_score`

**Files:** `tests/property/test_normalize_vlm_unit_score.py`

从 `app.services.export_ranking import normalize_vlm_unit_score`。

性质：

- 对 `st.integers(0, 100)`：结果在 `[0,1]` 且 `== n/100`。
- 对 `st.floats(0, 1, allow_nan=False, allow_infinity=False)` 且非「整型语义的 2–100」：结果在 `[0,1]` 或按实现 `None`。**整数 1 → 0.01，浮点 1.0 → 1.0**（已有单测）。生成器要把 `int` 与 `float` 分开。
- 输出非 None 时恒在 `[0,1]` 且 finite。
- `bool`、负数、`nan`、`inf`、`101`、`1.1` → `None`（pin 现状）。

- [ ] pytest + commit `test: property normalize_vlm_unit_score dual-track mapping`

---

### Task 3: `clip_merge`

**Files:** `tests/property/test_clip_merge.py`

`merge_scored_frames_into_clips`：

- 每个 clip `end_ts - start_ts <= max_merge_span_s + 1e-6`（`max_merge_span_s>0` 时）。
- 若相邻输入帧 `gap > merge_gap`，它们不得落在同一 clip。
- 输出 clip 按 `start_ts` 非降序；区间不重叠（`clips[i].end_ts <= clips[i+1].start_ts + 1e-9`，相等允许当单帧相接）。
- 帧 timestamp 用 `st.lists` 排序后 unique 的非负 float。

现有 `tests/test_clip_merge.py` 不修改。

- [ ] pytest + commit `test: property clip merge span, gap, and order`

---

### Task 4: calibration PAV

**Files:** `tests/property/test_calibration.py`

`fit_monotonic_calibrator`：

- `values` 非降序。
- `thresholds` 非降序。
- 对训练点：若实现是右闭阶梯，用 `MonotonicCalibrator` 的文档约定断言校准后序列单调。不要猜测端点外推；空输入 pin 为空 tuple（已有实现）。

`calibration_curve`：bin 边界覆盖 `[0,1]`，`count` 之和等于样本数（落在范围外的 pin 现状——若实现丢弃则测试先读代码再锁）。

- [ ] pytest + commit `test: property PAV monotonic calibrator`

---

### Task 5: `grid_select`

**Files:** `tests/property/test_grid_select.py`

`select_grid_frames(..., count=9)`：

- 有 `path` 的输入非空且 timestamps 不全相等 → 尽量覆盖时间两端（`min(selected ts) <= percentile 低` 可能过严；改为：9 桶中每个非空桶至多贡献 1 个在去重前——读实现：每桶先选再 phash 去重）。**锁：** `len(selected) <= count`。
- 去重后数量 ≤ `count`。
- 无 path → 空列表。

phash_fn 用假哈希（整数差）策略生成碰撞，断言碰撞帧不会双留。

- [ ] pytest + commit `test: property grid_select count cap and time buckets`

---

### Task 6: `taste_map.project_taste_map`

**Files:** `tests/property/test_taste_map.py`

- 输出长度 = n；n=0 → `[]`；n=1 → `(0.0, 0.0)`。
- 形状：每个点有限 x,y。
- **平移不变性：** `project_taste_map(X + c)` 与 `project_taste_map(X)` 的坐标差在符号/旋转内可能不唯一（SVD 符号翻转）。锁：**成对距离矩阵**（点之间欧氏距离）对平移不变，`atol=1e-4`。n>=3 才测距离。
- ids 长度不匹配 → `ValueError`。

- [ ] pytest + commit `test: property taste map shape and translation invariance`

---

### Task 7: `load_timeline_window`

**Files:** `tests/property/test_timeline.py`

内存 SQLite：最小 `media` + `video_clips` + `candidate_gifs` 表（照 `tests/test_timeline.py` 复制 helper **到本文件**，不改原测试）。

性质：

- 返回的所有 span 满足 overlap：`s < end_sec and e > start_sec`。
- 带非 None `thumbnail_path` 的 span 数 ≤ `max_thumbnails`。
- 窗口外插入的 clip 不得出现。

- [ ] pytest + commit `test: property timeline window clipping and thumbnail cap`

---

### Task 8 (conditional): `vector_math`

**仅当** `vector_math_in_baseline`。否则跳过本 Task，不创建文件。

**Files:** `tests/property/test_vector_math.py`

单向量（`vector_to_blob` **先 L2 再序列化**）：

- 有限、非空、非零：`blob_to_vector(vector_to_blob(v, embedding_dim=len(v))) ≈ l2_normalize(v)`，`atol=1e-6`。
- 已归一化：往返恒等，同 atol。
- `l2_normalize` 幂等。
- **零向量与 NaN/Inf：pin 现状**（读 `l2_normalize`：零向量原样返回；`vector_to_blob` 对 nan 的当前 raise/传播写断言，不猜「应该」）。

packed prototypes **不经** `vector_to_blob`：

- `np.float32.tobytes` 的 `k*dim` blob → `blob_to_vector` reshape 逐行一致。
- `max_cosine(candidate, packed_blob)` = 对每行 prototype 余弦的最大值（与实现循环一致）。
- `vector_to_blob` 对 packed 长度（`size != embedding_dim`）抛 mismatch（`ValueError` embedding_dim mismatch）。这是负性质。

注意：`max_cosine` 对 `packed.size % dim != 0` 的现状是截断第一截，不是 throw。不要把该路径写成 throw；可选单独 pin。

- [ ] pytest + commit `test: property vector_math roundtrip and packed prototypes`

---

### Task 9: Timing gate

```bash
uv run pytest -q tests/property/ --durations=10
```

Expected: all PASS。整目录墙钟增量 < 60s（在本机记录到报告）。若超：降 `max_examples`，不要删性质。

```bash
git add -- tests/property
git commit -m "test: complete hypothesis property suite for token sprint W5"
```

Reviewer 重跑同一命令后写 `.done`（以测试目录打包 tar 不合适；artifact 用 `docs/reports/2026-08-token-sprint/w5-pytest.txt` 保存 pytest 输出）。

---

## Verification matrix

| Gate | Pass |
|------|------|
| Facade | extract_config 不从 `app.pipeline.*` 内部 import |
| vector_math | 按 T-1 裁定存在或删除 |
| Green | `uv run pytest -q tests/property/` |
| Time | < 60s 增量 |
| No product diff | `git diff -- app/` 空 |

---

## Done looks like

- 固定目标性质全在。条件项按裁定执行或删除。
- 时长门通过。

---

## Explicit non-goals

- 改校准算法、merge 规则、SVD 符号规范化。
- 为 pipeline 内部模块写性质。
