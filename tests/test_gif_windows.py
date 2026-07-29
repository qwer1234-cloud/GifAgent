import pytest

from app.services.gif_windows import build_export_window


def test_single_frame_window_is_centered_and_capped():
    """A top-scoring single needs the full configured duration, never more."""
    window = build_export_window(
        clip={"frame_count": 1, "best_frame_ts": 10.0, "gif_worthiness": 1.0},
        total_duration_s=30.0,
        min_duration_s=1.5,
        max_duration_s=5.0,
    )

    assert window.duration_s == 5.0
    assert window.start_s == pytest.approx(8.0)
    assert window.end_s == pytest.approx(13.0)


def test_multi_frame_window_never_exceeds_max_duration():
    """A long merged run cannot bypass the configured export duration cap."""
    window = build_export_window(
        clip={
            "frame_count": 12,
            "start_ts": 10.0,
            "end_ts": 40.0,
            "best_frame_ts": 20.0,
            "gif_worthiness": 0.8,
        },
        total_duration_s=60.0,
        min_duration_s=2.0,
        max_duration_s=5.0,
    )

    assert window.duration_s == 5.0
    assert window.start_s == pytest.approx(18.0)
    assert window.end_s == pytest.approx(23.0)


def test_window_clamps_at_the_end_of_a_short_video():
    """Boundary clamping retains the requested duration when the video permits it."""
    window = build_export_window(
        clip={"frame_count": 1, "best_frame_ts": 29.5, "gif_worthiness": 1.0},
        total_duration_s=30.0,
        min_duration_s=1.5,
        max_duration_s=5.0,
    )

    assert window.start_s == 25.0
    assert window.end_s == 30.0
    assert window.duration_s == 5.0
