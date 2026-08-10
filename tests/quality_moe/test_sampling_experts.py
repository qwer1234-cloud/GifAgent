from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from app.quality_moe.experts import (
    CinematicExpert,
    TechnicalAestheticExpert,
    TemporalExpert,
)
from app.quality_moe.models import EvidenceStatus
from app.quality_moe.sampling import SampledClip, sample_clip_frames


def sampled_clip(
    frames: tuple[np.ndarray, ...], *, semantic_labels: tuple[str, ...] = ()
) -> SampledClip:
    return SampledClip(
        candidate_id="candidate-1",
        video_path="synthetic://candidate-1",
        start_ts=10.0,
        end_ts=12.0,
        timestamps=tuple(10.0 + index * 0.25 for index in range(len(frames))),
        frames=frames,
        semantic_labels=semantic_labels,
    )


def _checkerboard(*, height: int = 90, width: int = 160, cell_size: int = 1) -> np.ndarray:
    grid = np.indices((height, width)) // cell_size
    cells = (grid.sum(axis=0) % 2 * 255).astype(np.uint8)
    return np.repeat(cells[:, :, None], 3, axis=2)


def test_uniform_low_key_clip_is_descriptive_not_technical_negative():
    frames = tuple(np.full((90, 160, 3), 40, dtype=np.uint8) for _ in range(6))

    technical = TechnicalAestheticExpert().evaluate(sampled_clip(frames))
    cinematic = CinematicExpert().evaluate(sampled_clip(frames))

    assert technical.polarity.value == "NEUTRAL"
    assert not any(finding["code"] == "underexposed_subject" for finding in technical.findings)
    assert any(finding["code"] == "low_key_lighting" for finding in cinematic.findings)


def test_near_black_clipping_and_detail_loss_reports_underexposure_negative():
    frames = tuple(np.zeros((90, 160, 3), dtype=np.uint8) for _ in range(6))

    evidence = TechnicalAestheticExpert().evaluate(sampled_clip(frames))

    assert evidence.status is EvidenceStatus.AVAILABLE
    assert evidence.scores["technical_integrity"] < 0.5
    finding = next(finding for finding in evidence.findings if finding["code"] == "underexposed_subject")
    assert evidence.polarity.value == "NEGATIVE"
    assert finding["shadow_clipping"] > 0.8
    assert finding["detail_preservation"] < 0.1


def test_single_flash_frame_is_temporal_negative_signal():
    frames = [np.full((90, 160, 3), 80, np.uint8) for _ in range(6)]
    frames[3] = np.full((90, 160, 3), 250, np.uint8)

    evidence = TemporalExpert().evaluate(sampled_clip(tuple(frames)))

    assert evidence.scores["temporal_coherence"] < 0.7
    assert evidence.signal_family == "deterministic_temporal"
    assert evidence.polarity.value == "NEGATIVE"


def test_one_pixel_checkerboard_translation_is_motion_compensated_not_negative():
    frame = _checkerboard()
    frames = tuple(np.roll(frame, index, axis=1) for index in range(6))

    evidence = TemporalExpert().evaluate(sampled_clip(frames))

    assert evidence.polarity.value == "NEUTRAL"
    assert evidence.scores["temporal_coherence"] > 0.95


def test_three_pixel_checkerboard_translation_is_motion_compensated_not_negative():
    frame = _checkerboard(cell_size=4)
    frames = tuple(np.roll(frame, index * 3, axis=1) for index in range(6))

    evidence = TemporalExpert().evaluate(sampled_clip(frames))

    assert evidence.polarity.value == "NEUTRAL"
    assert evidence.scores["temporal_coherence"] > 0.95


def test_static_uniform_frames_do_not_emit_camera_motion():
    frames = tuple(np.full((90, 160, 3), 80, np.uint8) for _ in range(6))

    evidence = TemporalExpert().evaluate(sampled_clip(frames))

    assert evidence.polarity.value == "NEUTRAL"
    assert not any(finding["code"] == "camera_motion" for finding in evidence.findings)


def test_high_contrast_checkerboard_has_high_8bit_sharpness_without_blur_polarity():
    frame = _checkerboard()
    sharp = TechnicalAestheticExpert().evaluate(sampled_clip((frame,) * 6))
    blurred_frame = cv2.GaussianBlur(frame, (31, 31), 0)
    blurred = TechnicalAestheticExpert().evaluate(sampled_clip((blurred_frame,) * 6))

    assert sharp.scores["sharpness"] > 0.8
    assert blurred.scores["sharpness"] < sharp.scores["sharpness"]
    assert blurred.polarity.value == "NEUTRAL"


def test_identical_pixels_ignore_semantic_labels():
    frames = tuple(np.full((90, 160, 3), 100, np.uint8) for _ in range(6))
    night = sampled_clip(frames, semantic_labels=("low-key", "handheld"))
    daylight = sampled_clip(frames, semantic_labels=("daylight", "tripod"))

    for expert in (TechnicalAestheticExpert(), TemporalExpert(), CinematicExpert()):
        assert expert.evaluate(night).scores == expert.evaluate(daylight).scores


