# Pipeline Module Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task ends with a focused test run. Commit only the files listed for that task, and only when the user asks to commit (or when they explicitly say to execute this plan including commits).

**Goal:** 在不改变八阶段任务图、冻结 snapshot 缺省、子进程 stage 协议和任何历史数据的前提下，把 `scripts/test_video_adaptive.py`（约 5271 行）和 `app/task_engine/artifacts.py`（约 1890 行）拆成可独立维护的模块，并去掉 UI 兼容层与 Preference 子系统里残留的重复实现。

**Architecture:** 先搬家、后合并。P1 把自适应脚本按职责抽到 `app/pipeline/`，脚本只保留 CLI + 再导出，打包入口仍是 `scripts/test_video_adaptive.py`。P2 让 Direct `run_pipeline` 调用与 Staged 同一套 stage 函数。P3 把 `artifacts.py` 变成包并保持 `from app.task_engine.artifacts import X`。P4 把测试从 `candidate_review` 兼容层迁到 `tabs/*`。P5 清理 Preference 残留重复。全程禁止新运行时（不引入 LangGraph，不把 stage 改成进程内调用）。

**Tech Stack:** Python 3.11+、现有 pytest / PyInstaller / FastAPI / Gradio / SQLite。不新增第三方依赖。

**Companion:** 交互拆分表见 Cursor canvas `refactor-plan.canvas.tsx`。本文件是可执行实施方案。

---

## Global Constraints

- 八阶段图不变：`discover -> sample -> vlm -> refine -> synthesize -> rank_dedup -> gif_clip -> materialize`。
- `AdaptivePipelineAdapter` 必须继续子进程调用 `scripts/test_video_adaptive.py`。冻结构建仍是 `[GifAgentUI.exe, "--run-script", script]`。不要改成 in-process stage。
- `extract_config()` 缺省值不可变。历史 `config_json` 省略字段必须保持旧行为。Retry 不重写 snapshot。
- `gif_worthiness` 必须是 `[0, 1]` 有限数字；bool / 缺失 / 解析失败不得落到 `0.5`。
- Direct 与 Staged 对同一冻结配置必须产生相同的 keep/merge/dedup/quality 决策和相同的导出窗口 / FFmpeg 命令形状。
- `preference_memory.enabled` 时 caption rerank **替换** adult MoE mix，不与像素分混合。
- 测试只用 `tmp_path` 或内存 SQLite。禁止改动 `data/*.db*`、`data/exports/`、`data/labels/`、checkpoint、`dist/`、真实桌面同步目录。
- 每个新的 `app.pipeline.*` 模块必须加入 `build_exe.spec` hiddenimports，并同步 `tests/task_engine/test_packaged_stage_imports.py` 的 `_STAGE_DIRECT_IMPORTS`。
- `scripts/test_video_adaptive.py` 在 P1–P2 结束前必须再导出测试和 smoke 脚本正在 import 的名字（见 Task 1 清单）。搬家后若函数改到新模块，**同一任务内**更新 monkeypatch 目标，不要留「补丁打在脚本、实现跑在别处」的裂口。
- `app/task_engine/artifacts.py` 与 `app/task_engine/artifacts/` 不能并存。P3 必须在同一提交里删除文件并创建包。
- `configs/models.yaml` 与 `configs/models.adult_candidate.yaml` 已经分叉，本计划不要求对齐它们。
- 不要合并 `quality_lab` 与 `quality_moe`。不要改 `task_engine` 的 lease / heartbeat / retry / cancel。
- 用户可见文案若改动，保持现有中文；代码标识与测试名保持英文。

---

## Current Hotspots (2026-08-28)

Physical line counts (`Get-Content | Measure-Object -Line`):

| File | Lines | Role |
|------|------:|------|
| `scripts/test_video_adaptive.py` | 5271 | Direct 四阶段 + 8 个 production stage + 配置 + VLM 生命周期 + Quality 胶水 + CLI |
| `app/task_engine/artifacts.py` | 1890 | artifact identity + resolver + quality/action JSON schema |
| `app/ui/legacy_candidate_review.py` | 1599 | 旧 Review/Control/Profile 完整副本 |
| `app/services/action_boundary.py` | 924 | 已独立，本计划不拆 |
| `app/ui/tabs/review.py` | 792 | Workbench 审核页（主 UI） |
| `app/services/preference_memory.py` | 769 | 已基本拆开，P5 只清残留 |
| `app/services/candidate_vectors.py` | 699 | backfill + hashing 混在一起 |

已知从 `scripts.test_video_adaptive` 直接 import 的测试 / smoke：

- `tests/test_adaptive_config.py` — `extract_config`, `DEFAULT_MAX_REFINE_FRAMES`, `_palette_filters_for`, `collect_refine_timestamps`, `frame_passes_keep_gate`, `get_score_prompt`, `SCORE_PROMPT`, `SCORE_PROMPT_ADULT`
- `tests/test_two_tier_scoring.py` — 以上 prompt/scoring 符号 + `_score_vlm_frame`, `_scoring_vlm_options`, `backfill_clip_captions`
- `tests/test_score_calibration.py`, `tests/test_boundary_snap.py`, `tests/test_tasks_api.py` — `extract_config`
- `tests/test_rank_pipeline_preference.py` — `_rank_pipeline_clips`
- `tests/task_engine/test_production_artifact_contract.py` — `_stage_materialize`, `extract_config`
- `tests/task_engine/test_full_production_stage_chain.py` — `_stage_gif_clip`, `_stage_rank_dedup`
- `tests/task_engine/test_vlm_stage_runtime.py` — `import scripts.test_video_adaptive as mod`，调用 `_resolve_vlm_runtime` / `VlmRuntimeConfig` / `extract_config` / `stop_model` / `wait_model`
- `tests/test_adaptive_direct_transition.py` — `run_pipeline`；monkeypatch `stop_model`, `wait_model`, `wait_for_llm`, `_score_vlm_frame`, `guard_candidate_window`, `run_gif_export_attempt`, `rank_clips_for_export`, `httpx.post`, `subprocess.run`
- `tests/test_gif_windows.py` — monkeypatch `run_gif_export_attempt`
- `data/q8vl_smoke.py`, `data/q8vl_centiscale_smoke.py` — 从脚本 import（保持再导出即可）

