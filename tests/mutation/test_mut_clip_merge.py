"""W4 kill tests for clip_merge mutation survivors.

Each test targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/clip_merge.json``. All tests
pass against the ORIGINAL module (verified by running the plain test suite)
and additionally kill the previously surviving mutants:

- NumberReplacer / ReplaceOrWithAnd on the ``_clip_from_group`` max-key
  tiebreaker (line 19): exact bitwise adult-score ties decide via the
  ``gif_worthiness`` tiebreaker; mutants change the tiebreaker value.
- Index arithmetic mutants on ``group[0]``/``group[-1]``/``ordered[0]``/
  ``current[-1]``: start/end timestamp and gap-window assertions.
- Default-value NumberReplacer mutants (``sex_act`` 0.0, ``gif_worthiness``
  0.0, ``max_merge_span_s`` 24.0): omitted-key / omitted-argument cases.
- Comparison-operator mutants on shot-boundary, merge-gap, threshold,
  peak-demotion and span logic: boundary-equality scenarios.
- ``max(0.0, ...)`` / ``max_span <= 0`` mutants: negative/1.0 span inputs.

Equivalent mutants (annotation unions, ``*`` -> ``/`` keyword-marker,
``frame_count > 1`` variants unreachable for len>=1, ``max_span == 0`` vs
``<= 0`` on a ``max(0.0, ...)`` result) are registered in
``docs/reports/2026-08-token-sprint/w4-surviving.md`` and cannot be killed.
"""

from __future__ import annotations

from app.services.clip_merge import merge_scored_frames_into_clips


def _f(ts: float, worth: float, **extra: object) -> dict:
    frame = {
        "timestamp": ts,
        "path": f"f_{ts}",
        "gif_worthiness": worth,
        "caption": f"cap-{ts}",
    }
    frame.update(extra)
    return frame


# ── tiebreaker on the best-frame max key (line 19) ─────────────────────────


def test_best_frame_tiebreak_prefers_higher_worthiness():
    # adult_score == 0.40*0.25 + 0.60*0.0 == 0.40*0.0625 + 0.60*0.125 exactly
    # (bitwise, verified). Original tiebreaker (higher gif_worthiness) picks
    # the second frame; ``or 0.0 -> and 0.0`` collapses both keys to 0.0 and
    # would pick the first frame.
    frames = [
        _f(0.0, 0.0625, sex_act=0.125, caption="low-worth"),
        _f(1.0, 0.25, sex_act=0.0, caption="high-worth"),
    ]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=15, merge_score_threshold=-1.0, max_merge_span_s=60
    )
    assert len(clips) == 1
    assert clips[0]["best_frame"]["caption"] == "high-worth"


def test_best_frame_tiebreak_with_missing_worthiness_and_unit_default():
    # adult 0.60*0.3125 == 0.40*0.1875 + 0.60*0.1875 exactly. First frame has
    # NO gif_worthiness key: original key2 is 0.0 so the second frame wins;
    # ``0.0 -> 1.0`` in ``or 0.0`` makes the missing key worth 1.0 and would
    # flip the pick to the first frame.
    frames = [
        _f(0.0, 0.0, sex_act=0.3125, caption="no-worth"),
        _f(1.0, 0.1875, sex_act=0.1875, caption="worth"),
    ]
    del frames[0]["gif_worthiness"]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=15, merge_score_threshold=-1.0, max_merge_span_s=60
    )
    assert clips[0]["best_frame"]["caption"] == "worth"


def test_best_frame_tiebreak_with_negative_worthiness_and_minus_default():
    # Both frames normalize to adult 0.60*0.5 (0.0 and negative raw worthiness
    # both normalize to 0). Original key2 picks the first frame (0.0 > -0.5);
    # ``0.0 -> -1.0`` in ``or 0.0`` gives the falsy frame -1.0 and flips the
    # pick to the negative-worthiness frame.
    frames = [
        _f(0.0, 0.0, sex_act=0.5, caption="zero-worth"),
        _f(1.0, -0.5, sex_act=0.5, caption="negative"),
    ]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=15, merge_score_threshold=-1.0, max_merge_span_s=60
    )
    assert clips[0]["best_frame"]["caption"] == "zero-worth"
    assert clips[0]["gif_worthiness"] == 0.0


