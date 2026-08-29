"""Property tests for region-aware scored-frame merging (W5 Task 3).

Properties locked against ``merge_scored_frames_into_clips`` (with
``peak_threshold=None`` so clip groups stay contiguous runs of the ordered
input; the demotion path is pinned separately with fixed examples):

- every clip spans at most ``max_merge_span_s`` (when positive);
- adjacent input frames whose gap exceeds ``merge_gap`` never share a clip;
- output clips are ordered by ``start_ts`` and do not overlap.

The existing ``tests/test_clip_merge.py`` is not modified.
"""

from __future__ import annotations

from hypothesis import assume, given, seed, settings, strategies as st

from app.services.clip_merge import merge_scored_frames_into_clips

SEED = 20260828


@st.composite
def scored_frame_lists(draw):
    """Unique, sorted non-negative timestamps with unit-range scores."""
    count = draw(st.integers(1, 40))
    raw = draw(
        st.lists(
            st.floats(0.0, 600.0, allow_nan=False, allow_infinity=False),
            min_size=count,
            max_size=count,
        )
    )
    timestamps = sorted(set(raw))
    scores = draw(
        st.lists(
            st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
            min_size=len(timestamps),
            max_size=len(timestamps),
        )
    )
    return [
        {
            "timestamp": ts,
            "path": f"frame_{i}",
            "gif_worthiness": score,
            "emotional_core": "desire",
            "caption": f"cap-{i}",
        }
        for i, (ts, score) in enumerate(zip(timestamps, scores))
    ]


@given(
    frames=scored_frame_lists(),
    merge_gap=st.floats(
        0.0, 30.0, allow_nan=False, allow_infinity=False
    ),
    max_merge_span_s=st.floats(
        0.5, 60.0, allow_nan=False, allow_infinity=False
    ),
    merge_score_threshold=st.floats(
        0.0, 1.0, allow_nan=False, allow_infinity=False
    ),
)
@settings(deadline=None)
@seed(SEED)
def test_clips_respect_span_gap_and_order(
    frames, merge_gap, max_merge_span_s, merge_score_threshold
):
    """Span cap, gap break, and ordered non-overlap all hold together."""
    clips = merge_scored_frames_into_clips(
        frames,
        merge_gap=merge_gap,
        merge_score_threshold=merge_score_threshold,
        max_merge_span_s=max_merge_span_s,
        peak_threshold=None,
    )

    assert sum(clip["frame_count"] for clip in clips) == len(frames)

    cursor = 0
    for index, clip in enumerate(clips):
        # With demotion off, each clip consumes a contiguous run of the
        # timestamp-sorted frames, so its group is exactly reconstructible.
        group = frames[cursor : cursor + clip["frame_count"]]
        cursor += clip["frame_count"]

        assert clip["start_ts"] == group[0]["timestamp"]
        assert clip["end_ts"] == group[-1]["timestamp"]
        assert clip["end_ts"] - clip["start_ts"] <= max_merge_span_s + 1e-6

        for earlier, later in zip(group, group[1:]):
            gap = later["timestamp"] - earlier["timestamp"]
            assert gap <= merge_gap, (
                f"frames {earlier['timestamp']}->{later['timestamp']} with "
                f"gap {gap} > merge_gap {merge_gap} share clip {index}"
            )

        if index > 0:
            previous = clips[index - 1]
            assert previous["start_ts"] <= clip["start_ts"]
            assert previous["end_ts"] <= clip["start_ts"] + 1e-9


def test_empty_frames_yield_no_clips():
    assert merge_scored_frames_into_clips(
        [], merge_gap=12, merge_score_threshold=0.5, max_merge_span_s=24
    ) == []


def test_non_positive_span_cap_is_pinned_unbounded():
    """``max_merge_span_s <= 0`` disables the cap (dense runs stay one clip)."""
    frames = [
        {"timestamp": 0.0, "gif_worthiness": 0.7, "path": "a"},
        {"timestamp": 5.0, "gif_worthiness": 0.7, "path": "b"},
        {"timestamp": 10.0, "gif_worthiness": 0.7, "path": "c"},
    ]
    clips = merge_scored_frames_into_clips(
        frames,
        merge_gap=15,
        merge_score_threshold=0.5,
        max_merge_span_s=0.0,
    )
    assert len(clips) == 1
    assert clips[0]["frame_count"] == 3
    assert clips[0]["end_ts"] - clips[0]["start_ts"] == 10.0


def test_peak_threshold_demotion_keeps_ordering():
    """Pinned demotion path: output stays ordered and non-overlapping."""
    frames = [
        {"timestamp": 0.0, "gif_worthiness": 0.46, "path": "a"},
        {"timestamp": 5.0, "gif_worthiness": 0.47, "path": "b"},
        {"timestamp": 10.0, "gif_worthiness": 0.48, "path": "c"},
        # Below the merge threshold, so the weak run flushes before it.
        {"timestamp": 15.0, "gif_worthiness": 0.10, "path": "d"},
    ]
    clips = merge_scored_frames_into_clips(
        frames,
        merge_gap=15,
        merge_score_threshold=0.40,
        max_merge_span_s=24,
        peak_threshold=0.5,
    )

    # The first three frames merge into one weak run (best 0.48 < 0.5) and
    # get demoted to its best frame (ts 10.0); the last frame stays alone.
    assert [clip["frame_count"] for clip in clips] == [1, 1]
    assert clips[0]["best_frame_ts"] == 10.0
    assert clips[0]["start_ts"] == 10.0 and clips[0]["end_ts"] == 10.0
    assert clips[1]["start_ts"] == 15.0 and clips[1]["end_ts"] == 15.0
    assert clips[0]["start_ts"] <= clips[1]["start_ts"]
    assert clips[0]["end_ts"] <= clips[1]["start_ts"] + 1e-9
