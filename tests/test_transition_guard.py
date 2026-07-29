"""Behavioral media fixtures for transition-aware candidate extraction."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from app.services.transition_guard import guard_candidate_window


FPS = 8
FRAME_SIZE = (320, 180)
BASE_CFG = {
    "transition_guard_enabled": True,
    "transition_scan_fps": 8,
    "transition_scan_width": 320,
    "transition_boundary_margin_s": 0.25,
    "transition_min_duration_s": 2.0,
    "transition_motion_compensation": True,
    "transition_hard_threshold": 0.65,
    "transition_soft_threshold": 0.40,
    "transition_soft_run_frames": 3,
}


def _scene(seed: int) -> np.ndarray:
    """Return a feature-rich, deterministic BGR scene."""
    rng = np.random.default_rng(seed)
    frame = rng.integers(20, 220, (FRAME_SIZE[1], FRAME_SIZE[0], 3), dtype=np.uint8)
    for x in range(0, FRAME_SIZE[0], 32):
        cv2.line(frame, (x, 0), (x, FRAME_SIZE[1] - 1), (255, 255, 255), 1)
    for y in range(0, FRAME_SIZE[1], 30):
        cv2.line(frame, (0, y), (FRAME_SIZE[0] - 1, y), (0, 0, 0), 1)
    cv2.putText(frame, f"scene-{seed}", (70, 95), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
    return frame


def _write_video(path: Path, frames: list[np.ndarray]) -> Path:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, FRAME_SIZE
    )
    assert writer.isOpened(), "OpenCV VideoWriter could not create the synthetic video"
    try:
        for frame in frames:
            assert frame.shape == (FRAME_SIZE[1], FRAME_SIZE[0], 3)
            writer.write(frame)
    finally:
        writer.release()
    return path


def write_hard_cut_video(path: Path) -> Path:
    return _write_video(path, [_scene(1)] * 24 + [_scene(2)] * 24)


def write_static_video(path: Path) -> Path:
    return _write_video(path, [_scene(0)] * 32)


def write_affine_pan_video(path: Path, dy: float) -> Path:
    source = _scene(3)
    frames = [
        cv2.warpAffine(
            source,
            np.float32([[1, 0, 0], [0, 1, frame_index * dy]]),
            FRAME_SIZE,
            borderMode=cv2.BORDER_REFLECT,
        )
        for frame_index in range(32)
    ]
    return _write_video(path, frames)


def write_crossfade_video(path: Path) -> Path:
    first, second = _scene(4), _scene(5)
    fade = [cv2.addWeighted(first, 1.0 - alpha, second, alpha, 0.0) for alpha in np.linspace(0.0, 1.0, 16)]
    return _write_video(path, [first] * 16 + fade + [second] * 16)


def write_flash_video(path: Path) -> Path:
    scene = _scene(6)
    return _write_video(path, [scene] * 15 + [np.full_like(scene, 255)] + [scene] * 16)


def write_moving_subject_video(path: Path) -> Path:
    background = _scene(7)
    frames = []
    for frame_index in range(32):
        frame = background.copy()
        x = 10 + frame_index * 7
        cv2.rectangle(frame, (x, 65), (x + 35, 115), (0, 255, 255), -1)
        cv2.circle(frame, (x + 17, 57), 12, (255, 0, 255), -1)
        frames.append(frame)
    return _write_video(path, frames)


def test_hard_cut_splits_window(tmp_path: Path) -> None:
    video = write_hard_cut_video(tmp_path / "hard_cut.mp4")
    result = guard_candidate_window(video, 0.0, 6.0, 1.0, BASE_CFG)

    assert result.transition_action == "split"
    assert result.hard_cut_count >= 1
    assert len(result.segments) == 2
    assert all(segment.end_s - segment.start_s >= 2.0 for segment in result.segments)


def test_boundary_at_anchor_drops_candidate_instead_of_selecting_other_shot(tmp_path: Path) -> None:
    video = write_hard_cut_video(tmp_path / "hard_cut_at_anchor.mp4")
    # The post-cut side is shorter than the two-second minimum, leaving only
    # the pre-cut segment; it must not be silently selected for an anchor in
    # the boundary safety margin.
    result = guard_candidate_window(video, 0.0, 4.0, 3.0, BASE_CFG)

    assert result.transition_action == "drop"
    assert result.segments == ()
    assert result.anchor_segment is None
    assert result.anchor_ts_s == 3.0


def test_static_media_is_kept_without_transition_evidence(tmp_path: Path) -> None:
    video = write_static_video(tmp_path / "static.mp4")
    result = guard_candidate_window(video, 0.0, 4.0, 2.0, BASE_CFG)

    assert result.transition_action == "keep"
    assert result.hard_cut_count == 0
    assert result.soft_transition_count == 0
    assert result.to_dict()["guard_error"] is None


def test_slow_upward_motion_is_kept(tmp_path: Path) -> None:
    video = write_affine_pan_video(tmp_path / "slow_pan.mp4", dy=-2.0)
    result = guard_candidate_window(video, 0.0, 4.0, 2.0, BASE_CFG)

    assert result.transition_action in {"keep", "trim"}
    assert result.hard_cut_count == 0
    assert result.motion_type == "coherent_camera_motion"


def test_crossfade_splits_without_using_single_frame_score(tmp_path: Path) -> None:
    video = write_crossfade_video(tmp_path / "crossfade.mp4")
    result = guard_candidate_window(video, 0.0, 6.0, 1.0, BASE_CFG)

    assert result.soft_transition_count >= 1
    assert result.transition_action == "split"


def test_high_soft_threshold_suppresses_crossfade_classification(tmp_path: Path) -> None:
    video = write_crossfade_video(tmp_path / "crossfade_high_threshold.mp4")
    config = {**BASE_CFG, "transition_soft_threshold": 0.95}
    result = guard_candidate_window(video, 0.0, 6.0, 1.0, config)

    assert result.soft_transition_count == 0
    assert result.transition_action == "keep"


def test_single_flash_is_not_a_cut(tmp_path: Path) -> None:
    video = write_flash_video(tmp_path / "flash.mp4")
    result = guard_candidate_window(video, 0.0, 4.0, 2.0, BASE_CFG)

    assert result.hard_cut_count == 0
    assert result.transition_action in {"keep", "trim"}


def test_local_subject_motion_is_not_a_cut(tmp_path: Path) -> None:
    video = write_moving_subject_video(tmp_path / "subject_motion.mp4")
    result = guard_candidate_window(video, 0.0, 4.0, 2.0, BASE_CFG)

    assert result.hard_cut_count == 0
