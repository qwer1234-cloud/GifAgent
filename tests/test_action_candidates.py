"""Final action-duration policy and pure action candidate fan-out."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import numpy as np
import pytest

from app.services.action_boundary import (
    ActionBoundaryCandidate,
    ActionBoundaryConfig,
    ActionMotionAnalysis,
    finalize_action_analysis,
)
from app.services.action_candidates import build_action_clips
from app.services.temporal_evidence import (
    TemporalEvidence,
    TemporalFrame,
    TemporalPairEvidence,
)


ACTION_CFG = ActionBoundaryConfig()


def make_flat_evidence(start_s: float, end_s: float) -> TemporalEvidence:
    fps = 4.0
    indexes = range(round(start_s * fps), round(end_s * fps) + 1)
    gray = np.full((8, 8), 96, dtype=np.uint8)
    hsv = np.zeros((8, 8, 3), dtype=np.uint8)
    hsv[..., 2] = 96
    frames = tuple(
        TemporalFrame(index, index / fps, gray.copy(), hsv.copy()) for index in indexes
    )
    pairs = tuple(
        TemporalPairEvidence(
            frame.timestamp_s,
            previous.gray,
            frame.gray,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            1.0,
            np.zeros_like(gray),
        )
        for previous, frame in zip(frames, frames[1:])
    )
    return TemporalEvidence(start_s, end_s, fps, 8, frames, pairs)


def _candidate(start_s: float, peak_s: float, end_s: float) -> ActionBoundaryCandidate:
    return ActionBoundaryCandidate(
        start_s=start_s,
        peak_s=peak_s,
        end_s=end_s,
        confidence=0.9,
        start_settle=1.0,
        end_settle=1.0,
        peak_inclusion=1.0,
        boundary_quiet=1.0,
    )


def _analysis(
    candidate: ActionBoundaryCandidate,
    *,
    valleys: tuple[float, ...] = (),
    curve: tuple[tuple[float, float], ...] | None = None,
) -> ActionMotionAnalysis:
    return ActionMotionAnalysis(
        motion_type="subject_action",
        candidates=(candidate,),
        residual_curve=curve
        or ((candidate.start_s, 0.0), (candidate.peak_s, 0.2), (candidate.end_s, 0.0)),
        active_runs=((candidate.start_s, candidate.end_s),),
        stable_valleys=valleys,
        confidence=0.9,
    )


def _finalize(
    candidate: ActionBoundaryCandidate,
    safe_start_s: float,
    safe_end_s: float,
    *,
    valleys: tuple[float, ...] = (),
    curve: tuple[tuple[float, float], ...] | None = None,
):
    return finalize_action_analysis(
        _analysis(candidate, valleys=valleys, curve=curve),
        make_flat_evidence(safe_start_s, safe_end_s),
        safe_start_s,
        safe_end_s,
        candidate.peak_s,
        0,
        ACTION_CFG,
    )


def test_complete_three_second_action_is_not_padded_past_safe_context():
    result = _finalize(_candidate(2.0, 3.0, 5.0), 0.0, 8.0)

    segment = result.segments[0]
    assert 2.0 <= segment.end_s - segment.start_s < 4.0
    assert segment.start_s <= 2.0
    assert segment.end_s >= 5.0


@pytest.mark.parametrize(("start_s", "end_s"), ((2.0, 6.0), (2.0, 14.0), (2.0, 22.0)))
def test_complete_actions_from_four_through_twenty_seconds_stay_whole(start_s, end_s):
    result = _finalize(_candidate(start_s, (start_s + end_s) / 2.0, end_s), 0.0, 25.0)

    assert len(result.segments) == 1
    assert result.action_split_reason is None
    assert result.segments[0].start_s <= start_s
    assert result.segments[0].end_s >= end_s


def test_safe_padding_is_exactly_point_four_before_and_point_six_after():
    result = _finalize(_candidate(2.0, 4.0, 6.0), 0.0, 8.0)

    assert result.segments[0].start_s == pytest.approx(1.6)
    assert result.segments[0].end_s == pytest.approx(6.6)


def test_twenty_five_second_action_splits_at_stable_valley():
    candidate = _candidate(0.0, 8.0, 25.0)
    result = _finalize(
        candidate,
        0.0,
        30.0,
        valleys=(13.0,),
        curve=((0.0, 0.1), (8.0, 0.3), (13.0, 0.0), (20.0, 0.3), (25.0, 0.0)),
    )

    assert len(result.segments) == 2
    assert all(segment.end_s - segment.start_s <= 20.0 for segment in result.segments)
    assert result.segments[-1].end_s == pytest.approx(25.6)
    assert result.action_split_reason == "stable_motion_valley"


def test_recursive_split_prefers_balanced_stable_valleys():
    result = _finalize(
        _candidate(0.0, 9.0, 45.0),
        0.0,
        48.0,
        valleys=(14.0, 16.0, 30.0),
        curve=((0.0, 0.1), (9.0, 0.4), (14.0, 0.0), (16.0, 0.0), (30.0, 0.0), (45.0, 0.0)),
    )

    assert [(segment.start_s, segment.end_s) for segment in result.segments] == pytest.approx(
        [(0.0, 16.0), (16.0, 30.0), (30.0, 45.6)]
    )
    assert all(2.0 <= segment.end_s - segment.start_s <= 20.0 for segment in result.segments)


def test_equally_balanced_stable_valleys_choose_earlier_timestamp():
    result = _finalize(
        _candidate(0.0, 9.0, 30.0),
        0.0,
        30.0,
        valleys=(14.0, 16.0),
        curve=((0.0, 0.1), (9.0, 0.4), (14.0, 0.0), (16.0, 0.0), (30.0, 0.0)),
    )

    assert result.segments[0].end_s == 14.0


def test_long_action_without_reliable_split_uses_fixed_fallback():
    result = _finalize(_candidate(0.0, 8.0, 25.0), 0.0, 30.0)

    assert len(result.segments) == 1
    assert result.action_boundary_mode == "fallback_fixed"
    assert result.action_fallback_reason == "long_action_split_fallback"
    assert result.action_completeness_score is None
    assert result.segments[0].end_s - result.segments[0].start_s == pytest.approx(20.0)
    assert (result.segments[0].start_s, result.segments[0].end_s) == pytest.approx((0.0, 20.0))


def test_loop_adjustment_never_moves_inside_detected_action_core():
    result = _finalize(_candidate(2.0, 4.0, 6.0), 0.0, 8.0)
    segment = result.segments[0]

    assert segment.start_s <= result.action_start_ts
    assert segment.end_s >= result.action_end_ts
    assert result.loop_quality_score == pytest.approx(1.0)


def test_loop_adjustment_cannot_shrink_a_short_core_below_two_seconds():
    candidate = _candidate(1.5, 1.75, 2.0)
    evidence = make_flat_evidence(0.0, 4.0)
    for frame in evidence.frames:
        if frame.timestamp_s in (1.5, 2.0):
            frame.gray.fill(0)
            frame.hsv.fill(0)
        elif frame.timestamp_s < 1.5:
            frame.gray.fill(48)
            frame.hsv[..., 2].fill(48)
        else:
            frame.gray.fill(220)
            frame.hsv[..., 2].fill(220)

    result = finalize_action_analysis(
        _analysis(candidate),
        evidence,
        0.0,
        4.0,
        candidate.peak_s,
        0,
        ACTION_CFG,
    )

    assert result.segments[0].end_s - result.segments[0].start_s >= 2.0
    assert result.segments[0].start_s <= candidate.start_s
    assert result.segments[0].end_s >= candidate.end_s


def test_completeness_formula_and_all_result_fields_are_finite_json():
    candidate = ActionBoundaryCandidate(2.0, 4.0, 6.0, 0.75, 0.8, 0.9, 1.0, 0.7)
    result = _finalize(candidate, 0.0, 8.0)

    assert result.action_completeness_score == pytest.approx(
        0.25 * 0.8 + 0.30 * 0.9 + 0.20 * 1.0 + 0.15 * 0.7 + 0.10 * 0.5
    )
    payload = result.to_dict()
    json.dumps(payload, allow_nan=False)
    with pytest.raises(TypeError):
        result.diagnostics["selected_candidate_index"] = 99
    with pytest.raises(FrozenInstanceError):
        result.safe_start_s = 1.0


def test_finalizer_rejects_non_finite_selected_index_before_serialization():
    candidate = _candidate(2.0, 4.0, 6.0)

    with pytest.raises(ValueError):
        finalize_action_analysis(
            _analysis(candidate),
            make_flat_evidence(0.0, 8.0),
            0.0,
            8.0,
            4.0,
            float("nan"),
            ACTION_CFG,
        )


def test_each_fan_out_child_chooses_highest_scored_in_range_frame():
    result = _finalize(
        _candidate(0.0, 8.0, 25.0),
        0.0,
        30.0,
        valleys=(13.0,),
        curve=((0.0, 0.1), (8.0, 0.3), (13.0, 0.0), (20.0, 0.3), (25.0, 0.0)),
    )
    scored = [
        {"timestamp": 3.0, "path": "a.jpg", "gif_worthiness": 0.5},
        {"timestamp": 8.0, "path": "b.jpg", "gif_worthiness": 0.9},
        {"timestamp": 16.0, "path": "c.jpg", "gif_worthiness": 0.7},
        {"timestamp": 20.0, "path": "d.jpg", "gif_worthiness": 0.95},
    ]
    clips = build_action_clips(
        {
            "start_ts": 0.0,
            "end_ts": 30.0,
            "transition_action": "split",
            "transition_risk": 0.82,
            "guard_reason": "safe transition bounds",
        },
        result,
        scored,
        2.0,
    )

    assert [clip["best_frame_ts"] for clip in clips] == [8.0, 20.0]
    assert all(clip["guarded_export_window"] is True for clip in clips)
    assert all(clip["needs_rescore"] is False for clip in clips)
    assert clips[0]["transition_action"] == "split"
    assert clips[0]["action_split_reason"] == "stable_motion_valley"
    assert clips[0]["diagnostics"]["selected_candidate_index"] == 0
    json.dumps(clips, allow_nan=False)


def test_fan_out_marks_only_child_without_scored_frame_for_rescore():
    result = _finalize(
        _candidate(0.0, 8.0, 25.0),
        0.0,
        30.0,
        valleys=(13.0,),
        curve=((0.0, 0.1), (8.0, 0.3), (13.0, 0.0), (20.0, 0.3), (25.0, 0.0)),
    )
    clips = build_action_clips(
        {"start_ts": 0.0, "end_ts": 30.0},
        result,
        [{"timestamp": 8.0, "path": "best.jpg", "gif_worthiness": 0.9}],
        2.0,
    )

    assert [clip["needs_rescore"] for clip in clips] == [False, True]
    assert clips[1]["best_frame"] is None
    assert clips[1]["best_frame_ts"] is None
