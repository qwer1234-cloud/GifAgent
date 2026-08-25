"""Pure transformation tests for transition-guarded clip candidates."""

from app.services.transition_candidates import build_guarded_clips
from app.services.transition_guard import GuardSegment, TransitionGuardResult


def _f(timestamp: float, worthiness: float) -> dict:
    return {
        "timestamp": timestamp,
        "path": f"frame_{timestamp}.jpg",
        "gif_worthiness": worthiness,
    }


def fake_guard_result_with_segments(*segments: tuple[float, float], anchor: float) -> TransitionGuardResult:
    return TransitionGuardResult(
        transition_action="split",
        segments=tuple(GuardSegment(start, end, "trimmed_at_transition") for start, end in segments),
        boundaries=(),
        hard_cut_count=1,
        soft_transition_count=0,
        motion_type="static_or_local_motion",
        transition_risk=0.82,
        guard_reason="confirmed transition boundaries split the candidate window",
        anchor_ts_s=anchor,
    )


def test_guarded_split_chooses_best_frame_per_segment():
    result = fake_guard_result_with_segments((0.25, 2.5), (2.75, 5.0), anchor=1.0)
    clean = build_guarded_clips(
        clip={"start_ts": 0.0, "end_ts": 5.0, "best_frame_ts": 1.0,
              "frame_count": 3, "gif_worthiness": 0.7},
        guard_result=result,
        scored_frames=[_f(1.0, 0.7), _f(4.0, 0.9)],
        min_duration_s=2.0,
    )

    assert len(clean) == 2
    assert clean[1]["best_frame_ts"] == 4.0
    assert clean[1]["best_frame_path"] == "frame_4.0.jpg"
    assert clean[1]["frame_count"] == 1
    assert clean[1]["transition_action"] == "split"
    assert clean[1]["transition_risk"] == 0.82
    assert clean[1]["motion_type"] == "static_or_local_motion"
    assert clean[1]["guard_reason"] == "confirmed transition boundaries split the candidate window"


def test_segment_without_scored_frame_needs_rescore():
    result = fake_guard_result_with_segments((0.0, 2.0), (3.0, 5.0), anchor=1.0)

    clean = build_guarded_clips(
        clip={"start_ts": 0.0, "end_ts": 5.0, "best_frame_ts": 1.0,
              "best_frame_path": "original.jpg", "frame_count": 3},
        guard_result=result,
        scored_frames=[_f(1.0, 0.7)],
        min_duration_s=2.0,
    )

    assert len(clean) == 2
    assert clean[1]["needs_rescore"] is True
    assert clean[1]["start_ts"] == 3.0
    assert clean[1]["end_ts"] == 5.0


def test_export_minimum_filters_short_guard_segments():
    result = fake_guard_result_with_segments((0.0, 0.75), (1.0, 2.75), anchor=1.5)

    clean = build_guarded_clips(
        clip={"start_ts": 0.0, "end_ts": 2.75, "best_frame_ts": 1.5},
        guard_result=result,
        scored_frames=[_f(1.5, 0.8)],
        min_duration_s=1.5,
    )

    assert [(clip["start_ts"], clip["end_ts"]) for clip in clean] == [(1.0, 2.75)]


def test_drop_result_retains_original_window():
    result = TransitionGuardResult(
        transition_action="drop",
        segments=(),
        boundaries=(),
        hard_cut_count=1,
        soft_transition_count=0,
        motion_type="static_or_local_motion",
        transition_risk=0.95,
        guard_reason="transition margins left no exportable segment",
        original_start_s=0.0,
        original_end_s=5.0,
    )

    clean = build_guarded_clips(
        clip={"start_ts": 0.0, "end_ts": 5.0},
        guard_result=result,
        scored_frames=[_f(1.0, 0.7)],
        min_duration_s=2.0,
    )

    assert len(clean) == 1
    assert clean[0]["start_ts"] == 0.0
    assert clean[0]["end_ts"] == 5.0
    assert clean[0]["transition_action"] == "keep"
    assert clean[0]["best_frame_ts"] == 1.0


def test_drop_result_with_defensive_segment_still_retains_original_window():
    result = TransitionGuardResult(
        transition_action="drop",
        segments=(GuardSegment(0.0, 5.0),),
        boundaries=(),
        hard_cut_count=1,
        soft_transition_count=0,
        motion_type="static_or_local_motion",
        transition_risk=0.95,
        guard_reason="transition margins left no exportable segment",
        original_start_s=1.0,
        original_end_s=4.0,
    )

    clean = build_guarded_clips(
        clip={"start_ts": 0.0, "end_ts": 5.0},
        guard_result=result,
        scored_frames=[_f(2.0, 0.7)],
        min_duration_s=2.0,
    )

    assert [(clip["start_ts"], clip["end_ts"]) for clip in clean] == [(1.0, 4.0)]
    assert clean[0]["transition_action"] == "keep"