def test_expert_scores_are_always_finite_and_bounded():
    frames = tuple(
        np.full((90, 160, 3), index * 50, dtype=np.uint8)
        for index in range(6)
    )
    clip = sampled_clip(frames)

    for expert in (TechnicalAestheticExpert(), TemporalExpert(), CinematicExpert()):
        evidence = expert.evaluate(clip)
        assert evidence.status is EvidenceStatus.AVAILABLE
        assert all(math.isfinite(score) and 0.0 <= score <= 1.0 for score in evidence.scores.values())


def test_missing_or_corrupt_media_is_typed_unavailable(tmp_path):
    corrupt_path = tmp_path / "corrupt.mp4"
    corrupt_path.write_bytes(b"not a video")

    for media_path in (tmp_path / "does-not-exist.mp4", corrupt_path):
        result = sample_clip_frames(
            video_path=media_path,
            start_ts=1.0,
            end_ts=2.0,
            candidate_id="candidate-missing",
        )

        assert result.status is EvidenceStatus.UNAVAILABLE
        assert result.frames == ()
        assert result.diagnostics["code"] == "media_unavailable"


def test_sampling_is_deterministic_bounded_and_stays_in_candidate_interval(tmp_path):
    video_path = tmp_path / "source.avi"
    writer = cv2.VideoWriter(
        str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (800, 400)
    )
    assert writer.isOpened()
    for value in range(20):
        writer.write(np.full((400, 800, 3), value * 10, dtype=np.uint8))
    writer.release()

    first = sample_clip_frames(video_path, 0.4, 1.3, "candidate-video")
    second = sample_clip_frames(video_path, 0.4, 1.3, "candidate-video")

    assert first.status is EvidenceStatus.AVAILABLE
    assert first.timestamps == second.timestamps
    assert len(first.frames) == 6
    assert all(0.4 <= timestamp <= 1.3 for timestamp in first.timestamps)
    assert all(max(frame.shape[:2]) <= 640 for frame in first.frames)
    assert all(np.array_equal(left, right) for left, right in zip(first.frames, second.frames))


@pytest.mark.parametrize("sample_count", [1, 5, 9])
def test_sampling_rejects_sample_counts_outside_six_to_eight(tmp_path, sample_count):
    with pytest.raises(ValueError, match="sample_count"):
        sample_clip_frames(tmp_path / "missing.mp4", 0.4, 1.3, "candidate-video", sample_count=sample_count)


@pytest.mark.parametrize("frame_count", [5, 9])
def test_available_sampled_clip_requires_six_to_eight_frames(frame_count):
    frames = tuple(np.full((8, 8, 3), 80, dtype=np.uint8) for _ in range(frame_count))

    with pytest.raises(ValueError, match="six to eight"):
        SampledClip(
            candidate_id="candidate-count",
            video_path="synthetic://candidate-count",
            start_ts=0.0,
            end_ts=2.0,
            timestamps=tuple(index / 10 for index in range(frame_count)),
            frames=frames,
        )


class _FakeCapture:
    def __init__(self, *, seek_result: bool = True, pts_ms: float = 500.0) -> None:
        self.seek_result = seek_result
        self.pts_ms = pts_ms
        self.released = False

    def isOpened(self) -> bool:
        return True

    def set(self, _property: int, _value: float) -> bool:
        return self.seek_result

    def read(self) -> tuple[bool, np.ndarray]:
        return True, np.full((8, 8, 3), 80, dtype=np.uint8)

    def get(self, property_id: int) -> float:
        if property_id == cv2.CAP_PROP_POS_MSEC:
            return self.pts_ms
        if property_id == cv2.CAP_PROP_FPS:
            return 10.0
        return 0.0

    def release(self) -> None:
        self.released = True


def test_sampling_is_unavailable_when_random_access_seek_is_rejected(tmp_path, monkeypatch):
    video_path = tmp_path / "fake.mp4"
    video_path.touch()
    capture = _FakeCapture(seek_result=False)
    monkeypatch.setattr("app.quality_moe.sampling.cv2.VideoCapture", lambda _path: capture)

    result = sample_clip_frames(video_path, 0.4, 1.3, "candidate-video")

    assert result.status is EvidenceStatus.UNAVAILABLE
    assert result.diagnostics["code"] == "random_access_unavailable"
    assert capture.released


def test_sampling_is_unavailable_when_decoded_pts_is_outside_candidate_interval(tmp_path, monkeypatch):
    video_path = tmp_path / "fake.mp4"
    video_path.touch()
    capture = _FakeCapture(pts_ms=5000.0)
    monkeypatch.setattr("app.quality_moe.sampling.cv2.VideoCapture", lambda _path: capture)

    result = sample_clip_frames(video_path, 0.4, 1.3, "candidate-video")

    assert result.status is EvidenceStatus.UNAVAILABLE
    assert result.diagnostics["code"] == "decoded_timestamp_outside_interval"
    assert capture.released