已知 monkeypatch `app.ui.candidate_review` 的测试（P4 迁移对象）：`tests/test_candidate_review_*.py`、`tests/test_batch_process_status.py`、`tests/test_config_help_annotations.py`、`tests/test_launcher_gradio_options.py`、`tests/test_workbench_structure.py`、`tests/test_active_review_ui.py`、`tests/test_video_batch_queue.py`。

---

## File Map

**Create (P1)**

- `app/pipeline/__init__.py`
- `app/pipeline/config.py` — `extract_config`, `DEFAULT_MAX_REFINE_FRAMES`, `_extract_direct_snapshot_config`, `_optional_seed`, `_optional_int`
- `app/pipeline/prompts.py` — `SCORE_PROMPT*`、`get_score_prompt`、`_scoring_schema`、`_scoring_vlm_options`
- `app/pipeline/vlm_runtime.py` — `VlmRuntimeConfig`、`_resolve_vlm_runtime`、`_materialize_vlm_runtime`、`stop_model`、`wait_model`、`_ollama_command`、`_expand_vlm_base_url`
- `app/pipeline/scoring.py` — `parse_vlm_response`、`_score_vlm_frame`、`frame_passes_keep_gate`、`collect_refine_timestamps`、`_score_frames_concurrent`、checkpoint load/save、`backfill_clip_captions`
- `app/pipeline/quality_bridge.py` — `_evaluate_quality_pipeline_candidates` 及 ledger / lineage / repair 胶水
- `app/pipeline/ranking.py` — `_rank_pipeline_clips`、`_rank_clips_with_preference`、`_clip_base_export_payload`、`_quality_ranking_weights`
- `app/pipeline/export_gif.py` — `_palette_filters_for`、GIF fps 警告、与导出相关的纯函数
- `app/pipeline/direct.py` — `run_pipeline`、`run_direct_mode`（P2 前原样搬迁）
- `app/pipeline/stage_io.py` — `_load_manifest`、`_save_manifest`、`_make_artifact`、`_run_stage` dispatch
- `app/pipeline/stages/__init__.py`
- `app/pipeline/stages/discover.py` … `materialize.py`（八个 stage 各一文件）
- `app/pipeline/cli.py` — `parse_cli_args`、`main`、`_TeeIO`
- `tests/test_pipeline_facade.py` — 脚本再导出身份锁

**Create (P2)**

- `tests/test_direct_staged_parity.py` — mock VLM 下 Direct vs 八阶段对账

**Create (P3)**

- `app/task_engine/artifacts/__init__.py`（公开再导出，替换 `artifacts.py`）
- `app/task_engine/artifacts/identity.py`
- `app/task_engine/artifacts/store.py`
- `app/task_engine/artifacts/kinds.py` — `STAGE_ARTIFACT_KINDS` / `STAGE_INPUT_KINDS`
- `app/task_engine/artifacts/resolve.py`
- `app/task_engine/artifacts/manifests.py`
- `app/task_engine/artifacts/quality_schema.py`
- `app/task_engine/artifacts/action_schema.py`（若 action/gif v2 校验能从 quality 校验干净切开；否则留在 `quality_schema.py`）

**Modify**

- `scripts/test_video_adaptive.py` — 最终只留 stdout reconfigure、再导出、`if __name__ == "__main__"`
- `build_exe.spec` — hiddenimports
- `tests/task_engine/test_packaged_stage_imports.py` — `_STAGE_DIRECT_IMPORTS`
- P1/P2 涉及的 monkeypatch 测试文件（见各 Task）
- P4：上述 `candidate_review` 测试的 import 路径
- P5：`preference_memory.py`、`app/routers/preference.py`、可选 `candidate_vectors.py`
- `Agent.md` — 仅在全部阶段完成后更新 Architecture Overview（最后一个 Task）

**Do not create**

- 新的 task engine、新的 worker、LangGraph 图、进程内 adapter。

---

## Phase 0 — Guardrails

### Task 1: Facade identity lock + data snapshot

**Files:**

- Create: `tests/test_pipeline_facade.py`
- Modify: none yet

**Interfaces:**

脚本模块在整个计划期间必须继续提供至少这些名字（实现可以再导出）：

```python
FACADE_NAMES = [
    "DEFAULT_MAX_REFINE_FRAMES",
    "SCORE_PROMPT",
    "SCORE_PROMPT_ADULT",
    "SCORE_PROMPT_FAST",
    "SCORE_PROMPT_ADULT_FAST",
    "VlmRuntimeConfig",
    "extract_config",
    "get_score_prompt",
    "frame_passes_keep_gate",
    "collect_refine_timestamps",
    "parse_vlm_response",
    "_score_vlm_frame",
    "_scoring_vlm_options",
    "_palette_filters_for",
    "_rank_pipeline_clips",
    "backfill_clip_captions",
    "run_pipeline",
    "run_direct_mode",
    "run_stage_mode",
    "parse_cli_args",
    "stop_model",
    "wait_model",
    "_resolve_vlm_runtime",
    "_stage_discover",
    "_stage_sample",
    "_stage_vlm",
    "_stage_refine",
    "_stage_synthesize",
    "_stage_rank_dedup",
    "_stage_gif_clip",
    "_stage_materialize",
]
```

