#!/usr/bin/env python3
"""
Two-pass adaptive GIF extraction:
  Pass 1: coarse sample every N seconds -> VLM scores
  Pass 2: around high-score regions, re-sample at finer intervals
  Adjacent high-score frames are merged into longer clips.
  Top-50 ranked by gif_worthiness.
"""
from __future__ import annotations

import sys

# Windows console defaults to GBK -- reconfigure to handle Unicode.
# Line buffering keeps refine/VLM progress in stage.log if the process is killed.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, ".")

# ---------------------------------------------------------------------------
# CLI facade.  The implementation lives in app.pipeline.*; this module only
# re-exports the historical names that tests, smoke scripts, and the packaged
# ``--run-script`` entry point import from here.
#
# NOTE for tests: monkeypatching a name here does NOT affect the
# implementation, which reads its own module globals.  Patch the module the
# caller actually uses (e.g. ``app.pipeline.direct`` for the direct pipeline,
# ``app.pipeline.stages.<stage>`` for a stage handler).
# ---------------------------------------------------------------------------
import httpx
import subprocess
import time

from app.db import get_connection, init_db
from app.pipeline.cli import main, parse_cli_args
from app.pipeline.config import (
    DEFAULT_MAX_REFINE_FRAMES,
    _extract_direct_snapshot_config,
    _optional_int,
    _optional_seed,
    extract_config,
)
from app.pipeline.direct import run_direct_mode, run_pipeline
from app.pipeline.export_gif import (
    _WARNED_FPS,
    _apply_boundary_snaps,
    _ffmpeg_seconds,
    _palette_filters_for,
    _single_frame_cap,
    _temporal_media_duration,
    _warn_once_on_indivisible_fps,
)
from app.pipeline.prompts import (
    SCORE_PROMPT,
    SCORE_PROMPT_ADULT,
    SCORE_PROMPT_ADULT_FAST,
    SCORE_PROMPT_FAST,
    _scoring_schema,
    _scoring_vlm_options,
    _vlm_options,
    get_score_prompt,
)
from app.pipeline.quality_bridge import (
    _QUALITY_HARD_GATE_INPUT_FIELDS,
    _QUALITY_LINEAGE_FIELDS,
    _QUALITY_SOURCE_HASH_CACHE,
    _assert_quality_source_unchanged,
    _enrich_quality_assessment,
    _evaluate_quality_pipeline_candidates,
    _export_repair_recipe,
    _quality_candidate_ledger,
    _quality_config_from_pipeline_cfg,
    _quality_evidence_hash,
    _quality_export_lineage,
    _quality_hard_gate_context,
    _quality_moe_summary,
    _quality_source_sha256,
    _resolve_quality_runtime_snapshot,
    _stable_source_sha256,
    _validated_repair_recipe,
)
from app.pipeline.ranking import (
    _assign_candidate_identities,
    _clip_base_export_payload,
    _clip_embedding_text,
    _compute_clip_embeddings,
    _planned_output_count,
    _quality_ranking_weights,
    _rank_clips_with_preference,
    _rank_pipeline_clips,
)
from app.pipeline.scoring import (
    _ScoredItem,
    _load_scored_checkpoint,
    _resolve_score_calibrator,
    _save_scored_checkpoint,
    _score_frames_concurrent,
    _score_vlm_frame,
    _scored_checkpoint_path,
    _video_identity,
    backfill_clip_captions,
    collect_refine_timestamps,
    frame_passes_keep_gate,
    parse_vlm_response,
)
from app.pipeline.stage_io import (
    _MANIFEST_NAME,
    _PREV_STAGE,
    _TeeIO,
    _hash_artifact_id,
    _load_input_manifest,
    _load_manifest,
    _make_artifact,
    _read_upstream_manifest,
    _run_stage,
    _save_manifest,
    run_stage_mode,
)
from app.pipeline.stages.discover import _stage_discover
from app.pipeline.stages.gif_clip import _stage_gif_clip
from app.pipeline.stages.materialize import _stage_materialize
from app.pipeline.stages.rank_dedup import (
    _ARTIFACT_LINEAGE_FIELDS,
    _freeze_stage_action_config,
    _rank_source_artifact_lineage,
    _stage_rank_dedup,
    _write_rank_candidate_ledger,
)
from app.pipeline.stages.refine import _stage_refine
from app.pipeline.stages.sample import _resolve_legacy_sample_frame_ref, _stage_sample
from app.pipeline.stages.synthesize import _stage_synthesize, _synthesize_clips_with_llm
from app.pipeline.stages.vlm import _stage_vlm
from app.pipeline.timing import (
    _attach_timings,
    _timed,
    current_timings,
    reset_timings,
)
from app.pipeline.vlm_runtime import (
    OLLAMA_BASE,
    VlmRuntimeConfig,
    _attach_live_vlm_base_url,
    _expand_vlm_base_url,
    _is_stable_http_url,
    _materialize_vlm_runtime,
    _ollama_command,
    _resolve_vlm_config,
    _resolve_vlm_runtime,
    _should_manage_vlm_lifecycle,
    _validate_vlm_provider,
    stop_model,
    wait_model,
)

# Historical re-exports that tests monkeypatch on this module.  The pipeline
# implementation no longer reads them here; retarget those patches.
from app.services.action_pipeline import materialize_action_candidates
from app.services.batch_logging import format_gif_export_line, run_gif_export_attempt
from app.services.export_cleanup import (
    ExportDirectoryBusyError,
    ExportDirectoryLock,
    cleanup_adaptive_export_dir,
)
from app.services.export_ranking import (
    make_adult_moe_scorer,
    normalize_vlm_unit_score,
    rank_clips_for_export,
    sex_act_score,
)
from app.services.transition_candidates import build_guarded_clips
from app.services.transition_guard import guard_candidate_window

if __name__ == "__main__":
    main()
