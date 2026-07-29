"""Behavioral coverage for CV action motion and boundary candidates."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import math

import pytest

from app.services.action_boundary import (
    ActionBoundaryCandidate,
    ActionBoundaryConfig,
    ActionBoundaryResult,
    ActionMotionAnalysis,
    ActionSegment,
    analyze_action_motion,
)
from tests.action_media_fixtures import (
    scan_video,
    write_edge_action_video,
    write_gentle_zoom,
    write_pan_with_static_subject,
    write_paused_action_video,
    write_slow_upward_pan,
    write_start_move_settle_video,
    write_short_subject_action_during_pan,
    write_subject_action_during_pan,
    write_turn_video,
    write_two_lobe_wave_video,
)


BASE_ACTION_CFG = {
    "analysis_window_s": 30.0,
    "min_duration_s": 2.0,
    "max_duration_s": 20.0,
    "scan_fps": 4.0,
    "boundary_confidence_threshold": 0.65,
}


def test_static_then_move_then_settle_finds_complete_action(tmp_path):
    video = write_start_move_settle_video(tmp_path / "complete.mp4")
    evidence = scan_video(video, 0.0, 8.0)

    result = analyze_action_motion(evidence, 0.0, 8.0, 4.0, BASE_ACTION_CFG)
    best = result.candidates[0]

    assert result.motion_type == "subject_action"
    assert best.start_s == pytest.approx(2.0, abs=0.75)
    assert best.end_s == pytest.approx(6.0, abs=0.75)
    assert best.start_settle > 0.0
    assert best.end_settle > 0.0


def test_slow_global_pan_is_ambient_camera_motion(tmp_path):
    video = write_pan_with_static_subject(tmp_path / "pan.mp4")
    evidence = scan_video(video, 0.0, 8.0)

    result = analyze_action_motion(evidence, 0.0, 8.0, 4.0, BASE_ACTION_CFG)

    assert result.motion_type == "ambient_camera_motion"
    assert result.candidates == ()


def test_subject_action_during_camera_pan_remains_subject_action(tmp_path):
    video = write_subject_action_during_pan(tmp_path / "pan-action.mp4")
    result = analyze_action_motion(scan_video(video, 0.0, 8.0), 0.0, 8.0, 4.0, BASE_ACTION_CFG)

    assert result.motion_type == "subject_action"
    assert result.candidates
    assert result.candidates[0].start_s <= 2.75
    assert result.candidates[0].end_s >= 5.25


def test_short_subject_action_during_camera_pan_remains_subject_action(tmp_path):
    video = write_short_subject_action_during_pan(tmp_path / "short-pan-action.mp4")
    result = analyze_action_motion(scan_video(video, 0.0, 8.0), 0.0, 8.0, 4.0, BASE_ACTION_CFG)

    assert result.motion_type == "subject_action"
    assert result.candidates
    assert len(result.active_runs) == 1
    assert result.active_runs[0][0] == pytest.approx(3.5, abs=0.5)
    assert result.active_runs[0][1] == pytest.approx(4.5, abs=0.5)


@pytest.mark.parametrize(
    ("writer", "filename"),
    ((write_slow_upward_pan, "upward.mp4"), (write_gentle_zoom, "zoom.mp4")),
)
def test_pure_gentle_global_camera_motion_is_ambient(tmp_path, writer, filename):
    result = analyze_action_motion(
        scan_video(writer(tmp_path / filename), 0.0, 8.0), 0.0, 8.0, 4.0, BASE_ACTION_CFG
    )

    assert result.motion_type == "ambient_camera_motion"
    assert result.candidates == ()


@pytest.mark.parametrize(
    ("writer", "filename"), ((write_turn_video, "turn.mp4"), (write_two_lobe_wave_video, "wave.mp4"))
)
def test_complete_turn_and_two_lobe_wave_are_single_actions(tmp_path, writer, filename):
    result = analyze_action_motion(
        scan_video(writer(tmp_path / filename), 0.0, 8.0), 0.0, 8.0, 4.0, BASE_ACTION_CFG
    )

    assert result.motion_type == "subject_action"
    assert len(result.active_runs) == 1
    assert result.candidates[0].start_s <= 2.75
    assert result.candidates[0].end_s >= 5.25


@pytest.mark.parametrize(("edge", "anchor"), (("left", 1.0), ("right", 7.0)))
def test_action_active_at_analysis_edge_has_zero_settle_and_capped_confidence(
    tmp_path, edge, anchor
):
    result = analyze_action_motion(
        scan_video(write_edge_action_video(tmp_path / f"{edge}.mp4", edge=edge), 0.0, 8.0),
        0.0,
        8.0,
        anchor,
        BASE_ACTION_CFG,
    )
    best = result.candidates[0]

    assert (best.start_settle if edge == "left" else best.end_settle) == 0.0
    assert best.confidence <= 0.60


def test_0375_second_pause_is_bridged_into_one_active_run(tmp_path):
    result = analyze_action_motion(
        scan_video(write_paused_action_video(tmp_path / "short-pause.mp4", pause_s=0.375), 0.0, 8.0),
        0.0,
        8.0,
        4.0,
        BASE_ACTION_CFG,
    )

    assert len(result.active_runs) == 1
    assert not any(3.5 <= timestamp <= 4.5 for timestamp in result.stable_valleys)


def test_125_second_pause_becomes_stable_valley(tmp_path):
    result = analyze_action_motion(
        scan_video(write_paused_action_video(tmp_path / "long-pause.mp4", pause_s=1.25), 0.0, 8.0),
        0.0,
        8.0,
        3.0,
        BASE_ACTION_CFG,
    )

    assert len(result.active_runs) == 2
    assert any(3.5 <= timestamp <= 4.75 for timestamp in result.stable_valleys)


def test_config_is_immutable_and_strict_validation_rejects_invalid_values():
    config = ActionBoundaryConfig.from_mapping({})
    with pytest.raises(FrozenInstanceError):
        config.scan_fps = 8.0

    invalid = (
        {"analysis_version": 2},
        {"scan_fps": math.inf},
        {"boundary_confidence_threshold": 1.1},
        {"preferred_min_duration_s": 13.0},
        {"preferred_max_duration_s": 21.0},
        {"max_duration_s": 31.0},
        {"min_duration_s": 1.9},
        {"scan_fps": 0.0},
        {"fallback_mode": "stretch"},
    )
    for values in invalid:
        with pytest.raises(ValueError):
            ActionBoundaryConfig.from_mapping(values, strict=True)


def test_non_strict_config_uses_defaults_for_malformed_optional_values():
    config = ActionBoundaryConfig.from_mapping(
        {
            "enabled": "false",
            "vlm_verify_enabled": "1",
            "scan_fps": "broken",
            "boundary_confidence_threshold": math.nan,
            "fallback_mode": "stretch",
            "loop_adjust_s": "1.25",
        },
    )

    assert config.enabled is False
    assert config.vlm_verify_enabled is True
    assert config.scan_fps == 4.0
    assert config.boundary_confidence_threshold == 0.65
    assert config.fallback_mode == "fixed_window"
    assert config.loop_adjust_s == 1.25


def test_non_strict_config_convergently_repairs_dependent_durations():
    config = ActionBoundaryConfig.from_mapping(
        {
            "preferred_max_duration_s": 100,
            "max_duration_s": 100,
            "analysis_window_s": 10,
        }
    )

    assert config.analysis_window_s == 30.0
    assert config.preferred_min_duration_s == 4.0
    assert config.preferred_max_duration_s == 12.0
    assert config.min_duration_s == 2.0
    assert config.max_duration_s == 20.0


def test_non_strict_duration_repair_preserves_valid_independent_minimum():
    config = ActionBoundaryConfig.from_mapping(
        {
            "preferred_max_duration_s": 100,
            "max_duration_s": 100,
            "analysis_window_s": 10,
            "min_duration_s": 3,
        }
    )

    assert config.min_duration_s == 3.0
    assert config.preferred_min_duration_s <= config.preferred_max_duration_s
    assert config.preferred_max_duration_s <= config.max_duration_s
    assert config.max_duration_s <= config.analysis_window_s


def test_result_types_are_immutable_and_candidates_are_ranked_to_three(tmp_path):
    result = analyze_action_motion(
        scan_video(write_start_move_settle_video(tmp_path / "ranked.mp4"), 0.0, 8.0),
        0.0,
        8.0,
        4.0,
        BASE_ACTION_CFG,
    )

    assert isinstance(result, ActionMotionAnalysis)
    assert isinstance(result.candidates[0], ActionBoundaryCandidate)
    assert 1 <= len(result.candidates) <= 3
    assert [candidate.confidence for candidate in result.candidates] == sorted(
        (candidate.confidence for candidate in result.candidates), reverse=True
    )
    with pytest.raises(FrozenInstanceError):
        result.confidence = 0.0
    with pytest.raises(FrozenInstanceError):
        result.candidates[0].start_s = 0.0


def test_final_action_result_types_are_immutable():
    with pytest.raises(FrozenInstanceError):
        ActionSegment(0.0, 2.0, 1.0, "complete_action", False).start_s = 1.0

    result = ActionBoundaryResult(
        action_boundary_mode="complete_action",
        safe_start_s=0.0,
        safe_end_s=2.0,
        anchor_ts_s=1.0,
        boundary_candidates=(),
        segments=(ActionSegment(0.0, 2.0, 1.0, "complete_action", False),),
        action_start_ts=0.0,
        action_peak_ts=1.0,
        action_end_ts=2.0,
        action_completeness_score=0.8,
        action_boundary_confidence=0.8,
        loop_quality_score=1.0,
        action_split_reason=None,
        action_vlm_verified=False,
        action_fallback_reason=None,
    )
    with pytest.raises(FrozenInstanceError):
        result.action_boundary_mode = "fallback_fixed"
    with pytest.raises(TypeError):
        result.diagnostics["mutated"] = 1
    with pytest.raises(TypeError):
        result.diagnostics |= {"mutated": 1}
    assert "mutated" not in result.diagnostics
