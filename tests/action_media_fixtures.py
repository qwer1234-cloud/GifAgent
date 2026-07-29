"""Deterministic synthetic media for action-boundary behavior tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np

from app.services.temporal_evidence import (
    TemporalEvidence,
    TemporalEvidenceCache,
    TemporalScanConfig,
)


FPS = 8
FRAME_SIZE = (320, 180)
FrameDecorator = Callable[[np.ndarray, int], None]


def _background() -> np.ndarray:
    rng = np.random.default_rng(7429)
    image = rng.integers(25, 210, (FRAME_SIZE[1], FRAME_SIZE[0], 3), dtype=np.uint8)
    for x in range(0, FRAME_SIZE[0], 24):
        cv2.line(image, (x, 0), (x, FRAME_SIZE[1] - 1), (245, 245, 245), 1)
    for y in range(0, FRAME_SIZE[1], 20):
        cv2.line(image, (0, y), (FRAME_SIZE[0] - 1, y), (10, 10, 10), 1)
    for index, point in enumerate(((36, 32), (270, 42), (42, 145), (278, 145))):
        cv2.circle(image, point, 8 + index, (40, 80 + index * 30, 235), -1)
    cv2.putText(image, "ACTION", (106, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    return image


def _write_video(
    path: Path,
    decorator: FrameDecorator | None = None,
    *,
    dx_per_frame: float = 0.0,
    dy_per_frame: float = 0.0,
    scale_per_frame: float = 0.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, FRAME_SIZE)
    assert writer.isOpened(), "OpenCV VideoWriter could not create the action fixture"
    source = _background()
    center = (FRAME_SIZE[0] / 2.0, FRAME_SIZE[1] / 2.0)
    try:
        for index in range(65):
            frame = source.copy()
            if decorator is not None:
                decorator(frame, index)
            transform = cv2.getRotationMatrix2D(center, 0.0, 1.0 + index * scale_per_frame)
            transform[:, 2] += (index * dx_per_frame, index * dy_per_frame)
            frame = cv2.warpAffine(
                frame, transform, FRAME_SIZE, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT
            )
            writer.write(frame)
    finally:
        writer.release()
    return path


def _move_fraction(index: int, start: int, end: int) -> float:
    if index <= start:
        return 0.0
    if index >= end:
        return 1.0
    return (index - start) / (end - start)


def _moving_subject(
    frame: np.ndarray,
    index: int,
    *,
    start: int = 18,
    end: int = 46,
    pause: tuple[int, int] | None = None,
) -> None:
    if pause is None:
        fraction = _move_fraction(index, start, end)
    else:
        pause_start, pause_end = pause
        moving_frames = max(1, end - start - (pause_end - pause_start))
        progressed = min(max(index - start, 0), pause_start - start)
        progressed += min(max(index - pause_end, 0), end - pause_end)
        fraction = min(1.0, progressed / moving_frames)
    is_active = start < index < end and not (
        pause is not None and pause[0] <= index < pause[1]
    )
    oscillation = 28.0 * np.sin(fraction * 8.0 * np.pi) if is_active else 0.0
    x = round(48 + 150 * fraction + oscillation)
    cv2.rectangle(frame, (x, 67), (x + 60, 133), (0, 235, 255), -1)
    for stripe in range(7):
        cv2.line(
            frame,
            (x + 4, 72 + stripe * 8),
            (x + 56, 72 + stripe * 8),
            (20 + stripe * 25, 30, 220 - stripe * 20),
            3,
        )
    cv2.circle(frame, (x + 30, 51), 16, (255, 20, 210), -1)
    cv2.line(frame, (x + 10, 91), (x - 24, 68), (255, 255, 255), 9)


def write_start_move_settle_video(path: Path) -> Path:
    return _write_video(path, _moving_subject)


def write_pan_with_static_subject(path: Path) -> Path:
    return _write_video(path, lambda frame, index: _moving_subject(frame, 0), dx_per_frame=1.0)


def write_subject_action_during_pan(path: Path) -> Path:
    return _write_video(path, _moving_subject, dx_per_frame=1.0)


def write_short_subject_action_during_pan(path: Path) -> Path:
    return _write_video(
        path,
        lambda frame, index: _moving_subject(frame, index, start=28, end=36),
        dx_per_frame=1.0,
    )


def write_slow_upward_pan(path: Path) -> Path:
    return _write_video(path, lambda frame, index: _moving_subject(frame, 0), dy_per_frame=-1.1)


def write_gentle_zoom(path: Path) -> Path:
    return _write_video(path, lambda frame, index: _moving_subject(frame, 0), scale_per_frame=0.0015)


def write_turn_video(path: Path) -> Path:
    def decorate(frame: np.ndarray, index: int) -> None:
        fraction = _move_fraction(index, 18, 46)
        angle = round(300 * fraction)
        marker = np.zeros((130, 130, 3), dtype=np.uint8)
        cv2.rectangle(marker, (15, 42), (115, 88), (40, 245, 255), -1)
        for offset in range(20, 111, 15):
            cv2.line(marker, (offset, 45), (offset, 85), (255, 40, 220), 5)
        cv2.circle(marker, (107, 65), 13, (255, 255, 255), -1)
        transform = cv2.getRotationMatrix2D((65, 65), angle, 1.0)
        rotated = cv2.warpAffine(marker, transform, (130, 130))
        mask = np.any(rotated != 0, axis=2)
        region = frame[25:155, 95:225]
        region[mask] = rotated[mask]

    return _write_video(path, decorate)


def write_two_lobe_wave_video(path: Path) -> Path:
    def decorate(frame: np.ndarray, index: int) -> None:
        phase = _move_fraction(index, 18, 46)
        hand_y = round(112 - 68 * abs(np.sin(phase * 2.0 * np.pi)))
        cv2.circle(frame, (130, 115), 32, (0, 210, 255), -1)
        cv2.line(frame, (135, 108), (220, hand_y), (255, 40, 220), 22)
        cv2.line(frame, (125, 110), (76, 176 - hand_y), (40, 245, 255), 18)
        cv2.circle(frame, (220, hand_y), 20, (255, 255, 255), -1)
        cv2.circle(frame, (76, 176 - hand_y), 17, (255, 255, 255), -1)

    return _write_video(path, decorate)


def write_edge_action_video(path: Path, *, edge: str) -> Path:
    if edge == "left":
        return _write_video(path, lambda frame, index: _moving_subject(frame, index, start=-12, end=28))
    if edge == "right":
        return _write_video(path, lambda frame, index: _moving_subject(frame, index, start=36, end=76))
    raise ValueError("edge must be 'left' or 'right'")


def write_paused_action_video(path: Path, *, pause_s: float) -> Path:
    pause_frames = round(pause_s * FPS)
    pause_start = 31
    return _write_video(
        path,
        lambda frame, index: _moving_subject(
            frame, index, start=16, end=48, pause=(pause_start, pause_start + pause_frames)
        ),
    )


def scan_video(
    video_path: Path,
    start_s: float,
    end_s: float,
    fps: float = 8.0,
    width: int = 320,
) -> TemporalEvidence:
    return TemporalEvidenceCache().scan(
        video_path,
        start_s,
        end_s,
        TemporalScanConfig(fps=fps, width=width, motion_compensation=True),
    )
