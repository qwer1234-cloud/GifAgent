"""Tests for region-aware scored-frame merge."""

from app.services.clip_merge import merge_scored_frames_into_clips


def _f(ts: float, worth: float) -> dict:
    return {
        "timestamp": ts,
        "path": f"f_{ts}.jpg",
        "gif_worthiness": worth,
        "emotional_core": "desire",
        "caption": f"cap-{ts}",
    }


def test_dense_high_scores_split_by_max_span():
    # Every 5s at 0.55 for 100s would previously become one mega-clip.
    frames = [_f(float(t), 0.55) for t in range(0, 100, 5)]
    clips = merge_scored_frames_into_clips(
        frames,
        merge_gap=15,
        merge_score_threshold=0.45,
        max_merge_span_s=24,
        peak_threshold=0.55,
    )
    assert len(clips) >= 4
    assert all((c["end_ts"] - c["start_ts"]) <= 24.0 + 1e-6 for c in clips)
    assert sum(c["frame_count"] for c in clips) == len(frames)


def test_gap_breaks_merge_chain():
    frames = [_f(0, 0.7), _f(5, 0.7), _f(40, 0.7), _f(45, 0.7)]
    clips = merge_scored_frames_into_clips(
        frames,
        merge_gap=15,
        merge_score_threshold=0.50,
        max_merge_span_s=60,
    )
    assert len(clips) == 2
    assert clips[0]["frame_count"] == 2
    assert clips[1]["frame_count"] == 2


def test_low_score_breaks_chain():
    frames = [_f(0, 0.7), _f(5, 0.7), _f(10, 0.2), _f(15, 0.7)]
    clips = merge_scored_frames_into_clips(
        frames,
        merge_gap=15,
        merge_score_threshold=0.50,
        max_merge_span_s=60,
    )
    assert len(clips) == 3
    assert clips[0]["frame_count"] == 2
    assert clips[1]["frame_count"] == 1
    assert clips[2]["frame_count"] == 1


def test_peak_threshold_demotes_weak_multi_frame():
    frames = [_f(0, 0.46), _f(5, 0.47), _f(10, 0.48)]
    clips = merge_scored_frames_into_clips(
        frames,
        merge_gap=15,
        merge_score_threshold=0.45,
        max_merge_span_s=60,
        peak_threshold=0.55,
    )
    assert len(clips) == 1
    assert clips[0]["frame_count"] == 1
    assert clips[0]["gif_worthiness"] == 0.48


def test_boundary_breaks_high_score_merge():
    clips = merge_scored_frames_into_clips(
        [_f(0, 0.8), _f(5, 0.8), _f(10, 0.8)],
        merge_gap=15,
        merge_score_threshold=0.5,
        max_merge_span_s=24,
        shot_boundaries=[7.0],
    )
    assert [clip["frame_count"] for clip in clips] == [2, 1]


def test_merge_picks_higher_sex_act_as_best_frame():
    frames = [
        {**_f(0, 0.70), "sex_act": 0.0, "caption": "kitchen"},
        {**_f(5, 0.62), "sex_act": 0.90, "caption": "sex"},
    ]
    clips = merge_scored_frames_into_clips(
        frames,
        merge_gap=15,
        merge_score_threshold=0.50,
        max_merge_span_s=24,
    )
    assert len(clips) == 1
    assert clips[0]["caption"] == "sex"
    assert clips[0]["sex_act"] == 0.90