# ── group index arithmetic (start_ts / end_ts) ─────────────────────────────


def test_clip_spans_first_to_last_frame_timestamp():
    frames = [_f(ts, 0.7) for ts in (0.0, 5.0, 10.0)]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=15, merge_score_threshold=0.5, max_merge_span_s=60
    )
    assert len(clips) == 1
    assert clips[0]["start_ts"] == 0.0
    assert clips[0]["end_ts"] == 10.0


def test_merge_window_uses_last_frame_as_gap_reference():
    # 12 - 10 would still fit, but 12 - 0 must break the chain.
    frames = [_f(ts, 0.7) for ts in (0.0, 10.0, 12.0, 20.0)]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=11, merge_score_threshold=0.5, max_merge_span_s=60
    )
    assert len(clips) == 1
    assert [c["frame_count"] for c in clips] == [4]


def test_first_clip_starts_at_first_ordered_frame():
    frames = [_f(ts, 0.7) for ts in (0.0, 5.0, 40.0, 45.0)]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=15, merge_score_threshold=0.5, max_merge_span_s=60
    )
    assert clips[0]["start_ts"] == 0.0
    assert [c["frame_count"] for c in clips] == [2, 2]


# ── omitted-key defaults ────────────────────────────────────────────────────


def test_missing_sex_act_defaults_to_zero():
    frames = [_f(0.0, 0.7), _f(5.0, 0.7)]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=15, merge_score_threshold=0.5, max_merge_span_s=60
    )
    assert clips[0]["sex_act"] == 0.0


def test_missing_frame_worthiness_default_at_zero_threshold():
    # With threshold 0.0 the frame lacking the key must merge (default 0.0
    # passes >= 0.0); a -1.0 default would either refuse the merge for the
    # incoming frame (crashing the clip build) or, once the key-less frame is
    # the group tail, refuse to extend the run for the next frame.
    good = _f(0.0, 0.9, sex_act=0.9)
    keyless = _f(5.0, 0.0, sex_act=0.0)
    del keyless["gif_worthiness"]
    after = _f(10.0, 0.9, sex_act=0.9)
    clips = merge_scored_frames_into_clips(
        [good, keyless, after],
        merge_gap=15,
        merge_score_threshold=0.0,
        max_merge_span_s=60,
    )
    assert len(clips) == 1
    assert clips[0]["frame_count"] == 3
    assert clips[0]["gif_worthiness"] == 0.9


# ── default max_merge_span_s = 24.0 ────────────────────────────────────────


def test_default_max_merge_span_is_exactly_24_seconds():
    # 0 + 24 == 24.0 must merge with the default; a 23.0 default would split.
    clips = merge_scored_frames_into_clips(
        [_f(0.0, 0.55), _f(24.0, 0.55)],
        merge_gap=30,
        merge_score_threshold=0.45,
    )
    assert len(clips) == 1

    # Beyond 24.0 the run must flush: a 25.0 default would keep 25 in clip 0.
    frames = [_f(float(ts), 0.55) for ts in (0, 5, 10, 15, 20, 25)]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=15, merge_score_threshold=0.45
    )
    assert clips[0]["end_ts"] == 20.0
    assert clips[1]["start_ts"] == 25.0


# ── max_span clamping and span window comparisons ──────────────────────────


