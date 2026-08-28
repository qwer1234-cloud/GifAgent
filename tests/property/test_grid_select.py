"""Property tests for time-spread 9-grid selection (W5 Task 5).

Locked against ``app.services.grid_select.select_grid_frames``:

- with path-carrying input and non-constant timestamps, the selection never
  exceeds ``count`` (the plan replaces the over-strict two-end coverage
  assertion with this cap; buckets select one candidate each before pHash
  dedup, so a strict bucket-coverage bound does not exist);
- with an integer pHash and a non-negative threshold, a later accepted frame
  always differs from every earlier accepted frame by more than the
  threshold — so collided frames never double-stay (checked over the whole
  accepted prefix, since ``accept`` compares ``digest - existing``);
- pathless input yields an empty selection.

A fixed example additionally pins the all-identical-pHash collapse and the
all-equal-timestamp behavior.
"""

from __future__ import annotations

from hypothesis import assume, given, seed, settings, strategies as st

from app.services.grid_select import select_grid_frames

SEED = 20260828


@st.composite
def framed_frames(draw):
    """Frames with unique sorted timestamps, scores, paths, and pHashes."""
    count = draw(st.integers(1, 30))
    raw = draw(
        st.lists(
            st.floats(0.0, 1200.0, allow_nan=False, allow_infinity=False),
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
    phashes = draw(
        st.lists(
            st.integers(0, 100),
            min_size=len(timestamps),
            max_size=len(timestamps),
        )
    )
    return [
        {
            "timestamp": ts,
            "gif_worthiness": score,
            "path": f"frame_{i}",
            "phash": digest,
        }
        for i, (ts, score, digest) in enumerate(
            zip(timestamps, scores, phashes)
        )
    ]


@given(frames=framed_frames(), count=st.integers(1, 12))
@settings(deadline=None)
@seed(SEED)
def test_selection_never_exceeds_count(frames, count):
    """The dedup'd selection is capped at ``count`` and subset of the input."""
    assume(len({frame["timestamp"] for frame in frames}) > 1)

    selected = select_grid_frames(frames, count=count)

    assert len(selected) <= count
    assert all(frame.get("path") for frame in selected)
    assert all(frame in frames for frame in selected)


@given(frames=framed_frames(), threshold=st.integers(0, 20))
@settings(deadline=None)
@seed(SEED)
def test_phash_collisions_do_not_double_stay(frames, threshold):
    """Accepted integer pHashes strictly out-space the threshold.

    ``accept`` rejects a digest when ``digest - existing <= threshold`` for
    any already-accepted digest, so for the selection (in acceptance order)
    every later hash minus every earlier hash must exceed the threshold.
    """
    selected = select_grid_frames(
        frames,
        count=9,
        phash_fn=lambda frame: frame["phash"],
        phash_threshold=threshold,
    )

    assert len(selected) <= 9
    hashes = [frame["phash"] for frame in selected]
    for i, earlier in enumerate(hashes):
        for later in hashes[i + 1 :]:
            assert later - earlier > threshold


def test_identical_phash_frames_collapse_to_one():
    """Every frame sharing one hash leaves at most a single survivor."""
    frames = [
        {"timestamp": float(ts), "gif_worthiness": 0.5, "path": f"f{ts}", "phash": 7}
        for ts in (0, 10, 20, 30, 40, 50)
    ]

    selected = select_grid_frames(
        frames, count=3, phash_fn=lambda frame: frame["phash"], phash_threshold=4
    )

    assert len(selected) <= 1


def test_all_equal_timestamps_still_respects_count_cap():
    """Constant timestamps funnel everything into bucket 0; cap still holds."""
    frames = [
        {"timestamp": 5.0, "gif_worthiness": 0.5, "path": f"p{i}"}
        for i in range(20)
    ]

    selected = select_grid_frames(frames, count=4)

    assert len(selected) == 4
    assert len(selected) <= 4


def test_pathless_frames_yield_empty_selection():
    """Missing, empty, or None paths all drop out; empty input stays empty."""
    assert select_grid_frames([], count=9) == []
    assert select_grid_frames([{"timestamp": 1.0, "gif_worthiness": 0.9}], count=9) == []
    assert (
        select_grid_frames(
            [
                {"timestamp": 1.0, "gif_worthiness": 0.9, "path": ""},
                {"timestamp": 2.0, "gif_worthiness": 0.9, "path": None},
            ],
            count=9,
        )
        == []
    )


def test_non_positive_count_yields_empty_selection():
    frames = [{"timestamp": 0.0, "gif_worthiness": 0.5, "path": "p"}]
    assert select_grid_frames(frames, count=0) == []
    assert select_grid_frames(frames, count=-3) == []
