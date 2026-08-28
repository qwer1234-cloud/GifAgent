"""Facade identity lock for the 2026-08-28 pipeline module split.

`scripts/test_video_adaptive.py` must keep re-exporting every name that tests
and smoke scripts import directly, and `app.task_engine.artifacts` must keep
its public surface stable while it is converted from a module into a package.
"""

from __future__ import annotations

import importlib
import importlib.util

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

ARTIFACT_NAMES = [
    "make_artifact_id",
    "validate_artifact",
    "validate_artifact_strict",
    "insert_artifact_dedup",
    "STAGE_ARTIFACT_KINDS",
    "STAGE_INPUT_KINDS",
    "resolve_stage_inputs",
    "resolve_materialize_inputs",
    "validate_manifest_json",
    "validate_materialize_envelope",
    "validate_rank_manifest_with_db_lineage",
]


def test_adaptive_script_reexports_facade_names() -> None:
    spec = importlib.util.find_spec("scripts.test_video_adaptive")
    assert spec is not None, "scripts/test_video_adaptive.py must stay importable"
    module = importlib.import_module("scripts.test_video_adaptive")
    missing = [name for name in FACADE_NAMES if not hasattr(module, name)]
    assert missing == [], f"adaptive script lost facade names: {missing}"


def test_artifacts_public_names() -> None:
    module = importlib.import_module("app.task_engine.artifacts")
    missing = [name for name in ARTIFACT_NAMES if not hasattr(module, name)]
    assert missing == [], f"app.task_engine.artifacts lost public names: {missing}"