- [ ] **Step 1: 记录 `data/*.db` 基线**

```powershell
Get-Item data/*.db | Select-Object FullName, Length, LastWriteTime
```

把输出贴进该 Task 的工作笔记。每个后续 Task 结束后重跑，Length 与 LastWriteTime 必须不变。

- [ ] **Step 2: 写 `test_pipeline_facade.py`**

对 `scripts.test_video_adaptive` 断言 `FACADE_NAMES` 均 `hasattr`。对 `app.task_engine.artifacts` 断言至少导出：`make_artifact_id`, `validate_artifact`, `validate_artifact_strict`, `insert_artifact_dedup`, `STAGE_ARTIFACT_KINDS`, `STAGE_INPUT_KINDS`, `resolve_stage_inputs`, `resolve_materialize_inputs`, `validate_manifest_json`, `validate_materialize_envelope`, `validate_rank_manifest_with_db_lineage`。

- [ ] **Step 3: 跑测试（当前应通过，作为回归锁）**

```powershell
uv run pytest -q tests/test_pipeline_facade.py
```

Expected: PASS。

- [ ] **Step 4: Commit**（仅当用户要求提交）

```powershell
git add -- tests/test_pipeline_facade.py
git commit -m "test: lock adaptive script and artifacts public names before the module split"
```

---

## Phase 1 — Mechanical extract (`app/pipeline`)

原则：每个 Task 只搬一类职责；脚本改为 `from app.pipeline.X import name` 再绑定到原名；**行为字节级不变**。禁止在 P1 改 keep gate、prompt 文本、`extract_config` 缺省、stage manifest 字段。

### Task 2: Extract `extract_config` and scoring prompts

**Files:**

- Create: `app/pipeline/__init__.py`, `app/pipeline/config.py`, `app/pipeline/prompts.py`
- Modify: `scripts/test_video_adaptive.py`, `build_exe.spec`, `tests/task_engine/test_packaged_stage_imports.py`

**Move**

- `DEFAULT_MAX_REFINE_FRAMES`, `extract_config`, `_extract_direct_snapshot_config`, `_optional_seed`, `_optional_int` → `config.py`
- `SCORE_PROMPT`, `SCORE_PROMPT_ADULT`, `SCORE_PROMPT_FAST`, `SCORE_PROMPT_ADULT_FAST`, `get_score_prompt`, `_scoring_schema`, `_scoring_vlm_options` → `prompts.py`

继续使用已有 `app.services.score_prompt.normalize_score_prompt_mode` / `normalize_score_schema_mode`。不要把 prompt 字符串再复制一份到 `score_prompt.py`（避免双源）。

- [ ] **Step 1: 搬迁并在脚本顶部再导出同名符号**

```python
from app.pipeline.config import (
    DEFAULT_MAX_REFINE_FRAMES,
    extract_config,
    _extract_direct_snapshot_config,
)
from app.pipeline.prompts import (
    SCORE_PROMPT,
    SCORE_PROMPT_ADULT,
    SCORE_PROMPT_FAST,
    SCORE_PROMPT_ADULT_FAST,
    get_score_prompt,
    _scoring_schema,
    _scoring_vlm_options,
)
```

- [ ] **Step 2: hiddenimports**

`build_exe.spec` 与 `_STAGE_DIRECT_IMPORTS` 增加：`app.pipeline`, `app.pipeline.config`, `app.pipeline.prompts`。

- [ ] **Step 3: 测试**

```powershell
uv run pytest -q tests/test_pipeline_facade.py tests/test_adaptive_config.py tests/test_two_tier_scoring.py tests/test_tasks_api.py tests/test_score_calibration.py tests/test_boundary_snap.py tests/task_engine/test_packaged_stage_imports.py
```

Expected: PASS。`extract_config({"adaptive": {}})` 的 `sex_act_threshold` 仍为 `0.0`；`GIFAGENT_SCORE_PROMPT_MODE` 仍不得覆盖 frozen mode。

- [ ] **Step 4: Commit**

```powershell
git add -- app/pipeline/__init__.py app/pipeline/config.py app/pipeline/prompts.py scripts/test_video_adaptive.py build_exe.spec tests/task_engine/test_packaged_stage_imports.py
git commit -m "refactor: extract adaptive extract_config and scoring prompts into app.pipeline"
```

---

### Task 3: Extract VLM runtime

**Files:**

- Create: `app/pipeline/vlm_runtime.py`
- Modify: `scripts/test_video_adaptive.py`, `build_exe.spec`, `tests/task_engine/test_packaged_stage_imports.py`, `tests/task_engine/test_vlm_stage_runtime.py`（仅当 monkeypatch 目标必须改）

**Move**

- `VlmRuntimeConfig`, `_expand_vlm_base_url`, `_resolve_vlm_runtime`, `_materialize_vlm_runtime`, `_ollama_command`, `stop_model`, `wait_model`, `_should_manage_vlm_lifecycle`, `_resolve_vlm_config`, `_attach_live_vlm_base_url`, `_is_stable_http_url`

