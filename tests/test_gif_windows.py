import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.gif_windows import build_export_window
from scripts import test_video_adaptive


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


def test_direct_clip_shape_uses_nested_best_frame_timestamp():
    """Legacy direct clips anchor the 40/60 window on their nested best frame."""
    window = build_export_window(
        clip={
            "frame_count": 1,
            "gif_worthiness": 1.0,
            "best_frame": {"timestamp": 10.0},
        },
        total_duration_s=30.0,
        min_duration_s=1.5,
        max_duration_s=5.0,
    )

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


def test_staged_export_uses_the_bounded_shared_window(tmp_path, monkeypatch):
    """The staged ffmpeg boundary receives and records the capped window."""
    target_clip = {
        "clip_id": "long-clip",
        "start_ts": 10.0,
        "end_ts": 40.0,
        "best_frame_ts": 20.0,
        "frame_count": 12,
        "gif_worthiness": 0.8,
        "rank": 1,
    }
    captured_attempts = []
    monkeypatch.setattr(
        test_video_adaptive,
        "_read_upstream_manifest",
        lambda *_args: {"clips": [target_clip]},
    )
    monkeypatch.setattr(
        test_video_adaptive.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="60.0\n"),
    )

    def fake_export_attempt(**kwargs):
        captured_attempts.append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"GIF89a")
        return SimpleNamespace(success=True, size_bytes=6, error=None)

    monkeypatch.setattr(
        test_video_adaptive, "run_gif_export_attempt", fake_export_attempt
    )
    frames_dir = tmp_path / "frames"
    export_dir = tmp_path / "exports"
    work_dir = tmp_path / "work"
    for directory in (frames_dir, export_dir, work_dir):
        directory.mkdir()

    test_video_adaptive._stage_gif_clip(
        video_path=str(tmp_path / "source.mp4"),
        frames_dir=str(frames_dir),
        export_dir=str(export_dir),
        work_dir=str(work_dir),
        cfg={"gif_fps": 24, "gif_max_width": 720, "min_duration": 2.0, "max_duration": 5.0},
        clip_id="long-clip",
        inputs={"rank_dedup_manifest": [{"path": "ignored"}]},
    )

    assert float(captured_attempts[0]["palette_command"][5]) == 5.0
    manifest = json.loads((work_dir / "gif_clip_long-clip_manifest.json").read_text())
    assert manifest["start_ts"] == 18.0
    assert manifest["end_ts"] == 23.0
