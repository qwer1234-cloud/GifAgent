"""Property freeze for the adaptive pipeline config facade.

W5 Task 1 properties (token sprint):

- ``extract_config({})`` and ``extract_config({"adaptive": {}})`` agree with
  each other and with the frozen default keys (at least ``min_duration``,
  ``max_duration``, ``sex_act_threshold`` — matching
  ``tests/test_adaptive_config.py``).
- For random ``adaptive`` subsets that only use *existing* keys with legal
  types, ``extract_config`` is deterministic: two calls compare equal.
- Keys omitted from the snapshot fall back to the empty-dict baseline (never
  ``None`` drift).

The facade import is mandatory: no ``app.pipeline.*`` internal imports here.
"""

from __future__ import annotations

from hypothesis import given, seed, settings, strategies as st

from scripts.test_video_adaptive import extract_config

SEED = 20260828

# Keys whose values carry no cross-field validation, drawn with legal types
# only (the plan forbids inventing new key semantics).  Action keys
# (``min_duration``/``max_duration``/``action_*``) and the
# ``quality_ranking_*`` weights are deliberately excluded: their relationship
# constraints (preferred <= configured, weights summing to 1) would turn a
# property test into a re-run of the validator's own unit tests.
_INT_KEYS_1_TO_200 = (
    "sample_interval",
    "refine_interval",
    "refine_radius",
    "merge_gap",
    "gif_fps",
    "gif_max_width",
    "vlm_top_k",
    "transition_soft_run_frames",
    "transition_scan_width",
)
_INT_KEYS_0_TO_500 = (
    "max_refine_frames",
    "caption_backfill_max_frames",
)
_INT_KEYS_0_TO_4096 = (
    "vlm_num_predict_score",
    "vlm_num_predict_caption",
    "vlm_seed",
)
_INT_KEYS_1_TO_8 = (
    "frame_extract_workers",
    "vlm_score_workers",
)
_FLOAT_KEYS = (
    "refine_threshold",
    "worthiness_threshold",
    "merge_score_threshold",
    "max_merge_span_s",
    "merge_peak_threshold",
    "output_ratio",
    "vlm_temperature",
    "vlm_top_p",
    "min_brightness",
    "sex_act_threshold",
    "temporal_dedup_min_gap_s",
    "embed_dedup_max_gap_s",
    "boundary_snap_radius_s",
    "transition_min_duration_s",
    "transition_boundary_margin_s",
    "transition_scan_fps",
    "transition_hard_threshold",
    "transition_soft_threshold",
)
_BOOL_KEYS = (
    "embed_dedup_enabled",
    "temporal_dedup_enabled",
    "clear_output_dir",
    "potplayer_pbf_enabled",
    "boundary_snap_enabled",
    "score_calibration_enabled",
    "transition_guard_enabled",
    "transition_motion_compensation",
    "transition_rescore_split_segments",
)
_STRATEGY_KEYS = {
    # Whitelisted values only (see normalize_score_prompt_mode,
    # normalize_score_schema_mode and build_palette_filters).
    "score_prompt_mode": st.sampled_from(
        ["default", "adult", "optimized", "nsfw"]
    ),
    "score_schema_mode": st.sampled_from(["legacy", "two_tier", "full", "score"]),
    "gif_palette_stats_mode": st.sampled_from(["full", "diff", "single"]),
    "gif_dither": st.sampled_from(
        ["none", "bayer", "floyd_steinberg", "sierra2_4a"]
    ),
    "gif_diff_mode": st.sampled_from(["none", "rectangle"]),
    "vlm_keep_alive": st.sampled_from(["30m", "0m", "1h"]),
}

_ADAPTIVE_SNAPSHOT = st.fixed_dictionaries(
    {},
    optional={
        **{key: st.integers(1, 200) for key in _INT_KEYS_1_TO_200},
        **{key: st.integers(0, 500) for key in _INT_KEYS_0_TO_500},
        **{key: st.integers(0, 4096) for key in _INT_KEYS_0_TO_4096},
        **{key: st.integers(1, 8) for key in _INT_KEYS_1_TO_8},
        **{
            key: st.floats(
                0.0, 100.0, allow_nan=False, allow_infinity=False
            )
            for key in _FLOAT_KEYS
        },
        **{key: st.booleans() for key in _BOOL_KEYS},
        **_STRATEGY_KEYS,
    },
)