继续走 `app.services.ollama_runtime` 的 WSL/auto 解析。不要把 embedding runtime 与 VLM runtime 合成一个类。`manage_lifecycle` 缺省仍为 `False`。不要把 `172.x` 写进 frozen `vlm.base_url`。

- [ ] **Step 1: 搬迁；脚本再导出上述名字**

若 `test_vlm_stage_runtime.py` 通过 `mod.stop_model` 断言命令数组，再导出即可，不必改测试。若它 patch 的是脚本全局而实现已改到 `vlm_runtime.stop_model`，把 patch 改到 `app.pipeline.vlm_runtime`。

- [ ] **Step 2: hiddenimports** 增加 `app.pipeline.vlm_runtime`

- [ ] **Step 3: 测试**

```powershell
uv run pytest -q tests/test_pipeline_facade.py tests/task_engine/test_vlm_stage_runtime.py tests/test_ollama_runtime.py tests/task_engine/test_packaged_stage_imports.py
```

Expected: PASS。命令数组、`auto` URL、未知 `launch_mode` 立即 raise、不访问真实网络。

- [ ] **Step 4: Commit**

```powershell
git add -- app/pipeline/vlm_runtime.py scripts/test_video_adaptive.py build_exe.spec tests/task_engine/test_packaged_stage_imports.py tests/task_engine/test_vlm_stage_runtime.py
git commit -m "refactor: extract adaptive VLM runtime into app.pipeline.vlm_runtime"
```

---

### Task 4: Extract scoring, ranking, GIF helpers, quality glue

**Files:**

- Create: `app/pipeline/scoring.py`, `app/pipeline/ranking.py`, `app/pipeline/export_gif.py`, `app/pipeline/quality_bridge.py`
- Modify: `scripts/test_video_adaptive.py`, `build_exe.spec`, `tests/task_engine/test_packaged_stage_imports.py`, 以及本 Task 搬迁符号所在的 monkeypatch 测试（`tests/test_two_tier_scoring.py`, `tests/test_rank_pipeline_preference.py`, `tests/test_adaptive_direct_transition.py`, `tests/test_gif_windows.py`）

**Move**

- scoring：`parse_vlm_response`、`_score_vlm_frame`、`frame_passes_keep_gate`、`collect_refine_timestamps`、`_score_frames_concurrent`、`_ScoredItem`、checkpoint helpers、`backfill_clip_captions`、`_resolve_score_calibrator`
- ranking：`_clip_base_export_payload`、`_rank_clips_with_preference`、`_rank_pipeline_clips`、`_quality_ranking_weights`、`_clip_embedding_text`、`_compute_clip_embeddings`
- export_gif：`_palette_filters_for`、`_warn_once_on_indivisible_fps`、`_single_frame_cap`
- quality_bridge：从 `_quality_config_from_pipeline_cfg` 到 `_quality_export_lineage` 的 Quality MoE 胶水（约 L1590–2147），**调用** `app.quality_moe.*`，不要把 evaluator 再实现一遍

`_score_vlm_frame` 的三次重试、禁止 `0.5` 回退、整数 0–100 → `normalize_vlm_unit_score` 必须原样保留。

- [ ] **Step 1: 搬迁 + 再导出**

`run_gif_export_attempt` 仍来自 `app.services.batch_logging`。脚本继续 `from app.services.batch_logging import run_gif_export_attempt`，这样 `tests/test_gif_windows.py` 对脚本属性的 patch 在 Direct 仍走脚本全局时继续有效。若 `run_pipeline` 已改 import `batch_logging.run_gif_export_attempt`，把该测试改为 patch `app.services.batch_logging.run_gif_export_attempt`。

- [ ] **Step 2: hiddenimports** 增加四个新模块

- [ ] **Step 3: 测试**

```powershell
uv run pytest -q tests/test_pipeline_facade.py tests/test_two_tier_scoring.py tests/test_rank_pipeline_preference.py tests/test_adaptive_config.py tests/test_gif_windows.py tests/test_export_ranking.py tests/quality_moe/test_pipeline_integration.py tests/task_engine/test_packaged_stage_imports.py
```

Expected: PASS。

- [ ] **Step 4: Commit**

```powershell
git add -- app/pipeline/scoring.py app/pipeline/ranking.py app/pipeline/export_gif.py app/pipeline/quality_bridge.py scripts/test_video_adaptive.py build_exe.spec tests/task_engine/test_packaged_stage_imports.py tests/test_two_tier_scoring.py tests/test_rank_pipeline_preference.py tests/test_adaptive_direct_transition.py tests/test_gif_windows.py
git commit -m "refactor: extract adaptive scoring, ranking, and quality glue into app.pipeline"
```

---

### Task 5: Extract eight stage handlers

**Files:**

- Create: `app/pipeline/stage_io.py`, `app/pipeline/stages/__init__.py`, `app/pipeline/stages/discover.py`, `sample.py`, `vlm.py`, `refine.py`, `synthesize.py`, `rank_dedup.py`, `gif_clip.py`, `materialize.py`
- Modify: `scripts/test_video_adaptive.py`, `build_exe.spec`, `tests/task_engine/test_packaged_stage_imports.py`

**Move**

- stage I/O：`_load_manifest`、`_save_manifest`、`_make_artifact`、`_hash_artifact_id`、`_read_upstream_manifest`、`_load_input_manifest`、`_run_stage`
- 各 `_stage_*` 函数体迁到对应文件，**对外函数名保持 `_stage_<name>`**（再导出到脚本），避免 `test_full_production_stage_chain` / `test_production_artifact_contract` 大面积改 import。

