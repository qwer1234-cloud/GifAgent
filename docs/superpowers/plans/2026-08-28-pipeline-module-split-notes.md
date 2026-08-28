# 2026-08-28 Pipeline Module Split — 执行笔记

> **执行完成（2026-08-28）。** 全部 15 个 Task 落地，未提交（等用户指示）。
> 最终门禁：全量 pytest **1717 passed / 2 skipped / 1 failed**（唯一失败
> `tests/test_desktop_export_sync.py::test_graceful_shutdown_stops_ollama_runtime`
> 为工作区中**未提交的 launcher.py 改动**与既有测试的预先存在的不匹配——
> 用 HEAD 版 launcher.py 验证该测试通过，与本重构无关）；`compileall` 全过；
> `git diff --check` 干净；`data/*.db` 与基线完全一致。

## 执行结果摘要

| Phase | 结果 |
|-------|------|
| P0 门禁 | `tests/test_pipeline_facade.py` 建立；data 基线 library.db=183,320,576 / quality_lab.db=122,880 |
| P1 拆分 | `scripts/test_video_adaptive.py` 5833 → **181 行 facade**；实现迁入 `app/pipeline/`（config/prompts/vlm_runtime/scoring/ranking/export_gif/quality_bridge/timing/stage_io/stages×8/direct/cli）；hiddenimports 契约同步 |
| P2 统一 | `run_pipeline` 改为编排共享 stage 函数（in-process 写同一套 manifest）；parity 测试**提前绿**（两实现决策本就一致），作为回归锁保留；四条生产 E2E 全过 |
| P3 artifacts | `artifacts.py`(2060 行) → 包 `artifacts/`（identity/store/kinds/resolve/manifests/quality_schema）；`__init__` 再导出全部公开名；manifest 校验错误字符串未动 |
| P4 UI | candidate_review 测试迁至 `tabs/*`（队列类迁 `legacy_candidate_review`，作为 legacy 分支锁）；`_CompatibilityModule` + `__getattr__` 已删除，facade 仅剩显式薄再导出 |
| P5 preference | `_serialize_vector`/`_deserialize_vector` 移除，读路径用 `vector_math.blob_to_vector`；k=1 质心保留原始加权平均字节（`vector_to_blob` 会归一化+强校验 dim，测试锁定原始语义，见计划注意事项）；`GET /profiles` SQL 迁入 `PreferenceMemoryService.list_builds()`，router 只做 HTTP/503 |
| P6 文档 | Agent.md Architecture Overview 更新（app/pipeline/ 树 + facade 描述 + artifacts/ 包行） |

## 顺带修复（门禁所需，均在计划精神内）

- `run_stage_mode` 现通过 `app.config.swap_config_override()` 在 finally 中恢复
  之前的全局配置——修掉 in-process stage 测试向全进程泄漏 job snapshot 的
  预先存在缺陷（曾致 `test_preference_preflight` 3 例在全量下失败）。
- monkeypatch 目标全部随实现迁移（direct→`app.pipeline.direct`/`ranking`/
  `quality_bridge`；stage→`app.pipeline.stages.<name>`；`_WARNED_FPS`→
  `export_gif`；`run_gif_export_attempt`→`stages.gif_clip`）。
- `tests/test_batch_logging.py` 源码断言改为读 `app/pipeline/direct.py` +
  `app/pipeline/stages/gif_clip.py`。
- direct 导出命令统一为 staged 的 `-lavfi`（`-filter_complex` 的等价形式，
  production E2E 一直走 `-lavfi`）。

## 已知语义变化（统一 Direct/Staged 的结果，计划允许）

- Direct 的 guard/materialize 现在用 staged 的 action 归一化窗口
  （`test_direct_pipeline_fans_guarded_segments_out_before_dedup` 断言已按
  统一值 (0.0, 15.9375, 10.0) 更新）。
- Direct 的 VLM 生命周期现在遵守冻结配置的 `manage_lifecycle`（旧 direct 无视
  该配置总是 stop/wait）。
- 视频级 LLM synthesis 输入从「dedup 后 clips」变为「rank 截断后 clips」
  （字段不在任何测试断言内）。

## 已知遗留（不在本计划范围）

- `tests/test_desktop_export_sync.py::test_graceful_shutdown_stops_ollama_runtime`
  与工作区未提交的 `app/ui/launcher.py`（`_register_window_shutdown` watchdog）
  不匹配——需要 launcher 改动的作者跟进。

---

## Task 1 Step 1: data/*.db 基线（2026-08-28，执行开始时）

| File | Length | LastWriteTime |
|------|-------:|---------------|
| `data/library.db` | 183,320,576 | 2026-07-23 20:59 |
| `data/quality_lab.db` | 122,880 | 2026-08-23 20:44 |

每个后续 Task 结束后重跑，两者必须不变。

## 当前行数基线

- `scripts/test_video_adaptive.py`: 5833 行
- `app/task_engine/artifacts.py`: 2060 行
- `app/ui/candidate_review.py`: 217 行

## 符号地图（scripts/test_video_adaptive.py）

| 行范围 | 内容 |
|--------|------|
| 1–43 | imports |
| 99–148 | timings helpers（`_TIMINGS`, `reset_timings`, `current_timings`, `_timed`, `_attach_timings`）|
| 151–401 | `parse_vlm_response`、`SCORE_PROMPT*`、`get_score_prompt`、checkpoint helpers、`_freeze_stage_action_config` |
| 406–623 | `_validate_vlm_provider`、`_response_eval_count`、`_score_vlm_frame` |
| 628–884 | VLM runtime：`VlmRuntimeConfig`…`wait_model` |
| 885–891 | `OLLAMA_BASE` |
| 892–949 | `DEFAULT_MAX_REFINE_FRAMES`、`collect_refine_timestamps`、`frame_passes_keep_gate` |
| 950–1146 | `extract_config`、`_optional_seed`、`_optional_int` |
| 1147–1275 | `_scoring_schema`、`_resolve_score_calibrator`、`_apply_boundary_snaps`、`_scoring_vlm_options`、`backfill_clip_captions` |
| 1276–1355 | `_ScoredItem`、`_score_frames_concurrent`、`_vlm_options` |
| 1356–1398 | `_single_frame_cap`、`_palette_filters_for`、`_warn_once_on_indivisible_fps` |
| 1399–1518 | ranking：`_quality_ranking_weights`…`_rank_pipeline_clips` |
| 1519–1527 | `_extract_direct_snapshot_config` |
| 1528–1589 | `_assign_candidate_identities`、`_planned_output_count`、`_is_stable_http_url`、`_attach_live_vlm_base_url` |
| 1590–2146 | Quality MoE 胶水（`_quality_config_from_pipeline_cfg`…`_quality_export_lineage`）|
| 2147–2235 | `_resolve_vlm_config`、`_should_manage_vlm_lifecycle`、`_clip_embedding_text`、`_compute_clip_embeddings` |
| 2236–3256 | `run_pipeline`（Direct 主体）|
| 3257–3381 | `run_direct_mode` |
| 3382–3534 | `_TeeIO`、`run_stage_mode` |
| 3535–3777 | manifest I/O：`_load_manifest`…`_load_input_manifest` |
| 3778–3823 | `_run_stage` |
| 3824–5833 | 八个 `_stage_*` + CLI（`parse_cli_args` 5740, `main` 5796）|
