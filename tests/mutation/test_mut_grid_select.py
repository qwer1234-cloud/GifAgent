"""W4 kill tests for grid_select mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/grid_select.json``:

- span / bucket-index arithmetic (``t_max - t_min``, ``- t_min``,
  ``* count`` -> ``% count``, ``/`` -> ``//``) with t_min > 0 and
  multi-bucket scenarios where backfill cannot mask the change;
- last-bucket clipping (``index = count - 1``) with a following bucket pass
  and score-ordered backfill;
- leftovers loop gates (``len(selected) < count``, ``>= count`` break,
  ``reverse=True`` sort, zero-iteration, ``not``-inversion mutants);
- pHash dedup boundary (``<= threshold``), digest-None rejection, and the
  ``count``/``phash_threshold``/``or 0.0`` default-value mutants;
- same-object duplicate frames reaching the ``selected_ids`` guard.

Equivalent mutants (annotation union, ``*`` -> ``/`` keyword marker,
``len(...)`` comparisons neutralised by the following ``>= count`` break,
``t_max <= t_min`` variants unreachable for real extrema, ``index >= count``
vs ``==``/``is`` on the clipped range, ``buckets[0]`` relocation in the
all-equal-timestamp branch) are registered in w4-surviving.md.
"""

from __future__ import annotations

from app.services.grid_select import select_grid_frames


def _f(ts: float, score: float, path: str | None = None, **extra: object) -> dict:
    frame = {"timestamp": ts, "gif_worthiness": score, "path": path or f"f{ts}"}
    frame.update(extra)
    return frame


# ── span / bucket index arithmetic ─────────────────────────────────────────


def test_bucket_spread_uses_relative_offsets_with_positive_t_min():
    frames = [_f(100.0, 0.9), _f(150.0, 0.8), _f(200.0, 0.7), _f(300.0, 0.1)]
    selected = select_grid_frames(frames, count=2)
    assert [f["timestamp"] for f in selected] == [100.0, 200.0]


def test_four_buckets_select_one_frame_per_bucket_in_order():
    frames = [
        _f(100.0, 0.9),
        _f(175.0, 0.8),
        _f(250.0, 0.7),
        _f(325.0, 0.6),
        _f(400.0, 0.5),
    ]
    selected = select_grid_frames(frames, count=4)
    assert [f["timestamp"] for f in selected] == [100.0, 175.0, 250.0, 325.0]


def test_last_bucket_clip_and_score_ordered_backfill():
    frames = [_f(0.0, 0.9), _f(50.0, 0.5), _f(60.0, 0.4), _f(100.0, 0.3)]
    selected = select_grid_frames(frames, count=3)
    assert [f["timestamp"] for f in selected] == [0.0, 50.0, 100.0]


# ── leftovers loop gates ───────────────────────────────────────────────────


def test_backfill_takes_highest_score_leftover_and_stops_at_count():
    frames = [
        _f(0.0, 0.9),
        _f(5.0, 0.85),
        _f(900.0, 0.7),
        _f(905.0, 0.6),
    ]
    selected = select_grid_frames(frames, count=3)
    assert [f["timestamp"] for f in selected] == [0.0, 900.0, 5.0]


def test_backfill_stops_exactly_at_count_and_keeps_order():
    frames = [
        _f(0.0, 0.9),
        _f(5.0, 0.85),
        _f(10.0, 0.6),
        _f(900.0, 0.8),
    ]
    selected = select_grid_frames(frames, count=3)
    assert [f["timestamp"] for f in selected] == [0.0, 900.0, 5.0]
    assert all(f["timestamp"] != 10.0 for f in selected)


# ── pHash dedup ────────────────────────────────────────────────────────────