阶段文件之间用绝对 import（`from app.pipeline.scoring import _score_vlm_frame`），不要相对循环。`rank_dedup` 调用 `quality_bridge` 与 `ranking`；`gif_clip` 调用已有 `gif_encode` / `gif_windows` / `batch_logging`。

- [ ] **Step 1: 一阶段一文件搬迁，脚本：**

```python
from app.pipeline.stages.discover import _stage_discover
from app.pipeline.stages.sample import _stage_sample
# ...
from app.pipeline.stage_io import _run_stage, run_stage_mode
```

- [ ] **Step 2: hiddenimports** 增加 `app.pipeline.stage_io`、`app.pipeline.stages` 及八个 stage 模块

- [ ] **Step 3: 测试**

```powershell
uv run pytest -q tests/test_pipeline_facade.py tests/task_engine/test_stage_adapter.py tests/task_engine/test_stage_inputs.py tests/task_engine/test_production_artifact_contract.py tests/task_engine/test_packaged_stage_imports.py tests/task_engine/test_vlm_stage_runtime.py
```

Expected: PASS。adapter 仍指向 `scripts/test_video_adaptive.py`。

- [ ] **Step 4: Commit**

```powershell
git add -- app/pipeline/stage_io.py app/pipeline/stages scripts/test_video_adaptive.py build_exe.spec tests/task_engine/test_packaged_stage_imports.py
git commit -m "refactor: move eight adaptive stage handlers into app.pipeline.stages"
```

---

### Task 6: Extract Direct path and CLI; shrink the script to a facade

**Files:**

- Create: `app/pipeline/direct.py`, `app/pipeline/cli.py`
- Modify: `scripts/test_video_adaptive.py`, `app/pipeline/__init__.py`, `build_exe.spec`, `tests/task_engine/test_packaged_stage_imports.py`, `tests/test_adaptive_direct_transition.py`, `tests/test_adaptive_direct_action.py`

**Move**

- `run_pipeline`, `run_direct_mode` → `direct.py`（P2 前 **不要** 改算法，整段搬迁）
- `parse_cli_args`, `main`, `_TeeIO` → `cli.py`

脚本最终形态：

1. stdout/stderr UTF-8 reconfigure（打包控制台需要）
2. `sys.path.insert(0, ".")` 可保留
3. 从 `app.pipeline.*` 再导出 `FACADE_NAMES`
4. `if __name__ == "__main__":` 调 `app.pipeline.cli.main()`

目标：脚本自身（不含再导出）低于约 400 行。

- [ ] **Step 1: 搬迁 Direct / CLI**

`tests/test_adaptive_direct_transition.py` 若 patch `test_video_adaptive.stop_model` 而 `run_pipeline` 现调用 `vlm_runtime.stop_model`，在**本 Task** 把 patch 改为实际被调用的模块（通常同时 patch 脚本再导出名与 `app.pipeline.vlm_runtime.stop_model`，或只 patch 实现模块并确认 Direct 走该实现）。

- [ ] **Step 2: `app/pipeline/__init__.py` 只导出稳定公开 API**（`extract_config`, `run_pipeline`, `run_direct_mode`, `run_stage_mode`）。私有 `_stage_*` 继续从脚本再导出，不必全部升为包的公开 API。

- [ ] **Step 3: 测试**

```powershell
uv run pytest -q tests/test_pipeline_facade.py tests/test_adaptive_direct_transition.py tests/test_adaptive_direct_action.py tests/test_adaptive_config.py tests/task_engine/test_stage_adapter.py tests/task_engine/test_packaged_stage_imports.py
```

Expected: PASS。`scripts/test_video_adaptive.py` 行数显著下降。

- [ ] **Step 4: Commit**

```powershell
git add -- app/pipeline/direct.py app/pipeline/cli.py app/pipeline/__init__.py scripts/test_video_adaptive.py build_exe.spec tests/task_engine/test_packaged_stage_imports.py tests/test_adaptive_direct_transition.py tests/test_adaptive_direct_action.py
git commit -m "refactor: shrink test_video_adaptive.py to a CLI facade over app.pipeline"
```

---

### Task 7: Phase 1 integration gate

- [ ] **Step 1: 编译 + 自适应/stage 相关测试**

```powershell
uv run python -m compileall -q app/pipeline scripts/test_video_adaptive.py
uv run pytest -q tests/test_pipeline_facade.py tests/test_adaptive_config.py tests/test_two_tier_scoring.py tests/test_rank_pipeline_preference.py tests/test_adaptive_direct_transition.py tests/test_adaptive_direct_action.py tests/task_engine/test_vlm_stage_runtime.py tests/task_engine/test_stage_adapter.py tests/task_engine/test_stage_inputs.py tests/task_engine/test_packaged_stage_imports.py tests/task_engine/test_production_artifact_contract.py tests/quality_moe/test_pipeline_integration.py
```

- [ ] **Step 2: 数据体检**

```powershell
Get-Item data/*.db | Select-Object FullName, Length, LastWriteTime
```

与 Task 1 基线对比，必须一致。

- [ ] **Step 3:** 本 Task 无产品代码。若只有测试调整，单独提交；否则不提交空 commit。

**Stop here until Phase 1 is green.** 不要在 facade 测试或 Direct 测试失败时进入 P2。

---

## Phase 2 — Unify Direct and Staged

这是简洁度收益最大、回归风险最高的一步。只在 P1 完成后开始。