def test_negative_max_merge_span_behaves_like_zero_cap():
    # max(0.0, -5) == 0.0 short-circuits ``max_span <= 0`` -> always within
    # span; a max(1.0, -5) or ``max_span < 0`` / ``<= -1`` mutant would split.
    frames = [_f(0.0, 0.55), _f(5.0, 0.55)]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=15, merge_score_threshold=0.45, max_merge_span_s=-5.0
    )
    assert len(clips) == 1


def test_span_window_of_one_second_still_splits():
    # max_span == 1.0: ``max_span <= 1`` mutant would treat it as uncapped.
    frames = [_f(0.0, 0.55), _f(5.0, 0.55)]
    clips = merge_scored_frames_into_clips(
        frames, merge_gap=15, merge_score_threshold=0.45, max_merge_span_s=1.0
    )
    assert len(clips) == 2


def test_span_boundary_equality_merges():
    # span_if_added == max_span must still merge (``<`` mutant would split).
    clips = merge_scored_frames_into_clips(
        [_f(0.0, 0.55), _f(24.0, 0.55)],
        merge_gap=30,
        merge_score_threshold=0.45,
        max_merge_span_s=24.0,
    )
    assert len(clips) == 1


# ── shot boundary comparisons ──────────────────────────────────────────────


def test_boundary_equal_to_previous_timestamp_does_not_split():
    # previous < boundary is False when boundary == previous; ``<=`` and
    # ``is not`` mutants would split the run.
    clips = merge_scored_frames_into_clips(
        [_f(0.0, 0.8), _f(5.0, 0.8)],
        merge_gap=15,
        merge_score_threshold=0.5,
        max_merge_span_s=60,
        shot_boundaries=[0.0],
    )
    assert len(clips) == 1


def test_boundary_below_run_start_does_not_split():
    # boundary < previous_timestamp: ``!=`` mutant would (wrongly) split.
    clips = merge_scored_frames_into_clips(
        [_f(5.0, 0.8), _f(10.0, 0.8)],
        merge_gap=15,
        merge_score_threshold=0.5,
        max_merge_span_s=60,
        shot_boundaries=[3.0],
    )
    assert len(clips) == 1


def test_boundary_equal_to_next_timestamp_splits():
    # boundary <= timestamp is True when boundary == next frame; ``<`` mutant
    # would keep the run merged.
    clips = merge_scored_frames_into_clips(
        [_f(0.0, 0.8), _f(5.0, 0.8)],
        merge_gap=15,
        merge_score_threshold=0.5,
        max_merge_span_s=60,
        shot_boundaries=[5.0],
    )
    assert [c["frame_count"] for c in clips] == [1, 1]


# ── peak demotion comparison ───────────────────────────────────────────────


def test_strong_multi_frame_group_is_not_demoted():
    # worthiness 0.9 is NOT below peak 0.55: ``!=`` / ``is not`` mutants would
    # demote the two-frame group to a single frame.
    frames = [_f(0.0, 0.9), _f(5.0, 0.9)]
    clips = merge_scored_frames_into_clips(
        frames,
        merge_gap=15,
        merge_score_threshold=0.5,
        max_merge_span_s=60,
        peak_threshold=0.55,
    )
    assert len(clips) == 1
    assert clips[0]["frame_count"] == 2


# ── threshold boundary equality ────────────────────────────────────────────


def test_worthiness_equal_to_threshold_merges():
    # >= (not >) decides the merge at exactly the threshold, for both the
    # incoming and the previous frame.
    clips = merge_scored_frames_into_clips(
        [_f(0.0, 0.5), _f(5.0, 0.5)],
        merge_gap=15,
        merge_score_threshold=0.5,
        max_merge_span_s=60,
    )
    assert len(clips) == 1


def test_gap_equal_to_merge_gap_merges():
    # gap == merge_gap must merge (``<`` mutant would split).
    clips = merge_scored_frames_into_clips(
        [_f(0.0, 0.7), _f(15.0, 0.7)],
        merge_gap=15,
        merge_score_threshold=0.5,
        max_merge_span_s=60,
    )
    assert len(clips) == 1