def test_phash_rejects_difference_exactly_at_threshold():
    digests = {"f0": 100.0, "f1": 110.0, "far": 500.0}
    frames = [
        _f(0.0, 0.9, path="f0"),
        _f(450.0, 0.8, path="f1"),
        _f(900.0, 0.7, path="far"),
    ]
    selected = select_grid_frames(
        frames, count=3, phash_fn=lambda f: digests[f["path"]]
    )
    # 110 - 100 == threshold counts as a near-duplicate: f1 is rejected in
    # its bucket AND in the backfill, leaving only f0 and far.
    assert [f["path"] for f in selected] == ["f0", "far"]


def test_phash_threshold_default_is_ten_rejects_nine_point_five():
    digests = {"f0": 100.0, "f1": 109.5, "far": 500.0}
    frames = [
        _f(0.0, 0.9, path="f0"),
        _f(450.0, 0.8, path="f1"),
        _f(900.0, 0.7, path="far"),
    ]
    selected = select_grid_frames(frames, count=3, phash_fn=lambda f: digests[f["path"]])
    assert [f["path"] for f in selected] == ["f0", "far"]


def test_phash_threshold_default_is_ten_accepts_ten_point_five():
    digests = {"f0": 100.0, "f1": 110.5, "far": 500.0}
    frames = [
        _f(0.0, 0.9, path="f0"),
        _f(450.0, 0.8, path="f1"),
        _f(900.0, 0.7, path="far"),
    ]
    selected = select_grid_frames(frames, count=3, phash_fn=lambda f: digests[f["path"]])
    assert [f["path"] for f in selected] == ["f0", "f1", "far"]


def test_phash_none_digest_rejects_frame():
    frames = [_f(0.0, 0.9), _f(900.0, 0.8)]
    selected = select_grid_frames(frames, count=2, phash_fn=lambda f: None)
    assert selected == []


# ── default arguments ──────────────────────────────────────────────────────


def test_default_count_is_nine():
    frames = [_f(float(ts), 0.5) for ts in range(0, 180, 10)]
    selected = select_grid_frames(frames)
    assert len(selected) == 9


def test_count_zero_and_negative_return_empty():
    frames = [_f(0.0, 0.5), _f(10.0, 0.6)]
    assert select_grid_frames(frames, count=0) == []
    assert select_grid_frames(frames, count=-1) == []


def test_count_one_returns_the_best_frame():
    frames = [_f(0.0, 0.5), _f(10.0, 0.6)]
    selected = select_grid_frames(frames, count=1)
    assert [f["timestamp"] for f in selected] == [10.0]


# ── missing-key defaults ───────────────────────────────────────────────────


def test_missing_timestamp_defaults_to_zero_bucket_order():
    no_ts = {"gif_worthiness": 0.9, "path": "d"}
    scenario_early = select_grid_frames([dict(no_ts), _f(0.5, 0.1, path="b")], count=2)
    assert [f["path"] for f in scenario_early] == ["d", "b"]

    scenario_late = select_grid_frames(
        [dict(no_ts), _f(-0.5, 0.1, path="b")], count=2
    )
    assert [f["path"] for f in scenario_late] == ["b", "d"]


def test_missing_score_defaults_to_zero_in_bucket_sort():
    no_score = {"timestamp": 5.0, "path": "f"}
    with_score = _f(0.0, 0.8, path="g")
    selected = select_grid_frames([with_score, dict(no_score)], count=1)
    assert [f["path"] for f in selected] == ["g"]

    negative = _f(0.0, -0.5, path="n")
    selected = select_grid_frames([negative, dict(no_score)], count=1)
    assert [f["path"] for f in selected] == ["f"]


# ── duplicate frame objects in the bucket pass ─────────────────────────────


def test_duplicate_frame_objects_do_not_break_the_bucket_scan():
    dup = {"timestamp": 0.0, "gif_worthiness": 0.9, "path": "dup"}
    frames = [dup, dup, _f(100.0, 0.5, path="z1"), _f(900.0, 0.8, path="z2")]
    selected = select_grid_frames(frames, count=3)
    # The first dup fills bucket 0 and the inner loop breaks; z1 falls to the
    # score-ordered backfill behind z2. The selected_ids guard stays dead.
    assert [f["path"] for f in selected] == ["dup", "z2", "z1"]