### Task 8: Direct/Staged parity harness

**Files:**

- Create: `tests/test_direct_staged_parity.py`

**Pass condition:** 同一冻结 cfg + 同一 mock VLM/ffprobe/embedding，对 `run_pipeline` 与依次调用八个 `_stage_*`（或 `run_stage_mode` 链）比较：

- keep-gate 幸存者 timestamps
- merge 后 clip 的 `(start, end, peak_score)`
- embedding-dedup + temporal-dedup 后的 clip_id 集合
- quality decision / `candidate_id`
- 导出文件名（`build_gif_filename` 结果，允许不写真实 GIF：patch `run_gif_export_attempt`）

先写测试。P1 结束时 Direct 与 Staged 仍是两套实现，**本测试现在可以 FAIL**。不要为了让它绿而放宽断言。

- [ ] **Step 1: 写对账测试，使用 tmp_path，禁止读 `data/exports`**

- [ ] **Step 2: 跑测试，确认失败原因是两套实现差异或尚未统一，而不是 fixture 写错**

```powershell
uv run pytest -q tests/test_direct_staged_parity.py -s
```

Expected: FAIL（P2 前）。把失败摘要记下来。

- [ ] **Step 3: 不要在本 Task 改生产代码。Commit 测试。**

```powershell
git add -- tests/test_direct_staged_parity.py
git commit -m "test: add direct vs staged adaptive parity harness"
```

---

### Task 9: Make `run_pipeline` call the same stage functions

**Files:**

- Modify: `app/pipeline/direct.py`, `app/pipeline/stage_io.py`, 必要时 `app/pipeline/stages/*.py`
- Modify: `tests/test_direct_staged_parity.py` 若需要更稳的 mock
- Do not modify: adapter 子进程协议、stage 名称、`gif_clip` fan-out 契约

**Design**

- Direct 在 tmp/work_dir（或现有 frames/export 目录旁的一次性目录）里写与 Staged 相同的 manifest，然后调 `_run_stage` / 各 `_stage_*`。
- 允许 Direct 在进程内连续跑八个 stage（不经过 worker lease）。不允许为了 Direct 把 production worker 改成 in-process。
- 删除 Direct 里与 staged 重复的 sample/VLM/refine/merge/dedup/quality/export 循环。共享函数必须只有一份。
- `clear_output_dir`、`ExportDirectoryLock`、`adaptive_test_result.json` 写入仍只属于 Direct CLI，不要塞进 `_stage_materialize`。
- VLM scored checkpoint（`frames_dir` 上的身份 = `vlm_model` + `score_prompt_mode`）语义不变。
- Quality MoE 仍在 **output_ratio / max_output 截断之前** 评估全量 post-dedup 集合。

- [ ] **Step 1: 实现共享调度，删 Direct 重复循环**

- [ ] **Step 2: 对账必须变绿**

```powershell
uv run pytest -q tests/test_direct_staged_parity.py tests/test_adaptive_direct_transition.py tests/test_adaptive_direct_action.py tests/quality_moe/test_pipeline_integration.py
```

- [ ] **Step 3: 四条生产 E2E（硬门）**

```powershell
uv run pytest -q tests/task_engine/test_full_production_stage_chain.py -s
```

必须仍覆盖：全成功（refine>0、真实 GIF、LLM stub 被调用）、VLM 503 耗尽 attempts、非法 `{}` 无 0.5 默认分、真实低分零 clip。

- [ ] **Step 4: worker / adapter 回归**

```powershell
uv run pytest -q tests/task_engine/test_stage_adapter.py tests/task_engine/test_worker.py tests/task_engine/test_stage_inputs.py tests/task_engine/test_packaged_stage_imports.py
```

- [ ] **Step 5: Commit**

```powershell
git add -- app/pipeline/direct.py app/pipeline/stage_io.py app/pipeline/stages tests/test_direct_staged_parity.py
git commit -m "refactor: run direct adaptive mode through the same stage handlers as staged jobs"
```

---

## Phase 3 — Split `artifacts.py` into a package

### Task 10: Convert `artifacts.py` to a package with stable imports

**Files:**

- Delete: `app/task_engine/artifacts.py`（与创建包同一提交）
- Create: `app/task_engine/artifacts/__init__.py` 及下列模块
- Modify: none of the callers if `__init__.py` 再导出全部旧名字

**Split**

| New module | Contents |
|------------|----------|
| `kinds.py` | `STAGE_ARTIFACT_KINDS`, `STAGE_INPUT_KINDS`, `STAGE_OPTIONAL_INPUT_KINDS`, `_INPUT_PRODUCER` |
| `identity.py` | `make_artifact_id`, `validate_artifact`, `validate_artifact_strict`, `ArtifactCollisionError` |
| `store.py` | `insert_artifact_dedup`, `insert_artifacts_batch`, `_fetch_artifacts_for_stage` |
| `resolve.py` | `resolve_stage_inputs`, `resolve_all_gif_clip_artifacts`, `resolve_materialize_inputs`, `build_materialize_input_envelope`, `GifClipStatus`, `MaterializeInputs`, `validate_rank_manifest_with_db_lineage`, `_assert_zero_clip_proven` |
| `manifests.py` | `validate_manifest_json`, `validate_materialize_envelope`, `_MANIFEST_VALIDATORS` dispatch |
| `quality_schema.py` | quality assessment / ledger / repair / gif quality lineage 校验 |
| `action_schema.py` | action clip v2 / guard 校验（仅当能无环切开） |