# Frozen flat-config defaults, cross-checked against extract_config's
# implementation and the assertions in tests/test_adaptive_config.py.
FROZEN_DEFAULTS = {
    "min_duration": 2.0,
    "max_duration": 20.0,
    "single_frame_max_duration_s": 20.0,
    "sex_act_threshold": 0.0,
    "sample_interval": 10,
    "refine_interval": 10,
    "refine_radius": 20,
    "refine_threshold": 0.5,
    "max_refine_frames": 120,
    "worthiness_threshold": 0.2,
    "merge_gap": 12,
    "merge_score_threshold": 0.55,
    "max_merge_span_s": 24.0,
    "merge_peak_threshold": 0.55,
    "embed_sim_threshold": 0.94,
    "embed_dedup_enabled": True,
    "temporal_dedup_enabled": True,
    "temporal_dedup_min_gap_s": 12.0,
    "embed_dedup_max_gap_s": 0.0,
    "output_ratio": 1.0,
    "max_output": 0,
    "gif_fps": 24,
    "gif_max_width": 720,
    "gif_palette_stats_mode": "full",
    "gif_dither": "sierra2_4a",
    "gif_diff_mode": "none",
    "clear_output_dir": True,
    "potplayer_pbf_enabled": True,
    "preference_memory_enabled": False,
    "base_score_weight": 0.5,
    "preference_score_weight": 0.5,
    "vlm_temperature": 0.65,
    "vlm_top_p": 0.95,
    "vlm_top_k": 60,
    "vlm_seed": None,
    "vlm_keep_alive": "30m",
    "frame_extract_workers": 1,
    "vlm_score_workers": 1,
    "score_prompt_mode": "default",
    "score_schema_mode": "legacy",
    "caption_backfill_max_frames": 150,
    "vlm_num_predict_score": None,
    "vlm_num_predict_caption": None,
    "boundary_snap_enabled": False,
    "boundary_snap_radius_s": 0.6,
    "score_calibration_enabled": False,
    "score_calibration_path": "",
    "min_brightness": 25.0,
    "transition_guard_enabled": True,
    "transition_min_duration_s": 2.0,
    "transition_boundary_margin_s": 0.25,
    "transition_scan_fps": 8.0,
    "transition_scan_width": 320,
    "transition_motion_compensation": True,
    "transition_hard_threshold": 0.65,
    "transition_soft_threshold": 0.40,
    "transition_soft_run_frames": 3,
    "transition_rescore_split_segments": True,
    "action_guard_enabled": True,
    "action_vlm_verify_enabled": True,
    "action_analysis_version": 1,
    "action_analysis_window_s": 30.0,
    "action_preferred_min_duration_s": 4.0,
    "action_preferred_max_duration_s": 12.0,
    "action_scan_fps": 4.0,
    "action_boundary_confidence_threshold": 0.65,
    "action_loop_adjust_s": 0.75,
    "action_vlm_min_worthiness": 0.60,
    "action_fallback_mode": "fixed_window",
}


def test_empty_config_variants_are_frozen_and_identical():
    """``{}`` and ``{"adaptive": {}}`` freeze to the same default config."""
    cfg_empty = extract_config({})
    cfg_adaptive_empty = extract_config({"adaptive": {}})

    assert cfg_empty == cfg_adaptive_empty
    for key, expected in FROZEN_DEFAULTS.items():
        assert cfg_empty[key] == expected, key
    assert len(cfg_empty["quality_moe_config_hash"]) == 64
    assert cfg_empty["quality_moe"]["report_only"] is True


@given(snapshot=_ADAPTIVE_SNAPSHOT)
@settings(deadline=None)
@seed(SEED)
def test_extract_config_is_deterministic_on_random_adaptive_subsets(snapshot):
    """Two calls on the same legal snapshot produce identical flat configs."""
    cfg_one = extract_config({"adaptive": dict(snapshot)})
    cfg_two = extract_config({"adaptive": dict(snapshot)})

    assert cfg_one == cfg_two


@given(snapshot=_ADAPTIVE_SNAPSHOT)
@settings(deadline=None)
@seed(SEED)
def test_omitted_keys_fall_back_to_empty_dict_baseline(snapshot):
    """Every key omitted from the snapshot equals the empty-dict baseline.

    ``merge_peak_threshold`` is excluded: it carries a documented two-level
    fallback (itself -> ``refine_threshold`` -> 0.55), pinned separately in
    ``test_merge_peak_threshold_falls_back_to_refine_threshold``.
    """
    baseline = extract_config({})
    cfg = extract_config({"adaptive": dict(snapshot)})

    for key, expected in baseline.items():
        if key in snapshot or key == "merge_peak_threshold":
            continue
        assert cfg[key] == expected, key
        if expected is not None:
            assert cfg[key] is not None, f"None drift on omitted key {key}"


@given(snapshot=_ADAPTIVE_SNAPSHOT)
@settings(deadline=None)
@seed(SEED)
def test_merge_peak_threshold_falls_back_to_refine_threshold(snapshot):
    """Pin the documented two-level fallback for ``merge_peak_threshold``."""
    cfg = extract_config({"adaptive": dict(snapshot)})

    if "merge_peak_threshold" in snapshot:
        assert cfg["merge_peak_threshold"] == float(
            snapshot["merge_peak_threshold"]
        )
    elif "refine_threshold" in snapshot:
        assert cfg["merge_peak_threshold"] == float(
            snapshot["refine_threshold"]
        )
    else:
        assert cfg["merge_peak_threshold"] == 0.55
