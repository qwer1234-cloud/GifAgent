"""Frozen adaptive config extraction shared by direct and stage mode."""
from __future__ import annotations

from app.pipeline.export_gif import _palette_filters_for, _warn_once_on_indivisible_fps
from app.pipeline.ranking import _quality_ranking_weights
from app.quality_moe.config import QualityMoeConfig
from app.services.action_config import freeze_action_config
from app.services.gif_encode import DEFAULT_DIFF_MODE, DEFAULT_DITHER, DEFAULT_STATS_MODE
from app.services.score_prompt import normalize_score_prompt_mode, normalize_score_schema_mode


# ---- Config extraction (shared by direct and stage mode) ----------

DEFAULT_MAX_REFINE_FRAMES = 120


def extract_config(config_data: dict) -> dict:
    """Extract flat pipeline config from the full config dict."""
    adaptive = config_data.get("adaptive", {}) or {}
    pref_mem = config_data.get("preference_memory", {}) or {}
    quality_moe = QualityMoeConfig.from_mapping(config_data)
    normalized_action, computed_action_hash = freeze_action_config(adaptive)
    config = {
        "sample_interval": int(adaptive.get("sample_interval", 10)),
        "refine_interval": int(adaptive.get("refine_interval", 10)),
        "refine_radius": int(adaptive.get("refine_radius", 20)),
        "refine_threshold": float(adaptive.get("refine_threshold", 0.5)),
        "max_refine_frames": int(
            adaptive.get("max_refine_frames", DEFAULT_MAX_REFINE_FRAMES)
        ),
        **normalized_action,
        # A single scored frame says nothing about the rest of the window,
        # so it gets its own ceiling.  Falling back to the action
        # max_duration keeps every existing snapshot at today's behavior.
        "single_frame_max_duration_s": float(
            adaptive.get(
                "single_frame_max_duration_s",
                normalized_action["max_duration"],
            )
        ),
        "worthiness_threshold": float(adaptive.get("worthiness_threshold", 0.2)),
        # 0 = off (cinematic / historical snapshots). Adult 0-100 scoring
        # needs a floor so 0.87-worth setup cannot pass the keep gate.
        "sex_act_threshold": float(adaptive.get("sex_act_threshold", 0.0)),
        "merge_gap": int(adaptive.get("merge_gap", 12)),
        "merge_score_threshold": float(
            adaptive.get("merge_score_threshold", 0.55)
        ),
        # Hard cap so dense high-score runs cannot collapse into one mega-clip.
        "max_merge_span_s": float(adaptive.get("max_merge_span_s", 24)),
        # Multi-frame groups whose peak is below this are demoted to singles.
        # Defaults to refine_threshold when omitted.
        "merge_peak_threshold": float(
            adaptive.get(
                "merge_peak_threshold",
                adaptive.get("refine_threshold", 0.55),
            )
        ),
        "embed_sim_threshold": float(
            adaptive.get("embedding_dedup_threshold", 0.94)
        ),
        "embed_dedup_enabled": bool(
            adaptive.get("embedding_dedup_enabled", True)
        ),
        "temporal_dedup_enabled": bool(
            adaptive.get("temporal_dedup_enabled", True)
        ),
        "temporal_dedup_min_gap_s": float(
            adaptive.get("temporal_dedup_min_gap_s", 12)
        ),
        # 0 = collapse caption twins at any distance (historical).
        "embed_dedup_max_gap_s": float(
            adaptive.get("embedding_dedup_max_gap_s", 0)
        ),
        "output_ratio": float(adaptive.get("output_ratio", 1.0)),
        "max_output": int(adaptive.get("max_output", 0)),
        **_quality_ranking_weights(adaptive),
        "gif_fps": int(adaptive.get("gif_fps", 24)),
        "gif_max_width": int(adaptive.get("gif_max_width", 720)),
        # Palette knobs default to FFmpeg's own defaults, so an unmodified
        # snapshot still emits the bare palettegen/paletteuse commands.
        "gif_palette_stats_mode": str(
            adaptive.get("gif_palette_stats_mode", DEFAULT_STATS_MODE)
        ),
        "gif_dither": str(adaptive.get("gif_dither", DEFAULT_DITHER)),
        "gif_diff_mode": str(adaptive.get("gif_diff_mode", DEFAULT_DIFF_MODE)),
        "clear_output_dir": bool(adaptive.get("clear_output_dir", True)),
        "potplayer_pbf_enabled": bool(
            adaptive.get("potplayer_pbf_enabled", True)
        ),
        "preference_memory_enabled": bool(pref_mem.get("enabled", False)),
        "base_score_weight": float(pref_mem.get("base_score_weight", 0.50)),
        "preference_score_weight": float(
            pref_mem.get("preference_score_weight", 0.50)
        ),
        "vlm_temperature": float(adaptive.get("vlm_temperature", 0.65)),
        "vlm_top_p": float(adaptive.get("vlm_top_p", 0.95)),
        "vlm_top_k": int(adaptive.get("vlm_top_k", 60)),
        # None means "send no seed", which keeps default snapshots
        # byte-identical to the pre-seed request body.
        "vlm_seed": _optional_seed(adaptive.get("vlm_seed")),
        # Server-side residency only -- never affects a score, so unlike the
        # other vlm_* keys above it is free to default to something other
        # than "no keep_alive sent" (see Task 7 in the throughput plan).
        "vlm_keep_alive": str(adaptive.get("vlm_keep_alive", "30m")),
        # 1 = current serial behavior. Extraction is pure I/O + CPU decode,
        # so it can run alongside GPU scoring without contention.
        "frame_extract_workers": max(
            1, int(adaptive.get("frame_extract_workers", 1))
        ),
        # 1 = current serial scoring. >1 overlaps VLM HTTP calls inside
        # one stage; Ollama must allow matching NUM_PARALLEL.
        "vlm_score_workers": max(
            1, int(adaptive.get("vlm_score_workers", 1))
        ),
        "score_prompt_mode": normalize_score_prompt_mode(
            adaptive.get("score_prompt_mode", "default")
        ),
        # legacy = current full-schema scoring. two_tier = numeric schema
        # on coarse/refine plus caption backfill on each clip's best_frame.
        "score_schema_mode": normalize_score_schema_mode(
            adaptive.get("score_schema_mode", "legacy")
        ),
        "caption_backfill_max_frames": max(
            0, int(adaptive.get("caption_backfill_max_frames", 150))
        ),
        "vlm_num_predict_score": _optional_int(
            adaptive.get("vlm_num_predict_score")
        ),
        "vlm_num_predict_caption": _optional_int(
            adaptive.get("vlm_num_predict_caption")
        ),
        "boundary_snap_enabled": bool(
            adaptive.get("boundary_snap_enabled", False)
        ),
        "boundary_snap_radius_s": float(
            adaptive.get("boundary_snap_radius_s", 0.6)
        ),
        "score_calibration_enabled": bool(
            adaptive.get("score_calibration_enabled", False)
        ),
        "score_calibration_path": str(
            adaptive.get("score_calibration_path", "") or ""
        ),
        # 0 disables the dark-frame prefilter. Default 25 preserves legacy behavior.
        "min_brightness": float(adaptive.get("min_brightness", 25)),
        # Transition behavior comes only from this frozen config snapshot.
        "transition_guard_enabled": bool(
            adaptive.get("transition_guard_enabled", True)
        ),
        "transition_min_duration_s": float(
            adaptive.get("transition_min_duration_s", 2.0)
        ),
        "transition_boundary_margin_s": float(
            adaptive.get("transition_boundary_margin_s", 0.25)
        ),
        "transition_scan_fps": float(adaptive.get("transition_scan_fps", 8)),
        "transition_scan_width": int(adaptive.get("transition_scan_width", 320)),
        "transition_motion_compensation": bool(
            adaptive.get("transition_motion_compensation", True)
        ),
        "transition_hard_threshold": float(
            adaptive.get("transition_hard_threshold", 0.65)
        ),
        "transition_soft_threshold": float(
            adaptive.get("transition_soft_threshold", 0.40)
        ),
        "transition_soft_run_frames": int(
            adaptive.get("transition_soft_run_frames", 3)
        ),
        "transition_rescore_split_segments": bool(
            adaptive.get("transition_rescore_split_segments", True)
        ),
        "quality_moe": quality_moe.to_dict(),
        "quality_moe_config_hash": quality_moe.config_hash,
    }
    config["action_config_hash"] = computed_action_hash
    if config["max_refine_frames"] < 0:
        raise ValueError("max_refine_frames must be >= 0")
    # Validate the palette values here so a bad snapshot fails at config
    # freeze rather than deep inside an ffmpeg filtergraph.
    _palette_filters_for(config)
    _warn_once_on_indivisible_fps(config["gif_fps"])
    return config


def _optional_seed(value: object) -> int | None:
    """Parse ``adaptive.vlm_seed``; ``None``/absent means "send no seed".

    Booleans and non-integers are rejected rather than coerced, so a typo
    cannot silently disable reproducibility.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"vlm_seed must be an integer or null, got {value!r}"
        )
    return value


def _optional_int(value: object) -> int | None:
    """Parse an optional positive-or-zero integer config value.

    ``None``/absent means "omit this option from the request body".
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer or null, got {value!r}")
    return value


def _extract_direct_snapshot_config(config_data: dict) -> dict:
    """Freeze direct-mode settings from the single config load at job start."""
    return extract_config({
        "adaptive": config_data.get("adaptive", {}) or {},
        "preference_memory": config_data.get("preference_memory", {}) or {},
        "quality_moe": config_data.get("quality_moe", {}) or {},
    })