`__init__.py` 必须再导出 Task 1 锁住的全部公开名。禁止改 `validate_manifest_json` 的报错字符串（`test_manifest_validation.py` 按消息匹配）。

Windows：先写到临时包名再替换会更乱。正确顺序：读完 `artifacts.py` → 写子模块到新目录之外的 staging 不可行。一次性：创建 `artifacts/` 前必须先删 `artifacts.py`。本地操作：把文件内容读入编辑器，删除 `artifacts.py`，再写包文件。

- [ ] **Step 1: 建包并再导出**

- [ ] **Step 2: 测试（import 路径完全不变）**

```powershell
uv run pytest -q tests/test_pipeline_facade.py tests/task_engine/test_artifacts.py tests/task_engine/test_manifest_validation.py tests/task_engine/test_production_artifact_contract.py tests/task_engine/test_materialize_resolver.py tests/task_engine/test_stage_inputs.py tests/test_gif_windows.py
```

Expected: PASS。

- [ ] **Step 3: Commit**

```powershell
git add -- app/task_engine/artifacts app/task_engine/artifacts.py
git commit -m "refactor: split task_engine.artifacts into a package with stable imports"
```

---

### Task 11: Phase 3 gate

```powershell
uv run pytest -q tests/task_engine/test_manifest_validation.py tests/task_engine/test_production_artifact_contract.py tests/task_engine/test_materialize_production.py tests/task_engine/test_e2e.py tests/task_engine/test_production_e2e.py tests/quality_moe/test_pipeline_integration.py
Get-Item data/*.db | Select-Object FullName, Length, LastWriteTime
```

---

## Phase 4 — UI compatibility cleanup

Workbench 已是默认 UI。本阶段先迁测试，再删兼容魔术。不要与 P1/P2 混在同一提交。

### Task 12: Retarget tests off `app.ui.candidate_review` monkeypatches

**Files:**

- Modify: `tests/test_candidate_review_auto_advance.py`, `tests/test_candidate_review_favorites.py`, `tests/test_candidate_review_profile_publish.py`, `tests/test_candidate_review_layout.py`, `tests/test_candidate_review_status.py`, `tests/test_candidate_review_batch_queue.py`, `tests/test_candidate_review_legacy_api.py`, `tests/test_batch_process_status.py`, `tests/test_config_help_annotations.py`, `tests/test_launcher_gradio_options.py`, `tests/test_workbench_structure.py`, `tests/test_active_review_ui.py`, `tests/test_video_batch_queue.py`

**Rules**

- Review 行为测试 import `app.ui.tabs.review`
- Control / checkpoint 测试 import `app.ui.tabs.control`
- Settings / tooltip 测试 import `app.ui.tabs.settings`
- Profile 测试 import `app.ui.tabs.profile`
- `launch_kwargs` / Gradio `allowed_paths` 测试 import `app.ui.workbench` 或 `app.ui.candidate_review.launch_kwargs` 的最终定义处
- `GIFAGENT_LEGACY_QUEUE_UI` 仍须有至少一个测试锁在 `legacy_candidate_review` / `candidate_review` 的分支上
- `allowed_paths` 必须是 `list[str]`，不能是 tuple

- [ ] **Step 1: 改 import / monkeypatch 目标，不改产品行为**

- [ ] **Step 2: 测试**

```powershell
uv run pytest -q tests/test_candidate_review_*.py tests/test_batch_process_status.py tests/test_config_help_annotations.py tests/test_launcher_gradio_options.py tests/test_launcher_port.py tests/test_workbench_structure.py tests/test_active_review_ui.py tests/test_video_batch_queue.py
```

- [ ] **Step 3: Commit**

```powershell
git add -- tests/test_candidate_review_*.py tests/test_batch_process_status.py tests/test_config_help_annotations.py tests/test_launcher_gradio_options.py tests/test_workbench_structure.py tests/test_active_review_ui.py tests/test_video_batch_queue.py
git commit -m "test: point candidate-review tests at tab modules instead of the compatibility facade"
```

---

### Task 13: Delete `_CompatibilityModule`; quarantine legacy UI

**Files:**

- Modify: `app/ui/candidate_review.py`
- Keep: `app/ui/legacy_candidate_review.py` 仅由 `GIFAGENT_LEGACY_QUEUE_UI=1` 使用
- Optional follow-up（单独 Task，本计划不强制）：删 `legacy_candidate_review.py` 与 `GIFAGENT_LEGACY_QUEUE_UI`

`candidate_review.py` 允许保留薄 re-export（`summarize_checkpoint_status` 等）以免外部脚本立刻损坏，但必须删除：

- `sys.modules[__name__].__class__ = _CompatibilityModule`
- `__getattr__` 镜像到 legacy 的 setattr 魔术

- [ ] **Step 1: 删兼容 ModuleType 子类；必要时把仍被 import 的符号改为显式再导出**

- [ ] **Step 2: 测试**

```powershell
uv run pytest -q tests/test_candidate_review_*.py tests/test_workbench_structure.py tests/test_launcher_gradio_options.py tests/test_config_help_annotations.py
```

- [ ] **Step 3: Commit**

```powershell
git add -- app/ui/candidate_review.py
git commit -m "refactor: drop candidate_review ModuleType monkeypatch shim"
```

---

## Phase 5 — Preference leftovers

### Task 14: Remove duplicate vector IO and router SQL

**Files:**

- Modify: `app/services/preference_memory.py`, `app/routers/preference.py`
- Optional: `app/services/candidate_vectors.py` 仅当能干净切开 backfill vs hashing；不要为拆而拆成循环 import

