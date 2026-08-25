from app.services.grid_select import select_grid_frames


def _frame(ts: float, score: float = 0.70) -> dict:
    return {
        "timestamp": ts,
        "gif_worthiness": score,
        "path": f"frame_{ts}",
    }


def test_tied_scores_span_the_timeline_instead_of_taking_the_opening():
    frames = [_frame(float(ts)) for ts in (10, 20, 30, 40, 50, 600, 610, 620, 900)]

    selected = select_grid_frames(frames, count=3)
    times = [frame["timestamp"] for frame in selected]

    assert len(times) == 3
    assert min(times) <= 50
    assert max(times) >= 600


def test_higher_score_wins_inside_a_time_bucket():
    frames = [
        _frame(10, 0.40),
        _frame(12, 0.95),
        _frame(800, 0.50),
    ]

    selected = select_grid_frames(frames, count=2)
    times = [frame["timestamp"] for frame in selected]

    assert 12 in times
    assert 10 not in times


def test_phash_near_duplicate_is_skipped_for_the_later_bucket():
    class _Hash:
        def __init__(self, value: int) -> None:
            self.value = value

        def __sub__(self, other: "_Hash") -> int:
            return abs(self.value - other.value)

    frames = [_frame(10), _frame(500), _frame(900, 0.90)]

    def phash_fn(frame: dict) -> _Hash:
        if frame["timestamp"] in {10, 500}:
            return _Hash(0)
        return _Hash(20)

    selected = select_grid_frames(
        frames, count=3, phash_fn=phash_fn, phash_threshold=2,
    )
    times = [frame["timestamp"] for frame in selected]

    assert 10 in times
    assert 500 not in times
    assert 900 in times


def test_empty_or_pathless_frames_yield_empty_selection():
    assert select_grid_frames([], count=9) == []
    assert select_grid_frames([{"timestamp": 1, "gif_worthiness": 1}], count=9) == []