**Changes**

- `_serialize_vector` / `_deserialize_vector` 改为 `app.services.vector_math.vector_to_blob` / `blob_to_vector`（注意 centroid 打包长度可以是 `k * dim`，`max_cosine` 已处理；不要误用强制 `embedding_dim` 的 `vector_to_blob` 去写多 prototype blob）
- `GET /api/preference/profiles` 的 SQL 迁到 `PreferenceMemoryService.list_builds()`（或等价方法），router 只做 HTTP / 503 busy
- 不要改 7 个 profile gate 阈值、不要改 publish 锁 503 语义

- [ ] **Step 1: 实现**

- [ ] **Step 2: 测试**

```powershell
uv run pytest -q tests/test_preference_profiles.py tests/test_preference_profile_v2.py tests/test_preference_api.py tests/test_preference_evaluation.py tests/test_preference_reranker.py tests/test_candidate_vectors.py tests/test_vector_health.py tests/test_rank_pipeline_preference.py tests/test_preference_preflight.py
```

可选：`uv run python scripts/smoke_active_preference.py`（内存库，不碰 production db）。

- [ ] **Step 3: Commit**

```powershell
git add -- app/services/preference_memory.py app/routers/preference.py app/services/candidate_vectors.py
git commit -m "refactor: reuse vector_math and move preference list SQL into the service"
```

---

## Phase 6 — Release documentation

### Task 15: Update `Agent.md` Architecture Overview only

**Files:**

- Modify: `Agent.md`

在 Architecture Overview 中把 `scripts/test_video_adaptive.py` 描述改为「CLI facade，实现位于 `app/pipeline/`」；把 `app/task_engine/artifacts.py` 改为 `app/task_engine/artifacts/` 包。不要改模型表、adaptive 缺省数字、生产 release gate 命令，除非本计划过程中命令路径确实变了。

- [ ] **Step 1: 文档与代码一致**

- [ ] **Step 2: 全量发布门禁**

```powershell
uv run python -m compileall -q app scripts tests
uv run pytest -q tests/task_engine/test_full_production_stage_chain.py -s
uv run pytest -q tests/task_engine tests/quality_lab
uv run pytest -q
git diff --check
Get-Item data/*.db | Select-Object FullName, Length, LastWriteTime
```

- [ ] **Step 3: Commit**

```powershell
git add -- Agent.md
git commit -m "docs: record app.pipeline and artifacts package layout in Agent.md"
```

---

## Verification matrix

| Phase | Gate | Command | Pass |
|-------|------|---------|------|
| 0 | Facade lock | `pytest -q tests/test_pipeline_facade.py` | 脚本与 artifacts 公开名仍在 |
| 1 | Config freeze | `pytest -q tests/test_adaptive_config.py tests/test_tasks_api.py tests/test_two_tier_scoring.py` | 缺省不变；env 不能覆盖 score prompt mode |
| 1 | VLM runtime | `pytest -q tests/task_engine/test_vlm_stage_runtime.py tests/test_ollama_runtime.py` | 命令数组与 auto URL 不变 |
| 1 | Packaged closure | `pytest -q tests/task_engine/test_packaged_stage_imports.py` | 每个新 `app.pipeline.*` 都在 spec 里 |
| 2 | Parity | `pytest -q tests/test_direct_staged_parity.py` | Direct 与 Staged clip 集合一致 |
| 2 | Production E2E | `pytest -q tests/task_engine/test_full_production_stage_chain.py -s` | 四场景全过 |
| 3 | Manifests | `pytest -q tests/task_engine/test_manifest_validation.py tests/task_engine/test_production_artifact_contract.py` | import 路径与错误字符串不变 |
| 4 | Review UI | `pytest -q tests/test_candidate_review_*.py tests/test_workbench_structure.py tests/test_launcher_*.py` | 点赞打到当前页选中项；`allowed_paths` 为 list |
| 5 | Preference | `pytest -q tests/test_preference_*.py tests/test_candidate_vectors.py tests/test_rank_pipeline_preference.py` | gate / publish / rerank 语义不变 |
| All | Data | `Get-Item data/*.db` | size 与 mtime 相对 Task 1 不变 |
| All | Release | `compileall` + 全量 `pytest -q` + `git diff --check` | 与 `Agent.md` Production Release Gate 一致 |

---

## Done looks like

- `scripts/test_video_adaptive.py` 主要是再导出 + CLI，实现在 `app/pipeline/`。
- Direct 与 Staged 共用一套 stage 函数；`tests/test_direct_staged_parity.py` 为绿。
- `from app.task_engine.artifacts import validate_manifest_json` 仍然成立，文件变成包。
- `candidate_review.py` 没有 `ModuleType` 子类。
- Preference 不再手写一套 vector serialize。
- 全量 pytest 与四条 E2E 通过；`data/*.db` 未被测试改写。

---

## Explicit non-goals

- 把 stage 从子进程改为 worker 进程内调用。
- 用 LangGraph / 新编排器替换 `TaskWorker`。
- 合并 `quality_lab` 与 `quality_moe`。
- 对齐 `models.yaml` 与 `models.adult_candidate.yaml`。
- 删除 `GIFAGENT_LEGACY_QUEUE_UI` 或 `legacy_candidate_review.py`（P4 只隔离，不删除，除非另开计划）。
- 改 adaptive 阈值、prompt 文本、Quality 软拒绝策略、或任何导出排序公式。
- 为了「看起来更干净」重命名 `clip_id` / manifest 字段 / artifact_kind。
